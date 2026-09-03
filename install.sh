#!/bin/sh
# glintd installer. Universal across GL.iNet OpenWrt routers -
# detects what it needs at runtime, doesn't hardcode model strings.
#
# Usage:
#     curl -fsSL https://glint.nakitel.com/glintd/install.sh | sh -
# or, from the source tree, copy this file + the rest of glintd/
# onto the router and:
#     sh /tmp/glintd-bundle/install.sh
#
# Runs as root (which is the default user on GL.iNet). Idempotent -
# re-running it upgrades in place.

set -eu

GLINTD_HOME="/etc/glintd"
INIT_SCRIPT="/etc/init.d/glintd"
RPC_REG="/etc/glintd/ubus-register.sh"
LOG="/tmp/glintd-install.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2; }
die() { log "ERROR: $*"; exit 1; }

# procd's own liveness check is the authority when the init
# script exposes it (`running` comes from procd.sh, not every
# vendor build keeps it), so fall back to looking for the
# process. Guessing "not running" is the safe direction: a
# redundant `start` on a live procd service is a no-op.
rpcd_running() {
    /etc/init.d/rpcd running >/dev/null 2>&1 && return 0
    pgrep rpcd >/dev/null 2>&1
}

log "glintd installer starting"

# 1. Sanity - we need python3 and sqlite3. opkg ships -light
#    builds on every recent GL.iNet release; skip install only
#    if they're already there.
#
# `python3-urllib` is non-obvious: GL.iNet 4.x firmware (BE3600 /
# Slate 7, model id be9300) ships `python3-light` packaged WITHOUT
# the urllib stdlib, so even `from pathlib import Path` blows up
# at import-time with `ModuleNotFoundError: No module named
# 'urllib'` (pathlib does an internal `from urllib.parse import
# quote_from_bytes`). The relay client uses `urllib.request`
# directly anyway, so we'd hit it on the second tick at the
# latest. Older firmwares (Mudi 7 / E5800) bundle urllib inside
# python3-base and the package install no-ops there - opkg with
# `--force-reinstall=no`-equivalent default just skips an
# already-satisfied dep, so listing it unconditionally is safe.
need_pkg=""
command -v python3 >/dev/null 2>&1 || need_pkg="$need_pkg python3-light"
[ -e /usr/lib/python3*/sqlite3/__init__.py ] 2>/dev/null \
    || need_pkg="$need_pkg python3-sqlite3"
# GL.iNet 4.x firmware doesn't include `urllib`, the SSL
# extension, or the codecs registry in `python3-light`. We add
# all three unconditionally - opkg silently no-ops on already-
# installed packages, so older firmwares (Mudi 7) where
# `python3-base` already bundles them don't pay anything.
# Symptoms when missing:
#   urllib  → `from pathlib import Path` raises
#             `ModuleNotFoundError: urllib` at daemon import.
#   ssl     → `import ssl` raises `ModuleNotFoundError: _ssl`,
#             which `apns/relay_client.py` hits on every push.
#   codecs  → urllib's hostname encoding for the relay's domain
#             raises `LookupError: unknown encoding: idna` on
#             every push tick (silently dropping ALL events).
#             Observed on GL-BE3600 / Slate 7 stock firmware.
need_pkg="$need_pkg python3-urllib python3-openssl python3-codecs"
command -v ubus >/dev/null 2>&1 || die "ubus not found - not an OpenWrt box?"

if [ -n "$need_pkg" ]; then
    log "installing dependencies: $need_pkg"
    # `opkg update` writes the package index to /var/opkg-lists.
    # Older travel routers (Mudi V2) sometimes can't reach
    # downloads.openwrt.org - bad DNS upstream, IPv6 black-hole,
    # repo URL no longer reachable. We don't want a flaky
    # update to block the install when a previous run already
    # cached a usable index. Try the update; fall through to
    # the install regardless and only `die` if BOTH fail to
    # produce a working package set.
    if opkg update >>"$LOG" 2>&1; then
        log "opkg update succeeded"
    else
        log "opkg update failed - trying install with cached package list"
    fi
    # shellcheck disable=SC2086
    if ! opkg install $need_pkg >>"$LOG" 2>&1; then
        log "opkg install $need_pkg failed - see $LOG"
        die "couldn't install dependencies ($need_pkg). Run \`opkg update && opkg install $need_pkg\` manually on the router after fixing its internet/DNS."
    fi
