"""Tests for the ``fapiao_cn`` CLI control script."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fapiao_cn import cli as cli_mod

#: 这些用例依赖 POSIX 的进程组语义（``os.killpg`` / ``start_new_session``
#: 派生的进程组 / 子进程里的 ``signal.signal(SIGTERM, ...)``）。Windows 上
#: ``os.killpg`` 根本不存在，SIGTERM 也不会走到子进程的 handler，因此
#: 整套「信号 → 进程组 → 端口释放」的时序在 Windows 上无法复现。
#: Windows 的进程管理走 ``scripts/start-win.ps1``（从 TCP 监听表反查真实 PID）。
posix_only = pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="POSIX-only: needs os.killpg / process-group signal semantics",
)

#: 需要 ``/proc``（Linux 特有）才能验证的用例：PID 身份校验靠
#: ``/proc/<pid>/cmdline`` 判断进程归属。
linux_only = pytest.mark.skipif(
    not Path("/proc/self").exists(),
    reason="Linux-only: needs /proc to inspect a process' cmdline",
)


@pytest.fixture
def tmp_pid_file(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod, "PID_FILE", str(log_dir / "fapiao_cn.pid"))
    monkeypatch.setattr(cli_mod, "ENDPOINT_FILE", str(log_dir / "fapiao_cn.endpoint"))
    monkeypatch.setattr(cli_mod, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(cli_mod, "LOG_FILE", str(log_dir / "server.log"))
    monkeypatch.setattr(cli_mod, "DEV_LOG_FILE", str(log_dir / "server-dev.log"))
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

    @posix_only
    def test_writes_with_mode_0600(self, tmp_pid_file):
        # POSIX-only: Windows 的 st_mode 表达不了 0600（永远报 0666），
        # Windows 上的进程管理走 scripts/start-win.ps1。
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


class TestCmdDev:
    """``fapiao_cn dev`` 与 ``start`` 平行：写 PID + 后台 uvicorn + dev 日志。

    Popen 被 mock 掉，避免实际拉起 uvicorn 子进程——只断言 cmd_dev
    调用的参数、PID 写入、日志路径创建。
    """

    def test_dev_log_path_separate_from_start_log(self):
        """DEV_LOG_FILE 与 LOG_FILE 是两个不同的路径——避免 dev/prod
        互相覆盖日志。"""

        assert cli_mod.DEV_LOG_FILE != cli_mod.LOG_FILE
        assert cli_mod.DEV_LOG_FILE.endswith("server-dev.log")
        assert cli_mod.LOG_FILE.endswith("server.log")

    def test_cmd_dev_returns_1_when_port_busy(self, tmp_pid_file, monkeypatch, capsys):
        """H20：dev 模式也必须端口预检。"""

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        busy_port = s.getsockname()[1]
        try:
            args = type("Args", (), {"host": "127.0.0.1", "port": busy_port})()
            monkeypatch.setattr(cli_mod, "_pid_path", lambda: Path("/nonexistent"))
            rc = cli_mod.cmd_dev(args)
            assert rc == 1
            captured = capsys.readouterr()
            assert "already in use" in captured.err
        finally:
            s.close()

    def test_cmd_dev_writes_pid_and_dev_log(
        self, tmp_pid_file, monkeypatch, capsys
    ):
        """启动 dev：写 PID 到 PID_FILE、写日志路径 DEV_LOG_FILE。

        Popen 被替换为 fake，断言 cmd 参数正确（--reload、cwd、PACKAGE_ROOT）
        + PID 写入 + dev 日志父目录创建。
        """

        # Pick a free port to avoid the H20 short-circuit.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        # Fake Popen so we don't actually spawn a server.
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **kw: fake_proc)

        # _pid_path() must return the patched PID_FILE.
        monkeypatch.setattr(
            cli_mod, "_pid_path", lambda: Path(cli_mod.PID_FILE)
        )

        args = type("Args", (), {"host": "127.0.0.1", "port": free_port})()
        rc = cli_mod.cmd_dev(args)

        assert rc == 0
        # PID file must reflect fake_proc.pid
        pid_text = Path(cli_mod.PID_FILE).read_text().strip()
        assert pid_text == "99999"
        # DEV_LOG_FILE must be created (parent dir + file handle opened in "ab" mode)
        dev_log = Path(cli_mod.DEV_LOG_FILE)
        assert dev_log.parent.is_dir()
        # Log file may or may not be flushed yet (file opened with buffering=0);
        # existence isn't guaranteed without write, so check parent only.

        captured = capsys.readouterr()
        assert "dev started pid=99999" in captured.out
        assert cli_mod.DEV_LOG_FILE in captured.out

    def test_cmd_dev_passes_reload_and_cwd_to_popen(
        self, tmp_pid_file, monkeypatch
    ):
        """cmd_dev 必须把 ``--reload`` 和 cwd=PACKAGE_ROOT 传给 Popen，
        否则自动重载行为不会生效。"""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        captured_kwargs: dict = {}
        captured_args: list = []

        def fake_popen(cmd, **kwargs):
            captured_args.extend(cmd)
            captured_kwargs.update(kwargs)
            p = MagicMock()
            p.pid = 12345
            return p

        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cli_mod, "_pid_path", lambda: Path(cli_mod.PID_FILE))

        args = type("Args", (), {"host": "127.0.0.1", "port": free_port})()
        cli_mod.cmd_dev(args)

        assert "--reload" in captured_args, captured_args
        assert "--host" in captured_args and "127.0.0.1" in captured_args
        assert "--port" in captured_args and str(free_port) in captured_args
        # cwd must be pinned to PACKAGE_ROOT (H46).
        assert captured_kwargs.get("cwd") == str(cli_mod.PACKAGE_ROOT)
        # start_new_session must be True so the server is detached from
        # the CLI shell (so `fapiao_cn dev` returns immediately).
        assert captured_kwargs.get("start_new_session") is True
        # stdout/stderr must be redirected to the dev log file (not the
        # caller's terminal).
        stdout_arg = captured_kwargs.get("stdout")
        assert hasattr(stdout_arg, "write"), (
            f"stdout should be the dev log file handle, got {stdout_arg!r}"
        )

    def test_cmd_dev_refuses_when_already_running(
        self, tmp_pid_file, monkeypatch, capsys
    ):
        """如果 PID_FILE 已存在且进程存活，cmd_dev 必须直接返回（不重启）。

        与 cmd_start 行为一致——避免误杀用户的进程。
        """

        # Pretend a long-running process is alive.
        monkeypatch.setattr(cli_mod, "_read_pid", lambda: 54321)
        monkeypatch.setattr(cli_mod, "_alive", lambda _pid: True)

        args = type("Args", (), {"host": "127.0.0.1", "port": 8091})()
        rc = cli_mod.cmd_dev(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "already running" in captured.out


class TestSignalProcessFallback:
    """``_signal_process`` 必须在没有进程组的平台上优雅降级。

    ``os.killpg`` 只存在于 POSIX；在 Windows 上直接调用会抛
    ``AttributeError``，把 ``fapiao_cn stop`` / ``restart`` 整个带崩
    （改动前用的是 ``os.kill``，在 Windows 上能正常终止进程）。
    """

    def test_falls_back_to_os_kill_without_killpg(self, monkeypatch):
        calls = []
        monkeypatch.delattr(cli_mod.os, "killpg", raising=False)
        monkeypatch.setattr(
            cli_mod.os, "kill", lambda pid, sig: calls.append((pid, sig))
        )
        assert cli_mod._signal_process(1234, signal.SIGTERM) is True
        assert calls == [(1234, signal.SIGTERM)]

    def test_returns_false_when_target_is_gone(self, monkeypatch):
        def boom(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.delattr(cli_mod.os, "killpg", raising=False)
        monkeypatch.setattr(cli_mod.os, "kill", boom)
        assert cli_mod._signal_process(1234, signal.SIGTERM) is False

    def test_sigkill_constant_is_valid_on_this_platform(self):
        """``signal.SIGKILL`` 在 Windows 上不存在，模块常量必须已兜底。"""

        assert hasattr(signal, cli_mod._SIGKILL.name)


class TestWaitPidGone:
    """``_wait_pid_gone`` 必须等进程真退出（含 SIGKILL 升级），不能立刻返回。"""

    def test_returns_true_when_pid_already_dead(self):
        # Choose a PID that's guaranteed not to be alive.
        gone = cli_mod._wait_pid_gone(2**22, grace_seconds=0.5)
        assert gone is True

    @posix_only
    def test_signals_subprocess_and_waits(self, tmp_pid_file):
        """SIGTERM 一个会自杀的子进程，断言 wait 返回 True 且进程真退出。

        子进程注册 SIGTERM handler → 收到 SIGTERM 立即 sys.exit(0)。
        容器/沙箱里 SIGTERM 传递可能有 0.1–1s 抖动，所以只断言结果、
        不断言 elapsed 上限（之前的 3.0s 上限在容器内可能误报）。
        """

        proc = subprocess.Popen(
            ["python3", "-c",
             "import signal, sys, time; "
             "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
             "time.sleep(30)"],
            start_new_session=True,
        )
        try:
            gone = cli_mod._wait_pid_gone(proc.pid, grace_seconds=8.0)
            assert gone is True
            # 进程必须真的死了（SIGKILL 升级兜底确保）
            assert not cli_mod._alive(proc.pid)
        finally:
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass

    @posix_only
    def test_escalates_to_sigkill_when_ignoring_sigterm(self, tmp_pid_file):
        """子进程忽略 SIGTERM 时，必须升级到 SIGKILL 才能结束。

        在容器内 SIGTERM 传递可能慢，但只要升级到 SIGKILL 就一定能杀掉；
        所以仅断言 gone=True + proc 真死，不强求 elapsed >= grace_seconds
        （部分容器 SIGTERM 在 grace 内也能杀，elapsed 可能 < 2s）。
        """

        # signal.signal(SIGTERM, SIG_IGN) 忽略 SIGTERM。
        proc = subprocess.Popen(
            ["python3", "-c",
             "import signal, time; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "time.sleep(30)"],
            start_new_session=True,
        )
        try:
            gone = cli_mod._wait_pid_gone(proc.pid, grace_seconds=2.0)
            assert gone is True
            assert not cli_mod._alive(proc.pid)
        finally:
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass


class TestWaitPortFree:
    """``_wait_port_free`` 在端口被占时必须等到空闲，超时返回 False。"""

    def test_returns_immediately_when_port_is_free(self):
        # 找一个空闲端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # 端口在我们关掉 socket 后应该立即可绑
        free = cli_mod._wait_port_free("127.0.0.1", port, timeout=1.0)
        assert free is True

    def test_waits_until_port_released(self):
        """进程占住端口，几秒后释放；wait 应该等到释放为止。"""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            # s 还占着
            start = time.monotonic()
            try:
                # 在子线程里 0.5s 后关 socket
                import threading
                def releaser():
                    time.sleep(0.5)
                    s.close()
                threading.Thread(target=releaser, daemon=True).start()

                free = cli_mod._wait_port_free("127.0.0.1", port, timeout=2.0)
                elapsed = time.monotonic() - start
                assert free is True
                # 应该等到 0.5s 后才返回，不是立即也不是等到超时
                assert 0.4 < elapsed < 1.5, f"elapsed={elapsed:.2f}s"
            finally:
                try:
                    s.close()
                except OSError:
                    pass


class TestCmdStopWaitsForPortRelease:
    """``_wait_port_free`` 必须在端口释放前一直等待，超时返回 False。

    通过子进程持有端口 → SIGTERM 子进程 → 子进程关 socket → 端口释放
    来真实模拟"cmd_stop 发信号后 socket 释放需要时间"。
    """

    def _popen_port_holder(self, port: int) -> subprocess.Popen:
        """启一个子进程，绑定 (127.0.0.1, port)，响应 SIGTERM 后 exit。

        子进程脚本：
        1. bind 端口 + listen
        2. signal.signal(SIGTERM, lambda *_: sys.exit(0))
        3. sleep 无限长——SIGTERM 触发 exit
        """
        # 注：SO_REUSEADDR 让重启时 TIME_WAIT 状态也可绑——但我们的 wait
        # 测试目标是 TIME_WAIT 之外的实际端口占用，所以不用 SO_REUSEADDR。
        script = (
            "import socket, signal, sys, time;"
            "srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM);"
            "srv.bind(('127.0.0.1', %d));"
            "srv.listen(1);"
            "signal.signal(signal.SIGTERM, lambda *_: (srv.close(), sys.exit(0)));"
            "time.sleep(30)"
        ) % port
        return subprocess.Popen(
            ["python3", "-c", script],
            start_new_session=True,
        )

    @posix_only
    def test_returns_false_when_port_still_busy(self, tmp_pid_file):
        """端口被占且不会释放：wait 在 timeout 内必须返回 False。"""

        # 找一个空闲端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()

        proc = self._popen_port_holder(port)
        try:
            # 等子进程绑好端口
            time.sleep(0.3)

            start = time.monotonic()
            rc = cli_mod._wait_port_free("127.0.0.1", port, timeout=1.0)
            elapsed = time.monotonic() - start
            assert rc is False
            # 必须等够 timeout 才能返回
            assert elapsed >= 0.9, f"returned in {elapsed:.2f}s"
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    @posix_only
    def test_waits_until_port_released_by_sigterm(self, tmp_pid_file):
        """子进程占端口；0.3s 后 SIGTERM 它，wait 应该等到释放后返回 True。"""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()

        proc = self._popen_port_holder(port)
        try:
            time.sleep(0.2)  # 让子进程 bind 好

            # 0.3s 后 SIGTERM 子进程
            def killer():
                time.sleep(0.3)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
            import threading
            threading.Thread(target=killer, daemon=True).start()

            start = time.monotonic()
            rc = cli_mod._wait_port_free("127.0.0.1", port, timeout=2.0)
            elapsed = time.monotonic() - start
            assert rc is True
            # 必须等到 SIGTERM 后端口释放才能返回
            assert 0.25 <= elapsed <= 1.5, f"elapsed={elapsed:.2f}s"
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def test_cmd_stop_clears_stale_pid_file(self, tmp_pid_file, capsys):
        """PID 文件存在但进程已死时，cmd_stop 必须清理 PID 文件并返回 0。"""

        Path(cli_mod.PID_FILE).write_text("99999\n")  # 不存在的 pid
        args = type("Args", (), {})()
        rc = cli_mod.cmd_stop(args)
        assert rc == 0
        assert not Path(cli_mod.PID_FILE).exists()
        captured = capsys.readouterr()
        assert "not alive" in captured.out

    def test_cmd_stop_returns_0_when_no_pid_file(self, tmp_pid_file, capsys):
        """无 PID 文件：cmd_stop 不报错，直接返回 0。"""

        args = type("Args", (), {})()
        rc = cli_mod.cmd_stop(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "no pid file" in captured.out


class TestCmdRestartSequencing:
    """``cmd_restart`` = stop + start；stop 阻塞等待端口释放，start 才能成功。"""

    @posix_only
    def test_restart_succeeds_when_previous_server_held_port(
        self, tmp_pid_file, monkeypatch
    ):
        """回归：之前 stop 立刻返回 + start 端口检查 → 重启失败。

        修复后 cmd_stop 同步等待 → restart 全程一次成功。
        """

        # 模拟一个 uvicorn 子进程持有端口
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        Path(cli_mod.PID_FILE).write_text(f"{proc.pid}\n")

        # 让 _wait_pid_gone 一被调用就立刻让子进程退出 + 释放端口
        # （monkeypatch 真实路径——捕获 os.killpg 调用，避免 monkeypatch
        # 自身递归）
        real_killpg = os.killpg
        def fake_killpg(pid, sig):
            real_killpg(pid, sig)  # 不是 cli_mod.os.killpg，避免 monkeypatch 递归
            # 给 socket 释放留一点点时间
            time.sleep(0.3)

        monkeypatch.setattr(cli_mod.os, "killpg", fake_killpg)

        # 找一个空闲端口给 restart 用
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        # 用 monkeypatch 把 subprocess.Popen 替换掉，避免真拉起 uvicorn
        # 但要让 cmd_start 看到端口可绑
        fake_proc = MagicMock()
        fake_proc.pid = 88888

        popen_calls = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append((cmd, kwargs))
            return fake_proc

        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cli_mod, "_pid_path", lambda: Path(cli_mod.PID_FILE))

        # 让 cmd_start 看到 port 可用
        monkeypatch.setattr(cli_mod, "_port_available", lambda h, p: True)

        args = type("Args", (), {"host": "127.0.0.1", "port": free_port})()

        # 必须先把占住的端口释放（s 关掉）
        s.close()

        # restart 应该成功
        rc = cli_mod.cmd_restart(args)
        assert rc == 0
        assert popen_calls, "Popen should have been called by cmd_start"

        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


class TestEndpointPersistence:
    """``start`` / ``dev`` 必须把实际绑定的 host:port 落盘。

    ``stop`` 要等待「真正被占用」的那个 socket 被释放；如果 PID 文件只
    记 PID、不记端口，stop 只能去等 settings 的默认端口，
    ``start --port 9000`` 之后的 EADDRINUSE 竞态就还在。
    """

    def test_cmd_start_writes_endpoint(self, tmp_pid_file, monkeypatch):
        popen_calls = []

        class FakeProc:
            pid = 4242

        def fake_popen(cmd, **kwargs):
            popen_calls.append((cmd, kwargs))
            return FakeProc()

        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cli_mod, "_port_available", lambda h, p: True)

        args = type("Args", (), {"host": "127.0.0.1", "port": 9000})()
        assert cli_mod.cmd_start(args) == 0
        assert cli_mod._read_endpoint() == ("127.0.0.1", 9000)
        # 绑定参数必须原样传给 uvicorn
        assert "9000" in popen_calls[0][0]

    def test_cmd_start_writes_default_endpoint(self, tmp_pid_file, monkeypatch):
        class FakeProc:
            pid = 4242

        monkeypatch.setattr(
            cli_mod.subprocess, "Popen", lambda cmd, **kwargs: FakeProc()
        )
        monkeypatch.setattr(cli_mod, "_port_available", lambda h, p: True)

        args = type("Args", (), {"host": None, "port": None})()
        assert cli_mod.cmd_start(args) == 0
        assert cli_mod._read_endpoint() == (cli_mod.settings.host, cli_mod.settings.port)


class TestResolveBind:
    """``_resolve_bind`` 的优先级：命令行 > 端点文件 > settings。"""

    def test_endpoint_file_wins_over_settings(self, tmp_pid_file):
        cli_mod._write_endpoint("127.0.0.1", 9000)
        args = type("Args", (), {"host": None, "port": None})()
        assert cli_mod._resolve_bind(args) == ("127.0.0.1", 9000)

    def test_explicit_flag_wins_over_endpoint_file(self, tmp_pid_file):
        cli_mod._write_endpoint("127.0.0.1", 9000)
        args = type("Args", (), {"host": "0.0.0.0", "port": 7777})()
        assert cli_mod._resolve_bind(args) == ("0.0.0.0", 7777)

    def test_falls_back_to_settings(self, tmp_pid_file):
        args = type("Args", (), {"host": None, "port": None})()
        assert cli_mod._resolve_bind(args) == (cli_mod.settings.host, cli_mod.settings.port)

    def test_missing_endpoint_file_is_not_fatal(self, tmp_pid_file):
        assert cli_mod._read_endpoint() is None


class TestPidIdentityCheck:
    """PID 文件可能指向已被系统回收的 PID —— 不能对着陌生进程组发信号。

    ``os.killpg`` 会连带干掉整个进程组；如果那个 PID 已经被系统分配给
    别的程序，误杀范围就从「一个进程」扩大到「一个进程组」。
    """

    @linux_only
    def test_rejects_foreign_process(self):
        # pytest 自己的 cmdline 里没有 uvicorn
        assert cli_mod._pid_is_our_server(os.getpid()) is False

    @linux_only
    def test_accepts_uvicorn_cmdline(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)", "uvicorn"]
        )
        try:
            assert cli_mod._pid_is_our_server(proc.pid) is True
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_unreadable_pid_passes(self):
        # 不存在的 PID：/proc/<pid>/cmdline 读不到 -> 无法判定 -> 放行
        # （与改动前的行为一致：Windows 上没有 /proc，无法做身份校验）
        assert cli_mod._pid_is_our_server(2**22) is True

    @linux_only
    def test_cmd_stop_refuses_foreign_pid(self, tmp_pid_file, capsys):
        """PID 文件指向陌生进程时 cmd_stop 必须拒绝发信号（返回 1）。"""
        Path(cli_mod.PID_FILE).write_text(f"{os.getpid()}\n")
        args = type("Args", (), {"host": None, "port": None})()
        rc = cli_mod.cmd_stop(args)
        assert rc == 1
        assert "refusing to signal" in capsys.readouterr().err
