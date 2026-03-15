"""
Session manager: handles /etc/hosts hijacking and local server lifecycle.

Architecture:
- Server runs as root on ports 80/443 (started via osascript sudo)
- osascript handles all privileged ops in a single password prompt:
  hosts file, server launch, DNS flush
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time


log = logging.getLogger("site-override")

HOSTS_MARKER = "# SITE-OVERRIDE-MANAGED"


class SessionManager:
    def __init__(self, state_file: str, pid_file: str):
        self.state_file = state_file
        self.pid_file = pid_file

    def get_status(self) -> dict:
        """Get current session status."""
        if not os.path.exists(self.state_file):
            return {"active": False}
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            pid = state.get("server_pid")
            if pid and not self._is_process_running(pid):
                log.warning("Server PID %s no longer running, cleaning stale state", pid)
                self._remove_state()
                return {"active": False, "stale_cleaned": True}
            return {"active": True, **state}
        except (json.JSONDecodeError, OSError):
            return {"active": False}

    def start_session(
        self, domain: str, site_dir: str, cert_path: str, key_path: str
    ) -> dict:
        """Start a hijack session."""
        status = self.get_status()
        if status["active"]:
            return {
                "success": False,
                "error": f"Session already active for {status.get('domain')}. Stop it first.",
            }

        hijack_server_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hijack_server.py"
        )
        python_path = sys.executable
        log_path = os.path.join(os.path.dirname(self.state_file), "server.log")

        log.info("Starting hijack session for %s", domain)
        log.info("  site_dir: %s", site_dir)
        log.info("  cert: %s", cert_path)
        log.info("  server script: %s", hijack_server_path)
        log.info("  python: %s", python_path)

        # Pre-create log file as current user (root can still write to it)
        with open(log_path, "w"):
            pass

        # Single osascript call that does everything as root:
        # 1. Kill any zombie servers on 80/443
        # 2. Add /etc/hosts entry
        # 3. Start server on 80/443 in background
        # 4. Verify server started
        # 5. If failed, roll back hosts entry (no second password prompt)
        # 6. Flush DNS
        # 7. Return PID or FAILED
        sudo_script = (
            f"lsof -ti :80 -sTCP:LISTEN | xargs kill -9 2>/dev/null; "
            f"lsof -ti :443 -sTCP:LISTEN | xargs kill -9 2>/dev/null; "
            f"sleep 0.3; "
            f'echo "127.0.0.1 {domain} {HOSTS_MARKER}" >> /etc/hosts; '
            f'"{python_path}" "{hijack_server_path}" '
            f'"{site_dir}" "{cert_path}" "{key_path}" "{self.pid_file}" '
            f'</dev/null >"{log_path}" 2>&1 & '
            f"SERVER_PID=$!; "
            f"sleep 1; "
            f"if ! kill -0 $SERVER_PID 2>/dev/null; then "
            f'  sed -i "" "/{HOSTS_MARKER}/d" /etc/hosts; '
            f"  dscacheutil -flushcache; "
            f"  killall -HUP mDNSResponder 2>/dev/null; "
            f'  echo "FAILED"; '
            f"else "
            f"  dscacheutil -flushcache; "
            f"  killall -HUP mDNSResponder 2>/dev/null; "
            f"  echo $SERVER_PID; "
            f"fi"
        )

        log.info("Requesting admin privileges via osascript...")

        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'do shell script "{_escape_applescript(sudo_script)}" '
                    f"with administrator privileges",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            log.error("osascript timed out (120s)")
            return {"success": False, "error": "Sudo prompt timed out (2 minutes). Was the password dialog visible?"}

        log.info("osascript returncode: %s", result.returncode)
        log.info("osascript stdout: %r", result.stdout.strip())
        if result.stderr.strip():
            log.info("osascript stderr: %r", result.stderr.strip())

        if result.returncode != 0:
            err = result.stderr.strip()
            if "User canceled" in err or "canceled" in err.lower():
                log.info("User canceled sudo prompt")
                return {"success": False, "error": "Sudo canceled by user"}
            log.error("osascript failed: %s", err)
            return {"success": False, "error": f"Failed to start session: {err}"}

        output = result.stdout.strip()

        if output == "FAILED":
            try:
                with open(log_path) as f:
                    server_log = f.read().strip()[-500:]
            except OSError:
                server_log = "no log available"
            log.error("Server failed to start. Server log:\n%s", server_log)
            return {
                "success": False,
                "error": f"Server failed to start. Log: {server_log}",
            }

        server_pid = int(output) if output.isdigit() else None
        log.info("Session started! Server PID: %s", server_pid)

        # Save state
        state = {
            "domain": domain,
            "site_dir": site_dir,
            "server_pid": server_pid,
            "cert_path": cert_path,
            "key_path": key_path,
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)

        # Record in DB for crash recovery
        from ..models import record_session_start

        record_session_start(domain, server_pid)

        return {"success": True, "domain": domain, "pid": server_pid}

    def stop_session(self) -> dict:
        """Stop the active hijack session."""
        status = self.get_status()
        if not status.get("active"):
            return {"success": True, "message": "No active session"}

        domain = status.get("domain", "")
        pid = status.get("server_pid")

        log.info("Stopping session for %s (PID %s)", domain, pid)

        parts = []
        if pid:
            parts.append(f"kill -9 {pid} 2>/dev/null")
        parts.append("lsof -ti :80 -sTCP:LISTEN | xargs kill -9 2>/dev/null")
        parts.append("lsof -ti :443 -sTCP:LISTEN | xargs kill -9 2>/dev/null")
        parts.append(f'sed -i "" "/{HOSTS_MARKER}/d" /etc/hosts')
        parts.append("dscacheutil -flushcache")
        parts.append("killall -HUP mDNSResponder 2>/dev/null")
        parts.append('echo "ok"')

        sudo_script = "; ".join(parts)

        log.info("Requesting admin privileges for cleanup...")

        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'do shell script "{_escape_applescript(sudo_script)}" '
                    f"with administrator privileges",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.error("Cleanup osascript timed out")
            self._remove_state()
            return {"success": False, "error": "Sudo prompt timed out during cleanup"}

        log.info("Cleanup osascript returncode: %s", result.returncode)

        if result.returncode != 0:
            err = result.stderr.strip()
            if "User canceled" in err or "canceled" in err.lower():
                return {"success": False, "error": "Sudo canceled. Hosts file still modified!"}
            self._remove_state()
            return {"success": False, "error": f"Cleanup failed: {err}"}

        self._remove_state()

        from ..models import record_session_end

        record_session_end(domain)

        log.info("Session stopped for %s", domain)
        return {"success": True, "domain": domain}

    def cleanup_stale(self) -> dict | None:
        """Check for and clean up stale sessions from previous crashes."""
        if not os.path.exists(self.state_file):
            return None

        status = self.get_status()
        if status.get("active"):
            return {"stale": True, "domain": status.get("domain")}

        self._remove_state()
        return {"stale": True, "cleaned": True}

    def force_cleanup(self) -> dict:
        """Emergency cleanup on app exit. Kills server, warns about hosts."""
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGKILL)
                log.info("Force-killed server PID %s", pid)
            except (OSError, ValueError):
                pass

        self._remove_state()
        return {
            "success": True,
            "note": "Server killed. /etc/hosts may need manual cleanup.",
        }

    def _is_process_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, TypeError):
            return False

    def _remove_state(self):
        for f in (self.state_file, self.pid_file):
            try:
                os.unlink(f)
            except OSError:
                pass


def _escape_applescript(s: str) -> str:
    """Escape a string for embedding in AppleScript."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
