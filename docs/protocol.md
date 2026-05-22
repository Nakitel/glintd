# glintd ↔ Glint app protocol

Stable wire contract between the Glint companion app (iOS / iPadOS
/ macOS) and the glintd daemon running on a GL.iNet router.
Versioning is per-feature, not per-daemon-version: the app
discovers capabilities at connect time and gates UI/code paths on
the feature map returned by `ping`.

This document is the source of truth. If something in the daemon
or app contradicts what's written here, the doc wins and the
code is the bug.

## Wire format

All RPC calls go over the same SSH transport the rest of Glint
uses, dispatched through `ubus call mudi.glintd <method> [args]`.
Replies are single JSON objects on stdout. Errors are surfaced as
`{"error": "<message>"}` rather than raised exceptions, so the
app can render them inline instead of treating the call as a
crash.

The same methods are reachable on the router CLI as
`glintd-rpc <method> [args-json]` for debugging.

## Compatibility rules

These are non-negotiable. Breaking any of them ships a daemon
update that bricks older app versions, which is a road we don't
go down.

1. **Additive only.** Once a key, method, or feature flag is in
   a release, it stays. New fields land alongside; existing
   semantics never change.
2. **Absent = unsupported.** The app must treat any missing key
   or `false` feature flag exactly like the feature isn't there.
   No "throw if X missing." Cold-start of a v2 client against a
   v1 daemon is the default test case for every new feature.
3. **Type-stable.** A field that's `int` today is an `int`
   forever. Need a wider range? Add a new field, deprecate the
   old one in docs (not code).
4. **Lower_snake_case** for every JSON key, every feature-flag
   name, every metric name. We've been bitten by camelCase
   creeping in from Swift side before.

## `ping` method

Health probe. Cheap (~200 ms). The app fires it on every connect
and uses the reply to populate the daemon card in Settings, gate
v2 features, and route push registration.

### Args

None. Pass `{}` or no args at all.

### Reply (v0.6.0+)

```json
{
  "ok": true,
  "version": "0.6.0",
  "uptime_s": 12345,
  "router_id": "stable-uuid-string",
  "capabilities": {
    "has_battery": true,
    "battery_sysfs_path": "/sys/class/power_supply/cw221X-bat",
    "has_mcu": true,
    "has_modem": true,
    "sim_slots": 2,
    "wifi_radios": [{"name": "phy#0", "band": "2.4"},
                    {"name": "phy#1", "band": "5"}],
    "ethernet_ports": [{"name": "eth0", "role_hint": "wan"}],
    "kmwan_managed": true,
    "has_wireguard": true,
    "has_openvpn": true,
    "has_tailscale": false,
    "has_zerotier": false,
    "cpu_temp_path": "/sys/class/thermal/thermal_zone0/temp",
    "has_loadavg": true,
    "firmware_version": "4.7.6",
    "openwrt_release": "OpenWrt 23.05",
    "model": "GL-MT3000"
  },
  "features": {
    "capabilities_v2":       true,
    "snapshot_polling":      false,
    "snapshot_stream":       false,
    "history_internet":      false,
    "ping_loss":             false,
    "throughput_saturation": false
  }
}
```

Field semantics:

| Field | Type | Meaning |
|---|---|---|
| `ok` | bool | Always `true` on a healthy reply. Absent / false → treat as no daemon. |
| `version` | string | Daemon SemVer-ish version. Lexicographic compare on dot-separated integers. |
| `uptime_s` | int | Seconds since the daemon process started (not router uptime). |
| `router_id` | string | Stable UUID written at install time. Empty when missing. |
| `capabilities` | object | **Hardware** detection - what's physically on this router. |
| `features` | object | **Protocol** flags - which v2 endpoints this daemon implements. |

### Pre-0.6.0 daemons

`features` is omitted. The app parses absent map = empty
dict, which makes every `daemonSupports(...)` query return false
and routes to the legacy code path. No client work needed to
support old daemons - the silence-degrade is the whole point.

