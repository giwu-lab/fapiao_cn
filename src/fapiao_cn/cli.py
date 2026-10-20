"""``fapiao_cn`` console script — lifecycle management for the HTTP service.

* ``fapiao_cn start``    — launch uvicorn in the background (logs ``logs/server.log``)
* ``fapiao_cn stop``     — kill the background process (PID file ``logs/fapiao_cn.pid``)
* ``fapiao_cn restart``  — stop + start
* ``fapiao_cn dev``      — background uvicorn with auto-reload (logs ``logs/server-dev.log``)
* ``fapiao_cn test``     — run ``pytest``
* ``fapiao_cn status``   — show whether the background process is alive

The control-script is meant for **developer / CI** usage where the venv
is already installed (``pip install -e .``).  Production deployments use
the Docker image (``docker compose up -d``) which embeds its own restart
policy / healthcheck — no in-process supervisor needed (H28).
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from fapiao_cn.config import settings

PID_FILE = "logs/fapiao_cn.pid"
#: 记录 daemon 实际绑定的 host:port —— ``start --port 9000`` 之后
#: ``FC_PORT`` / settings 里的默认值并不是真正占用的端口。
ENDPOINT_FILE = "logs/fapiao_cn.endpoint"
LOG_DIR = "logs"
LOG_FILE = "logs/server.log"
DEV_LOG_FILE = "logs/server-dev.log"
LOG_CONFIG = "deploy/log_config.yaml"
APP_TARGET = "fapiao_cn.server:app"
# H46: run uvicorn with a fixed cwd (the package root) regardless of
# where the user invoked ``fapiao_cn start`` from.  This makes logs /
# sample lookups deterministic.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

#: ``signal.SIGKILL`` is POSIX-only — it does not exist on Windows, where
#: referencing it raises ``AttributeError``.  There, ``os.kill(pid, SIGTERM)``
#: maps to ``TerminateProcess``, which is an unconditional kill anyway, so
#: falling back to SIGTERM keeps the escalation path working.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _pid_path() -> Path:
    return Path(PID_FILE)


def _endpoint_path() -> Path:
    return Path(ENDPOINT_FILE)


def _read_pid() -> Optional[int]:
    p = _pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    """True iff the PID exists **and** is not in zombie state.

    ``os.kill(pid, 0)`` returns success on zombie processes (the PID is
    still in the process table until the parent reaps it via ``wait()``),
    so a naive kill(0) check would let ``cmd_stop`` think the daemon is
    alive long after SIGTERM actually killed it.  Read ``/proc/PID/status``
    to detect the ``Z`` state explicitly.  This is Linux-only; on other
    platforms (e.g. macOS dev) the kill(0) result alone is used.
    """

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # Linux: zombie state == dead-from-reaper's-perspective.
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line.split()
    except (OSError, IOError):
        # /proc not readable (macOS / sandboxed): fall back to kill(0).
        return True
    return True


def _port_available(host: str, port: int) -> bool:
    """H20: probe a (host, port) pair via SO_REUSEADDR-free bind.

    Returns True when the bind succeeds (port free) and False when the
    OS rejects with EADDRINUSE.  Other errors (permission, network) are
    also reported as "unavailable" so the caller can exit cleanly
    instead of starting a doomed uvicorn.
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def _atomic_write_pid(pid: int) -> None:
    """H23: write the PID file atomically with mode 0600.

    Other users must not be able to read or signal the daemon's PID,
    and a partial read by another tool must not produce garbage.
    """

    pid_path = _pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".fapiao_cn.pid.", dir=str(pid_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{pid}\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, pid_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (mode 0600, same protocol as
    the PID file)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_endpoint(host: str, port: int) -> None:
    """Persist the (host, port) the daemon was actually bound to.

    ``start`` / ``dev`` accept ``--host`` / ``--port``, so the port in
    ``FC_PORT`` / ``settings.port`` is not necessarily the port in use.
    ``stop`` has to wait for the *real* socket to be released, otherwise
    the next ``start`` still races into EADDRINUSE.
    """

    _atomic_write_text(_endpoint_path(), f"{host}:{port}\n")


def _read_endpoint() -> Optional[Tuple[str, int]]:
    """Read back the (host, port) recorded by :func:`_write_endpoint`."""

    path = _endpoint_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    host, _, port = raw.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return None


def _resolve_bind(args: argparse.Namespace) -> Tuple[str, int]:
    """Resolve the (host, port) the daemon is bound to.

    Precedence: explicit ``--host`` / ``--port`` > the endpoint recorded
    at start time > ``FC_HOST`` / ``FC_PORT`` (already folded into
    ``settings``) > the built-in defaults.
    """

    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if host is None or port is None:
        saved = _read_endpoint()
        if saved is not None:
            host = host or saved[0]
            port = port or saved[1]
    host = host or settings.host
    port = int(port if port is not None else settings.port)
    return host, port


def _pid_is_our_server(pid: int) -> bool:
    """Best-effort check that ``pid`` really is our uvicorn process.

    The PID file is plain text and can outlive the process it names.  If
    the OS has already recycled that PID, ``_alive(pid)`` is True for an
    unrelated program and ``os.killpg`` would take down a whole *foreign*
    process group.  On Linux we can read ``/proc/<pid>/cmdline`` and check
    that it looks like our server.  Where /proc is unavailable (Windows,
    macOS) we cannot verify, so the check passes and the caller proceeds
    as before.
    """

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True  # /proc not readable -> cannot judge, keep old behaviour
    cmdline = raw.replace(b"\0", b" ").decode("utf-8", errors="ignore")
    return "uvicorn" in cmdline or "fapiao_cn" in cmdline


def _signal_process(pid: int, sig: int) -> bool:
    """Signal ``pid``'s process group; fall back to the process itself.

    ``os.killpg`` is POSIX-only — it does not exist on Windows at all, so
    calling it unguarded raises ``AttributeError`` and takes the whole CLI
    down (``fapiao_cn stop`` / ``restart`` would die with a traceback
    instead of stopping the server).  Windows has no process groups, so
    there we signal the single process instead: ``os.kill(pid, SIGTERM)``
    maps to ``TerminateProcess`` there.

    Returns True when a signal was actually delivered, False when the
    target was already gone / not ours / the signal could not be sent.
    """

    killpg = getattr(os, "killpg", None)
    if killpg is None:
        # No process groups on this platform (Windows): signal the process.
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False
    try:
        killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        # Already dead, no permission (foreign pid), or not a process-group
        # leader — the caller falls through to the liveness probe.
        return False


def _wait_pid_gone(pid: int, *, grace_seconds: float = 10.0, poll_interval: float = 0.1) -> bool:
    """Wait until ``pid`` no longer exists, with a hard deadline.

    Sends ``SIGTERM`` first (whole process group, so uvicorn's reloader
    child + worker both get the signal), then polls ``os.kill(pid, 0)``
    every ``poll_interval`` seconds.  If the process is still alive after
    ``grace_seconds``, escalates to ``SIGKILL`` and waits another
    ``grace_seconds / 2`` before giving up.

    Returns True when the process is gone, False when the deadline was
    exceeded.
    """

    deadline = time.monotonic() + grace_seconds
    _signal_process(pid, signal.SIGTERM)

    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(poll_interval)

    # Grace expired — escalate to SIGKILL with a short extra window.
    if not _signal_process(pid, _SIGKILL):
        return True
    kill_deadline = time.monotonic() + max(0.5, grace_seconds / 2)
    while time.monotonic() < kill_deadline:
        if not _alive(pid):
            return True
        time.sleep(poll_interval)
    return not _alive(pid)


def _wait_port_free(
    host: str, port: int, *, timeout: float = 5.0, poll_interval: float = 0.1
) -> bool:
    """Poll ``(host, port)`` until the bind succeeds or ``timeout`` elapses.

    After SIGTERM the kernel needs a moment to release the listening
    socket (TIME_WAIT etc.); without this probe ``cmd_start`` would
    race the kernel and bail out with EADDRINUSE on the very next call.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_available(host, port):
            return True
        time.sleep(poll_interval)
    return _port_available(host, port)


def cmd_start(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"[fapiao_cn] already running (pid={pid})")
        return 0
    host, port = _resolve_bind(args)
    # H20: bail out early if the chosen port is already taken.
    if not _port_available(host, port):
        print(
            f"[fapiao_cn] port {port} on {host} is already in use; "
            "refusing to start uvicorn",
            file=sys.stderr,
        )
        return 1
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab", buffering=0)
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP_TARGET,
        "--host",
        host,
        "--port",
        str(port),
    ]
    log_config_path = Path(LOG_CONFIG)
    if log_config_path.exists():
        cmd.extend(["--log-config", str(log_config_path)])
    # H46: explicit cwd so log paths inside the app resolve consistently.
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(PACKAGE_ROOT),
    )
    _atomic_write_pid(proc.pid)
    _write_endpoint(host, port)
    print(f"[fapiao_cn] started pid={proc.pid} on {host}:{port}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Terminate the background process synchronously.

    Sends SIGTERM to the whole process group (uvicorn reloader + worker
    when running ``cmd_dev``), waits for the process to exit (with a
    SIGKILL fallback), then waits for the listening socket to be
    released so the next ``start`` / ``dev`` won't race on EADDRINUSE.

    The PID file is removed **last** so a crash here leaves the PID
    intact for a forensic ``fapiao_cn status`` follow-up.
    """

    pid = _read_pid()
    if pid is None:
        print("[fapiao_cn] no pid file")
        return 0
    if not _alive(pid):
        print(f"[fapiao_cn] pid {pid} not alive; cleaning pid file")
        _pid_path().unlink(missing_ok=True)
        _endpoint_path().unlink(missing_ok=True)
        return 0

    # 端口释放的等待必须打在**真正被占用**的那个 socket 上。``start`` /
    # ``dev`` 可以用 ``--port`` 绑到与 FC_PORT / settings 不同的端口，
    # 所以优先用启动时落盘的 endpoint，命令行参数可以覆盖它。
    host, port = _resolve_bind(args)

    # PID 文件是纯文本，可能比它指向的进程活得更久。PID 被系统回收给
    # 别的程序时，os.killpg 会连带干掉一个毫不相干的进程组 —— 先确认
    # 这个 PID 确实是我们的 uvicorn。
    if not _pid_is_our_server(pid):
        print(
            f"[fapiao_cn] pid {pid} does not look like a fapiao_cn/uvicorn "
            "process; refusing to signal it. Remove "
            f"{_pid_path()} manually if this is a stale pid file.",
            file=sys.stderr,
        )
        return 1

    gone = _wait_pid_gone(pid, grace_seconds=10.0)
    port_free = _wait_port_free(host, port, timeout=5.0)
    _pid_path().unlink(missing_ok=True)
    _endpoint_path().unlink(missing_ok=True)

    if not gone:
        print(
            f"[fapiao_cn] pid {pid} did not exit within 10s "
            "(SIGKILL sent); proceeding",
            file=sys.stderr,
        )
    elif not port_free:
        print(
            f"[fapiao_cn] stopped pid={pid} but port {port} still busy "
            "(next start will bail)",
            file=sys.stderr,
        )
    else:
        print(f"[fapiao_cn] stopped pid={pid}")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    """Stop then start, in that order.  ``cmd_stop`` blocks until the
    socket is actually released, so ``cmd_start``'s port probe will
    succeed."""

    # Restart must come back up on the port the running instance actually
    # used — not on the settings default.  ``_resolve_bind`` reads the
    # endpoint recorded at start time.
    host, port = _resolve_bind(args)
    args.host, args.port = host, port
    stop_rc = cmd_stop(args)
    if stop_rc != 0:
        return stop_rc
    return cmd_start(args)


def cmd_dev(args: argparse.Namespace) -> int:
    """Background uvicorn with --reload; logs to ``logs/server-dev.log``.

    Mirrors ``cmd_start`` so ``stop`` / ``restart`` / ``status`` work
    uniformly regardless of which mode started the process: same PID
    file, same port-availability preflight, same ``start_new_session``.
    """

    pid = _read_pid()
    if pid and _alive(pid):
        print(f"[fapiao_cn] already running (pid={pid})")
        return 0
    host, port = _resolve_bind(args)
    # H20: bail out early if the chosen port is already taken (same
    # guard as cmd_start).
    if not _port_available(host, port):
        print(
            f"[fapiao_cn] port {port} on {host} is already in use; "
            "refusing to start uvicorn",
            file=sys.stderr,
        )
        return 1
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    log = open(DEV_LOG_FILE, "ab", buffering=0)
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP_TARGET,
        "--host",
        host,
        "--port",
        str(port),
        "--reload",
    ]
    log_config_path = Path(LOG_CONFIG)
    if log_config_path.exists():
        cmd.extend(["--log-config", str(log_config_path)])
    # H46: explicit cwd so log paths inside the app resolve consistently.
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(PACKAGE_ROOT),
    )
    _atomic_write_pid(proc.pid)
    _write_endpoint(host, port)
    print(
        f"[fapiao_cn] dev started pid={proc.pid} on {host}:{port} "
        f"→ {DEV_LOG_FILE}"
    )
    return 0


