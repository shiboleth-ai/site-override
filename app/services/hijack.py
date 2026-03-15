"""
Session manager: handles /etc/hosts hijacking and local server lifecycle.

Architecture:
- Server runs as root on ports 80/443 (started via osascript sudo)
- osascript handles all privileged ops in a single password prompt:
  hosts file, server launch, DNS flush
"""

import json
import os
import signal
import subprocess
import sys
import time


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
            # Verify server is actually running
            pid = state.get("server_pid")
            if pid and not self._is_process_running(pid):
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

        # Pre-create log file as current user (root can still write to it)
        with open(log_path, "w"):
            pass

        # Single osascript call that does everything as root:
        # 1. Kill any zombie servers left on 80/443
        # 2. Add /etc/hosts entry
        # 3. Start server on ports 80/443 in background
        # 4. Flush DNS
        # 5. Return the server PID
        #
        # Key: use `&` to background (NOT nohup — nohup fails in osascript).
        # Redirect stdin/stdout/stderr so the process detaches cleanly.
        sudo_script = (
            # Kill any leftover server processes on 80/443
            f"lsof -ti :80 -sTCP:LISTEN | xargs kill -9 2>/dev/null; "
            f"lsof -ti :443 -sTCP:LISTEN | xargs kill -9 2>/dev/null; "
            f"sleep 0.3; "
            # Add hosts entry
            f'echo "127.0.0.1 {domain} {HOSTS_MARKER}" >> /etc/hosts; '
            # Start server in background
            f'"{python_path}" "{hijack_server_path}" '
            f'"{site_dir}" "{cert_path}" "{key_path}" "{self.pid_file}" '
            f'</dev/null >"{log_path}" 2>&1 & '
            f"SERVER_PID=$!; "
            # Wait for server to bind, then verify it's running
            f"sleep 1; "
            f"if ! kill -0 $SERVER_PID 2>/dev/null; then "
            # Server died — roll back hosts entry
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
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Sudo prompt timed out"}

        if result.returncode != 0:
            err = result.stderr.strip()
            if "User canceled" in err or "canceled" in err.lower():
                return {"success": False, "error": "Sudo canceled by user"}
            return {"success": False, "error": f"Failed to start session: {err}"}

        output = result.stdout.strip()

        if output == "FAILED":
            # Server died — hosts already cleaned up by the script
            try:
                with open(log_path) as f:
                    log = f.read().strip()[-300:]
            except OSError:
                log = "no log available"
            return {
                "success": False,
                "error": f"Server failed to start. Log: {log}",
            }

        server_pid = int(output) if output.isdigit() else None

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

        # Privileged cleanup: kill server (root process) + remove hosts + flush DNS
        parts = []
        if pid:
            parts.append(f"kill -9 {pid} 2>/dev/null")
        # Also kill anything left on 80/443
        parts.append("lsof -ti :80 -sTCP:LISTEN | xargs kill -9 2>/dev/null")
        parts.append("lsof -ti :443 -sTCP:LISTEN | xargs kill -9 2>/dev/null")
        parts.append(f'sed -i "" "/{HOSTS_MARKER}/d" /etc/hosts')
        parts.append("dscacheutil -flushcache")
        parts.append("killall -HUP mDNSResponder 2>/dev/null")
        parts.append('echo "ok"')

        sudo_script = "; ".join(parts)

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
            self._remove_state()
            return {"success": False, "error": "Sudo prompt timed out during cleanup"}

        if result.returncode != 0:
            err = result.stderr.strip()
            if "User canceled" in err or "canceled" in err.lower():
                return {"success": False, "error": "Sudo canceled. Hosts file still modified!"}
            self._remove_state()
            return {"success": False, "error": f"Cleanup failed: {err}"}

        self._remove_state()

        # Record in DB
        from ..models import record_session_end

        record_session_end(domain)

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
                # Server runs as root — try SIGKILL (SIGTERM may not work)
                os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self._remove_state()
        return {
            "success": True,
            "note": "Server killed. /etc/hosts may need manual cleanup.",
        }

    def _sudo_cleanup_hosts(self):
        """Remove our hosts entries via osascript. Best-effort."""
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'do shell script "sed -i \\"\\" \\"/{HOSTS_MARKER}/d\\" /etc/hosts; '
                    f'dscacheutil -flushcache; killall -HUP mDNSResponder 2>/dev/null" '
                    f"with administrator privileges",
                ],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass

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
