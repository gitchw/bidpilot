from __future__ import annotations

import csv
import io
import os
import stat
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

_HARDENED_PATHS: set[tuple[str, bool]] = set()
_HARDEN_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _windows_current_user_sid() -> str:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if result.returncode != 0 or not rows or len(rows[0]) < 2 or not rows[0][1].strip():
        raise PermissionError("无法识别当前 Windows 用户，不能安全创建本机密钥文件")
    return rows[0][1].strip()


def _windows_acl_command(path: Path, *, directory: bool, sid: str) -> list[str]:
    permission = f"*{sid}:{'(OI)(CI)' if directory else ''}F"
    return [
        "icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        permission,
        "/remove:g",
        "*S-1-1-0",  # Everyone
        "*S-1-5-11",  # Authenticated Users
        "*S-1-5-32-545",  # Built-in Users
    ]


def harden_private_path(path: Path, *, directory: bool) -> None:
    """Restrict a local secret path to the current user on Windows and POSIX."""
    resolved = path.resolve()
    cache_key = (str(resolved), directory)
    with _HARDEN_LOCK:
        if cache_key in _HARDENED_PATHS:
            return
        if not resolved.exists():
            raise FileNotFoundError(resolved)
        if os.name == "nt":
            result = subprocess.run(
                _windows_acl_command(
                    resolved,
                    directory=directory,
                    sid=_windows_current_user_sid(),
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip()
                raise PermissionError(f"无法收紧本机私密路径权限：{resolved}；{message}")
        else:
            resolved.chmod(stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR)
        _HARDENED_PATHS.add(cache_key)