fi

# Best-effort: install `coreutils-base64` if the firmware's busybox
# doesn't ship a `base64` applet. The daemon itself doesn't need
# this - it's purely a speed-up for the Glint iOS/Mac companion's
# SMS-polling path, which SSHes in and base64-encodes every file
# under /etc/spool/sms/{incoming,storage}/. On busybox without
# coreutils-base64, the fallback is `openssl base64` - one fork
# per SMS file, ~150 ms each on ARM. A Mudi 7 with 66 SMS burned
# ~10 s of router CPU per scan (load avg climbed past 4 before
# the read-side throttle on the companion side capped the cadence).
# With this package present, `base64` is a single ~1 ms fork.
#
# Best-effort because:
#   - Functionality is unaffected if it doesn't install - the
#     companion just runs slower on first scan, then the 30 s
#     throttle on the client side keeps things from snowballing.
#   - Some downstream-only firmware variants may not have it in
#     their feed; we don't want one stale opkg URL to abort the
#     whole install on an otherwise-working router.
if ! command -v base64 >/dev/null 2>&1; then
    log "installing coreutils-base64 (best-effort, speeds up Glint SMS scan)"
    opkg install coreutils-base64 >>"$LOG" 2>&1 \
        || log "coreutils-base64 install failed - companion SMS scan will use the slower openssl fallback (still functional)"
fi

# 2. Lay out the source tree under /etc/glintd. Source location
#    depends on caller - `curl | sh` mode pulls the tarball from
#    glint.nakitel.com first; manual mode finds the files we
#    already scp'd into /tmp/glintd-bundle/ or alongside this
#    script.
SRC=""
# Probe each candidate for the FULL set of top-level files we need
# to copy. Listing every required file here keeps the cache check
# honest: any missing piece forces a fresh tarball fetch instead
# of silently picking up a half-stale `/tmp/glintd-bundle/` from
# a previous install whose layout doesn't match the current one.
for cand in /tmp/glintd-bundle /tmp/glintd "$(dirname "$0")"; do
    if [ -f "$cand/daemon.py" ] \
       && [ -f "$cand/capabilities.py" ] \
       && [ -f "$cand/version.py" ]; then
        SRC="$cand"
        break
    fi
done
if [ -z "$SRC" ]; then
    log "fetching glintd.tar.gz from glint.nakitel.com"
    mkdir -p /tmp/glintd-bundle
    TARBALL=/tmp/glintd-bundle/glintd.tar.gz
    SIGFILE=/tmp/glintd-bundle/glintd.tar.gz.sig
    # Resolve current version first so we can suffix the tarball
    # URL with `?v=<version>`. Cloudflare treats query strings as
    # part of the cache key, so each release gets a fresh edge
    # cache and a stale negative cache from a previous deploy
    # (e.g. a 404 served while the file was still being uploaded)
    # can't poison the next release's URL for hours.
    VERSION=$(curl -fsSL --max-time 10 \
        "https://glint.nakitel.com/glintd/version.txt" 2>/dev/null \
        | tr -d '\r\n ' || true)
    [ -n "$VERSION" ] || die "couldn't fetch version stamp - check internet on the router"
    RELEASE_PUB=/tmp/glintd-bundle/release.pub
    RELEASE_SIG=/tmp/glintd-bundle/release.pub.sig
    if ! curl -fsSL --max-time 60 -o "$TARBALL" \
            "https://glint.nakitel.com/glintd/glintd.tar.gz?v=$VERSION" \
       || ! curl -fsSL --max-time 30 -o "$SIGFILE" \
            "https://glint.nakitel.com/glintd/glintd.tar.gz.sig?v=$VERSION" \
       || ! curl -fsSL --max-time 30 -o "$RELEASE_PUB" \
            "https://glint.nakitel.com/glintd/release.pub" \
       || ! curl -fsSL --max-time 30 -o "$RELEASE_SIG" \
            "https://glint.nakitel.com/glintd/release.pub.sig"; then
        die "couldn't fetch glintd tarball, signature, or release key - check internet on the router"
    fi
    if ! command -v usign >/dev/null 2>&1; then
        die "usign not found - this firmware is too old for signed glintd installs (need OpenWrt 21+)"
    fi
    # Two-tier signature chain. Only the root public key is
    # embedded in this script - it never rotates. The release
    # public key is fetched from the server every time and
    # verified by the embedded root key, so a compromised
    # release.sec can be rotated without re-flashing routers
    # (publish a new release.pub.sig signed by the offline root
    # key; cron picks it up on the next tick). A compromised
    # root.sec is the disaster scenario - requires a fresh
    # install.sh shipped through the app's bundled installer.
    ROOT_PUB=/tmp/glintd-bundle/root.pub
    cat > "$ROOT_PUB" <<'ROOT_PUB_EOF'
