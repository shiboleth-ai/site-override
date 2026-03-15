"""
Session manager: handles /etc/hosts hijacking and local server lifecycle.

Uses osascript for macOS native sudo prompts. All privileged operations
(hosts file, port 80/443 servers) go through a single sudo invocation.
"""

import json
import os
import signal
import subprocess
import sys


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

        # Build the sudo script that:
        # 1. Adds hosts entry
        # 2. Flushes DNS
        # 3. Starts the local server in background
        # 4. Returns the server PID
        hijack_server_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hijack_server.py"
        )
        python_path = sys.executable

        sudo_script = (
            f'echo "127.0.0.1 {domain} {HOSTS_MARKER}" >> /etc/hosts && '
            f"dscacheutil -flushcache && "
            f"killall -HUP mDNSResponder 2>/dev/null; "
            f'nohup "{python_path}" "{hijack_server_path}" '
            f'"{site_dir}" "{cert_path}" "{key_path}" "{self.pid_file}" '
            f"> /tmp/site-override-server.log 2>&1 & "
            f"echo $!"
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

        server_pid = result.stdout.strip()

        # Save state
        state = {
            "domain": domain,
            "site_dir": site_dir,
            "server_pid": int(server_pid) if server_pid.isdigit() else None,
            "cert_path": cert_path,
            "key_path": key_path,
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)

        return {"success": True, "domain": domain, "pid": server_pid}

    def stop_session(self) -> dict:
        """Stop the active hijack session."""
        status = self.get_status()
        if not status.get("active"):
            return {"success": True, "message": "No active session"}

        domain = status.get("domain", "")
        pid = status.get("server_pid")

        # Build cleanup script
        parts = []
        if pid:
            parts.append(f"kill {pid} 2>/dev/null")
        parts.append(f'sed -i "" "/{HOSTS_MARKER}/d" /etc/hosts')
        parts.append("dscacheutil -flushcache")
        parts.append("killall -HUP mDNSResponder 2>/dev/null")

        sudo_script = " && ".join(parts[:-1]) + "; " + parts[-1]

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
            return {"success": False, "error": "Sudo prompt timed out during cleanup"}

        if result.returncode != 0:
            err = result.stderr.strip()
            if "User canceled" in err or "canceled" in err.lower():
                return {"success": False, "error": "Sudo canceled. Session still active!"}
            return {"success": False, "error": f"Cleanup failed: {err}"}

        self._remove_state()

        # Clean up PID file
        if os.path.exists(self.pid_file):
            try:
                os.unlink(self.pid_file)
            except OSError:
                pass

        return {"success": True, "domain": domain}

    def cleanup_stale(self) -> dict | None:
        """Check for and clean up stale sessions from previous crashes."""
        if not os.path.exists(self.state_file):
            return None

        status = self.get_status()
        if status.get("active"):
            # Session is active with a running process - leave it
            return {"stale": True, "domain": status.get("domain")}

        # State file exists but process is dead = stale session
        # We'll try to clean up hosts file on next stop_session call
        self._remove_state()
        return {"stale": True, "cleaned": True}

    def force_cleanup(self) -> dict:
        """Emergency cleanup - try to remove hosts entries without sudo.
        Called from signal handlers where osascript may not work.
        """
        # Try to kill server process directly
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass

        self._remove_state()
        return {"success": True, "note": "Server killed. /etc/hosts may need manual cleanup."}

    def _is_process_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, TypeError):
            return False

    def _remove_state(self):
        try:
            os.unlink(self.state_file)
        except OSError:
            pass


def _escape_applescript(s: str) -> str:
    """Escape a string for embedding in AppleScript."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
