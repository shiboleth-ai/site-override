#!/usr/bin/env python3
"""Entry point for site-override."""

import atexit
import logging
import signal
import sys

from app import create_app

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] %(levelname)s %(message)s",
)

app = create_app()
session_manager = app.config["SESSION_MANAGER"]


def cleanup(signum=None, frame=None):
    """Best-effort cleanup on exit."""
    status = session_manager.get_status()
    if status.get("active"):
        print("\n[site-override] Cleaning up active session...")
        result = session_manager.force_cleanup()
        if result.get("note"):
            print(f"[site-override] {result['note']}")
            print(
                "[site-override] To manually clean up, run:\n"
                '  sudo sed -i "" "/# SITE-OVERRIDE-MANAGED/d" /etc/hosts && '
                "sudo dscacheutil -flushcache && "
                "sudo killall -HUP mDNSResponder"
            )
    if signum:
        sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)
atexit.register(cleanup)

# Check for stale sessions from previous crashes
with app.app_context():
    stale = session_manager.cleanup_stale()
    if stale and stale.get("stale"):
        if stale.get("cleaned"):
            print("[site-override] Cleaned up stale session state from previous crash.")
        else:
            print(
                f"[site-override] WARNING: Found active session for {stale.get('domain')}. "
                "Use the UI to stop it."
            )


if __name__ == "__main__":
    print("[site-override] Starting on http://127.0.0.1:5000")
    print("[site-override] Press Ctrl+C to quit (active sessions will be cleaned up)")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
