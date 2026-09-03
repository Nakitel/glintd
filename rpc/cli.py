"""CLI entry that ubus shells out to. Parses argv as
`<method> <json-args>` and prints the dispatched result as JSON.

Wired by /etc/glintd/glintd-rpc.sh, which is itself registered in
`/etc/init.d/glintd` via `procd_add_jail_mount` etc. - see
install.sh.

Usage:
    python3 -m glintd.rpc.cli ping '{}'
    python3 -m glintd.rpc.cli get_history '{"metric":"battery.pct","since":1730000000}'
    python3 -m glintd.rpc.cli stream_snapshots '{}'   # long-running

The streaming form is intended to be opened via SSH directly
(not through ubus, which is request/response). The Glint app's
CitadelTransport.streamLines pipes stdout back to the dashboard
in real time. Each emitted line is a complete JSON object;
clients split on '\\n'.
"""
from __future__ import annotations
import json
import os
import signal
import sys
import time
from glintd.rpc.server import dispatch
from glintd.rpc.context import open_store

# Cadence for `stream_snapshots`. Daemon polls its own store once
# per `STREAM_INTERVAL_S`, emits a JSON line when anything
# changed, and a heartbeat line every `STREAM_HEARTBEAT_S` even
# without change so the client can detect a stalled stream.
STREAM_INTERVAL_S = 1.0
STREAM_HEARTBEAT_S = 30.0


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: cli <method> [json-args]"}))
        sys.exit(1)
    method = sys.argv[1]
    args_raw = sys.argv[2] if len(sys.argv) >= 3 else "{}"
    try:
        args = json.loads(args_raw) if args_raw.strip() else {}
        if not isinstance(args, dict):
            args = {}
    except ValueError:
        print(json.dumps({"error": "args must be a JSON object"}))
        sys.exit(1)

    if method == "stream_snapshots":
        _run_stream(args)
        return

    result = dispatch(method, args)
    # No pretty-print - ubus relays the bytes verbatim, and the
    # app parses with `json.loads`. Compact saves a few bytes
    # over the SSH channel.
    print(json.dumps(result, separators=(",", ":")))


def _reap_stale_streams() -> None:
    """Kill any other `stream_snapshots` CLI processes before this
    one starts. Only one live snapshot stream per router is ever
    useful - the app keeps a single consumer. Stale ones pile up
    because an SSH exec channel isn't always torn down on the client
    side: dropbear keeps the abandoned reader's channel open, so the
    writer never sees BrokenPipe and lingers (observed 8 python procs
    at ~27 MB RSS each holding ~190 MB). Enforcing "one stream" here
    makes the leak self-correcting - each fresh stream the app opens
    reaps the orphaned ones. Best-effort: any /proc race just skips
    that pid."""
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if "glintd.rpc.cli" in cmd and "stream_snapshots" in cmd:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def _run_stream(args: dict) -> None:
    """Stream snapshot deltas as newline-separated JSON until
    stdout closes. Format per line:

        {"ts": <int>, "samples": {metric: {"ts": ..., "value": ...}}}

    On every poll tick, compare the current snapshot against the
    last one we emitted. If anything changed (any metric's ts
    moved forward), emit a fresh snapshot. If nothing changed but
    STREAM_HEARTBEAT_S has elapsed, emit {"heartbeat": <ts>} so
    the client knows the stream is still alive. Either way,
    stdout is flushed immediately - the SSH pipe is line-buffered
    by default on busybox.
    """
    _reap_stale_streams()
    store = open_store()
    last_signature: tuple = ()
    last_emit = 0.0
    metrics_filter: set | None = None
    if isinstance(args.get("metrics"), list):
        metrics_filter = {str(m) for m in args["metrics"] if m}

    try:
        while True:
            now = time.time()
            rows = store.latest_per_metric()
            if metrics_filter is not None:
                rows = {k: v for k, v in rows.items() if k in metrics_filter}
            signature = tuple(sorted((m, ts) for m, (ts, _v) in rows.items()))

            if signature != last_signature:
                payload = {
                    "ts": max((ts for _m, (ts, _v) in rows.items()), default=0),
                    "samples": {
                        m: {"ts": ts, "value": v}
                        for m, (ts, v) in rows.items()
                    },
                }
                sys.stdout.write(json.dumps(payload, separators=(",", ":")))
                sys.stdout.write("\n")
                sys.stdout.flush()
                last_signature = signature
                last_emit = now
            elif now - last_emit >= STREAM_HEARTBEAT_S:
                sys.stdout.write(json.dumps({"heartbeat": int(now)},
                                            separators=(",", ":")))
                sys.stdout.write("\n")
                sys.stdout.flush()
                last_emit = now

            time.sleep(STREAM_INTERVAL_S)
    except (BrokenPipeError, KeyboardInterrupt):
        # SSH channel closed (popover dismissed, app suspended,
        # connection dropped). Exit quietly - the supervising
        # caller is gone; nothing to clean up here.
        os._exit(0)


if __name__ == "__main__":
    main()
