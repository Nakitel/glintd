"""Threshold engine. Runs each scheduler tick after collectors
have written. Pulls the latest hot row for relevant metrics,
compares against per-event thresholds, and fires through the
relay when state crosses (rising-edge - fire once on entry, not
every tick while bad).

Events:

    wan.lost            - all `interfaces.<wan>.up` series went 0
    wan.recovered       - same series flipped back to 1 after lost
    wan.failover_up     - primary uplink upgraded (e.g. ethernet
                          plugged in, was on cellular)
    wan.failover_down   - primary uplink degraded (e.g. ethernet
                          unplugged, falling back to cellular)
    battery.low         - `battery.pct` dropped below 20
    battery.critical    - `battery.pct` dropped below 10
    battery.unplugged   - `battery.charging` flipped 1 → 0 with pct < 95
    battery.charging    - flipped 0 → 1
    sim.switched        - active SIM slot changed
    pings.all_dead      - every `pings.<host>.rtt_ms` had no sample
                          for the last 3 ticks (host unreachable)

State across ticks is stored in `state` dict (in-memory) so we
fire on transitions only. Persistence isn't required - restart
just resets edge detection, worst case the user gets a re-fire
of an active alert which is harmless.
"""
from __future__ import annotations
import json
import os
import subprocess
import time
from typing import Any
from glintd.apns.relay_client import push


# WAN-iface priority. Higher number wins. Ethernet beats cellular
# beats nothing. The threshold engine uses this to call out
# "uplink upgraded" vs "uplink degraded" when the primary iface
# changes - same data Mudi's own route_policy uses, just folded
# into a single ranking we can transition-detect on.
_WAN_PRIORITY = {
    "eth0":     30,  # WAN ethernet
    "eth1":     30,
    "wlan-sta": 20,  # repeater Wi-Fi
    "rmnet_data0": 10,  # cellular
    "rmnet_data1": 10,
}


def _wan_priority_of(name: str) -> int:
    if name in _WAN_PRIORITY:
        return _WAN_PRIORITY[name]
    if name.startswith("rmnet_"):
        return 10
    if name.startswith("eth"):
        return 30
    return 5


def _wan_label(name: str) -> str:
    """Human-readable name for an iface in the notification body.
    GL.iNet supports more than just ethernet / cellular: Wi-Fi
    repeater (wlan-sta family) and USB tethering (rndis / usb /
    tether) both show up as alternative WAN routes. Order matters
    here - the more specific prefix matches (e.g. wlan-sta) have
    to come before the generic "wlan" -> Wi-Fi catch-all.
    """
    n = name.lower()
    if n.startswith("rmnet_") or n.startswith("wwan"):
        return "cellular"
    if n.startswith("rndis") or n.startswith("usb") or "tether" in n:
        return "tethering"
    if "sta" in n and n.startswith(("wlan", "wifi", "ra", "ath")):
        return "Wi-Fi repeater"
    if n.startswith("eth") or n in ("wan", "lan"):
        return "ethernet"
    if n.startswith("wlan") or n.startswith("wifi"):
        return "Wi-Fi"
    return name


# Per-event default minimum interval between identical fires
# (seconds). Even if the threshold flaps, we won't push the same
# event twice within this window. Keeps the user's notification
# tray quiet during bad-network thrashing.
COOLDOWN_S = {
    "wan.lost":           300,
    "wan.recovered":      300,
    "wan.failover_up":    60,
    "wan.failover_down":  60,
    "battery.low":        1800,
    "battery.critical":   600,
    "battery.unplugged":  600,
    "battery.charging":   600,
    "sim.switched":       60,
    "vpn.up":             60,
    "vpn.down":           60,
    "pings.all_dead":     300,
    # SMS arrivals burst (operator promo blasts can land 3 in a
    # row) - keep the cooldown low so each genuine new message
    # fires its own notification, but not so low that a flood
    # blows up the user's tray.
    "sms.received":       2,
}


