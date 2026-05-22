"""Active uplink kind + median ping latency, once per tick.

Feeds the Internet detail card's iface-timeline + connection-
quality strips. Each Glint client that opens the detail card
asks the daemon for the historical window via
`get_internet_history` (RPC); the daemon serves bucketed data
from sqlite without re-probing.

We collect three series:

  * `internet.kind` - integer code for the active uplink type
    at this moment (0 unknown, 1 ethernet, 2 wifi, 3 cellular,
    4 tethering). The kind RPC returns it as a string label;
    storage keeps it numeric so the warm/cool roll-ups produce
    sensible "most common kind in this minute" via mode.

  * `internet.latency_ms` - median across all configured ping
    hosts at this tick. Same hosts as the pings collector;
    median (not mean) so one slow host doesn't drag the
    headline number.

  * `internet.loss_pct` - aggregate packet loss across all
    configured hosts. Computed as `(sent - received) / sent`,
    in 0-100 percent. `ping -c 5` per host gives us five
    samples per tick - enough to detect 20%/40%/60%/80% drops
    without doubling the probe rate to absurd levels. The app
    uses this as the second input to its connection-quality
    classifier (latency OR loss = poor).

INTERVAL_SEC = 15 matches the pings collector's effective
cadence after dedup. Ticking faster than the underlying
pings.collect data refresh would emit duplicate samples.
"""
from __future__ import annotations
import json
import os
import re
import subprocess


NAME = "internet"
INTERVAL_SEC = 15


_KIND_CODE = {
    "unknown":       0,
    "ethernet":      1,
    "wifi":          2,
    # Generic "cellular" kept for routers where we can't tell the
    # active SIM slot. New installs typically resolve to one of
    # the slot-specific codes below.
    "cellular":      3,
    "tethering":     4,
    "cellular_sim1": 5,
    "cellular_sim2": 6,
}

# Last SIM slot (1/2) we positively resolved. Carried forward across
# ticks so a brief detection gap on a cellular link doesn't emit a
# generic-"cellular" sliver between two same-SIM segments. 0 = never
# resolved yet.
_last_cellular_slot = 0


def requires(caps) -> bool:
    # Always-on. Even a Wi-Fi-only repeater has *some* uplink
    # kind to record (wifi vs ethernet vs nothing).
    return True


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}
    kind = _detect_active_kind(caps)
    out["internet.kind"] = float(_KIND_CODE.get(kind, 0))
    latency, loss = _ping_aggregate()
    if latency is not None:
        out["internet.latency_ms"] = latency
    if loss is not None:
        out["internet.loss_pct"] = loss
    return out


def _detect_active_kind(caps) -> str:
    """Best-effort uplink classification by examining the kernel
    default route and matching the egress device against known
    interface families. We deliberately don't read uci - the
    kernel routing table is the truth even when the user has
    weird custom routes.

    Cellular returns differ depending on whether we can resolve
    the active SIM slot:
        - "cellular_sim1" / "cellular_sim2" when the modem reports
          an online slot (lets the client paint per-SIM segments)
        - "cellular" generic fallback when ubus / gl_modem can't
          tell us (e.g. older firmware, single-SIM device)
    """
    dev = _default_route_dev()
    if not dev:
        return "unknown"
    name = dev.lower()
    if name.startswith(("wwan", "qmi", "modem", "rmnet")):
        global _last_cellular_slot
        slot = _detect_active_sim_slot()
        if slot in (1, 2):
            _last_cellular_slot = slot
        elif _last_cellular_slot in (1, 2):
            # Slot momentarily unresolvable — during a reconnect or
            # SIM handover the modem drops `ipv4.ip` for a tick or two
            # before the new session is up. The link is still
            # cellular, so carrying the last known slot keeps the
            # timeline on the same SIM instead of flickering to a
            # generic "Cellular" sliver between two same-SIM stretches
            # (which read as a spurious third uplink type in the
            # legend). Generic "cellular" is reserved for the case we
            # have genuinely *never* resolved a slot — e.g. a
            # single-SIM device whose firmware never reports one.
            slot = _last_cellular_slot
        if slot == 1:
            return "cellular_sim1"
        if slot == 2:
            return "cellular_sim2"
        return "cellular"
    if name.startswith(("usb",)):
        return "tethering"
    if name.startswith(("wlan", "sta", "wifi")):
        return "wifi"
    if name.startswith(("eth", "wan", "lan")):
        return "ethernet"
    return "unknown"


