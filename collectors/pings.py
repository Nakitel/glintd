"""Multi-host ping. Always available - `ping` is in busybox on
every OpenWrt build. Targets come from /etc/glintd/config.json (or
defaults to 1.1.1.1 / 8.8.8.8).

Daemon-side pinging means the app gets a continuous "router has
internet" series even when no client is connected. This is what
powers the all-pings-down push notification: if every host in
the list has been unreachable for ≥ N consecutive samples, fire
"WAN dropped" once.

Ping timing matches the app's PingProbe (`-c 2 -W 1`) - 2 packets
so a single-packet drop doesn't false-alarm, and emits the avg
RTT. Failure → series records nothing for that tick (a gap),
which is a meaningful signal in its own right.
"""
from __future__ import annotations
import json
import os
import re
import subprocess

NAME = "pings"
INTERVAL_SEC = 30  # router-side pings are extra; 2× the system rate


def requires(caps) -> bool:
    return True


_RTT_RE = re.compile(r"min/avg/max[^=]*=\s*[\d.]+/([\d.]+)/")


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}
    for host in _hosts():
        try:
            r = subprocess.run(
                ["ping", "-c", "2", "-W", "1", "-q", host],
                capture_output=True, text=True, timeout=4.0)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode != 0:
            # Don't write a sentinel "failed" value - a gap in the
            # series is the failure signal. The downsampler will
            # see the gap when it rolls and the threshold engine
            # reads it as an unreachable tick.
            continue
        m = _RTT_RE.search(r.stdout)
        if not m:
            continue
        try:
            avg = float(m.group(1))
        except ValueError:
            continue
        # Series key sanitises the host into a SQL-safe token -
        # stripping dots/colons keeps the metric name readable
        # while still uniquely identifying the host.
        key = re.sub(r"[^a-zA-Z0-9]", "_", host)
        out[f"pings.{key}.rtt_ms"] = avg
    return out


def _hosts() -> list[str]:
    """Read host list from /etc/glintd/config.json. Falls back to
    the same defaults the app uses so a fresh install still
    produces useful data without configuration."""
    path = "/etc/glintd/config.json"
    try:
        with open(path) as f:
            cfg = json.load(f)
        hosts = cfg.get("ping_hosts", [])
        if isinstance(hosts, list) and hosts:
            return [str(h) for h in hosts]
    except (OSError, ValueError):
        pass
    return ["1.1.1.1", "8.8.8.8"]