untrusted comment: glintd ROOT signing key (offline) public key
RWQM45Ps/jmIfrk8y6EICpcJ0WKrUCGCoVvJ1dPnGVTSwfEeXXcoXkqw
ROOT_PUB_EOF
    # Step 1: verify the release public key against the embedded
    # offline-root key.
    if ! usign -V -m "$RELEASE_PUB" -p "$ROOT_PUB" -x "$RELEASE_SIG" >/dev/null 2>&1; then
        die "release.pub signature does not chain to root - ABORTING (suspected key compromise or MITM)"
    fi
    # Step 2: verify the tarball against the now-trusted release key.
    if ! usign -V -m "$TARBALL" -p "$RELEASE_PUB" -x "$SIGFILE" >/dev/null 2>&1; then
        rm -f "$TARBALL" "$SIGFILE"
        die "tarball signature verification failed - forged or corrupted, ABORTING"
    fi
    log "signature chain verified ✓ (root → release → tarball)"
    if ! tar -xzf "$TARBALL" -C /tmp/glintd-bundle; then
        die "tarball extraction failed"
    fi
    SRC="/tmp/glintd-bundle"
fi
log "installing from $SRC"

# Stop the running daemon ONLY now that we have a verified
# source tree ready to copy in. Earlier revisions stopped the
# daemon before the signature check, which meant a tampered or
# corrupted tarball left the router with no daemon at all until
# procd's keepalive kicked in. With the stop deferred to here,
# any verify/fetch failure aborts cleanly with the previous
# daemon still running.
if [ -x "$INIT_SCRIPT" ]; then
    log "stopping previous glintd"
    "$INIT_SCRIPT" stop || true
fi

mkdir -p "$GLINTD_HOME"
# Copy everything except this installer (which lives outside
# $GLINTD_HOME) and any local dev artefacts.
cp -R "$SRC/capabilities.py" "$SRC/daemon.py" "$SRC/version.py" "$GLINTD_HOME/"
cp -R "$SRC/collectors" "$SRC/storage" "$SRC/rpc" "$SRC/apns" "$GLINTD_HOME/"
chmod +x "$GLINTD_HOME/daemon.py"

# 3. router_id + router_secret - stable per-install credentials.
#    router_id is the public tenant key the relay routes by;
#    router_secret is the HMAC key the daemon signs every push
#    request with. Both written 0600 root-only. The relay's
#    matching record is created on first authenticated request
#    (tofu - first signature observed for a router_id wins).
#    Re-running the installer doesn't rotate either; uninstall
#    first if you want fresh creds.
if [ ! -f "$GLINTD_HOME/router_id" ]; then
    head -c 16 /dev/urandom | hexdump -ve '1/1 "%02x"' | head -c 32 \
        > "$GLINTD_HOME/router_id"
    chmod 600 "$GLINTD_HOME/router_id"
    log "wrote new router_id"
fi
if [ ! -f "$GLINTD_HOME/router_secret" ]; then
    head -c 32 /dev/urandom | hexdump -ve '1/1 "%02x"' | head -c 64 \
        > "$GLINTD_HOME/router_secret"
    chmod 600 "$GLINTD_HOME/router_secret"
    log "wrote new router_secret"
fi

