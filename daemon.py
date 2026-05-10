#!/usr/bin/env python3
"""glintd entrypoint. procd-supervised long-running process.

Lifecycle:
    1. Detect capabilities once at startup.
    2. Build the active collector set: each module's `requires(caps)`
       is consulted, modules that say "no" are dropped silently.
    3. Schedule each collector at its declared INTERVAL_SEC.
    4. Run the rollup task every 60 s.
    5. Run the ubus RPC server (in the same process - single-thread
       scheduler is enough for our load).

We deliberately don't fork. procd already restarts us on crash;
self-supervision would just hide bugs.
"""
from __future__ import annotations
import os
import signal
import sqlite3
import sys
import time
import json
from pathlib import Path

# Make `from glintd.x` imports work whether we're running from
# /etc/glintd/ (production) or from the source tree.
HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from glintd.capabilities import detect, Capabilities
from glintd.collectors import discover
from glintd.storage.store import Store
from glintd.rpc.server import RpcServer
from glintd.apns.thresholds import ThresholdEngine
from glintd.apns.liveactivity import LiveActivityUpdater
from glintd.apns.snapshot_pusher import SnapshotPusher
from glintd.apns.relay_client import heartbeat as relay_heartbeat

DEFAULT_DB    = "/tmp/glintd.db"
SNAPSHOT_PATH = "/etc/glintd/state.db"
SCHEMA_PATH   = str(HERE / "storage" / "schema.sql")
ROLL_INTERVAL = 60
SNAPSHOT_INTERVAL = 30 * 60   # 30 min - survives reboots
# Prune push tokens that haven't been re-registered in this long.
# The iOS / macOS app re-registers on every launch (the OS hands
# its current APNs token back through
# `application:didRegisterForRemoteNotificationsWithDeviceToken:`),
# so a token absent from a register call for two weeks means
# either the user moved their device off this router profile,
# uninstalled the app, or APNs rotated the token already. Either
# way pushing to it is wasted - and worse, accumulates "zombie"
# tokens that the relay forwards to APNs and gets 410'd on every
# event, eating push budget without reaching a real device.
# Live-Activity push-to-start tokens age the same way (only
# refreshed on a fresh Activity registration), so the same
# window applies; they just renew through a different RPC.
TOKEN_TTL_S       = 14 * 86400
TOKEN_PRUNE_EVERY = 3600       # check once an hour - cheap query
# Heartbeat to the relay's watchdog. 120 s cadence pairs with the
# relay-side 360 s offline threshold (3 missed beats + grace).
# Skipped when there are no registered push tokens - if nobody is
# listening, an offline alert has nothing to fan out to anyway,
# and we'd just be paying the round-trip for nothing.
HEARTBEAT_EVERY = 120