def cmd_test(_args: argparse.Namespace) -> int:
    import pytest

    return pytest.main(["tests/", "-v"])


def cmd_status(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("[fapiao_cn] not running (no pid file)")
        return 1
    if _alive(pid):
        print(f"[fapiao_cn] running pid={pid}")
        return 0
    print(f"[fapiao_cn] pid {pid} not alive (stale pid file)")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fapiao_cn",
        description="fapiao_cn — electronic invoice field extractor (HTTP service).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    for name, fn, help_text, bind_args in [
        ("start", cmd_start, "launch uvicorn in the background", "with_default"),
        ("stop", cmd_stop, "kill the background process", "optional"),
        ("restart", cmd_restart, "stop + start", "with_default"),
        ("dev", cmd_dev, "background uvicorn with auto-reload", "with_default"),
        ("test", cmd_test, "run pytest", "none"),
        ("status", cmd_status, "show background-process state", "none"),
    ]:
        p = sub.add_parser(name, help=help_text)
        if bind_args == "with_default":
            # default=None（而不是 settings.host）：显式参数与「用户没传」
            # 必须可区分，stop / restart 才知道该用端点文件里记录的端口。
            p.add_argument("--host", default=None, help="bind host")
            p.add_argument("--port", type=int, default=None, help="bind port")
        elif bind_args == "optional":
            p.add_argument(
                "--host",
                default=None,
                help="bind host (default: the host the server was started with)",
            )
            p.add_argument(
                "--port",
                type=int,
                default=None,
                help="bind port (default: the port the server was started with)",
            )
        p.set_defaults(func=fn)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
