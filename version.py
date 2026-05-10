"""Single source of truth for the glintd version.

Bumped on every meaningful change to the daemon - schema, push
event id additions, RPC method changes, capability detection
fixes, anything that the app or the relay needs to know about
to handle this revision correctly.

Read by:

  * `daemon.py` (sets it on the ubus `ping` reply so the app can
    show "Installed: <version>" without parsing logs).
  * The installed `glintd-rpc` CLI (returned in the `ping` JSON).

The string format is plain SemVer-ish - major.minor.patch - but
the app's compare is just lexicographic over dot-separated
integers; don't introduce build suffixes (`0.2.0+abc`) or
pre-releases (`0.2.0-rc1`) without updating the comparator on
the app side.
"""
VERSION = "0.5.6"