class ThresholdEngine:
    """Holds last-fire timestamps + last-known state per metric
    so transitions can be detected. Single instance per daemon."""

    def __init__(self):
        # event_id → unix-seconds of last fire
        self.last_fire: dict[str, int] = {}
        # metric → last seen value (for edge detection)
        self.prev_value: dict[str, float] = {}
        # parallel store for non-numeric tracking (active SIM slot,
        # primary WAN iface name) - using prev_value would conflate
        # the two and risk type errors.
        self.prev_string: dict[str, str] = {}
        # SMS filenames we've already seen (union of incoming/ and
        # storage/). Lazy-init on first SMS tick (None) so we don't
        # flood the tray with the existing inbox right after daemon
        # startup.
        self.seen_sms: set[str] | None = None
        # Stashed during evaluate() so `_fire` can prune
        # APNs-rejected tokens without us threading store through
        # every call site (~10 fires from this file). The daemon's
        # main loop is single-threaded, so the lifetime is "one
        # call to evaluate" - no concurrency hazard.
        self._store = None
        # Last router-uptime sample. Reset to None at startup; the
        # first non-None comparison after that detects a kernel-level
        # reboot (uptime regressed) and fires `router.booted`. Cleaner
        # than tracking daemon-process lifetime because users sometimes
        # bounce just the daemon without rebooting the box.
        self._prev_router_uptime: float | None = None

    def evaluate(self, store, caps, tokens: list[dict]) -> None:
        """One pass. `store` is the daemon's Store; `tokens` is the
        list of registered device-token dicts. Returns nothing -
        side effects only (push + state update)."""
        if not tokens:
            return  # no clients listening; cheap no-op

        # Stash for `_fire` - see __init__'s comment.
        self._store = store
        now = int(time.time())
        # Pull latest hot row per relevant metric. We don't
        # need history here - only the freshest sample.
        latest: dict[str, float] = {}
        for metric in self._watched_metrics(caps):
            row = store.conn.execute(
                "SELECT value FROM samples_hot "
                "WHERE metric = ? ORDER BY ts DESC LIMIT 1",
                (metric,)).fetchone()
            if row is not None:
                latest[metric] = row["value"]

        # - battery -
        if "battery.pct" in latest:
            pct = latest["battery.pct"]
            self._maybe_fire("battery.critical", pct < 10,
                             tokens, now,
                             title="Mudi battery critical",
                             body=f"{int(pct)}% - find a charger.")
            self._maybe_fire("battery.low", pct < 20 and pct >= 10,
                             tokens, now,
                             title="Mudi battery low",
                             body=f"{int(pct)}% remaining.")
        if "battery.charging" in latest:
            charging = latest["battery.charging"] > 0.5
            prev = self.prev_value.get("battery.charging")
            pct = int(latest.get("battery.pct", 0))
            if prev is not None and prev > 0.5 and not charging:
                if pct < 95:
                    self._fire("battery.unplugged", tokens, now,
                               title="Mudi unplugged",
                               body=f"Running on battery, {pct}% left.")
            elif prev is not None and prev <= 0.5 and charging:
                self._fire("battery.charging", tokens, now,
                           title="Mudi charging",
                           body=f"Power restored at {pct}%.")
            self.prev_value["battery.charging"] = float(charging)

        # - sim switch - read the active slot from the cellular ubus
        # method (same source the live-state path uses). Fire
        # whenever the slot id changes between ticks. We carry the
        # carrier name into the notification body so the user sees
        # "SIM 2 (Vodafone UA)" instead of a bare slot number.
        active = _active_sim_now()
        if active is not None:
            slot, carrier = active
            prev_slot = self.prev_string.get("__active_sim_slot__")
            if prev_slot is not None and prev_slot != slot:
                friendly = f"SIM {slot}"
                if carrier:
                    friendly += f" ({carrier})"
                self._fire("sim.switched", tokens, now,
                           title="SIM switched",
                           body=f"Cellular switched to {friendly}.")
            self.prev_string["__active_sim_slot__"] = slot

        # - wan priority change - pick the highest-priority iface
        # whose .up flag is 1 right now and remember it. Transitions
        # fire `wan.failover_up` (better link replaced worse) or
        # `wan.failover_down` (worse link took over after better
        # went away). Distinct from wan.lost/recovered which only
        # fire when *every* uplink is down.
        wan_metrics = [m for m in latest if m.startswith("interfaces.")
                       and m.endswith(".up")]
        ifaces_up = [m.split(".")[1] for m in wan_metrics
                     if latest[m] > 0.5]
        primary = (max(ifaces_up, key=_wan_priority_of)
                   if ifaces_up else None)
        prev_primary = self.prev_string.get("__wan_primary__")
        if (prev_primary is not None and primary is not None
                and prev_primary != primary):
            new_p = _wan_priority_of(primary)
            old_p = _wan_priority_of(prev_primary)
            if new_p > old_p:
                self._fire("wan.failover_up", tokens, now,
                           title="WAN upgraded",
                           body=f"Switched to {_wan_label(primary)} (was {_wan_label(prev_primary)}).")
            elif new_p < old_p:
                self._fire("wan.failover_down", tokens, now,
                           title="WAN failover",
                           body=f"Fell back to {_wan_label(primary)} (was {_wan_label(prev_primary)}).")
        self.prev_string["__wan_primary__"] = primary or ""

        # - VPN tunnel up/down - derived from per-peer handshake
        # age. WireGuard considers a peer alive if a handshake
        # completed in the last ~3 min; we use 180 s as the cutoff
        # (one beyond the protocol's keepalive interval). A peer
        # transitioning from "stale handshake" to "fresh" is
        # `vpn.up`; the reverse is `vpn.down`. Per-peer state lives
        # in `prev_value` keyed by `__vpn_<peer>__` so multiple
        # tunnels each get their own edge detection.
        for metric, value in latest.items():
            if not (metric.startswith("tunnels.wg.")
                    and metric.endswith(".handshake_age_s")):
                continue
            # tunnels.wg.<peer>.handshake_age_s
            peer = metric.split(".")[2]
            up_now = value <= 180.0
            key = f"__vpn_{peer}__"
            prev_up = self.prev_value.get(key)
            if prev_up is not None:
                if prev_up <= 0.5 and up_now:
                    self._fire("vpn.up", tokens, now,
                               title="VPN connected",
                               body=f"Tunnel '{peer}' is up.")
                elif prev_up > 0.5 and not up_now:
                    self._fire("vpn.down", tokens, now,
                               title="VPN disconnected",
                               body=f"Tunnel '{peer}' lost handshake.")
            self.prev_value[key] = 1.0 if up_now else 0.0

        # - router reboot - kernel uptime is the cleanest signal.
        # When this tick's uptime is LOWER than the previous tick's,
        # the box rebooted (or the daemon restarted onto a host
        # that did). We fire `router.booted` once per detected
        # boot, including the boot caught when the daemon itself
        # comes up (prev = None and current uptime < 5 min). That
        # second clause covers users who power-cycle the router
        # without the daemon being able to fire a "going down"
        # event - the next launch's `router.booted` is the only
        # signal we'll get from the daemon side.
        try:
            with open("/proc/uptime") as _f:
                _cur_uptime = float(_f.read().split()[0])
        except (OSError, ValueError, IndexError):
            _cur_uptime = None
        if _cur_uptime is not None:
            _prev = self._prev_router_uptime
            if _prev is None:
                # Daemon just started. Fire boot only when the box
                # itself is freshly booted (uptime < 5 min) so a
                # daemon-only restart on a long-running router
                # doesn't spam a misleading "router rebooted".
                if _cur_uptime < 300:
                    self._fire("router.booted", tokens, now,
                               title="Router started",
                               body="Glint companion is back online.")
            elif _cur_uptime + 5 < _prev:
                # Uptime regressed (with 5 s slack for clock drift).
                self._fire("router.booted", tokens, now,
                           title="Router rebooted",
                           body="Router restarted and is back online.")
            self._prev_router_uptime = _cur_uptime

        # - wan link -
        wan_metrics = [m for m in latest if m.startswith("interfaces.")
                       and m.endswith(".up")]
        if wan_metrics:
            any_up = any(latest[m] > 0.5 for m in wan_metrics)
            prev_up = self.prev_value.get("__wan_any_up__")
            if prev_up is not None:
                if prev_up > 0.5 and not any_up:
                    self._fire("wan.lost", tokens, now,
                               title="WAN dropped",
                               body="No upstream link on the router.")
                elif prev_up <= 0.5 and any_up:
                    self._fire("wan.recovered", tokens, now,
                               title="WAN restored",
                               body="Router is back online.")
            self.prev_value["__wan_any_up__"] = 1.0 if any_up else 0.0

        # Pings-all-dead is the only check that needs *absence*
        # of recent samples, not a value comparison. We look at
        # the past 90 s - three 30 s tick intervals - and fire
        # if every ping host has zero samples in that window.
        ping_metrics = [m for m in self._watched_metrics(caps)
                        if m.startswith("pings.")]
        if ping_metrics:
            cutoff = now - 90
            alive = False
            for m in ping_metrics:
                row = store.conn.execute(
                    "SELECT 1 FROM samples_hot "
                    "WHERE metric = ? AND ts >= ? LIMIT 1",
                    (m, cutoff)).fetchone()
                if row is not None:
                    alive = True
                    break
            if not alive:
                self._maybe_fire("pings.all_dead", True, tokens, now,
                                 title="No internet on the router",
                                 body="Every pinged host is unreachable.")

        # - SMS arrivals - list /etc/spool/sms/{incoming,storage}/
        # each tick, diff against the set we saw last time. New
        # filenames → one push each, with sender + first chunk of
        # the body parsed straight out of the smstools file.
        # Skipped when neither spool dir exists (Wi-Fi-only boards)
        # so we don't burn syscalls on every threshold tick.
        if caps.has_modem:
            self._evaluate_sms(tokens, now)

    def _evaluate_sms(self, tokens: list[dict], now: int) -> None:
        # On GL.iNet 4.x firmware (Mudi 7, Slate 7) smsd writes
        # fresh SMS only into `incoming/`; the `sms_manager`
        # daemon archives them into `storage/` lazily - typically
        # only when the user reads them in the web UI. Looking
        # at `storage/` alone misses every fresh SMS, so we union
        # both dirs and prefer `storage/` on filename collisions
        # (canonical post-archive copy).
        file_dirs: dict[str, str] = {}
        for d in ("/etc/spool/sms/storage", "/etc/spool/sms/incoming"):
            try:
                for n in os.listdir(d):
                    # `storage/` walked first → keep that path; only
                    # write the `incoming/` entry if the name isn't
                    # already known.
                    if n not in file_dirs:
                        file_dirs[n] = d
            except (FileNotFoundError, OSError):
                continue
        if not file_dirs:
            return
        files = set(file_dirs.keys())
        # First tick after daemon start - seed the seen-set with
        # whatever's already there. Without this, each fresh start
        # would re-push every existing inbox message.
        if self.seen_sms is None:
            self.seen_sms = files
            return
        new_files = files - self.seen_sms
        # Replace early so a parse-failure path doesn't leak the
        # arrival back into the diff next tick.
        self.seen_sms = files
        if not new_files:
            return
        # Newest mtime first so a backlog of N drops the most
        # recent at the top of the user's notification stack.
        ordered = sorted(
            new_files,
            key=lambda n: os.path.getmtime(f"{file_dirs[n]}/{n}"),
            reverse=False)
        for name in ordered:
            sender, body, slot = _read_sms_brief(name, file_dirs[name])
            if not body:
                continue
            title = sender or "SMS"
            if slot:
                title = f"{title} · SIM {slot}"
            # Truncate body to ~150 chars so APNs doesn't drop
            # multi-segment messages outright. Trailing ellipsis
            # makes the cut visible.
            snippet = body if len(body) <= 150 else body[:149] + "…"
            # Bypass `_maybe_fire`'s rising-edge logic - every new
            # SMS file is a discrete event, no transition tracking.
            # Cooldown still applies via _fire so a 3-in-a-row
            # blast respects the 2-second floor.
            # Include the spool filename as `glint.sms_id` so the
            # app's tap-handler can scroll/select THIS exact
            # message in Messages instead of just landing on the
            # section's default top row. Filenames are GL.iNet's
            # canonical id and survive the inbox→storage move
            # (the markRead path keeps the same name).
            self._fire("sms.received", tokens, now,
                       title=title, body=snippet,
                       extra_payload={"glint.sms_id": name})

    # ---- helpers ----

    def _watched_metrics(self, caps) -> list[str]:
        out: list[str] = []
        if caps.has_battery or caps.has_mcu:
            out += ["battery.pct", "battery.charging"]
        for port in caps.ethernet_ports:
            out.append(f"interfaces.{port.name}.up")
        # Pings track presence, not value - but we still look at
        # latest hot rows per host. The host list comes from the
        # collector's effective config (defaults: 1.1.1.1 / 8.8.8.8).
        out += ["pings.1_1_1_1.rtt_ms", "pings.8_8_8_8.rtt_ms"]
        return out

    def _maybe_fire(self, event: str, condition: bool,
                    tokens: list[dict], now: int,
                    title: str, body: str) -> None:
        """Fire `event` only on the *rising edge* (false→true) so
        we don't notify every tick while the bad state persists.
        Tracks previous condition state in `prev_value[event]`."""
        prev = self.prev_value.get(event, 0.0) > 0.5
        self.prev_value[event] = 1.0 if condition else 0.0
        if condition and not prev:
            self._fire(event, tokens, now, title=title, body=body)

    def _fire(self, event: str, tokens: list[dict], now: int,
              title: str, body: str,
              extra_payload: dict | None = None) -> None:
        # Log every threshold-fire to syslog under the [glintd]
        # tag. The iOS Logs page (Glint daemon category) reads
        # `logread` over SSH and surfaces these so the user can
        # eyeball what the daemon decided to push and when -
        # useful for both "did my SIM really switch?" and
        # debugging cooldown / dedup decisions. We log even when
        # the cooldown gate below suppresses the actual push, so
        # the trace shows attempts too.
        print(f"[glintd] event {event}: {title} - {body}",
              flush=True)
        # No startup-grace gate here. We had one briefly to mask
        # `pings.all_dead` false-positives on the first tick after
        # restart (prev_value is empty, the snapshot's pings rows
        # are stale, and "no recent samples" reads as "all dead"),
        # but it was swallowing legitimate user-triggered events
        # too - e.g. the user plugs in seconds after a daemon
        # restart and the resulting `battery.charging` transition
        # gets eaten while `prev_value` updates anyway, leaving no
        # rising edge for the next tick to catch. The relay client
        # already caps every HTTP attempt at TIMEOUT_S via
        # `socket.setdefaulttimeout`, so a stray false-positive
        # push costs ~6 s of latency in the worst case rather
        # than wedging the tick loop. Any single false-positive
        # also self-suppresses on the next tick because the rules
        # are rising-edge.
        cooldown = COOLDOWN_S.get(event, 300)
        last = self.last_fire.get(event, 0)
        if now - last < cooldown:
            return
        # Group tokens by (platform, bundle_id) so we send one
        # batched request per cohort. Relay forwards each token
        # to APNs with appropriate topic. Tokens whose user has
        # muted this event are silently skipped - daemon-side
        # filter so the relay never sees them and APNs delivery
        # quotas don't tick on muted events.
        # Cohort by (platform, bundle, environment) - tokens
        # bound to sandbox APNs (dev-cert) and tokens bound to
        # production APNs (release-cert) must travel in separate
        # requests; the relay picks the APNs host per request.
        cohorts: dict[tuple[str, str, str], list[str]] = {}
        for t in tokens:
            disabled = t.get("disabled_events") or set()
            if event in disabled:
                continue
            plat = t.get("platform", "ios")
            bid = t.get("bundle_id", "com.nakitel.glint.ios")
            env = t.get("environment", "production")
            cohorts.setdefault((plat, bid, env), []).append(t["token"])
        if not cohorts:
            # All recipients muted this event. Still set last_fire
            # so the cooldown applies - otherwise next-tick we'd
            # re-evaluate the threshold and try to fire again.
            self.last_fire[event] = now
            return
        # Routing hint for the iOS app's tap-handler. Maps event
        # families to a sidebar section so a tap on the banner
        # lands the user where they'd want to look - SMS goes to
        # Messages, anything WAN/battery/SIM/VPN goes to the
        # dashboard where the relevant tile already shows fresh
        # state. The iOS handler validates the value against the
        # user's entitlement and falls back to the dashboard when
        # the section is Pro-gated for a Free user.
        target = "dashboard"
        if event.startswith("sms."):
            target = "messages"
        # Merge caller-supplied extras into the APNs payload. We
        # use this for routing hints that are too specific for a
        # boolean / enum (e.g. `glint.sms_id` so a tap on an SMS
        # banner scrolls Messages to that exact row instead of
        # the section's default selection). Keys are prefixed with
        # `glint.` by convention to keep the namespace clean and
        # avoid colliding with Apple's reserved `aps` keys.
        payload: dict[str, object] = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
            },
            "glint.target": target,
        }
        if extra_payload:
            for k, v in extra_payload.items():
                payload[k] = v
        invalid: list[str] = []
        for (plat, bid, env), toks in cohorts.items():
            _ok, dead = push(
                tokens=toks,
                platform=plat,
                bundle_id=bid,
                event=event,
                payload=payload,
                environment=env,
            )
            invalid.extend(dead)
        self.last_fire[event] = now
        if invalid and self._store is not None:
            # Per-event APNs feedback: relay tells us which tokens
            # came back 410 / "Unregistered" / "BadDeviceToken",
            # we drop those rows immediately. Without this the
            # daemon would re-fire to dead tokens on every event
            # (cheap server-side at the relay, but eats Apple's
            # per-router quota and shows up as wasted work in
            # logs). Deletes happen in one statement bounded by
            # the cohort size (≤ 50), so no need for chunking.
            try:
                placeholders = ",".join("?" for _ in invalid)
                self._store.conn.execute(
                    f"DELETE FROM push_tokens WHERE token IN "
                    f"({placeholders})",
                    invalid)
                print(f"[glintd] pruned {len(invalid)} APNs-rejected "
                      f"token(s) after event {event!r}", flush=True)
            except Exception as e:
                print(f"[glintd] APNs-feedback prune failed: {e}",
                      flush=True)


