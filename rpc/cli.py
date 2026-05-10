"""CLI entry that ubus shells out to. Parses argv as
`<method> <json-args>` and prints the dispatched result as JSON.

Wired by /etc/glintd/glintd-rpc.sh, which is itself registered in
`/etc/init.d/glintd` via `procd_add_jail_mount` etc. - see
install.sh.

Usage:
    python3 -m glintd.rpc.cli ping '{}'
    python3 -m glintd.rpc.cli get_history '{"metric":"battery.pct","since":1730000000}'
"""
from __future__ import annotations
import json
import sys
from glintd.rpc.server import dispatch


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
    result = dispatch(method, args)
    # No pretty-print - ubus relays the bytes verbatim, and the
    # app parses with `json.loads`. Compact saves a few bytes
    # over the SSH channel.
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
