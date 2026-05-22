# Key rotation

`glintd` updates are protected by a two-tier Ed25519 signature
chain. This document describes how the chain works and how to
rotate keys when needed.

This document focuses on the key-rotation primitive in isolation;
the surrounding day-to-day release and deploy procedures are kept
separately by the operator.

## Roles

| Key            | Where it lives                          | Used for                                    | Rotation cadence |
|----------------|------------------------------------------|---------------------------------------------|------------------|
| `root.sec`     | offline (1Password / hardware token)     | signing the release public key              | once per decade  |
| `root.pub`     | embedded in `install.sh`                 | the only key the router fundamentally trusts| never            |
| `release.sec`  | dev machine (`~/.glint-signing/`)        | signing each release tarball                | on compromise    |
| `release.pub`  | published at `glint.nakitel.com/glintd/` | verifying tarballs                          | with rotation    |
| `release.pub.sig` | published next to `release.pub`       | proves release.pub chains to root           | with rotation    |

## Verification path on the router

For every install or self-update tick:

1. Fetch `glintd.tar.gz`, `glintd.tar.gz.sig`, `release.pub`,
   `release.pub.sig`.
2. `usign -V -m release.pub -p <embedded root.pub> -x release.pub.sig`
   - if this fails, abort. Either `release.pub` was tampered
     with in transit, or someone is trying to push an
     unauthorised release key.
3. `usign -V -m glintd.tar.gz -p release.pub -x glintd.tar.gz.sig`
   - if this fails, abort. The tarball does not match what the
     release key signed.

Only after both checks pass does any code from the tarball run.

## Rotating the release key (after compromise)

Trigger: `release.sec` is suspected leaked, exposed in a backup,
copied off the dev machine, or sat on a compromised system.

```bash
# 1. Generate a new release keypair.
cd ~/.glint-signing
mv release.sec release.sec.OLD-$(date +%Y%m%d)
mv release.pub release.pub.OLD-$(date +%Y%m%d)
signify -G -n -p release.pub -s release.sec \
        -c "glintd release signing key"
chmod 600 release.sec

# 2. Sign the new release.pub with the OFFLINE root key. This is
#    the only step that needs the root private key. Keep the
#    root.sec session as short as possible: copy it onto the
#    machine, sign, wipe.
signify -S -s root.sec \
        -m release.pub \
        -x release.pub.sig \
        -c "verify with root.pub"

# 3. Verify the chain locally before publishing.
signify -V -p root.pub -m release.pub -x release.pub.sig
# expect: "Signature Verified"

# 4. Bump glintd/version.py (otherwise self-update sees the same
#    version number and skips the fetch).

# 5. Deploy. The deploy script picks up the new release.pub +
#    release.pub.sig and signs the tarball with the new
#    release.sec.
scripts/deploy-glintd.sh
```

Routers will pick up the rotated key on the next 04:17 cron tick
(or sooner via the app's update flow). Old release.sec is no
longer accepted - `release.pub.sig` only points at the current
key.

There's no explicit revocation list because the chain itself is
the revocation: once the server publishes a new `release.pub.sig`,
anything signed by the old `release.sec` fails step 2 above.

## Rotating the root key (disaster scenario)

Trigger: `root.sec` is exposed. This is rare because the key
lives offline and is touched only during release-key rotations.
Recovery cannot happen through the existing self-update channel
because every router trusts the now-compromised `root.pub`.

1. Generate a new root keypair, store the new `root.sec` in a
   fresh offline location.
2. Sign the current `release.pub` with the new `root.sec`.
3. Update `install.sh` (both the standalone `curl`-bootstrap path
   and the embedded `self-update.sh`) with the new `root.pub`.
4. Build a new Glint app release that bundles the updated
   `install.sh`. Apple's notarisation acts as an out-of-band
   trust root: users get the new installer through the App
   Store update, which is signed by Apple, not by us.
5. Users who run the Glint app trigger a re-install from the
   in-app "Update daemon" flow - the bundled installer overwrites
   the old `install.sh` (and its embedded compromised `root.pub`)
   with the new one.
6. Routers whose owners never open the app cannot recover
   automatically. Document the manual SSH path on the public
   site and email known users where contact info is on file.

This is by design. Trading "automatic recovery from root
compromise" for "no permanently online key that can sign
anything" is the standard offline-root tradeoff used by Debian,
OpenWrt opkg, Apple notarisation, and TUF.