def _active_sim_now() -> tuple[str, str | None] | None:
    """Active SIM slot id + carrier name from `cellular.network info`
    cross-referenced with `cellular.sim status`. Returns None when
    the modem is offline or the ubus call errors. The carrier name
    is whatever the SIM provisioned in its EF_SPN file (e.g.
    "Vodafone UA"). The slot id is "1" / "2" - same shape the
    signal collector emits.

    Same data source liveactivity uses; the two could share a
    helper, but threshold ticks already shell out for pings, an
    extra ubus pair is in the noise."""
    try:
        r = subprocess.run(
            ["ubus", "call", "cellular.network", "info", "{}"],
            capture_output=True, text=True, timeout=2.0)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        info = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    # Detect active slot: GL.iNet's cellular.network info doesn't
    # actually emit `active` / `default` fields on a Mudi 7
    # (firmware 4.8.x). The reliable signal is "this slot's modem
    # has an `ipv4.ip` assigned" - that means it's the one
    # currently carrying cellular data. Earlier code only looked
    # at non-existent flags and always returned None, so
    # sim.switched never fired.
    active_slot: str | None = None
    if isinstance(info, dict):
        nets = info.get("networks")
        if isinstance(nets, list):
            for n in nets:
                if not isinstance(n, dict):
                    continue
                slot = n.get("slot")
                if slot is None:
                    continue
                # Belt-and-braces: still honour explicit
                # active/default if firmware ever provides them,
                # but fall through to ipv4-presence when not.
                if (n.get("active") in (True, 1, "1", "true")
                        or n.get("default") in (True, 1, "1", "true")):
                    active_slot = str(slot)
                    break
                ipv4 = n.get("ipv4")
                if isinstance(ipv4, dict) and ipv4.get("ip"):
                    active_slot = str(slot)
                    break
    if active_slot is None:
        return None
    carrier: str | None = None
    try:
        r = subprocess.run(
            ["ubus", "call", "cellular.sim", "status", "{}"],
            capture_output=True, text=True, timeout=2.0)
        if r.returncode == 0 and r.stdout.strip():
            sim = json.loads(r.stdout)
            if isinstance(sim, dict):
                slots = sim.get("sims")
                if isinstance(slots, list):
                    for s in slots:
                        if (isinstance(s, dict)
                                and str(s.get("slot")) == active_slot):
                            # GL.iNet's actual key is `carrier`;
                            # other firmwares used `operator` /
                            # `name` / `provider` so we still try
                            # those as fallbacks.
                            carrier = (s.get("carrier")
                                       or s.get("operator")
                                       or s.get("name")
                                       or s.get("provider"))
                            break
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return active_slot, carrier