## Feature flags

The `features` map in `ping`'s reply is the contract for what the
daemon promises to honour. The Swift side mirrors this in
`LiveRefresher.DaemonFeature` (rawValue must match key spelling).

| Key | Since | Endpoints it gates | Status |
|---|---|---|---|
| `capabilities_v2` | 0.6.0 | This feature map itself - presence-as-detector. | **stable** |
| `snapshot_polling` | 0.6.0 | `get_snapshot` returns latest sample of every metric in one round-trip. | **stable** |
| `snapshot_stream` | 0.6.0 | `stream_snapshots` CLI - newline-delimited JSON push over SSH. | **stable** |
| `history_internet` | 0.6.0 | `get_internet_history` returns iface kind + latency timeline. | **stable** |
| `ping_loss` | 0.9.0 (planned) | Daemon-side packet-loss collector; loss field added to ping samples. | reserved |
| `throughput_saturation` | 0.6.0 | Client-side saturation banner using daemon's RX/TX + ping data. Daemon flag is informational. | **stable** |

"Reserved" keys may exist in the `features` map but always return
`false` until the corresponding stage lands. They are listed in
advance so clients can render forward-looking "your daemon is
missing X; update for Y" hints without parsing version strings.

## `stream_snapshots` (long-running, not via ubus)

Opened directly via SSH `exec`, not through the ubus dispatcher
(ubus is request/response and would buffer the stream). The
companion app calls it with:

```
/etc/glintd/glintd-rpc.sh stream_snapshots '{}'
```

(direct `python3 -m glintd.rpc.cli` works only when PYTHONPATH
includes `/etc`. The wrapper script sets that up.)

Output is newline-delimited JSON. Each line is one of:

- **Snapshot delta** — emitted whenever any metric's latest ts
  moves forward. Same shape as `get_snapshot`'s reply, with
  the full set of metrics' latest values:
  ```json
  {"ts": 1730000000,
   "samples": {"battery.pct": {"ts": 1730000000, "value": 92.0}, ...}}
  ```
- **Heartbeat** — emitted every 30 s when no metric changed,
  so the client can detect a dead stream vs an idle router:
  ```json
  {"heartbeat": 1730000030}
  ```

Optional arg `{"metrics": [...]}` filters by metric name (same
semantics as `get_snapshot`).

The stream runs until stdout closes. The app's CitadelTransport
maps the SSH channel close to a SIGHUP on the remote process, so
cancelling the consumer task on the iOS side cleanly terminates
the daemon-side loop within one tick (1 s).

Gated by the `snapshot_stream` feature flag. Older daemons that
don't ship `stream_snapshots` reject the exec with "method not
found"; the app falls back to `get_snapshot` polling.

## Existing methods (pre-0.6.0, still supported)

These predate the feature-map design and are always available on
any installed glintd. Listed for completeness; the contract is
unchanged.

| Method | Purpose |
|---|---|
| `get_history` | Bucketed time-series for one metric. |
| `list_metrics` | Names of every metric the daemon collects. |
| `register_device_token` | APNs / Live Activity token registration. |
| `unregister_device_token` | Mirror of register, for sign-out. |
| `set_push_preferences` | Per-event opt-in/out for push. |
| `get_router_credentials` | One-shot HMAC handshake material. |
| `test_push` | Fire a test notification through the relay. |

## Versioning policy

- **Patch** (`0.6.0 → 0.6.1`): bug fixes that don't touch the
  feature map or any reply shape.
- **Minor** (`0.6.0 → 0.7.0`): a feature flag flips from
  `false` to `true`, or a new flag is added.
- **Major** (`0.x → 1.0`): contract stabilises. From 1.0 onwards,
  the rules above are load-bearing for App Store stability and
  no flag can flip back to false in a non-major release.

We are at 0.6.0. The 1.0.0 cutover happens when snapshot streaming
+ history backfill + packet loss are all shipped and live in
production for at least one full release cycle without rollback.