# 4. ubus registration shim. ubus' shell-out method dispatches
#    `mudi.glintd.<method>` calls to our CLI. The standalone
#    file makes it easy to test (`/etc/glintd/glintd-rpc ping`)
#    without going through ubus.
cat > "$GLINTD_HOME/glintd-rpc.sh" <<'EOF'
#!/bin/sh
# Args:  <method> [json-args]
# rpcd's exec plugin spawns us with a stripped env (no PYTHONPATH),
# so we set it here. `/etc` is the parent of `/etc/glintd`, which
# lets `import glintd.rpc.cli` resolve.
#
# GLINTD_DB pins the handler to the daemon's live working sqlite
# (/tmp/glintd.db on tmpfs) - the same connection the daemon's
# main loop and snapshot_pusher use. The persistent snapshot at
# /etc/glintd/state.db is a periodic-flush mirror, not a live
# store: if RPC writes there, the next daemon flush overwrites
# the file with the daemon's live state and any RPC-written rows
# (push tokens, preferences) vanish.
PYTHONPATH=/etc GLINTD_DB=/tmp/glintd.db exec python3 -m glintd.rpc.cli "$@"
EOF
chmod +x "$GLINTD_HOME/glintd-rpc.sh"

# 5. procd init script. Restarts the daemon on crash, captures
#    stdout/err to syslog, runs as root (we need raw socket
#    access for ping + privileged sysfs reads).
cat > "$INIT_SCRIPT" <<EOF
#!/bin/sh /etc/rc.common
USE_PROCD=1
START=99
STOP=10

start_service() {
    # rpcd is what publishes our ubus object, and some firmwares
    # ship it disabled (GL.iNet 4.10 on the E5800 / Mudi 7 -
    # their web UI talks to ubus directly, so nothing else on the
    # box needs rpcd). It also gets switched back off by a
    # "keep settings" firmware upgrade, which wipes the overlay's
    # opkg state. Without this the daemon boots perfectly and the
    # app still sees no router daemon at all, so re-assert it on
    # every start. Both calls are no-ops when rpcd is already
    # enabled and up.
    /etc/init.d/rpcd enabled 2>/dev/null || \
        /etc/init.d/rpcd enable >/dev/null 2>&1 || true
    /etc/init.d/rpcd running >/dev/null 2>&1 || \
        /etc/init.d/rpcd start >/dev/null 2>&1 || true

    procd_open_instance
    procd_set_param command python3 $GLINTD_HOME/daemon.py
    procd_set_param env PYTHONPATH=/etc
    procd_set_param respawn
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}

# Best-effort "router going down" push. Fires before procd takes
# the daemon down via clean shutdown / reboot / "service glintd
# stop". The push is one-shot, capped at 3 s so it can't stall
# the shutdown sequence - on a typical OpenWrt box procd waits
# ~5 s for STOP=10 services before SIGKILL, so 3 s leaves
# slack for the actual cleanup. If the network's already dead
# the curl times out and we exit silently; the next router.booted
# on come-up serves as the bookend.
stop_service() {
    # PYTHONPATH=/etc is REQUIRED here - procd's stop_service hook
    # runs in a clean shell with no inherited env from the running
    # daemon, so without explicit PYTHONPATH the python3 -m import
    # fails with "No module named 'glintd'" and the push never
    # fires. start_service sets the same env via procd_set_param
    # for the long-running daemon process; we must repeat it for
    # this short-lived helper.
    PYTHONPATH=/etc python3 -m glintd.rpc.shutdown_notify \\
        >/dev/null 2>&1 &
    NOTIFY_PID=\$!
    ( sleep 3; kill \$NOTIFY_PID 2>/dev/null ) &
    wait \$NOTIFY_PID 2>/dev/null
}
EOF
chmod +x "$INIT_SCRIPT"

# 6. Wire up the ubus method handler. ubus reads
#    /usr/share/ubus/json/<file>.json on registration, but our
#    method needs to shell out - easier to use rpcd's `exec`
#    plugin which is enabled on every GL.iNet build.
mkdir -p /usr/share/rpcd/acl.d
cat > /usr/share/rpcd/acl.d/glintd.json <<'EOF'
{
    "glintd": {
        "description": "Glint companion daemon RPC",
        "read": {
            "ubus": {
                "mudi.glintd": ["*"]
            }
        }
    }
}
EOF