def _detect_active_sim_slot() -> int:
    """Return 1 or 2 for the currently-online SIM slot; 0 when
    indeterminate. Reads from `cellular.network info` on GL.iNet
    firmware; falls back to `gl_modem -s status` for older builds.
    Both queries are cheap (~100 ms) and the result feeds straight
    into the kind code emitted by `collect()`.
    """
    try:
        r = subprocess.run(
            ["ubus", "call", "cellular.network", "info", "{}"],
            capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if r.returncode == 0 and r.stdout.strip():
        try:
            data = json.loads(r.stdout)
        except ValueError:
            data = None
        if isinstance(data, dict):
            # New shape: {"networks": [{"slot": "1", "ipv4": {"ip": ...}, ...}]}
            #
            # Mudi 7 firmware doesn't emit a `status` field on each
            # network entry, only the network_mode + payload itself.
            # The reliable signal is "this slot has a real IPv4
            # address assigned" - if it does, that's the slot
            # currently providing the data session. Status is checked
            # only as a fallback for firmwares that do emit it.
            networks = data.get("networks")
            if isinstance(networks, list):
                for n in networks:
                    if not isinstance(n, dict):
                        continue
                    try:
                        s = int(n.get("slot", 0))
                    except (TypeError, ValueError):
                        continue
                    if s not in (1, 2):
                        continue
                    ipv4 = n.get("ipv4") or {}
                    if isinstance(ipv4, dict) and ipv4.get("ip"):
                        return s
                    status = str(n.get("status", "")).lower()
                    if status in ("online", "connected", "registered"):
                        return s
            # Older shape: {"sim1": {...}, "sim2": {...}}
            for key in ("sim1", "sim2"):
                entry = data.get(key)
                if isinstance(entry, dict):
                    ipv4 = entry.get("ipv4") or {}
                    if isinstance(ipv4, dict) and ipv4.get("ip"):
                        return 1 if key == "sim1" else 2
                    status = str(entry.get("status", "")).lower()
                    if status in ("online", "connected", "registered"):
                        return 1 if key == "sim1" else 2
    # Fallback: gl_modem CLI on builds without `cellular.network`
    # ubus method. `gl_modem -s status` prints `Slot: N` somewhere
    # in the output; one regex extract is enough.
    try:
        r = subprocess.run(
            ["gl_modem", "-s", "status"],
            capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if r.returncode == 0:
        m = re.search(r"\bSlot\s*[:=]\s*(\d+)", r.stdout, re.IGNORECASE)
        if m:
            try:
                s = int(m.group(1))
                if s in (1, 2):
                    return s
            except ValueError:
                pass
    return 0


def _default_route_dev() -> str:
    """Return the iface name carrying the default IPv4 route, or
    empty string when no default route is installed (offline)."""
    try:
        r = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True, text=True, timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    # First line of `ip route show default`:
    #   default via 192.168.50.1 dev eth0 proto static metric 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            i = parts.index("dev")
            if i + 1 < len(parts):
                return parts[i + 1]
    return ""


_RTT_RE = re.compile(r"min/avg/max[^=]*=\s*[\d.]+/([\d.]+)/")
# busybox ping prints "5 packets transmitted, 3 packets received,
# 40% packet loss" - we need both the numerator and the denominator
# because a host that didn't reply at all (n_recv = 0) gets clipped
# differently from a host that lost a couple of packets.
_LOSS_RE = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets?\s+)?received")


# Packets sent per host per tick. 3 gives loss resolution at ~33%
# increments - enough to surface a real outage - while keeping the
# per-tick wall-time low. The daemon's main loop is single-threaded
# (`while running: _tick(); sleep(1)`), so a slow internet tick
# stalls every other collector and the snapshot/threshold passes.
# We measured an 8 s internet tick with 5 sequential packets × 2
# hosts; dropping to 3 packets and pinging hosts concurrently (see
# below) brings it to ~3 s.
PING_COUNT = 3


def _ping_aggregate() -> tuple[float | None, float | None]:
    """Median latency + aggregate loss across configured hosts.

    Returns (latency_ms, loss_pct) where either side can be None
    if no packets succeeded / failed in a useful way.

    Hosts are pinged CONCURRENTLY: we launch one `ping` per host up
    front and then collect their output, so the wall-time is ~one
    host's run (PING_COUNT seconds) regardless of host count, rather
    than the serial sum. This matters because the collector blocks
    the single-threaded daemon loop for its whole duration.
    """
    hosts = _hosts()
    # Launch all pings at once.
    procs: list[tuple[str, "subprocess.Popen[str]"]] = []
    for host in hosts:
        try:
            p = subprocess.Popen(
                ["ping", "-c", str(PING_COUNT), "-W", "1", "-q", host],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True)
        except OSError:
            continue
        procs.append((host, p))
    latencies: list[float] = []
    sent_total = 0
    recv_total = 0
    for _host, p in procs:
        try:
            out, _ = p.communicate(timeout=PING_COUNT + 4.0)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.communicate(timeout=1.0)
            except Exception:
                pass
            continue
        except Exception:
            continue
        m = _LOSS_RE.search(out)
        if m:
            try:
                sent_total += int(m.group(1))
                recv_total += int(m.group(2))
            except ValueError:
                pass
        # rtt line only printed when at least one packet returned;
        # skip silently when the host was entirely unreachable.
        m = _RTT_RE.search(out)
        if m:
            try:
                latencies.append(float(m.group(1)))
            except ValueError:
                pass
    latency = _median(latencies) if latencies else None
    loss: float | None
    if sent_total > 0:
        loss = 100.0 * (sent_total - recv_total) / sent_total
    else:
        loss = None
    return latency, loss


def _median(samples: list[float]) -> float:
    samples = sorted(samples)
    n = len(samples)
    if n % 2 == 1:
        return samples[n // 2]
    return (samples[n // 2 - 1] + samples[n // 2]) / 2.0


def _hosts() -> list[str]:
    """Same hosts the pings collector reads. Duplicated here so
    importing pings.py isn't required (avoids a circular import
    if pings ever needs internet state in the future)."""
    path = "/etc/glintd/config.json"
    try:
        with open(path) as f:
            cfg = json.load(f)
        hosts = cfg.get("ping_hosts", [])
        if isinstance(hosts, list) and hosts:
            return [str(h) for h in hosts]
    except (OSError, ValueError):
        pass
    return ["1.1.1.1", "8.8.8.8"]
