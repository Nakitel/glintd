# glintd

Optional router-side daemon for [Glint](https://glint.nakitel.com), a
macOS / iOS app that monitors GL.iNet OpenWrt routers.

Glint works without it. Installing `glintd` adds:

- **24 h history.** The daemon keeps a tiered SQLite ring buffer
  (15 s / 1 m / 5 m) so the app backfills graphs after gaps.
- **Push notifications + Live Activity updates** via Glint's APNs
  relay - WAN drop, on-battery, SIM switch, low battery.

## Supported hardware

`glintd` is device-agnostic. It probes capabilities at startup
(see `capabilities.py`) and only collects metrics whose backing
data sources exist. Verified on GL-E5800 Mudi 7, GL-XE3000 Puli AX,
GL-BE3600 Slate 7, GL-BE9300 / BE6500 Flint 3 / 3e, GL-MT6000 Flint 2,
GL-AXT1800 Slate AX (OpenWrt 23.05). Adding a model = one row in
`capabilities.py`.

## Manual install

You'll need SSH access to the router as `root` (default on GL.iNet).

```sh
# On your machine:
git clone https://github.com/Nakitel/glintd.git
scp -r glintd root@<router>:/tmp/glintd-bundle

# On the router:
ssh root@<router> 'sh /tmp/glintd-bundle/install.sh'
```

`install.sh` is idempotent. It will:

- `opkg install` any missing deps (`python3-light`, `python3-sqlite3`,
  `python3-urllib`, `python3-openssl`, `python3-codecs`)
- copy sources to `/etc/glintd/`
- write `/etc/init.d/glintd` (procd) + ubus shim at
  `/usr/libexec/rpcd/mudi.glintd` and ACL at
  `/usr/share/rpcd/acl.d/glintd.json`
- enable + start the service
- verify `ubus call mudi.glintd ping` answers within 3 s

Re-running it upgrades in place.

## Layout

```
daemon.py         procd-supervised main loop
capabilities.py   runtime capability detection
collectors/       one file per metric class
storage/          SQLite tiered ring buffer (hot/warm/cool)
rpc/              ubus method handlers
apns/             push relay client
install.sh        installer / upgrader
```

## RPC

All over `ubus` namespace `mudi.glintd` (local socket, SSH-gated):

| Method                    | Args                          | Returns |
|---------------------------|-------------------------------|---------|
| `ping`                    | -                             | `{version, uptime, capabilities}` |
| `list_metrics`            | -                             | `[metric_name...]` |
| `get_history`             | `metric, since, tier?`        | rows |
| `register_device_token`   | `platform, token, bundle_id`  | `{router_id}` |
| `unregister_device_token` | `token`                       | `{ok}` |
| `set_push_threshold`      | `event, level`                | `{ok}` |

## Uninstall

```sh
[ -x /etc/init.d/glintd ] && /etc/init.d/glintd stop    2>/dev/null
[ -x /etc/init.d/glintd ] && /etc/init.d/glintd disable 2>/dev/null
rm -rf /etc/glintd
rm -f  /etc/init.d/glintd
rm -f  /usr/libexec/rpcd/mudi.glintd
rm -f  /usr/share/rpcd/acl.d/glintd.json
[ -f /etc/crontabs/root ] && sed -i '/glintd\/self-update\.sh/d' /etc/crontabs/root
/etc/init.d/rpcd reload  2>/dev/null
/etc/init.d/cron  reload 2>/dev/null || /etc/init.d/cron restart 2>/dev/null
rm -f  /tmp/glintd.db /tmp/glintd-install.log
rm -rf /tmp/glintd-bundle /tmp/glintd-self-update.*
```

Installed opkg packages are left in place - other things may depend
on them.

## License

MIT - see [LICENSE](./LICENSE).
