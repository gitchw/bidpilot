from __future__ import annotations

import os
import stat

from bidpilot.private_files import _windows_acl_command, harden_private_path


def test_windows_acl_command_removes_broad_reader_groups(tmp_path):
    path = tmp_path / "secret.key"
    command = _windows_acl_command(path, directory=False, sid="S-1-5-21-1234")

    assert command[:5] == [
        "icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-21-1234:F",
    ]
    assert "*S-1-1-0" in command
    assert "*S-1-5-11" in command
    assert "*S-1-5-32-545" in command


def test_harden_private_path_keeps_secret_readable_by_current_process(tmp_path):
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret = secret_dir / "secret.key"
    secret.write_text("not-a-real-secret", encoding="utf-8")

    harden_private_path(secret_dir, directory=True)
    harden_private_path(secret, directory=False)

    assert secret.read_text(encoding="utf-8") == "not-a-real-secret"
    if os.name != "nt":
        assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
