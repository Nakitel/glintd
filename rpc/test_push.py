"""test_push - fire a one-off push through the relay so the
operator can verify the chain is alive without waiting for a
real threshold crossing. Reads every registered token, builds
a basic alert payload, hands it to relay_client.push.

Returns the per-token result so the operator can see HTTP status
+ APNs reason inline. No state change on the daemon side beyond
incrementing the relay's rate counter."""
from __future__ import annotations
import time
from typing import Any
from glintd.rpc.context import open_store
from glintd.apns.relay_client import push as relay_push


def handle(args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title") or "Glint test"
    body  = args.get("body")  or "Push delivered through Nakitel relay."
    store = open_store()
    cur = store.conn.execute(
        "SELECT token, platform, bundle_id FROM push_tokens")
    rows = list(cur)
    if not rows:
        return {"error": "no push tokens registered yet"}

    cohorts: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        plat = r["platform"]; bid = r["bundle_id"]
        cohorts.setdefault((plat, bid), []).append(r["token"])

    results = []
    sent = 0
    for (plat, bid), toks in cohorts.items():
        ok = relay_push(
            tokens=toks,
            platform=plat,
            bundle_id=bid,
            event="test",
            payload={
                "aps": {
                    "alert": {"title": title, "body": body},
                    "sound": "default",
                },
            },
        )
        results.append({
            "platform": plat, "bundle_id": bid,
            "token_count": len(toks), "ok": ok,
        })
        if ok:
            sent += 1
    return {
        "ok": sent > 0,
        "fired_at": int(time.time()),
        "cohorts": results,
    }
