"""Always-available system metrics: load average, memory, CPU temp.

`/proc/loadavg` and `/proc/meminfo` are kernel-side, so they exist
on every router we'd ever deploy to. CPU temp varies - we use the
sysfs path the capability probe found at startup, falling back to
"don't sample" when the box doesn't expose any thermal zone (rare
but seen on some Wi-Fi-only models).
"""
from __future__ import annotations

NAME = "system"
INTERVAL_SEC = 15


def requires(caps) -> bool:
    return caps.has_loadavg  # always true, kept for symmetry


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}

    # Load - first three whitespace-separated values are 1/5/15 min.
    try:
        with open("/proc/loadavg") as f:
            parts = f.readline().split()
        out["sys.load1"]  = float(parts[0])
        out["sys.load5"]  = float(parts[1])
        out["sys.load15"] = float(parts[2])
    except (OSError, ValueError, IndexError):
        pass

    # Memory - /proc/meminfo is a key:value block. We only need
    # MemTotal, MemAvailable, and Buffers/Cached so we can show a
    # fraction-used graph without misleading the user (free RAM
    # alone overstates pressure).
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                tail = rest.strip().split()
                if tail and tail[0].isdigit():
                    mem[k] = int(tail[0])  # value is in kB
        if "MemTotal" in mem and "MemAvailable" in mem:
            used = mem["MemTotal"] - mem["MemAvailable"]
            out["sys.mem_pct"] = used / mem["MemTotal"] * 100.0
    except (OSError, ValueError):
        pass

    # CPU temperature - sysfs reports milli-degrees Celsius.
    if caps.cpu_temp_path:
        try:
            with open(caps.cpu_temp_path) as f:
                raw = int(f.read().strip())
            out["sys.cpu_temp_c"] = raw / 1000.0
        except (OSError, ValueError):
            pass

    return out
