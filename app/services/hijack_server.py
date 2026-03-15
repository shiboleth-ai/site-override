#!/usr/bin/env python3
"""
Standalone HTTP/HTTPS file server for serving cloned sites.
Runs with elevated privileges (port 80/443).
Launched by SessionManager via osascript sudo.

Usage: python hijack_server.py <site_dir> <cert_path> <key_path> <pid_file>
"""

import http.server
import os
import signal
import ssl
import sys
import threading


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler that serves from a specific directory and suppresses logs."""

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):
        # Log to file instead of stderr
        pass


def make_handler(directory):
    """Create a handler class bound to a specific directory."""

    class Handler(QuietHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    return Handler


def run():
    if len(sys.argv) < 5:
        print(f"Usage: {sys.argv[0]} <site_dir> <cert_path> <key_path> <pid_file>")
        sys.exit(1)

    site_dir = sys.argv[1]
    cert_path = sys.argv[2]
    key_path = sys.argv[3]
    pid_file = sys.argv[4]

    if not os.path.isdir(site_dir):
        print(f"Error: {site_dir} is not a directory")
        sys.exit(1)

    # Write PID
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    handler_class = make_handler(site_dir)

    servers = []

    # HTTP server on port 80
    try:
        http_server = http.server.HTTPServer(("127.0.0.1", 80), handler_class)
        servers.append(http_server)
    except OSError as e:
        print(f"Warning: Could not bind port 80: {e}")

    # HTTPS server on port 443
    try:
        https_server = http.server.HTTPServer(("127.0.0.1", 443), handler_class)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        https_server.socket = context.wrap_socket(
            https_server.socket, server_side=True
        )
        servers.append(https_server)
    except OSError as e:
        print(f"Warning: Could not bind port 443: {e}")

    if not servers:
        print("Error: Could not bind to any port")
        cleanup_pid(pid_file)
        sys.exit(1)

    def shutdown(signum, frame):
        for s in servers:
            s.shutdown()
        cleanup_pid(pid_file)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threads = []
    for server in servers:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        threads.append(t)

    # Keep main thread alive
    for t in threads:
        t.join()


def cleanup_pid(pid_file):
    try:
        os.unlink(pid_file)
    except OSError:
        pass


if __name__ == "__main__":
    run()
