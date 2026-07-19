from __future__ import annotations

import json
import os
import secrets
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
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)
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
        if pid is not None and state and state.get("pid") != pid:
            return
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            # A stale diagnostics file is safer than deleting an unrelated path or process.
            pass


def local_control_url(host: str, port: int) -> str:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]", "localhost"} else host
    if ":" in connect_host and not connect_host.startswith("["):
        connect_host = f"[{connect_host}]"
    return f"http://{connect_host}:{port}"
