"""Push notification machinery.

Two pieces:

    relay_client.py     - outbound HTTPS to glint.nakitel.com/apns,
                          HMAC-signed per request with the router's
                          install-time secret.
    thresholds.py       - runs once per scheduler tick, compares
                          fresh metric values against per-event
                          thresholds, fires push events through
                          relay_client when crossings happen.

Daemon's main loop calls `thresholds.evaluate()` every tick after
the collectors have written. The thresholds module pulls just-
written hot rows back out of the store and decides whether anything
warrants a notification.
"""
