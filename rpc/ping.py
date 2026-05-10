"""ping - health check. Returns daemon version, uptime, and the
serialised Capabilities so the app can populate its UI without a
separate `list_metrics` call on first connect."""
from __future__ import annotations
import json
import time
from typing import Any
from glintd.rpc.context import open_store
# Single source of truth lives in glintd.version; the app reads
# the same string back out of `ping`'s reply to compare against
# the published `glint.nakitel.com/glintd/version.txt`.
from glintd.version import VERSION


def handle(args: dict[str, Any]) -> dict[str, Any]:
    store = open_store()
    caps_json = store.get_meta("capabilities") or "{}"
    boot_ts_raw = store.get_meta("boot_ts")
    uptime_s = 0
    if boot_ts_raw:
        try:
            uptime_s = max(0, int(time.time() - int(boot_ts_raw)))
        except ValueError:
            pass
    # Surface router_id so the client can key per-router push
    # registrations on it. Stable across restarts (written once at
    # install time, never rotated). Best-effort read - if the file
    # is missing we still return the rest of the ping payload.
    router_id = ""
    try:
        with open("/etc/glintd/router_id") as f:
            router_id = f.read().strip()
    except (OSError, ValueError):
        pass
    return {
        "ok": True,
        "version": VERSION,
        "uptime_s": uptime_s,
        "router_id": router_id,
        "capabilities": json.loads(caps_json),
    }
