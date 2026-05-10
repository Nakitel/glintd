"""get_history + list_metrics handlers."""
from __future__ import annotations
import time
from typing import Any
from glintd.rpc.context import open_store


def handle(args: dict[str, Any]) -> dict[str, Any]:
    """get_history(metric, since, tier='auto', limit=2000)

    Returns oldest-first. App passes the last unix-seconds it has
    locally as `since`. `limit` caps wire payload - even at 15 s
    resolution a full 24 h is 5 760 rows × 30 metrics, which the
    SSH channel will tolerate but the app's chart code prefers
    one metric at a time.
    """
    metric = args.get("metric")
    if not isinstance(metric, str) or not metric:
        return {"error": "metric is required"}
    since = args.get("since")
    if not isinstance(since, int):
        # Default: last hour. Lets a debug `ubus call` succeed
        # without a `since` param.
        since = int(time.time()) - 3600
    tier = args.get("tier", "auto")
    if tier not in ("auto", "hot", "warm", "cool"):
        tier = "auto"
    limit = args.get("limit", 2000)
    if not isinstance(limit, int) or limit <= 0 or limit > 10_000:
        limit = 2000

    store = open_store()
    rows = store.query(metric, since, tier)
    if len(rows) > limit:
        # Oldest entries trimmed first - the app cares about the
        # latest data overlapping the user's visible window.
        rows = rows[-limit:]
    return {"metric": metric, "tier": tier, "samples": rows}


def list_metrics_handle(args: dict[str, Any]) -> dict[str, Any]:
    store = open_store()
    return {"metrics": store.list_metrics()}
