"""Tests for the ``fapiao_cn`` CLI control script."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from fapiao_cn import cli as cli_mod


@pytest.fixture
def tmp_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "PID_FILE", str(tmp_path / "logs" / "fapiao_cn.pid"))
    monkeypatch.setattr(cli_mod, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli_mod, "LOG_FILE", str(tmp_path / "logs" / "server.log"))
    return tmp_path


class TestPortAvailable:
    """H20: refuse to start uvicorn when the port is already bound."""

    def test_returns_true_when_port_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # Release the socket and verify the helper sees the port as free.
        assert cli_mod._port_available("127.0.0.1", free_port)

    def test_returns_false_when_port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            busy_port = s.getsockname()[1]
            assert not cli_mod._port_available("127.0.0.1", busy_port)


class TestAtomicPidWrite:
    """H23: PID file written atomically with restrictive perms."""

    def test_writes_with_mode_0600(self, tmp_pid_file):
        cli_mod._atomic_write_pid(4242)
        p = Path(cli_mod.PID_FILE)
        assert p.exists()
        assert p.read_text().strip() == "4242"
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_replaces_existing_file(self, tmp_pid_file):
        cli_mod._atomic_write_pid(1)
        cli_mod._atomic_write_pid(2)
        p = Path(cli_mod.PID_FILE)
        assert p.read_text().strip() == "2"


class TestStartWithPortInUse:
    """H20 integration: cmd_start fails fast when port is taken."""

    def test_cmd_start_returns_1_when_port_busy(self, tmp_pid_file, monkeypatch, capsys):
        # Occupy a port.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        busy_port = s.getsockname()[1]
        try:
            args = type("Args", (), {"host": "127.0.0.1", "port": busy_port})()
            monkeypatch.setattr(cli_mod, "_pid_path", lambda: Path("/nonexistent"))
            rc = cli_mod.cmd_start(args)
            assert rc == 1
            captured = capsys.readouterr()
            assert "already in use" in captured.err
        finally:
            s.close()


class TestPackageRootConstant:
    """H46: PACKAGE_ROOT points at the installed package, not cwd."""

    def test_package_root_is_absolute(self):
        assert cli_mod.PACKAGE_ROOT.is_absolute()

    def test_package_root_contains_src(self):
        assert (cli_mod.PACKAGE_ROOT / "src" / "fapiao_cn").is_dir()
