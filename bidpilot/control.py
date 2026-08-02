from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import UTC, datetime
from typing import Any

from bidpilot.config import Settings
from bidpilot.private_files import harden_private_path


class ControlPlane:
    """Local token and runtime state used for safe cross-platform lifecycle commands."""

    def __init__(self, settings: Settings):
        self.token_path = settings.control_dir / "secrets" / "control.token"
        self.state_path = settings.control_dir / "runtime" / "server.json"

    def ensure_token(self) -> str:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.token_path.parent, directory=True)
        if self.token_path.exists():
            harden_private_path(self.token_path, directory=False)
            return self.token_path.read_text(encoding="utf-8").strip()
        token = secrets.token_urlsafe(48)
        try:
            with self.token_path.open("x", encoding="utf-8") as handle:
                handle.write(token)
        except FileExistsError:
            token = self.token_path.read_text(encoding="utf-8").strip()
        harden_private_path(self.token_path, directory=False)
        return token

    def read_token(self) -> str | None:
        if not self.token_path.exists():
            return None
        harden_private_path(self.token_path, directory=False)
        token = self.token_path.read_text(encoding="utf-8").strip()
        return token or None

    def verify(self, candidate: str | None) -> bool:
        expected = self.read_token()
        return bool(expected and candidate and secrets.compare_digest(expected, candidate))

    def write_state(self, *, host: str, port: int, version: str) -> dict[str, Any]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "started_at": datetime.now(UTC).isoformat(),
            "version": version,
        }
        # The runtime record is also the cross-platform single-instance claim.
        # Exclusive creation makes concurrent ``serve`` commands deterministic:
        # the winner owns lifecycle control and later starters cannot orphan it.
        with self.state_path.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return state

    def read_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                return None
            state["port"] = int(state["port"])
            state["pid"] = int(state["pid"])
            return state
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return None

    def clear_state(self, pid: int | None = None) -> None:
        state = self.read_state()
        if pid is not None:
            # A fenced cleanup may only remove a claim that can still be parsed and
            # belongs to that exact process.  Treat a partial or corrupted file as
            # unknown ownership instead of racing a newly starting replacement.
            if not state or state.get("pid") != pid:
                return
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            # A stale diagnostics file is safer than deleting an unrelated path or process.
            pass

    @staticmethod
    def process_is_alive(pid: int) -> bool:
        """Conservatively check whether a runtime-claim owner still exists."""
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # Access denied means the process may still exist. Keeping the claim is safer
                # than letting a second server race an owner we cannot inspect.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def local_control_url(host: str, port: int) -> str:
    normalized = host.strip()
    if normalized in {"::", "[::]"}:
        connect_host = "[::1]"
    elif normalized in {"0.0.0.0", "localhost"}:
        connect_host = "127.0.0.1"
    else:
        connect_host = normalized
    if ":" in connect_host and not connect_host.startswith("["):
        connect_host = f"[{connect_host}]"
    return f"http://{connect_host}:{port}"
