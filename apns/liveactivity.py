"""Periodic Live Activity content updater. Runs alongside the
threshold engine in the daemon's tick loop. For every registered
`ios.liveactivity` token, builds a ContentState payload from the
latest hot samples and pushes via the relay.

APNs Live Activity payload shape:

    {
      "aps": {
        "timestamp": <unix s>,
        "event": "update",          # or "end"
        "content-state": { ... },   # fields the widget extension
                                    # decodes - must match the
                                    # iOS app's ConnectionAttributes
                                    # exactly, byte-for-byte.
        "stale-date": <unix s>,
        "dismissal-date": <unix s>  # only on event="end"
      }
    }

We update every 60 s while at least one LA token is registered.
The widget extension's lock-screen UI uses `Text(date:.relative)`
for the timestamp, so the user sees a ticking "12 sec ago" between
our discrete updates without us having to push faster.
"""
from __future__ import annotations
import json
import os
import subprocess
import time
from glintd.apns.relay_client import push as relay_push


class LiveActivityUpdater:
    """Compares newest hot rows against the last successful push
    timestamp. Skips when nothing's changed beyond a clock tick;
    Apple's Live Activity push budget (≤ 120/h on average) is
    already aligned with our 60 s cadence."""

    def __init__(self):
        # Last unix-time we sent any LA push. Drives the rate
        # limit; we don't yet pace per-token because a single
        # router fans out to one user's devices, all at once.
        self.last_push_at: float = 0.0
        self.min_interval_s: float = 60.0

    def tick(self, store, caps) -> None:
        now = time.time()
        if now - self.last_push_at < self.min_interval_s:
            return
        # Pull the LA-platform tokens grouped by bundle_id; the
        # relay takes a list of tokens per call.
        cur = store.conn.execute(
            "SELECT token, bundle_id FROM push_tokens "
            "WHERE platform = ?",
            ("ios.liveactivity",))
        rows = list(cur)
        if not rows:
            return

        cohorts: dict[str, list[str]] = {}
        for r in rows:
            cohorts.setdefault(r["bundle_id"], []).append(r["token"])

        content_state = self._content_state(store, caps)
        payload = {
            "aps": {
                "timestamp": int(now),
                "event": "update",
                "content-state": content_state,
                # 15 min window - matches the iOS controller's
                # local Activity.update staleDate so users get
                # consistent "stale" treatment between the
                # local fast-path and the APNs slow-path.
                "stale-date": int(now + 15 * 60),
            },
        }
        # Advance the rate-limit anchor BEFORE the push attempt
        # rather than only on success. Anchoring on success only
        # would turn a transient relay failure (429, 5xx, network
        # blip) into a per-tick retry loop, accumulating 429s
        # until the relay rate-limits the token outright.
        # Anchoring up-front holds the ~60 s cadence regardless
        # of outcome; the widget's lock-screen UI uses
        # Text(date:.relative) so a missed cycle just shows
        # "1m 5s ago" instead of "12s ago", which is the correct
        # degraded behaviour.
        self.last_push_at = now
        invalid: list[str] = []
        for bid, tokens in cohorts.items():
            _ok, dead = relay_push(
                tokens=tokens,
                platform="ios.liveactivity",
                bundle_id=bid,
                event="la.update",
                payload=payload,
            )
            invalid.extend(dead)
        if invalid:
            # APNs-feedback prune. Live Activity push-update tokens
            # die naturally - Apple's window is 8 hours per Activity,
            # after that the token is 410'd. Without this prune the
            # daemon re-pushes to corpses every minute and never
            # converges; with it, a dead token is gone the very
            # next la.update tick (typically within 60 s of the
            # Activity ending or the user dismissing the lock-screen
            # widget).
            try:
                placeholders = ",".join("?" for _ in invalid)
                store.conn.execute(
                    f"DELETE FROM push_tokens WHERE token IN "
                    f"({placeholders})",
                    invalid)
                print(f"[glintd] pruned {len(invalid)} APNs-rejected "
                      f"LA token(s)", flush=True)
            except Exception as e:
                print(f"[glintd] LA-feedback prune failed: {e}",
                      flush=True)

    def _content_state(self, store, caps) -> dict:
        """Build the dict that the widget extension's
        `ConnectionAttributes.ContentState` decodes from. Field
        names must be EXACT - even a typo silently fails to
        decode and the lock screen sticks on the previous tile.

        Three classes of source:
          1. Latest hot-tier sample (battery / signal / ping).
          2. Adjacent-sample delta (throughput → Mbps).
          3. Live ubus / wg poll (carrier, band, active tunnel,
             public IP). These aren't in the rolled-up store
             because we don't graph them; pulling on each LA
             tick is cheap (≤ 100 ms total).
        """
        latest = self._latest_values(store, [
            "battery.pct", "battery.charging",
            "signal.sim1.rsrp_dbm", "signal.sim2.rsrp_dbm",
        ])
        # Status: unreachable if every ping host is dead in the
        # last 120 s window. The iOS UI uses the same heuristic.
        ping_alive = self._any_ping_alive(store)
        first_ping_ms = self._first_ping_ms(store)
        status = "healthy" if ping_alive else "unreachable"

        # Pick the higher-RSRP SIM as the active one. Apple's
        # widget renders one signal value, not per-slot.
        rsrps = [latest.get(f"signal.sim{i}.rsrp_dbm") for i in (1, 2)]
        rsrps = [v for v in rsrps if v is not None]
        rsrp = max(rsrps) if rsrps else None

        # Throughput - daemon stores cumulative byte counters;
        # we synthesise a Mbps from the last two samples on
        # whatever interface looks like the WAN egress (eth0 on
        # Mudi, wwan*/4g* on cellular-only boxes).
        wan_iface = self._guess_wan_iface(caps)
        rx_mbps = self._mbps_delta(store, wan_iface, "rx_bytes")
        tx_mbps = self._mbps_delta(store, wan_iface, "tx_bytes")

        carrier, band = self._cellular_live()
        public_ip, ip_geo = self._public_ip_live()
        active_tunnels = self._active_tunnels_live()
        active_tunnel = active_tunnels[0] if active_tunnels else None
        on_battery_for = self._on_battery_for_live(store)

        # Unread SMS - files in `incoming/` are messages the
        # GL.iNet web UI hasn't moved into `storage/` yet (the
        # "mark read" action does the move). Counting the dir is
        # cheaper than parsing smstools state and gives the widget
        # a fresh badge on every snapshot push.
        try:
            unread = len(os.listdir("/etc/spool/sms/incoming"))
        except OSError:
            unread = 0

        return {
            "status":          status,
            "routerModel":     self._friendly_model(caps.model),
            "capturedAt":      int(time.time()),
            "firstPingMs":     first_ping_ms,
            "signalRSRPdBm":   int(rsrp) if rsrp is not None else None,
            "carrier":         carrier,
            "band":            band,
            "publicIP":        public_ip,
            "ipGeo":           ip_geo,
            "activeTunnel":    active_tunnel,
            "activeTunnels":   active_tunnels,
            "batteryPercent":  int(latest["battery.pct"])
                if latest.get("battery.pct") is not None else None,
            "batteryCharging": (latest.get("battery.charging") or 0) > 0.5,
            "rxMbps":          rx_mbps,
            "txMbps":          tx_mbps,
            "onBatteryFor":    on_battery_for,
            "unreadSmsCount":  unread if unread > 0 else None,
        }

    # ---- Hot-tier readers ----

    def _latest_values(self, store, metrics: list[str]) -> dict[str, float | None]:
        out: dict[str, float | None] = {m: None for m in metrics}
        if not metrics:
            return out
        ph = ",".join("?" * len(metrics))
        cur = store.conn.execute(
            f"SELECT metric, value FROM samples_hot "
            f"WHERE metric IN ({ph}) "
            f"AND ts >= ? "
            f"ORDER BY ts DESC",
            (*metrics, int(time.time()) - 120))
        for row in cur:
            if out.get(row["metric"]) is None:
                out[row["metric"]] = row["value"]
        return out

    def _any_ping_alive(self, store) -> bool:
        """Any pings.* row in the last 90 s window means at least
        one host responded. The pings collector only writes on
        successful replies - a gap is the failure signal."""
        cutoff = int(time.time()) - 90
        row = store.conn.execute(
            "SELECT 1 FROM samples_hot "
            "WHERE metric LIKE 'pings.%.rtt_ms' AND ts >= ? LIMIT 1",
            (cutoff,)).fetchone()
        return row is not None

    def _first_ping_ms(self, store) -> int | None:
        cutoff = int(time.time()) - 90
        row = store.conn.execute(
            "SELECT value FROM samples_hot "
            "WHERE metric LIKE 'pings.%.rtt_ms' AND ts >= ? "
            "ORDER BY ts DESC LIMIT 1",
            (cutoff,)).fetchone()
        return int(row["value"]) if row else None

    def _mbps_delta(self, store, iface: str | None, suffix: str) -> float | None:
        """Pull last two cumulative byte samples for one iface,
        return the delta-rate in Mbps. Returns None when the
        iface name is unknown, or when we don't have two adjacent
        samples (router just rebooted / iface just appeared)."""
        if not iface:
            return None
        metric = f"throughput.{iface}.{suffix}"
        rows = store.conn.execute(
            "SELECT ts, value FROM samples_hot "
            "WHERE metric = ? ORDER BY ts DESC LIMIT 2",
            (metric,)).fetchall()
        if len(rows) < 2:
            return None
        # rows[0] = newest, rows[1] = older.
        dt = max(1, rows[0]["ts"] - rows[1]["ts"])
        delta_bytes = max(0, rows[0]["value"] - rows[1]["value"])
        # bytes/sec → Mbps: × 8 / 1e6
        return round(delta_bytes / dt * 8 / 1_000_000, 2)

    def _on_battery_for_live(self, store) -> str | None:
        """Walk hot-tier `battery.charging` samples backwards;
        find the most recent 1→0 transition. Format the elapsed
        seconds as `Xh Ym` / `Mm` / `Ss`. Returns None if the
        router is currently charging or we don't have enough
        history to know."""
        rows = store.conn.execute(
            "SELECT ts, value FROM samples_hot "
            "WHERE metric = 'battery.charging' "
            "ORDER BY ts DESC LIMIT 60",
            ).fetchall()
        if not rows or rows[0]["value"] > 0.5:
            return None
        unplugged_at = rows[0]["ts"]
        for r in rows:
            if r["value"] > 0.5:
                break
            unplugged_at = r["ts"]
        elapsed = max(0, int(time.time()) - unplugged_at)
        if elapsed < 60:
            return f"{elapsed}s"
        if elapsed < 3600:
            return f"{elapsed // 60}m"
        return f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"

    # ---- Live (subprocess) readers ----

    def _guess_wan_iface(self, caps) -> str | None:
        """Heuristic: prefer the iface whose name screams "WAN"
        (wwan* / 4g* / wan*). Fall back to the first ethernet
        port the cap probe found (eth0 on Mudi). Conscious
        trade-off: on a Wi-Fi-only multi-WAN box we'd want a
        smarter pick, but Live Activity's single Mbps line means
        we have to choose *one* number anyway."""
        for p in caps.ethernet_ports:
            n = p.name.lower()
            if n.startswith(("wwan", "4g", "wan")):
                return p.name
        if caps.ethernet_ports:
            return caps.ethernet_ports[0].name
        return None

    def _cellular_live(self) -> tuple[str | None, str | None]:
        """Carrier name + LTE band for the active SIM slot. Two
        ubus calls because GL.iNet split them: `cellular.network
        info` has the cell radio metrics (band, RSRP …) keyed by
        slot; `cellular.sim status` has the carrier name keyed
        by slot. We pick the slot whose network entry has an
        `ipv4.ip` (the live data path) and match its carrier
        name from sim-status."""
        rc, out = self._run(
            ["ubus", "call", "cellular.network", "info", "{}"], 1.5)
        active_slot = None
        band = None
        if rc == 0 and out:
            try:
                data = json.loads(out)
                nets = data.get("networks") if isinstance(data, dict) else None
                if isinstance(nets, list):
                    for n in nets:
                        if isinstance(n, dict) and n.get("ipv4", {}).get("ip"):
                            active_slot = n.get("slot")
                            cell = n.get("cell_info") or {}
                            raw_band = cell.get("band")
                            if isinstance(raw_band, (int, str)) and str(raw_band).strip():
                                band = (f"B{raw_band}"
                                        if str(raw_band).isdigit()
                                        else str(raw_band))
                            break
            except ValueError:
                pass

        carrier = None
        if active_slot:
            rc2, out2 = self._run(
                ["ubus", "call", "cellular.sim", "status", "{}"], 1.0)
            if rc2 == 0 and out2:
                try:
                    sim_data = json.loads(out2)
                    sims = sim_data.get("sims") if isinstance(sim_data, dict) else None
                    if isinstance(sims, list):
                        for s in sims:
                            if (isinstance(s, dict)
                                    and str(s.get("slot")) == str(active_slot)):
                                carrier = s.get("carrier") or s.get("operator")
                                break
                except ValueError:
                    pass
        return carrier, band

    # 5-minute cache for ipinfo.io results so we don't hammer the
    # service on every snapshot push (default tick on charging is
    # also 5 min, so effectively one poll per push). Public IP
    # rarely changes mid-session and the geo string never does
    # for a given IP.
    _PUBLIC_IP_CACHE_TTL_S = 300
    _public_ip_cached_at: float = 0.0
    _public_ip_cached: tuple[str | None, str | None] = (None, None)

    def _public_ip_live(self) -> tuple[str | None, str | None]:
        """Public IP + "City, Country · ISP" string. Two-tier
        lookup:
          1. Some GL.iNet builds expose a cached value via
             `ubus call gl-tracking.public_ip get` - used when
             present (cheap, no network round-trip from the
             daemon).
          2. Fallback to ipinfo.io directly, with a 5-min cache
             so a tight tick cadence doesn't hammer the service.
             Required for the Mudi 7 firmware family (`gl-tracking`
             ubus object isn't present there) - without this the
             widget's `publicIP` field stayed nil on every daemon
             push, and the iOS app's snapshot store would lose
             the IP whenever a daemon push landed after a
             foreground refresh that had populated it.
        Either way the daemon-reported tuple matches what the
        iOS-side ExternalIpProbe writes when the app's open, so
        the widget shows the same value regardless of which side
        wrote the snapshot last."""
        # First try the GL.iNet cache.
        rc, out = self._run(
            ["ubus", "call", "gl-tracking.public_ip", "get", "{}"], 1.0)
        if rc == 0 and out:
            try:
                data = json.loads(out)
                ip = data.get("ip") or data.get("public_ip")
                if ip:
                    loc_bits = []
                    for k in ("city", "region", "country"):
                        v = data.get(k)
                        if v:
                            loc_bits.append(str(v))
                    return ip, (", ".join(loc_bits)
                                if loc_bits else None)
            except ValueError:
                pass

        # Fallback: direct ipinfo.io poll, cached.
        now = time.time()
        if now - self._public_ip_cached_at < self._PUBLIC_IP_CACHE_TTL_S:
            return self._public_ip_cached
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://ipinfo.io/json",
                headers={"User-Agent": "glintd/0.4"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ip = data.get("ip")
            # ipinfo's "city,region,country" + "org" (e.g.
            # "AS197207 Lifecell"). Trim the AS prefix to match
            # the iOS-side rendering.
            loc_bits = []
            for k in ("city", "region", "country"):
                v = data.get(k)
                if v:
                    loc_bits.append(str(v))
            org = data.get("org") or ""
            if org.startswith("AS"):
                # "AS12345 Foo Bar" → "Foo Bar"
                parts = org.split(None, 1)
                org = parts[1] if len(parts) > 1 else org
            geo_left = ", ".join(loc_bits) if loc_bits else ""
            geo = (f"{geo_left} · {org}".strip(" ·")
                   if (geo_left or org) else None)
            self._public_ip_cached = (ip, geo)
            self._public_ip_cached_at = now
            return self._public_ip_cached
        except Exception:
            return self._public_ip_cached

    def _active_tunnel_live(self) -> str | None:
        """Name of the FIRST WireGuard tunnel with a recent
        handshake (≤ 3 min old). Returns nil when no tunnel is up.
        Legacy single-tunnel field kept for older snapshot
        consumers; new code should call `_active_tunnels_live`
        and use the full list."""
        names = self._active_tunnels_live()
        return names[0] if names else None

    def _active_tunnels_live(self) -> list[str]:
        """All WireGuard tunnels currently up (handshake within
        the last 180 s). Used for the widget's split-tunnel
        display ("M.Home + Primary Tunnel" when both are active).
        Order is the order `wg show all dump` returns peers - the
        router-side ordering matches the GL.iNet web UI's "From"
        column, which is what the user is comparing against."""
        rc, out = self._run(["wg", "show", "all", "dump"], 1.0)
        if rc != 0:
            return []
        now = int(time.time())
        seen: set[str] = set()
        out_list: list[str] = []
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) < 9:  # interface lines have 5; peers have 9
                continue
            try:
                last_hs = int(cols[5])
            except ValueError:
                continue
            if last_hs > 0 and now - last_hs < 180:
                name = cols[0]
                if name and name not in seen:
                    seen.add(name)
                    out_list.append(name)
        return out_list

    @staticmethod
    def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, check=False)
            return r.returncode, r.stdout
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""

    # Mapping of GL.iNet SoC/SKU strings → human-readable router
    # name. The kernel's `/tmp/sysinfo/model` is verbose ("GL.iNet
    # E5800, Qualcomm Technologies, Inc. SDXPINN IDP MBB") and
    # gets line-wrapped on the lock screen; the lookup table
    # keeps the LA tile compact. Keys are case-folded substrings
    # so we match on bits like "E5800" without being picky about
    # the surrounding text. Add new rows here as we verify them.
    _MODEL_FRIENDLY = (
        ("e5800",      "Mudi 7"),
        ("e750v2",     "Mudi V2"),
        ("e750",       "Mudi"),
        ("x3000",      "Spitz AX"),
        ("xe3000",     "Puli AX"),
        ("be9300",     "Flint 3"),
        ("be3600",     "Slate 7"),
        ("mt6000",     "Flint 2"),
        ("axt1800",    "Slate AX"),
        ("mt3000",     "Beryl AX"),
        ("mt2500",     "Beryl"),
        ("mt1300",     "Beryl"),
        ("ar750s",     "Slate"),
        ("mt300n-v2",  "Mango"),
    )

    @classmethod
    def _friendly_model(cls, model_raw: str | None) -> str:
        """Compact router-model string for the LA lock-screen
        tile. Falls back to the first comma-separated chunk of
        the kernel's verbose name when the SKU isn't in the
        mapping ("GL.iNet E5800, Qualcomm Technologies…" → "GL.iNet
        E5800")."""
        if not model_raw:
            return "Router"
        haystack = model_raw.lower()
        for needle, friendly in cls._MODEL_FRIENDLY:
            if needle in haystack:
                return friendly
        # Fallback: keep just the first chunk before a comma.
        return model_raw.split(",", 1)[0].strip() or "Router"
