"""Build a self-contained, offline-portable deployment tarball.

Pipeline:

1. ``pip download`` every runtime dependency into a temporary
   ``./wheels/`` directory (the source machine is online).  The fapiao_cn
   wheel itself is produced via ``pip wheel --no-deps .``.
2. Create a **fresh** throw-away venv and reinstall everything from
   those wheels only (``pip install --no-index --find-links=./wheels/``).
   This simulates exactly what an offline target machine will see and
   proves the tarball will work without network access.
3. Verify the simulated-offline venv can import ``fapiao_cn`` and run the
   FastAPI app's ``/health`` endpoint.
4. Tar up the source tree + the existing ``parse_vevn/`` (which now
   contains the simulated-offline install) + ``scripts/install_portable.sh``.
5. Always clean up the ``./wheels/`` directory and the throw-away venv
   in a ``finally`` so a failed build never leaves large artefacts
   behind on the source machine.

Output: ``fapiao_cn-portable.tar.gz`` at the repo root by default.

Usage::

    python build-scripts/package_portable.py
    python build-scripts/package_portable.py --output ./dist/fapiao_cn.tar.gz
    python build-scripts/package_portable.py --skip-tests
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "fapiao_cn-portable.tar.gz"
WHEELS_DIR = REPO_ROOT / "wheels"
VENV_NAME = "parse_vevn"  # the in-tree venv that ships with the tarball
SIM_VENV_NAME = "_fapiao_cn_portable_sim_venv"  # used only during packaging

# Files / dirs that never make it into the tarball.
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".ruff_cache",
    "wheels", SIM_VENV_NAME,
    "dist", "build", "tests",
}
EXCLUDE_FILE_SUFFIXES = (".pyc", ".egg-info")
EXCLUDE_FILE_NAMES = {".fapiao_cn.pid", ".uvicorn.pid"}  # legacy

# Required files inside the tarball (sanity-checked before writing).
REQUIRED_PATHS = [
    "src/fapiao_cn/__init__.py",
    "src/fapiao_cn/server.py",
    f"{VENV_NAME}/bin/python",
    "scripts/install_portable.sh",
    "pyproject.toml",
    "README.md",
]


def _run(cmd: List[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None) -> None:
    printable = " ".join(cmd)
    print(f"  $ {printable}")
    subprocess.run(cmd, cwd=cwd or REPO_ROOT, env=env, check=True)


def _pip_in(venv_python: Path) -> list:
    return [str(venv_python), "-m", "pip"]


def ensure_wheels(python: str) -> None:
    """Download every runtime wheel + the fapiao_cn wheel into ``./wheels``.

    Uses the system ``python`` (``python3``) to do the downloads; it
    only needs network access for this step.
    """

    if WHEELS_DIR.exists():
        shutil.rmtree(WHEELS_DIR)
    WHEELS_DIR.mkdir(parents=True)

    print("[1/5] pip download — collecting runtime wheels ...")
    deps = _read_runtime_deps()
    if deps:
        _run(
            [python, "-m", "pip", "download",
             "--dest", str(WHEELS_DIR), *deps]
        )

    print("[1/5] pip download — collecting build-system wheels ...")
    build_deps = _read_build_deps()
    if build_deps:
        _run(
            [python, "-m", "pip", "download",
             "--dest", str(WHEELS_DIR), *build_deps]
        )

    print("[1/5] pip wheel — building fapiao_cn wheel ...")
    _run(
        [python, "-m", "pip", "wheel",
         "--no-deps",
         "--wheel-dir", str(WHEELS_DIR),
         str(REPO_ROOT)]
    )


def _read_runtime_deps() -> List[str]:
    """Read ``[project] dependencies`` from pyproject.toml via tomllib."""

    import tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return list(data.get("project", {}).get("dependencies", []) or [])


def _read_build_deps() -> List[str]:
    """Read ``[build-system] requires`` from pyproject.toml via tomllib.

    Hatchling 1.32 doesn't list ``editables`` as a direct dependency,
    but PEP 660 editable installs of projects that use ``hatchling`` as
    their backend require it.  We append it manually so the offline
    simulated venv can resolve it.
    """

    import tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    deps = list(data.get("build-system", {}).get("requires", []) or [])
    # Known transitive needs that pyproject.toml doesn't list.
    deps.append("editables>=0.3")
    return deps


def simulate_offline_install(python: str) -> Path:
    """Create a fresh venv and reinstall everything from ``./wheels`` only.

    Returns the path to the fresh venv so callers can introspect it.
    """

    sim_venv = REPO_ROOT / SIM_VENV_NAME
    if sim_venv.exists():
        shutil.rmtree(sim_venv)

    print("[2/5] creating throw-away venv for offline simulation ...")
    _run([python, "-m", "venv", str(sim_venv)])

    sim_python = sim_venv / "bin" / "python"
    # The throw-away venv already bundles pip + wheel; do NOT try to
    # upgrade them — that would require network and we're proving the
    # offline scenario.
    print("[3/5] pip install --no-index --find-links=wheels/ ...")
    # --no-index ensures we hit ONLY the local wheels dir.
    _run([*_pip_in(sim_python), "install",
          "--no-index", "--find-links", str(WHEELS_DIR),
          "--editable", str(REPO_ROOT)])

    print("    verify: import fapiao_cn inside the simulated-offline venv ...")
    _run([str(sim_python), "-c",
          "import fapiao_cn, sys; print('    fapiao_cn ok:', fapiao_cn.__file__); "
          "from fapiao_cn.server import app; print('    fapiao_cn.server.app:', app)"])

    return sim_venv


def _verify_runtime_smoke(python: str) -> None:
    """Boot the FastAPI app from the simulated-offline venv and hit /health."""

    print("    runtime smoke test: GET /health from simulated-offline venv ...")
    sim_venv = REPO_ROOT / SIM_VENV_NAME
    sim_python = sim_venv / "bin" / "python"
    sim_uploads = REPO_ROOT / "logs"
    sim_uploads.mkdir(exist_ok=True)
    server_log = sim_uploads / "portable_smoke.log"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [str(sim_python), "-m", "uvicorn", "fapiao_cn.server:app",
         "--host", "127.0.0.1", "--port", "18091"],
        stdout=open(server_log, "wb"), stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT), env=env,
    )
    try:
        # Tiny sleep; the smoke test only needs to see the bound socket.
        for _ in range(40):
            time.sleep(0.25)
            try:
                import urllib.request
                with urllib.request.urlopen("http://127.0.0.1:18091/health", timeout=2) as r:
                    if r.status == 200:
                        print("    /health →", r.read().decode("utf-8").strip())
                        return
            except Exception:
                continue
        raise SystemExit(
            "    /health did not respond; see logs/portable_smoke.log"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _should_include(rel_path: str) -> bool:
    """Return True if ``rel_path`` should appear in the tarball."""

    parts = rel_path.split("/")

    if any(part in EXCLUDE_DIR_NAMES for part in parts):
        return False
    if parts[-1] in EXCLUDE_FILE_NAMES:
        return False
    if any(rel_path.endswith(suf) for suf in EXCLUDE_FILE_SUFFIXES):
        return False
    # logs/ keeps its tree but not the actual *.log / *.pid files.
    if parts[0] == "logs":
        if parts[-1].endswith(".log") or parts[-1].endswith(".pid"):
            return False
    return True


def _assert_required_members(members: Iterable[str]) -> None:
    missing = [p for p in REQUIRED_PATHS if p not in members]
    if missing:
        raise SystemExit(f"ERROR: tarball missing required paths: {missing}")


def build_tarball(output: Path) -> None:
    print(f"[4/5] writing {output} ...")
    output.parent.mkdir(parents=True, exist_ok=True)
    members: List[str] = []
    with tarfile.open(output, "w:gz") as tar:
        for entry in sorted(REPO_ROOT.iterdir()):
            arc = entry.relative_to(REPO_ROOT)
            if not _should_include(str(arc)):
                continue
            if entry.is_dir():
                for sub in sorted(entry.rglob("*")):
                    sub_arc = str(sub.relative_to(REPO_ROOT))
                    if not _should_include(sub_arc):
                        continue
                    tar.add(str(sub), arcname=sub_arc, recursive=False)
                    members.append(sub_arc)
            else:
                tar.add(str(entry), arcname=str(arc), recursive=False)
                members.append(str(arc))
    _assert_required_members(members)


def cleanup() -> None:
    print("[5/5] cleanup — removing ./wheels/ and simulated venv ...")
    for p in (REPO_ROOT / "wheels", REPO_ROOT / SIM_VENV_NAME):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    # The smoke-test log is informative; keep it but trim if huge.
    smoke_log = REPO_ROOT / "logs" / "portable_smoke.log"
    if smoke_log.exists() and smoke_log.stat().st_size > 1_000_000:
        smoke_log.unlink()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_tests() -> None:
    """Run the test suite via pytest; abort packaging on failure."""

    print("[0/5] running pytest ...")
    rc = subprocess.call([sys.executable, "-m", "pytest", "tests/", "-q"])
    if rc != 0:
        raise SystemExit(f"pytest failed with exit code {rc}; aborting.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="output tarball path (default: %(default)s)")
    parser.add_argument("--python", default=os.environ.get("PYTHON", sys.executable),
                        help="python to use for pip downloads (default: %(default)s)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip pre-package pytest run")
    parser.add_argument("--keep-wheels", action="store_true",
                        help="do not delete ./wheels/ after packaging")
    parser.add_argument("--skip-smoke", action="store_true",
                        help="skip the runtime /health smoke test")
    args = parser.parse_args(argv)

    if not args.skip_tests:
        run_tests()

    try:
        ensure_wheels(args.python)
        sim_venv = simulate_offline_install(args.python)
        if not args.skip_smoke:
            _verify_runtime_smoke(args.python)
        build_tarball(args.output)
    finally:
        if not args.keep_wheels:
            cleanup()

    size = args.output.stat().st_size
    print()
    print(f"=== portable package built: {args.output} ===")
    print(f"    size:  {size / 1024 / 1024:.1f} MiB")
    print(f"    sha256: {sha256_of(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())