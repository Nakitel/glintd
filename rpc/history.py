"""get_history + list_metrics handlers."""
from __future__ import annotations
import time
from typing import Any, Optional
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


def snapshot_handle(args: dict[str, Any]) -> dict[str, Any]:
    """get_snapshot([metrics])

    Returns the latest (ts, value) for every metric in one round
    trip. Without args returns everything the daemon has hot
    data for; with `metrics: [...]` filters down to that subset
    so the app can ask for just the keys its charts render.

    Shape:
        {
          "ts": <int unix seconds, latest across all metrics>,
          "samples": {
            "battery.pct":      {"ts": 1730000000, "value": 92.0},
            "internet.kind":    {"ts": 1730000000, "value": 1.0},
            ...
          }
        }

    Use case: cold-open seed. The app calls `get_snapshot` once
    after connect to populate every chart's "current value"
    instantly, then issues `get_history` per metric in
    background to fill the rest of the window. This replaces
    `list_metrics` + N × `get_history` for the "just give me
    the current state" path that runs on every fresh launch.
    """
    store = open_store()
    rows = store.latest_per_metric()
    filt = args.get("metrics")
    if isinstance(filt, list) and filt:
        wanted = {str(m) for m in filt}
        rows = {k: v for k, v in rows.items() if k in wanted}
    samples: dict[str, Any] = {}
    latest_ts = 0
    for metric, (ts, value) in rows.items():
        samples[metric] = {"ts": ts, "value": value}
        if ts > latest_ts:
            latest_ts = ts
    return {"ts": latest_ts, "samples": samples}


# Maps the internet kind code stored as a float in samples_hot
# back to its string label. Mirrors `_KIND_CODE` in
# `collectors/internet.py` - keep these two tables in sync. The
# app expects the same label spelling its own `GlintInternetKind`
# raw values use, so renaming any value here is a wire break.
_KIND_LABEL = {
    0: "unknown",
    1: "ethernet",
    2: "wifi",
    3: "cellular",
    4: "tethering",
    5: "cellular_sim1",
    6: "cellular_sim2",
}


def internet_history_handle(args: dict[str, Any]) -> dict[str, Any]:
    """get_internet_history(since, until=now, limit=4000)

    Returns the iface-timeline + latency series in one round
    trip. Mirrors the data the app builds in-memory from the
    refresh loop, just persisted across restarts and across
    multiple clients.

    Shape:
        {
          "since": <int>, "until": <int>,
          "samples": [
            {"ts": 12345, "kind": "ethernet", "latency_ms": 23.4},
            ...
          ]
        }

    `kind` is the string label; `latency_ms` is optional (missing
    when no ping host responded at that tick - rendered as a gap).
    """
    since = args.get("since")
    if not isinstance(since, int):
        since = int(time.time()) - 3600
    until = args.get("until")
    if not isinstance(until, int) or until <= since:
        until = int(time.time())
    limit = args.get("limit", 4000)
    if not isinstance(limit, int) or limit <= 0 or limit > 20_000:
        limit = 4000

    store = open_store()
    # "auto" tier - not "hot". Hot retains only 1 h, so a client
    # asking for a 12 h / 24 h window (e.g. reopening the app the
    # morning after leaving the router running overnight) used to
    # get just the last hour of iface-timeline + latency + loss,
    # with the rest of the strip rendered as a gap. The warm (6 h)
    # and cool (24 h) tiers hold the older buckets; auto picks the
    # narrowest tier that covers `since` and falls finer for the
    # empty-tier case. Warm/cool rows are aggregates (min/avg/max)
    # rather than a single `value`, so read both shapes below.
    # `auto` returns the first non-empty tier only, so a wide
    # window resolves to cool and the freshest minutes (still in
    # hot, not yet rolled into cool) would tear off the right edge
    # of the strip. Always union the hot tier back in - the by_ts
    # join below dedups, and hot/cool live on different ts grids so
    # they compose into a continuous strip (coarse in the old part,
    # fine at the live edge).
    hot_since = max(since, int(time.time()) - 3600)

    def _series(metric: str) -> list[dict]:
        rows = store.query(metric, since, "auto")
        if since < hot_since:
            rows = rows + store.query(metric, hot_since, "hot")
        return rows

    kind_rows = _series("internet.kind")
    lat_rows = _series("internet.latency_ms")
    loss_rows = _series("internet.loss_pct")

    def _val(row: dict[str, Any]) -> Optional[float]:
        # Hot rows carry `value`; warm/cool carry min/avg/max. The
        # avg is the right representative for latency/loss buckets,
        # and rounds back to the dominant kind code for the
        # categorical iface series (kind rarely flips inside a
        # single 5-min cool bucket).
        v = row.get("value")
        if v is None:
            v = row.get("avg")
        return None if v is None else float(v)

    # Join on timestamp. Daemon collector emits the metrics in
    # the same tick when ping succeeded, so the timestamp grid
    # aligns one-to-one; the dict approach below tolerates
    # missing latency / loss without dropping the kind sample.
    by_ts: dict[int, dict[str, Any]] = {}
    for row in kind_rows:
        ts = int(row.get("ts", 0))
        if ts <= 0 or ts > until:
            continue
        v = _val(row)
        if v is None:
            continue
        code = int(round(v))
        by_ts.setdefault(ts, {"ts": ts})["kind"] = _KIND_LABEL.get(code, "unknown")
    for row in lat_rows:
        ts = int(row.get("ts", 0))
        if ts <= 0 or ts > until:
            continue
        lat = _val(row)
        if lat is None:
            continue
        by_ts.setdefault(ts, {"ts": ts})["latency_ms"] = lat
    for row in loss_rows:
        ts = int(row.get("ts", 0))
        if ts <= 0 or ts > until:
            continue
        loss = _val(row)
        if loss is None:
            continue
        by_ts.setdefault(ts, {"ts": ts})["loss_pct"] = loss
    samples = sorted(by_ts.values(), key=lambda r: r["ts"])
    if len(samples) > limit:
        samples = samples[-limit:]
    return {"since": since, "until": until, "samples": samples}