class Daemon:
    def __init__(self):
        self.caps: Capabilities = detect()
        self.store = Store(DEFAULT_DB, SCHEMA_PATH)
        # Restore snapshot first - if we set meta before this, the
        # restore copies the old snapshot's DB on top and the
        # caps row vanishes. Order matters.
        self._restore_snapshot_if_any()
        self.store.set_meta("capabilities", self.caps.to_json())
        # Daemon boot timestamp - read by ping.py to compute
        # uptime (the CLI is a fresh process per call, so it
        # can't measure its own runtime).
        self.store.set_meta("boot_ts", str(int(time.time())))
        self.collectors = self._active_collectors()
        self.next_due: dict[str, float] = {
            m.NAME: 0.0 for m in self.collectors
        }
        self.next_roll = 0.0
        self.next_snapshot = time.monotonic() + SNAPSHOT_INTERVAL
        # First prune fires immediately (catches accumulated stale
        # rows on daemon restart), then on TOKEN_PRUNE_EVERY.
        self.next_token_prune = 0.0
        # First heartbeat fires immediately so a daemon restart
        # clears any stale "offline" state on the relay before the
        # user notices.
        self.next_heartbeat_tick = 0.0
        # Threshold engine - runs after each tick so push events
        # see the just-written hot rows. No-op until any push
        # tokens are registered.
        self.thresholds = ThresholdEngine()
        self.next_thresholds = 0.0
        # Live Activity content-state push. Self-paced
        # via its own min-interval guard; tick() checks every
        # second and the updater no-ops until 60 s has elapsed.
        self.live_activity = LiveActivityUpdater()
        # Widget-snapshot push (background channel). Cadence is
        # adaptive: 5 min on charging, 15 min on battery, payload
        # dedup so an idle router stops pushing entirely. Reuses
        # `live_activity._content_state` so both surfaces share
        # field semantics. Self-paced; tick every loop is fine.
        self.snapshot_pusher = SnapshotPusher(self.live_activity)
        self.running = True
        self.rpc = RpcServer(self.store, self.caps)

    def _active_collectors(self) -> list:
        """Filter `discover()` output by each module's `requires(caps)`.
        Logs the active set once at startup so the install path
        can verify what it ended up wiring up."""
        out = []
        for mod in discover():
            try:
                if mod.requires(self.caps):
                    out.append(mod)
                    print(f"[glintd] enabled: {mod.NAME} "
                          f"@ {mod.INTERVAL_SEC}s",
                          flush=True)
                else:
                    print(f"[glintd] skipped: {mod.NAME} "
                          f"(capability missing)",
                          flush=True)
            except Exception as e:
                # Don't let one bad module crash the daemon.
                print(f"[glintd] error inspecting {mod.NAME}: {e}",
                      flush=True)
        return out

    def _wal_inode(self) -> int | None:
        """Inode of the current `/tmp/glintd.db-wal`, or None when
        absent. Used to detect file rotation while we hold an open
        fd."""
        try:
            return os.stat(DEFAULT_DB + "-wal").st_ino
        except OSError:
            return None

    def _check_wal_inode_drift(self) -> None:
        """If the WAL inode changed since we last opened it, our
        writer's fd is pointing at a now-orphaned file. Common on
        tmpfs after checkpoint races. Reopens immediately."""
        cur = self._wal_inode()
        if cur is None:
            return
        prev = getattr(self, "_known_wal_inode", None)
        if prev is None:
            self._known_wal_inode = cur
            return
        if cur != prev:
            self._reopen_store(
                f"WAL inode rotated ({prev} → {cur}); writer's fd is stale")
            self._known_wal_inode = self._wal_inode()

    def _guard_wal(self) -> None:
        """Detect WAL-write isolation and recover.

        We've seen the daemon's writer connection silently lose
        write visibility - collectors log success, write_hot
        returns clean, but RPC reads (and CLI sqlite3) see stale
        data. Two known triggers: tmpfs unlinks /tmp/glintd.db-wal
        while we hold an open fd, and (more rarely) a sqlite
        auto-checkpoint that leaves the connection in a state where
        subsequent writes don't land in the main DB.

        Heartbeat-based detection: every ~30 s we stamp a
        monotonically-increasing token into `meta` via the writer
        connection, then read it back through a *fresh*
        connection. A drift between the two means new writes
        aren't surfacing. We then reopen.

        File-presence check stays as a cheap first pass - if both
        -wal and -shm vanish, we know we're in zombie state without
        waiting for the heartbeat round-trip."""
        wal = DEFAULT_DB + "-wal"
        shm = DEFAULT_DB + "-shm"
        if not os.path.isfile(wal) and not os.path.isfile(shm):
            self._reopen_store("WAL+SHM files missing")
            return
        # Heartbeat - every 30 s. Cheap on the writer; the verify
        # uses a fresh sqlite handle so stale-snapshot bugs surface.
        now_mono = time.monotonic()
        if now_mono < getattr(self, "_next_heartbeat", 0.0):
            return
        self._next_heartbeat = now_mono + 30
        token = f"{int(time.time())}.{now_mono:.3f}"
        try:
            self.store.set_meta("__heartbeat", token)
        except Exception as e:
            print(f"[glintd] heartbeat write failed: {e}", flush=True)
            return
        # Verify via independent connection so the zombie path
        # can't satisfy the read with the writer's own buffer.
        try:
            verifier = sqlite3.connect(DEFAULT_DB,
                                       isolation_level=None,
                                       check_same_thread=False)
            cur = verifier.execute(
                "SELECT value FROM meta WHERE key = '__heartbeat'")
            row = cur.fetchone()
            verifier.close()
        except Exception as e:
            print(f"[glintd] heartbeat verify failed: {e}", flush=True)
            return
        if row is None or row[0] != token:
            seen = (row[0] if row else "<none>")
            self._reopen_store(
                f"heartbeat drift - wrote {token!r}, read {seen!r}")

    def _reopen_store(self, reason: str) -> None:
        print(f"[glintd] reopening sqlite - {reason}", flush=True)
        try:
            self.store.conn.close()
        except Exception:
            pass
        self.store = Store(DEFAULT_DB, SCHEMA_PATH)
        self.store.set_meta("capabilities", self.caps.to_json())
        self.rpc.store = self.store
        self._next_heartbeat = 0.0
        self._known_wal_inode = self._wal_inode()

    def _restore_snapshot_if_any(self):
        """If a previous run flushed a snapshot to overlay, copy it
        over our fresh tmpfs DB so we keep continuity across
        reboots. Best-effort: missing / corrupt snapshot just
        starts the day from zero.

        push_tokens is intentionally CLEARED after restore. Reason:
        snapshot can hold push-token rows with stale `environment`
        values from earlier daemon builds (the env column was
        added retroactively, and devices whose iOS app registered
        before the embedded-mobileprovision detection landed have
        a wrong env stored). Carrying those forward across a
        router reboot causes the first round of post-boot threshold
        pushes (router.booted, widget.snapshot) to fire to a token
        bound to one APNs environment but routed through the other,
        which APNs answers with BadDeviceToken and the daemon
        prunes the row anyway. The app's foreground reconnect
        re-registers each token with the current correct env
        within ~10 s, so the only thing we lose by clearing here
        is the router.booted push to phones that were already
        backgrounded when the reboot happened - an acceptable
        trade vs the slow-recovery cycle.
        """
        if not os.path.isfile(SNAPSHOT_PATH):
            return
        try:
            import shutil
            # Already opened our DB; close, copy, reopen.
            self.store.conn.close()
            shutil.copyfile(SNAPSHOT_PATH, DEFAULT_DB)
            self.store = Store(DEFAULT_DB, SCHEMA_PATH)
            print(f"[glintd] restored snapshot from {SNAPSHOT_PATH}",
                  flush=True)
            # Clear push_tokens. The hot-sample history we DO want
            # (24-hour ring) survives; only the auth/identity rows
            # are dropped.
            try:
                cur = self.store.conn.execute(
                    "DELETE FROM push_tokens")
                if cur.rowcount > 0:
                    print(f"[glintd] cleared {cur.rowcount} stale "
                          f"push_tokens after snapshot restore "
                          f"(app will re-register on next foreground)",
                          flush=True)
            except Exception as e:
                # Schema mismatch on very old snapshots - log and
                # continue. The token rows will heal on register
                # anyway via INSERT OR REPLACE.
                print(f"[glintd] couldn't clear push_tokens: {e}",
                      flush=True)
        except OSError as e:
            print(f"[glintd] snapshot restore failed: {e}",
                  flush=True)
            # Nuke the partial DB and start fresh - better than
            # a half-restored state lying around.
            try:
                os.remove(DEFAULT_DB)
            except OSError:
                pass
            self.store = Store(DEFAULT_DB, SCHEMA_PATH)

    def _flush_snapshot(self):
        """Atomic-ish copy of the live DB to the overlay path.
        The collectors keep writing during this - sqlite WAL
        handles the consistency, and an occasional tiny missed
        sample post-snapshot is acceptable for a 24 h ring."""
        try:
            import shutil
            os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
            tmp = SNAPSHOT_PATH + ".tmp"
            shutil.copyfile(DEFAULT_DB, tmp)
            os.replace(tmp, SNAPSHOT_PATH)
        except OSError as e:
            print(f"[glintd] snapshot flush failed: {e}", flush=True)

    def _tick(self) -> None:
        """One scheduler tick. Runs collectors that have come due,
        the rollup at its interval, and the snapshot at its."""
        now_mono = time.monotonic()
        self._check_wal_inode_drift()
        self._guard_wal()
        for mod in self.collectors:
            if now_mono < self.next_due[mod.NAME]:
                continue
            self.next_due[mod.NAME] = now_mono + mod.INTERVAL_SEC
            try:
                t0 = time.monotonic()
                samples = mod.collect(self.caps)
                dt = time.monotonic() - t0
                if samples:
                    self.store.write_hot(samples)
                    # Read-back verification: the writer connection
                    # should immediately see one of the rows it just
                    # wrote. If it doesn't, the writer's WAL is
                    # disconnected - reopen and re-apply the batch.
                    sample_metric = next(iter(samples))
                    cur = self.store.conn.execute(
                        "SELECT 1 FROM samples_hot "
                        "WHERE metric = ? ORDER BY ts DESC LIMIT 1",
                        (sample_metric,))
                    if cur.fetchone() is None:
                        print(f"[glintd] write-readback miss on "
                              f"{sample_metric!r} - reopening",
                              flush=True)
                        self._reopen_store("write-readback miss")
                        # Replay the batch on the fresh connection.
                        try:
                            self.store.write_hot(samples)
                        except Exception as e:
                            print(f"[glintd] replay after reopen "
                                  f"failed: {e}", flush=True)
                # Per-tick visibility into what each collector is
                # producing. Throughput especially: we lost 3+ minutes
                # of `rmnet_data*` samples once and it was invisible
                # without per-tick counts. Cheap to log; one line per
                # collector per cadence interval.
                if mod.NAME == "throughput":
                    rmnet_keys = [k for k in samples
                                  if "rmnet" in k and k.endswith(".rx_bytes")]
                    print(f"[glintd] tick {mod.NAME}: "
                          f"{len(samples)} keys, "
                          f"rmnet={len(rmnet_keys)}, "
                          f"{dt*1000:.0f}ms",
                          flush=True)
                else:
                    print(f"[glintd] tick {mod.NAME}: "
                          f"{len(samples)} keys, {dt*1000:.0f}ms",
                          flush=True)
            except Exception as e:
                # Single bad probe shouldn't take down the daemon.
                print(f"[glintd] {mod.NAME} collect failed: {e}",
                      flush=True)
        if now_mono >= self.next_roll:
            self.next_roll = now_mono + ROLL_INTERVAL
            try:
                self.store.roll()
            except Exception as e:
                print(f"[glintd] roll failed: {e}", flush=True)
        if now_mono >= self.next_snapshot:
            self.next_snapshot = now_mono + SNAPSHOT_INTERVAL
            self._flush_snapshot()
        # Prune push tokens older than TOKEN_TTL_S. Every fresh
        # `register_device_token` RPC call refreshes a row's
        # `registered` timestamp, so anything older means no
        # device announced ownership recently - APNs has almost
        # certainly invalidated it. Keeping it would hand the
        # relay a doomed delivery on every event-fire and
        # accumulate 410s on its budget. Cheap query (single
        # indexed column), one-line side effect.
        if now_mono >= self.next_token_prune:
            self.next_token_prune = now_mono + TOKEN_PRUNE_EVERY
            try:
                cutoff = int(time.time()) - TOKEN_TTL_S
                cur = self.store.conn.execute(
                    "DELETE FROM push_tokens WHERE registered < ?",
                    (cutoff,))
                if cur.rowcount > 0:
                    print(f"[glintd] pruned {cur.rowcount} stale "
                          f"push_tokens (older than "
                          f"{TOKEN_TTL_S // 86400}d)", flush=True)
            except Exception as e:
                print(f"[glintd] token prune failed: {e}", flush=True)
        # Threshold engine - runs every 30 s (slower than the 15 s
        # collectors so we don't burn requests on micro-fluctuation
        # but fast enough that wan.lost lands within ~30 s of the
        # actual outage). No-op when push_tokens table is empty.
        if now_mono >= self.next_thresholds:
            self.next_thresholds = now_mono + 30
            try:
                tokens = self._registered_tokens()
                if tokens:
                    self.thresholds.evaluate(self.store, self.caps, tokens)
            except Exception as e:
                print(f"[glintd] thresholds failed: {e}", flush=True)
        # Heartbeat to the relay watchdog. Skipped on empty roster
        # (nobody to fan an offline alert out to). Bundles the full
        # token meta so the relay can fan out without contacting us
        # if it later trips - and includes per-token muted CSV so
        # the relay can honour disabled_events from the app side.
        if now_mono >= self.next_heartbeat_tick:
            self.next_heartbeat_tick = now_mono + HEARTBEAT_EVERY
            try:
                roster = self._heartbeat_roster()
                if roster:
                    ok = relay_heartbeat(roster)
                    if not ok:
                        # Single-line log; the relay is allowed to
                        # be unreachable (CT down, Cloudflare blip,
                        # router on a metered link) and we should
                        # not spam the journal on extended outages.
                        print("[glintd] heartbeat: relay unreachable",
                              flush=True)
            except Exception as e:
                print(f"[glintd] heartbeat failed: {e}", flush=True)
        # Live Activity content-state updater. Self-rate-limits
        # to 60 s so we can call every tick without thinking.
        try:
            self.live_activity.tick(self.store, self.caps)
        except Exception as e:
            print(f"[glintd] live activity tick failed: {e}",
                  flush=True)
        # Widget-snapshot push (silent background, adaptive
        # cadence). Self-rate-limits to 5-15 min based on charge
        # state; idempotent on identical payloads. Tick is cheap
        # (one SQL on charging-state) when not yet due.
        try:
            self.snapshot_pusher.tick(self.store, self.caps)
        except Exception as e:
            print(f"[glintd] snapshot push tick failed: {e}",
                  flush=True)

    def _registered_tokens(self) -> list[dict]:
        """Pull the current push-token roster. Empty list when no
        client has paired - most installs, since pairing is opt-in
        per-device. Each row includes `disabled_events` (set of
        event ids the user has muted) so the threshold engine can
        skip the token for those events."""
        # CRITICAL: include `environment` in the SELECT. Without it,
        # the threshold engine's cohort builder falls through to its
        # `t.get("environment", "production")` default for every
        # token, sending sandbox-bound dev tokens to api.push.apple.
        # com (production) and getting BadDeviceToken back on every
        # event. The relay-side prune feedback then deletes the row,
        # iOS app re-registers (correctly, with env=development),
        # and the cycle restarts forever. Spent multiple hours
        # debugging the iOS env-detection layer before realising
        # the daemon's reader query never asked for the column at
        # all - the env was stored fine, just never read here.
        cur = self.store.conn.execute(
            "SELECT token, platform, bundle_id, "
            "COALESCE(environment, 'production') AS environment, "
            "disabled_events "
            "FROM push_tokens")
        out: list[dict] = []
        for r in cur:
            csv = r["disabled_events"] or ""
            disabled = {e.strip() for e in csv.split(",") if e.strip()}
            out.append({
                "token": r["token"],
                "platform": r["platform"],
                "bundle_id": r["bundle_id"],
                "environment": r["environment"],
                "disabled_events": disabled,
            })
        return out

    def _heartbeat_roster(self) -> list[dict]:
        """Roster blob sent with each heartbeat. The relay caches
        this verbatim per router so the watchdog can fan out an
        offline alert without us being reachable. One entry per
        push token with the cohort fields (platform, bundle_id,
        environment) the relay needs to group + the muted CSV so
        the relay can honour per-token event mutes.
        """
        cur = self.store.conn.execute(
            "SELECT token, platform, bundle_id, "
            "COALESCE(environment, 'production') AS environment, "
            "disabled_events "
            "FROM push_tokens")
        out: list[dict] = []
        for r in cur:
            out.append({
                "token":       r["token"],
                "platform":    r["platform"],
                "bundle_id":   r["bundle_id"],
                "environment": r["environment"],
                "muted":       r["disabled_events"] or "",
            })
        return out

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        # Start the RPC server in a background thread; it shares
        # the same Store instance and reads only - no locking
        # beyond sqlite's own.
        self.rpc.start()
        print(f"[glintd] running (model={self.caps.model!r}, "
              f"firmware={self.caps.firmware_version!r})", flush=True)
        while self.running:
            self._tick()
            time.sleep(1.0)
        # Shutdown - final snapshot so a clean stop preserves the
        # whole 24 h ring.
        self._flush_snapshot()
        self.rpc.stop()
        print("[glintd] stopped", flush=True)

    def _stop(self, *_):
        self.running = False


if __name__ == "__main__":
    Daemon().run()
