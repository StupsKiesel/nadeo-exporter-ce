"""Dependency checks and installation using Blender's bundled Python."""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
import subprocess
import sys
from functools import lru_cache

log = logging.getLogger("trackmania_blender_bridge.dependencies")

# Import name, pip distribution name.
DEPENDENCIES = (
    ("construct", "construct"),
    ("gbx.lzo", "gbx-py"),
    ("PIL", "Pillow"),
    ("numpy", "numpy"),
)

restart_required = False


@lru_cache(maxsize=1)
def missing_dependencies() -> tuple[str, ...]:
    missing = []
    for module, package in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except (ImportError, OSError) as exc:
            log.warning("Dependency %s unavailable: %s", package, exc)
            missing.append(package)
    return tuple(missing)


def python_executable() -> str:
    """Do not use sys.executable blindly: Blender can return blender.exe."""
    prefix = Path(sys.prefix)
    candidates = (
        prefix / "bin" / "python.exe",
        prefix / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}",
        prefix / "bin" / "python3",
        prefix / "python.exe",
        Path(sys.executable),
    )
    for candidate in candidates:
        if candidate.name.lower().startswith("python") and candidate.is_file():
            return str(candidate)
    raise RuntimeError(f"Cannot locate Blender's Python interpreter under {prefix}")


def _run(command: list[str]) -> subprocess.CompletedProcess:
    log.info("Running dependency command: %s", subprocess.list2cmdline(command))
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        log.info("%s", result.stdout.rstrip())
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Dependency command failed (exit {result.returncode}): {detail}"
        )
    if result.stderr:
        log.warning("%s", result.stderr.rstrip())
    return result


def install(target_dir: str) -> None:
    """Install only unavailable packages, then verify in a fresh interpreter."""
    global restart_required
    packages = missing_dependencies()
    if not packages:
        return
    python = python_executable()
    os.makedirs(target_dir, exist_ok=True)
    try:
        _run([python, "-m", "pip", "--version"])
    except RuntimeError:
        log.warning("pip unavailable; bootstrapping it with ensurepip")
        _run([python, "-m", "ensurepip", "--user"])
    _run([
        python, "-m", "pip", "--isolated", "install",
        "--disable-pip-version-check", "--no-input",
        "--target", target_dir, *packages,
    ])
    # Avoid reloading binary packages or partially imported modules in Blender.
    modules = [module for module, _package in DEPENDENCIES]
    _run([
        python, "-c",
        "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
        "[importlib.import_module(name) for name in sys.argv[2:]]",
        target_dir, *modules,
    ])
    restart_required = True
