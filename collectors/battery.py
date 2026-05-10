"""Battery telemetry. Skipped entirely on Wi-Fi-only routers.

Two sources, in order of preference:
    1. `ubus call mcu status` - what the GL.iNet web UI uses; gives
       capacity %, voltage, current, temp, charging flag in one
       call. Available on Mudi 7 and Spitz AX.
    2. /sys/class/power_supply/<x>-bat/* - direct from the fuel-gauge
       chip. Universal Linux fallback; the cw221X driver on Mudi
       reports current_now noisily so we prefer the mcu path.

The capability probe records *which* sysfs directory and *whether*
mcu is present; here we just consume whichever was found.
"""
from __future__ import annotations
import json
import subprocess
import os

NAME = "battery"
INTERVAL_SEC = 15


def requires(caps) -> bool:
    return caps.has_battery or caps.has_mcu


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}

    # Path 1: mcu status. Mirrors what the app's BatteryProbe prefers.
    if caps.has_mcu:
        try:
            r = subprocess.run(
                ["ubus", "call", "mcu", "status"],
                capture_output=True, text=True, timeout=2.0)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    if "charge_percent" in data:
                        out["battery.pct"] = float(data["charge_percent"])
                    # GL.iNet's mcu reports voltage in mV, current in mA.
                    if "voltage" in data:
                        out["battery.voltage_v"] = float(data["voltage"]) / 1000.0
                    if "current" in data:
                        out["battery.current_a"] = float(data["current"]) / 1000.0
                    # `temperature` is sometimes a string ("37.3") on
                    # Mudi 7's mcu - a float coercion handles both.
                    if "temperature" in data:
                        try:
                            t = float(data["temperature"])
                            # Heuristic: mcu often reports tenths
                            # of °C as int (e.g. 373) when the
                            # value is integer-typed; reports
                            # actual °C when string-typed
                            # ("37.3"). Threshold @ 100 covers
                            # the realistic battery range cleanly.
                            out["battery.temp_c"] = t / 10.0 if t > 100 else t
                        except (TypeError, ValueError):
                            pass
                    # GL.iNet field varies by firmware: older
                    # builds expose `charging` (bool); current
                    # 4.x exposes `charging_status` (int) plus
                    # `fastcharge` (bool). Either-or both → on.
                    charging = None
                    if "charging" in data:
                        charging = bool(data["charging"])
                    elif "charging_status" in data:
                        try:
                            charging = int(data["charging_status"]) > 0
                        except (TypeError, ValueError):
                            pass
                    if data.get("fastcharge"):
                        charging = True
                    if charging is not None:
                        out["battery.charging"] = 1.0 if charging else 0.0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    # Path 2: sysfs fallback. Only fills in fields the mcu path
    # didn't already answer - non-clobbering merge.
    if caps.has_battery and caps.battery_sysfs_path:
        sysfs = caps.battery_sysfs_path
        if "battery.pct" not in out:
            v = _read_int(os.path.join(sysfs, "capacity"))
            if v is not None:
                out["battery.pct"] = float(v)
        if "battery.voltage_v" not in out:
            v = _read_int(os.path.join(sysfs, "voltage_now"))  # µV
            if v is not None:
                out["battery.voltage_v"] = v / 1_000_000.0
        if "battery.current_a" not in out:
            v = _read_int(os.path.join(sysfs, "current_now"))  # µA
            if v is not None:
                out["battery.current_a"] = v / 1_000_000.0
        if "battery.temp_c" not in out:
            v = _read_int(os.path.join(sysfs, "temp"))  # tenths of °C
            if v is not None:
                out["battery.temp_c"] = v / 10.0
        if "battery.charging" not in out:
            try:
                with open(os.path.join(sysfs, "status")) as f:
                    status = f.read().strip().lower()
                out["battery.charging"] = 1.0 if status == "charging" else 0.0
            except OSError:
                pass

    return out


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None