mkdir -p /usr/libexec/rpcd
cat > /usr/libexec/rpcd/mudi.glintd <<EOF
#!/bin/sh
case "\$1" in
    list)
        echo '{"ping":{},"get_history":{"metric":"str","since":0,"tier":"str"},"list_metrics":{},"get_internet_history":{"since":0,"until":0,"limit":0},"get_snapshot":{"metrics":[]},"register_device_token":{"token":"str","platform":"str","bundle_id":"str","disabled_events":[]},"unregister_device_token":{"token":"str"},"set_push_preferences":{"token":"str","disabled_events":[]},"get_router_credentials":{},"test_push":{"title":"str","body":"str"}}'
    ;;
    call)
        read input
        $GLINTD_HOME/glintd-rpc.sh "\$2" "\$input"
    ;;
esac
EOF
chmod +x /usr/libexec/rpcd/mudi.glintd

# 7. Self-update script + cron.
#    The companion app's bundled installer is the primary update
#    path (user taps Update in Settings → Router daemon → app
#    pushes a fresh tarball from its bundle). This cron is the
#    safety net for users who don't open the iOS / Mac app for
#    weeks: runs once a day, compares /etc/glintd/version.py
#    against the public version.txt, fetches + reinstalls when
#    newer. No-op when offline or up to date - quiet, no email
#    alerts, log line in syslog.
SELF_UPDATE="$GLINTD_HOME/self-update.sh"
cat > "$SELF_UPDATE" <<'EOF'
#!/bin/sh
# glintd self-update - periodic check against the public
# version-stamp endpoint, idempotent reinstall when newer.
# Verifies an Ed25519 signature on the tarball before running it.
set -e
GLINTD_HOME="/etc/glintd"
LATEST_URL="https://glint.nakitel.com/glintd/version.txt"
TARBALL_URL="https://glint.nakitel.com/glintd/glintd.tar.gz"
SIG_URL="https://glint.nakitel.com/glintd/glintd.tar.gz.sig"
INSTALL_URL="https://glint.nakitel.com/glintd/install.sh"
INSTALL_SIG_URL="https://glint.nakitel.com/glintd/install.sh.sig"
RELEASE_PUB_URL="https://glint.nakitel.com/glintd/release.pub"
RELEASE_PUB_SIG_URL="https://glint.nakitel.com/glintd/release.pub.sig"

