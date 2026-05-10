"""Widget-snapshot push. Sibling of `LiveActivityUpdater` but for
the static Home Screen / Lock Screen widget surface.

Why a separate channel:
  * Live Activity pushes need an ALIVE Activity on the device -
    one that the user explicitly started from "Monitor connection".
    Most users won't do that for "I just want my widget to be
    fresh"; they install the widget and walk away.
  * iOS has hourly silent-push budgets that are SEPARATE per type.
    Burning the LA budget on widget refresh would cap the LA
    throughput in a bad-network event (the case that matters most).
  * Widget snapshots can use `apns-push-type: background` (the
    relay routes this when `event` starts with `widget.`), which
    iOS allows without a foreground Activity and rate-limits more
    permissively (~3/h sustained, bursts up to ~10/h).

Adaptive cadence (battery-aware):

    Charging        →  5 min  cadence
    On battery      → 15 min  cadence
    Idle (no change)→  skip   (don't push if payload identical to
                                last successful one - saves Apple's
                                budget and the router's 4G airtime)

The "no change" detection compares a STABLE FINGERPRINT of the
payload (excluding capturedAt/timestamp). Without that exclusion
every tick would look "different" on the timestamp alone and we'd
push uselessly forever.
"""
from __future__ import annotations
import hashlib
import json
import time
from typing import Any
from glintd.apns.relay_client import push as relay_push


