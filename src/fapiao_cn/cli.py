"""``fapiao_cn`` console script — lifecycle management for the HTTP service.

* ``fapiao_cn start``    — launch uvicorn in the background
* ``fapiao_cn stop``     — kill the background process (PID file ``logs/fapiao_cn.pid``)
* ``fapiao_cn restart``  — stop + start
* ``fapiao_cn dev``      — foreground uvicorn with auto-reload
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
from pathlib import Path
from typing import List, Optional

from fapiao_cn.config import settings

PID_FILE = "logs/fapiao_cn.pid"
LOG_DIR = "logs"
LOG_FILE = "logs/server.log"
LOG_CONFIG = "deploy/log_config.yaml"
APP_TARGET = "fapiao_cn.server:app"
# H46: run uvicorn with a fixed cwd (the package root) regardless of
# where the user invoked ``fapiao_cn start`` from.  This makes logs /
# sample lookups deterministic.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


def _pid_path() -> Path:
    return Path(PID_FILE)


def _read_pid() -> Optional[int]:
    p = _pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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


def cmd_start(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"[fapiao_cn] already running (pid={pid})")
        return 0
    # H20: bail out early if the chosen port is already taken.
    if not _port_available(args.host, args.port):
        print(
            f"[fapiao_cn] port {args.port} on {args.host} is already in use; "
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
        args.host,
        "--port",
        str(args.port),
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
    print(f"[fapiao_cn] started pid={proc.pid} on {args.host}:{args.port}")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("[fapiao_cn] no pid file")
        return 0
    if not _alive(pid):
        print(f"[fapiao_cn] pid {pid} not alive; cleaning pid file")
        _pid_path().unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _pid_path().unlink(missing_ok=True)
    print(f"[fapiao_cn] stopped pid={pid}")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    return cmd_start(args)


def cmd_dev(args: argparse.Namespace) -> int:
    # H46: dev mode also pins cwd so log_config.yaml is found.
    os.chdir(PACKAGE_ROOT)
    import uvicorn

    uvicorn.run(
        APP_TARGET,
        host=args.host,
        port=args.port,
        reload=True,
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

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default=settings.host, help="bind host")
        p.add_argument("--port", type=int, default=settings.port, help="bind port")

    for name, fn, help_text, has_endpoint in [
        ("start", cmd_start, "launch uvicorn in the background", True),
        ("stop", cmd_stop, "kill the background process", False),
        ("restart", cmd_restart, "stop + start", True),
        ("dev", cmd_dev, "foreground uvicorn with auto-reload", True),
        ("test", cmd_test, "run pytest", False),
        ("status", cmd_status, "show background-process state", False),
    ]:
        p = sub.add_parser(name, help=help_text)
        if has_endpoint:
            _common(p)
        p.set_defaults(func=fn)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