def _read_sms_brief(name: str,
                    directory: str = "/etc/spool/sms/storage"
                    ) -> tuple[str, str, int | None]:
    """Pull (sender, body, slot) out of one smstools file.
    Same RFC822-ish parse the iOS / Mac client does - header/body
    split on the first blank line, UCS2 bodies decoded as
    UTF-16BE. Returns ("", "", None) on any parse failure so the
    caller can decide to skip the notification.

    `directory` is whichever spool dir the caller located the
    file in - `storage/` for archived messages, `incoming/` for
    fresh ones on GL.iNet 4.x where sms_manager hasn't archived
    them yet. Defaulting to `storage/` keeps the previous public
    surface for any callers that haven't been updated.
    """
    path = f"{directory}/{name}"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return "", "", None
    sep = raw.find(b"\n\n")
    if sep < 0:
        sep2 = raw.find(b"\r\n\r\n")
        if sep2 >= 0:
            head_b, body_b = raw[:sep2], raw[sep2 + 4:]
        else:
            head_b, body_b = raw, b""
    else:
        head_b, body_b = raw[:sep], raw[sep + 2:]

    headers: dict[str, str] = {}
    for line in head_b.decode("utf-8", errors="replace").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()

    alphabet = headers.get("Alphabet", "GSM").upper()
    if alphabet.startswith("UCS"):
        try:
            body = body_b.decode("utf-16-be", errors="replace") \
                         .rstrip("\x00")
        except Exception:  # noqa: BLE001
            body = body_b.decode("utf-8", errors="replace")
    else:
        body = body_b.decode("utf-8", errors="replace")
    body = body.strip()

    sender = headers.get("From", "")
    slot: int | None
    try:
        slot = int(headers.get("Slot", "")) or None
    except ValueError:
        slot = None
    return sender, body, slot