class SnapshotPusher:
    """One instance per daemon, lifecycle managed alongside the
    threshold engine. Reuses the LiveActivityUpdater's state-builder
    so both surfaces stay in lock-step on field semantics."""

    # Cadence floors. The daemon's `_tick` runs ~every 5 s - these
    # are the soonest we'll consider sending another push.
    MIN_INTERVAL_CHARGING_S = 5 * 60
    MIN_INTERVAL_BATTERY_S  = 15 * 60
    # Forced re-push interval even if payload looks identical. iOS
    # widgets visibly age via `Text(date:.relative)` - a stale
    # snapshot looks broken to the user even if the underlying state
    # hasn't moved. After this many seconds we re-push regardless.
    HEARTBEAT_S = 60 * 60

    def __init__(self, live_activity):
        # Borrow the LA's payload builder. We don't want to
        # re-implement carrier / public-IP / tunnel poll three times.
        self._la = live_activity
        self.last_push_at: float = 0.0
        self.last_payload_hash: str | None = None

    def tick(self, store, caps) -> None:
        now = time.time()
        # Fast bail: any registered widget-capable tokens at all?
        # Widget-snapshot push uses the same `push_tokens` rows as
        # alert pushes (platform="ios" / "macos") - the relay
        # decides push-type from the event string, not the platform.
        cur = store.conn.execute(
            "SELECT token, bundle_id, platform FROM push_tokens "
            "WHERE platform IN ('ios', 'macos')")
        rows = list(cur)
        if not rows:
            return

        # Battery-aware cadence. Read straight from the latest hot
        # sample - if the metric is missing (Wi-Fi-only board with
        # no MCU) we treat it as "charging" because such routers are
        # always on mains power. That keeps the more-frequent
        # cadence on tabletop installs and avoids false-positive
        # 15-min throttling there.
        charging = self._is_charging(store)
        min_interval = (self.MIN_INTERVAL_CHARGING_S if charging
                        else self.MIN_INTERVAL_BATTERY_S)
        if now - self.last_push_at < min_interval:
            return

        # Build the payload from the LA's helper to avoid
        # duplicating the carrier / public-IP / tunnel poll. The
        # widget's WidgetSnapshot decoder is a strict subset of the
        # LA's ContentState (no rxMbps / txMbps / onBatteryFor yet),
        # so passing the wider dict through just means a few keys
        # the widget ignores via Codable's default unknown-key skip.
        state = self._la._content_state(store, caps)

        # `uplinkKind` - the widget reads this to render the SIM /
        # ethernet / wifi-repeater icon. LA's ContentState carries
        # the data through `wanKind` only; widgets need the finer
        # split for the icon picker.
        state["uplinkKind"] = self._uplink_kind(store, caps, state)
        # Translate `wanKind` from the LA dict (it's a friendly
        # English label) to the snapshot's machine-readable shape
        # (cellular/ethernet/wifi/tethering). Default to whatever
        # was already there if we can't classify.
        state["wanKind"] = self._normalize_wan_kind(state)
        # Fields the Swift `WidgetSnapshot` Codable decoder requires
        # but `_content_state` doesn't currently emit. Without these
        # the iOS push handler bails at `JSONDecoder.decode` with
        # `keyNotFound` and the snapshot file never gets written -
        # widget shows the previous (stale) data and the device's
        # diagnostic ledger surfaces "decode-fail". The defaults
        # here mirror Swift's `WidgetSnapshot.init` defaults so
        # absence-side semantics match between local and pushed
        # payloads.
        state.setdefault("tunnelKillswitch", False)
        state.setdefault("batteryTimeRemaining", None)
        state.setdefault("schema", 1)

        fingerprint = self._fingerprint(state)
        # Heartbeat override: even if the user's parked their router
        # at the same numbers, we re-push every HEARTBEAT_S so the
        # widget's relative timestamp doesn't drift past "1 hour ago"
        # and look broken. Without the override the user could leave
        # for the day and come home to a "5 hours ago" widget that
        # technically reflects current truth.
        elapsed = now - self.last_push_at
        if (fingerprint == self.last_payload_hash
                and elapsed < self.HEARTBEAT_S):
            return

        # Cohort + push. Same group-by-bundle pattern threshold uses,
        # because we send to one (platform, bundle) at a time.
        cohorts: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            key = (r["platform"], r["bundle_id"])
            cohorts.setdefault(key, []).append(r["token"])

        # Anchor BEFORE attempting the push so a flaky relay doesn't
        # turn into a per-tick retry loop. Same rationale as the
        # rate-limit anchor in `LiveActivityUpdater.tick`.
        self.last_push_at = now

        invalid: list[str] = []
        any_ok = False
        total_tokens = sum(len(v) for v in cohorts.values())
        for (plat, bid), tokens in cohorts.items():
            ok, dead = relay_push(
                tokens=tokens,
                platform=plat,
                bundle_id=bid,
                event="widget.snapshot",
                # The widget snapshot push-type is `background` per
                # APNs spec - set in the relay based on the
                # `widget.` event prefix. The payload MUST carry
                # `aps.content-available: 1` for iOS to wake the
                # app delegate's `didReceiveRemoteNotification`.
                # `aps.alert` and `aps.sound` are intentionally
                # absent - including either turns this into a
                # visible push and Apple rejects the priority=5
                # combo.
                payload={
                    "aps": {
                        "content-available": 1,
                    },
                    "glint.snapshot": state,
                },
            )
            invalid.extend(dead)
            any_ok = any_ok or ok
        # Only update the dedup hash on at-least-one-success; a
        # blanket prune (all tokens 410'd) shouldn't lock us into
        # "skip next tick" because the next snapshot might land on
        # a fresh token that the user installs in the meantime.
        if invalid and len(invalid) == total_tokens:
            self.last_payload_hash = None
        else:
            self.last_payload_hash = fingerprint
        # One log line per successful tick. Cadence is 5-15 min so
        # this won't flood. Lets ops verify the pipeline works
        # without scraping the relay's journal - and gives us a
        # local timeline if APNs starts dropping pushes silently.
        print(f"[glintd] widget.snapshot push: ok={any_ok} "
              f"tokens={total_tokens} invalid={len(invalid)} "
              f"heartbeat={'yes' if elapsed >= self.HEARTBEAT_S else 'no'}",
              flush=True)

        if invalid:
            try:
                placeholders = ",".join("?" for _ in invalid)
                store.conn.execute(
                    f"DELETE FROM push_tokens WHERE token IN "
                    f"({placeholders})",
                    invalid)
                print(f"[glintd] pruned {len(invalid)} APNs-rejected "
                      f"widget-snapshot token(s)", flush=True)
            except Exception as e:
                print(f"[glintd] widget-snapshot prune failed: {e}",
                      flush=True)

    # ---- helpers ----

    def _is_charging(self, store) -> bool:
        """True when the latest battery.charging hot row says >0.5,
        OR there's no battery row at all (Wi-Fi-only mains-powered
        router). Querying both metrics in one statement keeps the
        check to a single SQL round-trip."""
        try:
            row = store.conn.execute(
                "SELECT value FROM samples_hot "
                "WHERE metric = 'battery.charging' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
        except Exception:
            return True  # safe-default: more-frequent cadence
        if row is None:
            return True
        return row["value"] > 0.5

    def _fingerprint(self, state: dict[str, Any]) -> str:
        """Stable hash of the meaningful payload fields. Excludes
        any field that would change every tick (capturedAt is a
        Unix-seconds counter - including it would defeat the dedup
        outright). Sort keys to be order-stable across Python
        dict-iteration order changes."""
        ignore = {"capturedAt"}
        slim = {k: v for k, v in state.items() if k not in ignore}
        blob = json.dumps(slim, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _uplink_kind(self, store, caps, state: dict) -> str | None:
        """Resolve the widget's finer uplink classification -
        `sim1`/`sim2` when cellular and slot is known, otherwise
        `cellular`/`ethernet`/`wifi-repeater`/`tethering`. Mirrors
        the same resolution the Glint app applies on its own
        snapshot writes, so widget icons render identically
        whether the snapshot arrived via push or via SSH probe.
        """
        wan = (state.get("wanKind") or "").lower()
        if wan in ("ethernet", "wired", "lan", "wan"):
            return "ethernet"
        if wan in ("wifi", "wi-fi", "repeater"):
            return "wifi-repeater"
        if wan in ("usb", "tether", "tethering"):
            return "tethering"
        # Cellular path - pick the active slot if we can.
        try:
            row = store.conn.execute(
                "SELECT metric, value FROM samples_hot "
                "WHERE metric IN "
                "('signal.sim1.rsrp_dbm','signal.sim2.rsrp_dbm') "
                "AND ts >= ? ORDER BY ts DESC",
                (int(time.time()) - 120,)).fetchall()
        except Exception:
            row = []
        # We don't truly know "active" from RSRP alone, but since
        # the LA helper already settles on the higher-RSRP slot for
        # `signalRSRPdBm`, mirror that choice for the icon.
        rsrps = {1: None, 2: None}
        for r in row:
            slot = 1 if "sim1" in r["metric"] else 2
            if rsrps[slot] is None:
                rsrps[slot] = r["value"]
        candidates = [s for s, v in rsrps.items() if v is not None]
        if candidates:
            best = max(candidates, key=lambda s: rsrps[s] or -999)
            return f"sim{best}"
        if wan == "cellular":
            return "cellular"
        return None

    def _normalize_wan_kind(self, state: dict) -> str:
        """Map a friendly LA wanKind ("ethernet", "Cellular", "Wi-Fi"
        etc.) to the lowercase machine token the widget snapshot
        decoder expects. Falls back to the input lowercased so a
        new value passes through readably rather than as an empty
        string."""
        s = (state.get("wanKind") or "").strip().lower()
        if not s:
            return "-"
        if s in ("ethernet", "wired"):  return "ethernet"
        if s.startswith("cell"):        return "cellular"
        if s.startswith("wi"):          return "wifi"
        if s in ("usb", "tether", "tethering"): return "tethering"
        return s
