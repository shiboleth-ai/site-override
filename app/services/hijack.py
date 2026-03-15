"""
Session manager: handles /etc/hosts hijacking, pfctl port forwarding,
and local server lifecycle.

Architecture:
- Server runs as a normal subprocess on ports 8080/8443 (no root needed)
- osascript handles privileged ops: /etc/hosts, pfctl, DNS flush
- pfctl redirects 80→8080 and 443→8443 on loopback
"""

import json
import os
import signal
import subprocess
import sys
import time


HOSTS_MARKER = "# SITE-OVERRIDE-MANAGED"

# pfctl anchor name for our rules
PFCTL_ANCHOR = "site-override"


class SessionManager:
    def __init__(self, state_file: str, pid_file: str):
        self.state_file = state_file
        self.pid_file = pid_file
        self._server_process = None

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

        # Step 1: Start the local server as a normal subprocess (no root needed)
        hijack_server_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hijack_server.py"
        )

        try:
            self._server_process = subprocess.Popen(
                [
                    sys.executable,
                    hijack_server_path,
                    site_dir,
                    cert_path,
                    key_path,
                    self.pid_file,
                ],
                stdout=open(os.path.join(os.path.dirname(self.state_file), "server.log"), "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,  # Detach from parent
            )
        except OSError as e:
            return {"success": False, "error": f"Failed to start server: {e}"}

        # Give server a moment to bind ports
        time.sleep(0.5)

        # Check it's actually running
        if self._server_process.poll() is not None:
            return {
                "success": False,
                "error": "Server failed to start. Check /tmp/site-override-server.log",
            }

        server_pid = self._server_process.pid

        # Step 2: Privileged operations via osascript (single password prompt)
        # - Add /etc/hosts entry
        # - Set up pfctl port forwarding (80→8080, 443→8443)
        # - Flush DNS
        sudo_script = (
            # Add hosts entry
            f'echo "127.0.0.1 {domain} {HOSTS_MARKER}" >> /etc/hosts; '
            # Set up pfctl port forwarding via anchor
            f'echo "'
            f"rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 80 -> 127.0.0.1 port 8080\\n"
            f"rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 443 -> 127.0.0.1 port 8443"
            f'" | pfctl -a "{PFCTL_ANCHOR}" -f - 2>/dev/null; '
            # Enable pfctl (may already be enabled)
            f"pfctl -E 2>/dev/null; "
            # Flush DNS
            f"dscacheutil -flushcache; "
            f"killall -HUP mDNSResponder 2>/dev/null; "
            f'echo "ok"'
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
            # Kill the server we started
            self._kill_server(server_pid)
            return {"success": False, "error": "Sudo prompt timed out"}

        if result.returncode != 0:
            # Kill the server we started
            self._kill_server(server_pid)
            err = result.stderr.strip()
            if "User canceled" in err or "canceled" in err.lower():
                return {"success": False, "error": "Sudo canceled by user"}
            return {"success": False, "error": f"Failed to set up network: {err}"}

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

        # Step 1: Kill the server process (no root needed)
        self._kill_server(pid)

        # Step 2: Privileged cleanup via osascript
        sudo_script = (
            # Remove hosts entry
            f'sed -i "" "/{HOSTS_MARKER}/d" /etc/hosts; '
            # Remove pfctl anchor rules
            f'pfctl -a "{PFCTL_ANCHOR}" -F all 2>/dev/null; '
            # Flush DNS
            f"dscacheutil -flushcache; "
            f"killall -HUP mDNSResponder 2>/dev/null; "
            f'echo "ok"'
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
        # Kill server process (no root needed)
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                self._kill_server(pid)
            except (OSError, ValueError):
                pass

        if self._server_process:
            try:
                self._server_process.terminate()
                self._server_process.wait(timeout=3)
            except Exception:
                try:
                    self._server_process.kill()
                except Exception:
                    pass

        self._remove_state()
        return {
            "success": True,
            "note": "Server killed. /etc/hosts and pfctl may need manual cleanup.",
        }

    def _kill_server(self, pid):
        """Kill a server process by PID."""
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for clean shutdown
            for _ in range(10):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                # Force kill if still running
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        except OSError:
            pass

        # Clean up PID file
        try:
            os.unlink(self.pid_file)
        except OSError:
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
