"""One-shot push that fires when the daemon's init script invokes
its `stop_service` hook (clean shutdown, reboot, or
`service glintd stop`). Best-effort - if the network's already
torn down by the time procd reaches this, the relay POST times
out and we exit silently.

Intentionally NOT routed through the live threshold engine
because that runs inside the daemon's main loop, which is
already being torn down here. Standalone module so a fresh
Python process can do the work without competing for resources
or stale state.
"""
from __future__ import annotations
import sys
from glintd.rpc.context import open_store
from glintd.apns.relay_client import push as relay_push


def main() -> int:
    try:
        store = open_store()
        cur = store.conn.execute(
            "SELECT token, platform, bundle_id, "
            "COALESCE(environment, 'production') AS environment, "
            "disabled_events "
            "FROM push_tokens")
        rows = list(cur)
    except Exception:
        return 0

    cohorts: dict[tuple[str, str, str], list[str]] = {}
    for r in rows:
        csv = r["disabled_events"] or ""
        muted = {e.strip() for e in csv.split(",") if e.strip()}
        if "router.shutting_down" in muted:
            continue
        cohorts.setdefault(
            (r["platform"], r["bundle_id"], r["environment"]),
            []
        ).append(r["token"])

    payload = {
        "aps": {
            "alert": {
                "title": "Router shutting down",
                "body":  "Glint companion is going offline.",
            },
            "sound": "default",
        },
        "glint.target": "dashboard",
    }
    for (plat, bid, env), toks in cohorts.items():
        try:
            relay_push(
                tokens=toks,
                platform=plat,
                bundle_id=bid,
                event="router.shutting_down",
                payload=payload,
                environment=env,
            )
        except Exception:
            # Best-effort - if relay is unreachable we just exit.
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
