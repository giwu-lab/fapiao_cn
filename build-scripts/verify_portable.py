"""Verify a built ``fapiao_cn-portable.tar.gz`` without touching production.

The script:

1. Extracts the tarball into a temporary directory.
2. Asserts the in-tree venv can ``import fapiao_cn`` and ``from fapiao_cn.server import app``.
3. Boots ``uvicorn fapiao_cn.server:app`` on a loopback port and curls ``/health``.
4. Always removes the temp directory, even on failure.

Usage::

    python build-scripts/verify_portable.py
    python build-scripts/verify_portable.py --archive ./dist/fapiao_cn-portable.tar.gz
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = REPO_ROOT / "fapiao_cn-portable.tar.gz"


def _read_url(url: str, timeout: float = 2.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8").strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE,
                        help="tarball to verify (default: %(default)s)")
    parser.add_argument("--port", type=int, default=18091,
                        help="loopback port for the smoke-test server")
    args = parser.parse_args(argv)

    if not args.archive.exists():
        print(f"ERROR: archive not found: {args.archive}", file=sys.stderr)
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="fapiao_cn_verify_"))
    try:
        print(f"[1/3] extracting {args.archive} → {tmpdir} ...")
        subprocess.run(
            ["tar", "xzf", str(args.archive), "-C", str(tmpdir)],
            check=True,
        )

        venv_python = tmpdir / "parse_vevn" / "bin" / "python"
        if not venv_python.exists():
            print(f"ERROR: {venv_python} missing", file=sys.stderr)
            return 1

        print("[2/3] import smoke test ...")
        subprocess.run(
            [str(venv_python), "-c",
             "import fapiao_cn; from fapiao_cn.server import app; print('fapiao_cn ok:', fapiao_cn.__file__)"],
            check=True,
        )

        print(f"[3/3] runtime smoke test: GET /health on 127.0.0.1:{args.port} ...")
        log_path = tmpdir / "smoke.log"
        with open(log_path, "wb") as logf:
            proc = subprocess.Popen(
                [str(venv_python), "-m", "uvicorn", "fapiao_cn.server:app",
                 "--host", "127.0.0.1", "--port", str(args.port)],
                stdout=logf, stderr=subprocess.STDOUT,
                cwd=str(tmpdir),
            )
        try:
            for attempt in range(40):
                time.sleep(0.25)
                try:
                    body = _read_url(f"http://127.0.0.1:{args.port}/health")
                    if body:
                        print("    /health →", body)
                        print("PASS — portable tarball is healthy.")
                        return 0
                except (urllib.error.URLError, ConnectionError):
                    continue
            print(f"ERROR: /health did not respond; see {log_path}",
                  file=sys.stderr)
            return 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())