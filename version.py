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
VERSION = "1.0.4"

# Protocol features this daemon implements. Returned in the
# ping reply under "features" so the app can branch on what is
# safe to call. The app treats every absent / false key as
# unsupported, so removing a feature is silent-degrade (no
# client crashes) - additive only is the rule.
#
# Stable contract documented in `docs/glintd-protocol.md`. Once
# a feature key is shipped here, the key + semantics never
# change; only new keys get added. Spelled lower_snake_case to
# match the rest of the wire format.
FEATURES = {
    # Always-on baseline that every glintd >= 0.6.0 honours. The
    # app uses presence-of-this-key to detect "this daemon
    # speaks the v2 feature negotiation protocol" vs "this is an
    # older daemon I should just talk to via the legacy methods".
    "capabilities_v2": True,
    # Reserved for upcoming stages; false until the corresponding
    # endpoint actually lands. Listing them here is intentional -
    # the app can show "you have 0.6.0 but not snapshot streaming;
    # update for the live-push experience" without guessing.
    "snapshot_polling":     True,   # get_snapshot          (0.6.0)
    "snapshot_stream":      True,   # stream_snapshots       (0.6.0)
    "history_internet":     True,   # get_internet_history    (0.6.0)
    "ping_loss":            True,   # daemon-side packet loss (0.6.0)
    "throughput_saturation": True,  # client-side overlay hint    (0.6.0)
}
