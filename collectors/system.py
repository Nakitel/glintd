"""Always-available system metrics: load average, memory, CPU temp,
flash usage.

`/proc/loadavg` and `/proc/meminfo` are kernel-side, so they exist
on every router we'd ever deploy to. CPU temp varies - we use the
sysfs path the capability probe found at startup, falling back to
"don't sample" when the box doesn't expose any thermal zone (rare
but seen on some Wi-Fi-only models).
"""
from __future__ import annotations

import os

NAME = "system"
INTERVAL_SEC = 15

# Previous /proc/stat aggregate-cpu snapshot, (total_jiffies,
# idle_jiffies). CPU utilisation is a rate, so it needs two
# samples to delta against; we keep the last one here between
# collect() calls. None until the first tick has run.
_prev_cpu: tuple[int, int] | None = None


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

    # CPU utilisation - the REAL busy fraction from /proc/stat, i.e.
    # the share of time the cores were NOT idle since the last tick.
    # This is deliberately separate from load average: load counts
    # runnable+uninterruptible tasks and reads as 90 %+ on a box that
    # is only queue-contended (load 3.6 / 4 cores) while the CPU is
    # actually ~35 % busy. The app used to derive "CPU %" as
    # min(load1/ncpu, 1)*100, which is that misleading number; this
    # metric replaces it. First tick after start has no prior sample
    # to delta against, so it emits nothing (one 15 s gap, then live).
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
        # fields[0] == "cpu"; the rest are jiffies in the order
        # user nice system idle iowait irq softirq steal guest ...
        vals = [int(x) for x in fields[1:]]
        idle = vals[3]                 # idle jiffies (index 3)
        total = sum(vals)
        global _prev_cpu
        if _prev_cpu is not None:
            d_total = total - _prev_cpu[0]
            d_idle = idle - _prev_cpu[1]
            if d_total > 0:
                busy = (d_total - d_idle) / d_total * 100.0
                out["sys.cpu_pct"] = max(0.0, min(100.0, busy))
        _prev_cpu = (total, idle)
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

    # Flash / root-overlay usage. Mirrors the app's live `df -kP /`
    # reading (used / total %). Stored as history so the Flash
    # strip in the router-detail card backfills after an app
    # restart instead of showing an overnight gap - the app used
    # to populate it only from live ticks, so anything older than
    # the current session was a blank strip. statvfs is a single
    # cheap syscall; safe at the 15 s collector cadence. `used`
    # excludes free blocks the same way `df` reports it.
    try:
        st = os.statvfs("/")
        if st.f_blocks > 0:
            used = st.f_blocks - st.f_bfree
            out["sys.flash_pct"] = used / st.f_blocks * 100.0
    except OSError:
        pass

    return out