INSTALLED=$(awk -F\" '/^VERSION =/ {print $2; exit}' \
    "$GLINTD_HOME/version.py" 2>/dev/null || echo "")
LATEST=$(curl -fsSL --max-time 10 "$LATEST_URL" 2>/dev/null \
    | tr -d '\r\n ' || true)

if [ -z "$LATEST" ] || [ "$INSTALLED" = "$LATEST" ]; then
    exit 0
fi

# Version compare via `sort -V` so 0.10.0 > 0.9.0 stays correct
# once minors hit double digits. Falls back to lexicographic
# compare on busybox builds without -V (rare on OpenWrt 21+).
NEWER=$(printf '%s\n%s\n' "$INSTALLED" "$LATEST" \
    | sort -V 2>/dev/null | tail -1 || echo "$LATEST")
[ "$NEWER" = "$LATEST" ] || exit 0
[ "$NEWER" = "$INSTALLED" ] && exit 0

logger -t glintd "self-update: $INSTALLED -> $LATEST, fetching"
TMP=$(mktemp -d /tmp/glintd-self-update.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
if ! curl -fsSL --max-time 60 -o "$TMP/install.sh" "$INSTALL_URL?v=$LATEST" \
   || ! curl -fsSL --max-time 30 -o "$TMP/install.sh.sig" "$INSTALL_SIG_URL?v=$LATEST" \
   || ! curl -fsSL --max-time 60 -o "$TMP/glintd.tar.gz" "$TARBALL_URL?v=$LATEST" \
   || ! curl -fsSL --max-time 30 -o "$TMP/glintd.tar.gz.sig" "$SIG_URL?v=$LATEST" \
   || ! curl -fsSL --max-time 30 -o "$TMP/release.pub" "$RELEASE_PUB_URL" \
   || ! curl -fsSL --max-time 30 -o "$TMP/release.pub.sig" "$RELEASE_PUB_SIG_URL"; then
    logger -t glintd "self-update: download failed - will retry tomorrow"
    exit 0
fi

# Two-tier signature chain (see install.sh). The embedded key is
# the OFFLINE ROOT - never rotates. release.pub is fetched and
# verified each tick so a compromised release.sec rotates with
# the next cron run.
cat > "$TMP/root.pub" <<'ROOT_PUB_EOF'
untrusted comment: glintd ROOT signing key (offline) public key
RWQM45Ps/jmIfrk8y6EICpcJ0WKrUCGCoVvJ1dPnGVTSwfEeXXcoXkqw
ROOT_PUB_EOF
if ! command -v usign >/dev/null 2>&1; then
    logger -t glintd "self-update: usign missing on this firmware - skipping"
    exit 0
fi
if ! usign -V -m "$TMP/release.pub" \
        -p "$TMP/root.pub" \
        -x "$TMP/release.pub.sig" >/dev/null 2>&1; then
    logger -t glintd "self-update: release.pub does not chain to root - aborting (suspected key compromise)"
    exit 0
fi
if ! usign -V -m "$TMP/glintd.tar.gz" \
        -p "$TMP/release.pub" \
        -x "$TMP/glintd.tar.gz.sig" >/dev/null 2>&1; then
    logger -t glintd "self-update: tarball signature verification FAILED - aborting"
    exit 0
fi
# Verify install.sh ITSELF before running it. Without this the
# signed-tarball design is undermined: we'd be executing a freshly
# downloaded, unauthenticated shell script as root and trusting it
# to verify everything else. A compromised CDN/origin or a
# mis-issued TLS cert could swap install.sh for a payload that
# ignores the signature checks entirely. The script is signed with
# the same release key as the tarball and chains to the embedded
# offline root, so a forged install.sh fails here and we abort with
# the previous version still installed and running.
if ! usign -V -m "$TMP/install.sh" \
        -p "$TMP/release.pub" \
        -x "$TMP/install.sh.sig" >/dev/null 2>&1; then
    logger -t glintd "self-update: install.sh signature verification FAILED - aborting (suspected MITM / CDN compromise)"
    exit 0
fi

# Stage the verified tarball + sig where install.sh's local-source
# probe will find them, so it skips its own re-download path.
rm -rf /tmp/glintd-bundle
mkdir -p /tmp/glintd-bundle
cp "$TMP/glintd.tar.gz" "$TMP/glintd.tar.gz.sig" /tmp/glintd-bundle/
tar -xzf "$TMP/glintd.tar.gz" -C /tmp/glintd-bundle
sh "$TMP/install.sh"
logger -t glintd "self-update: installed $LATEST"
EOF
chmod +x "$SELF_UPDATE"

# Cron entry: every day at 04:17 local time. Off-hour to avoid
# colliding with the daemon's own midnight retention sweeps and
# the kernel's NTP step at 04:00 on some firmwares.
mkdir -p /etc/crontabs
CRON_LINE="17 4 * * * $SELF_UPDATE >/dev/null 2>&1"
if [ -f /etc/crontabs/root ]; then
    grep -v 'glintd/self-update.sh' /etc/crontabs/root > /tmp/crontab.new \
        || true
    mv /tmp/crontab.new /etc/crontabs/root
fi
echo "$CRON_LINE" >> /etc/crontabs/root
/etc/init.d/cron reload >/dev/null 2>&1 || \
    /etc/init.d/cron restart >/dev/null 2>&1 || true

# 7b. Survive firmware upgrades. GL.iNet's sysupgrade reads
#     /lib/upgrade/keep.d/* BEFORE replacing the rootfs+overlay, to
#     build the "keep settings" backup set. glintd's code lives in the
#     overlay and is wiped wholesale on every firmware update, but
#     listing the credential + history files here makes them survive a
#     "Keep settings" upgrade - so a post-upgrade reinstall (via the
#     companion app's auto-reinstall, or this script run again) keeps
#     the relay's TOFU identity and the metric history intact. The
#     keep.d file itself is wiped too, but it has already been read by
#     the time that happens, so it does its job for the upgrade in
#     progress and this installer re-creates it afterwards.
mkdir -p /lib/upgrade/keep.d
cat > /lib/upgrade/keep.d/glintd <<'EOF'
/etc/glintd/router_id
/etc/glintd/router_secret
/etc/glintd/state.db
EOF

# 8. Enable + start.
"$INIT_SCRIPT" enable
"$INIT_SCRIPT" start

# 8b. Make sure rpcd is actually up. It - not the daemon - is what
#     publishes `mudi.glintd` on ubus: it scans /usr/libexec/rpcd
#     at startup, re-scans on SIGHUP, and forks the shim for every
#     call the app makes. GL.iNet 4.10 firmware (GL-E5800 / Mudi 7
#     and siblings) ships rpcd NEITHER enabled NOR running, since
#     their own web UI speaks to ubus directly. Against that box
#     the bare `reload` this step used to send was a no-op - procd
#     only signals a live instance - so the object never appeared
#     and the install failed its smoke test below with a perfectly
#     healthy daemon running. Reported from the field on
#     e5800-4.10.0_release5 and reproduced on the 4.10.0 test unit.
if [ ! -x /etc/init.d/rpcd ]; then
    die "rpcd is not installed on this firmware - Glint needs it to publish 'mudi.glintd' on ubus. Run \`opkg update && opkg install rpcd\` on the router, then re-run this installer."
fi
if ! /etc/init.d/rpcd enabled 2>/dev/null; then
    log "rpcd was not enabled at boot - enabling it"
    /etc/init.d/rpcd enable >>"$LOG" 2>&1 \
        || log "WARNING: couldn't enable rpcd - glintd's ubus object won't come back after a reboot"
fi
if rpcd_running; then
    /etc/init.d/rpcd reload >>"$LOG" 2>&1 || true
else
    log "rpcd was not running (stock state on GL.iNet 4.10) - starting it"
    /etc/init.d/rpcd start >>"$LOG" 2>&1 \
        || log "WARNING: /etc/init.d/rpcd start failed - see 'logread | grep rpcd'"
fi

# 9. Smoke test - give the daemon up to 10 s to come up, then
#    verify ubus answers `ping`. busybox `sleep` on some boards
#    (GL-BE3600 / Slate 7 shipping firmware) doesn't accept
#    fractional arguments, so we poll once a second for 10 s.
i=0
while [ $i -lt 10 ]; do
    if ubus call mudi.glintd ping >/dev/null 2>&1; then
        log "ubus call mudi.glintd ping → OK"
        log ""
        log "✓ glintd installed. Open Glint → Settings → Router daemon"
        log "  to confirm pairing and history backfill."
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done

# Ping never answered. Work out WHICH half is broken before we
# give up, and put the evidence in the log: with rpcd down the
# daemon answers its CLI perfectly and only the bus is silent,
# and that distinction is the difference between a one-line fix
# and a support round-trip.
{
    echo "--- diagnostics ---"
    echo '$ /etc/init.d/glintd status'
    "$INIT_SCRIPT" status 2>&1 || true
    echo '$ /etc/init.d/rpcd running'
    /etc/init.d/rpcd running 2>&1 || echo "(not running)"
    echo '$ ubus list'
    ubus list 2>&1 || true
    echo '$ glintd-rpc.sh ping'
    "$GLINTD_HOME/glintd-rpc.sh" ping 2>&1 | head -5 || true
    echo '$ logread | grep -iE "glintd|rpcd" | tail -30'
    logread 2>/dev/null | grep -iE 'glintd|rpcd' | tail -30 || true
} >>"$LOG" 2>&1 || true

if ! rpcd_running; then
    die "rpcd won't stay running on this firmware, so 'mudi.glintd' can't be published on ubus - the daemon itself may be fine. Try \`/etc/init.d/rpcd enable && /etc/init.d/rpcd start\`, check \`logread | grep rpcd\`, then re-run this installer. Details in $LOG."
fi
if "$GLINTD_HOME/glintd-rpc.sh" ping >/dev/null 2>&1; then
    die "the daemon answers directly but rpcd didn't publish 'mudi.glintd' on ubus. Check \`logread | grep rpcd\` and that /usr/libexec/rpcd/mudi.glintd is executable. Details in $LOG."
fi
die "daemon didn't answer ubus ping within 10s - see $LOG and 'logread | grep glintd'"
