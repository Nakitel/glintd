"""Runtime capability detection.

Probes the router once at daemon startup and produces a stable
description of *which metrics this hardware can actually report*.
The collectors then ask whether their backing source exists and
silently drop themselves if not - same shape as the app's
`CapabilityProbe`, but server-side and reused for storage schema
decisions.

The design rule: every probe here is a positive existence test
("is this file readable?", "does this ubus method respond?"). We
never key off model strings or firmware versions. A future
GL.iNet box with a battery we've never seen will Just Work.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict


def _run(cmd: list[str], timeout: float = 1.5) -> tuple[int, str]:
    """Best-effort subprocess call. Returns (returncode, stdout).

    Daemon startup mustn't crash because some side probe blew up,
    so every helper here swallows OSError / TimeoutExpired and
    returns an empty result instead of propagating.
    """
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout, check=False)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _ubus_supports(method: str) -> bool:
    """True when `ubus list <method>` finds a registered handler.

    Cheaper than calling the method itself - `ubus list` is local
    and doesn't dispatch to the underlying daemon. We use it as a
    pre-check before any get-data ubus call.
    """
    rc, out = _run(["ubus", "list", method])
    return rc == 0 and method in out


def _read_first_line(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.readline().strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class EthernetPort:
    """One physical (or bridge-member) ethernet interface that can
    carry user traffic. Distinguishing WAN vs LAN happens in the
    interfaces collector at sample time - we just need to know
    *what to sample*."""
    name: str
    role_hint: str  # "wan" / "lan" / "unknown" - first guess from name


@dataclass(frozen=True)
class WifiRadio:
    """One enabled radio (one row per phyN). The name is what
    `iwinfo` / `iw dev` use; the band is informational only."""
    name: str
    band: str  # "2.4" / "5" / "6" / "unknown"


@dataclass
class Capabilities:
    # - power -
    has_battery: bool = False
    battery_sysfs_path: str = ""  # /sys/class/power_supply/<x>-bat
    has_mcu: bool = False         # `ubus call mcu status` works

    # - cellular -
    has_modem: bool = False       # `gl_modem` binary present
    sim_slots: int = 0            # 0 / 1 / 2

    # - wi-fi -
    wifi_radios: list[WifiRadio] = field(default_factory=list)

    # - wired -
    ethernet_ports: list[EthernetPort] = field(default_factory=list)

    # - routing / failover -
    kmwan_managed: bool = False   # multi-WAN config exists

    # - vpn / tunnels -
    has_wireguard: bool = False
    has_openvpn: bool = False
    has_tailscale: bool = False
    has_zerotier: bool = False

    # - system -
    cpu_temp_path: str = ""       # /sys/class/thermal/.../temp
    has_loadavg: bool = True      # /proc/loadavg - universal on Linux

    # - diagnostics, never gates a collector -
    firmware_version: str = ""    # /etc/glversion (GL.iNet)
    openwrt_release: str = ""     # PRETTY_NAME from /etc/openwrt_release
    model: str = ""               # /tmp/sysinfo/model or `uname -n`

    def to_json(self) -> str:
        d = asdict(self)
        # dataclass list-of-dataclass already serialises. Stable
        # key order so the SQLite schema-version hash is
        # reproducible (we use it later to decide when to ALTER).
        return json.dumps(d, sort_keys=True, separators=(",", ":"))


def detect() -> Capabilities:
    caps = Capabilities()

    # - battery -
    # Walk /sys/class/power_supply/ and pick the first directory
    # whose name contains "bat" and has a CAPACITY readout. The
    # cw221X chip on the Mudi enumerates as `cw221X-bat`, GL.iNet
    # refers to it as `battery` on some boxes - names vary, the
    # presence of the capacity file does not.
    psu_root = "/sys/class/power_supply"
    if os.path.isdir(psu_root):
        for entry in sorted(os.listdir(psu_root)):
            if "bat" not in entry.lower():
                continue
            cap_file = os.path.join(psu_root, entry, "capacity")
            if os.path.isfile(cap_file):
                caps.has_battery = True
                caps.battery_sysfs_path = os.path.join(psu_root, entry)
                break
    # GL.iNet's mcu daemon exposes battery+power info on some
    # models *without* a sysfs power_supply entry; we use it as
    # a secondary path in the battery collector.
    caps.has_mcu = _ubus_supports("mcu")

    # - cellular -
    # `gl_modem` ships on most GL.iNet boards regardless of whether
    # the unit actually has a cellular module - e.g. GL-BE3600
    # (Marble) has the binary in /usr/bin but no modem hardware,
    # and `ubus call cellular.network info` returns "Not found".
    # Treat a working `cellular.network` ubus method as the real
    # signal; `gl_modem` alone isn't enough.
    if shutil.which("gl_modem") and _ubus_supports("cellular.network"):
        # Slot count via `cellular.network info` (no args returns
        # both slots' state). GL.iNet's response shape on stable
        # 4.x firmwares is `{"networks":[{"slot":"1",...},
        # {"slot":"2",...}]}` - earlier ubus payload variants used
        # `{"sim1":{...},"sim2":{...}}` keys directly. We accept
        # either by counting whichever shape we see.
        rc, out = _run(["ubus", "call", "cellular.network", "info", "{}"], 2.0)
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
            except (ValueError, AttributeError):
                data = None
            if isinstance(data, dict):
                if isinstance(data.get("networks"), list):
                    caps.sim_slots = sum(
                        1 for n in data["networks"]
                        if isinstance(n, dict) and n.get("slot")
                    )
                else:
                    caps.sim_slots = sum(
                        1 for k in data
                        if k.startswith("sim")
                        and isinstance(data[k], dict)
                    )
            # Only mark has_modem true when ubus actually answered.
            caps.has_modem = True

    # - Wi-Fi radios -
    # `iw dev` prints `phyN\n\tInterface wlanX` blocks. We count
    # phyN entries that have at least one Interface child. Band
    # comes from `iwinfo <iface> info`.
    rc, out = _run(["iw", "dev"], 1.0)
    if rc == 0:
        radios: list[WifiRadio] = []
        current_phy = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("phy#"):
                # Keep the verbatim "phy#0" / "phy#1" name `iw` uses.
                current_phy = line
            elif line.startswith("Interface ") and current_phy is not None:
                iface = line.split()[1]
                band = _wifi_band_for(iface)
                radios.append(WifiRadio(name=current_phy, band=band))
                current_phy = None  # one iface per radio is enough
        caps.wifi_radios = radios

    # - Ethernet ports -
    # `ip -o link show` is reliable across all OpenWrt versions.
    # Filter to lines whose interface name starts with eth/wan/lan
    # AND whose link-type is `link/ether` (drops bridges, vlans).
    rc, out = _run(["ip", "-o", "link", "show"], 1.0)
    if rc == 0:
        ports: list[EthernetPort] = []
        for line in out.splitlines():
            # Format: "N: <name>: <flags> ... link/ether <mac> ..."
            parts = line.split()
            if len(parts) < 2 or "link/ether" not in line:
                continue
            name = parts[1].rstrip(":").split("@")[0]
            if not (name.startswith("eth") or
                    name.startswith("wan") or
                    name.startswith("lan")):
                continue
            # role_hint is best-effort: the interfaces collector
            # disambiguates at sample time using uci network config.
            if "wan" in name:
                role = "wan"
            elif "lan" in name:
                role = "lan"
            else:
                role = "unknown"
            ports.append(EthernetPort(name=name, role_hint=role))
        caps.ethernet_ports = ports

    # - kmwan multi-wan -
    rc, out = _run(["uci", "show", "kmwan"], 1.0)
    caps.kmwan_managed = (rc == 0 and "kmwan" in out)

    # - VPN tooling presence -
    caps.has_wireguard = bool(shutil.which("wg"))
    caps.has_openvpn = bool(shutil.which("openvpn"))
    caps.has_tailscale = bool(shutil.which("tailscale"))
    caps.has_zerotier = bool(shutil.which("zerotier-cli"))

    # - CPU temperature -
    # Pick the first thermal_zone with a `temp` file we can read.
    # Type names vary (cpu-thermal / soc-thermal / SOC-THERM-tsens
    # on Mudi); the value is what we actually care about.
    thermal_root = "/sys/class/thermal"
    if os.path.isdir(thermal_root):
        for entry in sorted(os.listdir(thermal_root)):
            if not entry.startswith("thermal_zone"):
                continue
            temp_file = os.path.join(thermal_root, entry, "temp")
            if os.path.isfile(temp_file):
                caps.cpu_temp_path = temp_file
                break

    # - diagnostics -
    caps.firmware_version = _read_first_line("/etc/glversion")
    rc, out = _run(["sh", "-c",
                    ". /etc/openwrt_release && echo $DISTRIB_DESCRIPTION"], 0.5)
    if rc == 0:
        caps.openwrt_release = out.strip()
    caps.model = (_read_first_line("/tmp/sysinfo/model") or
                  _read_first_line("/proc/sys/kernel/hostname"))

    return caps


def _wifi_band_for(iface: str) -> str:
    """Best-effort band classification from `iwinfo <iface> info`.

    Returns "2.4" / "5" / "6" / "unknown". Frequencies cited are
    the lower edge of each band; `iwinfo` reports MHz integers so
    a coarse range comparison is enough.
    """
    rc, out = _run(["iwinfo", iface, "info"], 1.0)
    if rc != 0:
        return "unknown"
    # Look for "Channel: X (...)" line; channel ranges are stable.
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Channel:"):
            tail = line.split(":", 1)[1].strip()
            # "Channel: 36 (5180 MHz)" → grab the MHz number
            if "MHz" in tail:
                try:
                    mhz = int(tail.split("(")[1].split()[0])
                    if mhz < 3000:
                        return "2.4"
                    elif mhz < 5945:
                        return "5"
                    else:
                        return "6"
                except (IndexError, ValueError):
                    pass
    return "unknown"


if __name__ == "__main__":
    # `python3 capabilities.py` for ad-hoc inspection on a router.
    caps = detect()
    print(caps.to_json())
