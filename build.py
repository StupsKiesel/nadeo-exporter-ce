"""Build a Blender addon zip including NICE, GBXBPY, GBXMAT and gpuui.

Usage:
    python build.py
    python build.py --release

Output:
    build/nadeo_exporter_ce_<version>_<timestamp>.zip
    dist/nadeo_exporter_ce_<version>.zip (--release, no live install)
"""

from __future__ import annotations

import argparse
import os
import importlib.util
import re
import shutil
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ADDON_SOURCE_DIR = ROOT / "Blender Addon"
NICE_DIR = ROOT / "NICE"
GBXBPY_DIR = ROOT / "GBXBPY"
GBXMAT_DIR = ROOT / "GBXMAT"
GPUUI_DIR = ROOT / "gpuui"
OPENPLANET_PLUGIN_DIR = ROOT / "Openplanet Plugin"
BUILD_DIR = ROOT / "build"
RELEASE_DIR = ROOT / "dist"

# This becomes the addon package directory name inside the zip.
ADDON_PACKAGE = "trackmania_blender_bridge"
ARCHIVE_PREFIX = "nadeo_exporter_ce"


def get_version() -> str:
    """Read the addon version from bl_info in the addon __init__.py."""
    init_file = ADDON_SOURCE_DIR / "__init__.py"
    text = init_file.read_text(encoding="utf-8")
    match = re.search(r'"version"\s*:\s*\((\d+),\s*(\d+),\s*(\d+)\)', text)
    if not match:
        raise RuntimeError("Could not find bl_info version tuple in Blender Addon/__init__.py")
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def should_include(path: Path, base_dir: Path) -> bool:
    """Return whether a file should be included in the addon zip."""
    rel = path.relative_to(base_dir)

    for part in rel.parts:
        if part in {"__pycache__", "debugLiveLogs"}:
            return False
        if part.startswith("."):
            return False

    if path.suffix.lower() in {".pyc", ".pyo", ".log"}:
        return False

    return True


def collect_tree(source_dir: Path, archive_root: Path) -> list[tuple[Path, str]]:
    """Collect files from one source tree and map them to archive paths."""
    if not source_dir.is_dir():
        raise RuntimeError(f"Required source directory does not exist: {source_dir}")

    files: list[tuple[Path, str]] = []
    for path in source_dir.rglob("*"):
        if not path.is_file():
            continue
        if not should_include(path, source_dir):
            continue
        archive_path = archive_root / path.relative_to(source_dir)
        files.append((path, str(archive_path).replace("\\", "/")))
    return files


def collect_files() -> list[tuple[Path, str]]:
    """Collect all files that belong in the addon zip."""
    files: list[tuple[Path, str]] = []

    # Addon package root (__init__.py, future addon modules, etc.)
    files.extend(collect_tree(ADDON_SOURCE_DIR, Path(ADDON_PACKAGE)))

    # Bundled local modules used by the addon.
    files.extend(collect_tree(NICE_DIR, Path(ADDON_PACKAGE) / "NICE"))
    files.extend(collect_tree(GBXBPY_DIR, Path(ADDON_PACKAGE) / "GBXBPY"))
    files.extend(collect_tree(GBXMAT_DIR, Path(ADDON_PACKAGE) / "GBXMAT"))
    files.extend(collect_tree(GPUUI_DIR, Path(ADDON_PACKAGE) / "gpuui"))
    # Installed into OpenplanetNext/Plugins from the addon preferences.
    files.extend(collect_tree(OPENPLANET_PLUGIN_DIR, Path(ADDON_PACKAGE) / "openplanet_plugin"))

    return sorted(files, key=lambda item: item[1])


def build_id() -> str:
    """Return a UTC timestamp suitable for artifact names."""
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def blender_addon_install_dir() -> Path | None:
    """Return the installed addon directory under %APPDATA%, if present."""
    appdata = Path(os.environ.get("APPDATA", ""))
    blender_root = appdata / "Blender Foundation" / "Blender"
    if not blender_root.is_dir():
        return None

    for version_dir in sorted(blender_root.iterdir(), reverse=True):
        candidate = version_dir / "scripts" / "addons" / ADDON_PACKAGE
        if candidate.is_dir():
            return candidate

    return None


def install_to_blender(files: list[tuple[Path, str]]) -> None:
    """Mirror the built addon contents into Blender's installed addon dir."""
    dest_base = blender_addon_install_dir()
    if dest_base is None:
        print("Blender addon install directory not found; skipping live install.")
        return

    copied = 0
    for src, arc in files:
        rel = Path(arc).relative_to(ADDON_PACKAGE)
        dest = dest_base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1

    cleanup_path = ADDON_SOURCE_DIR / "_package_cleanup.py"
    spec = importlib.util.spec_from_file_location("addon_package_cleanup", cleanup_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load addon package cleanup: {cleanup_path}")
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    for path in cleanup.remove_retired_importers(dest_base):
        print(f"Removed retired map importer: {path}")

    print(f"Installed {copied} files to {dest_base}")


def build(*, release: bool = False) -> Path:
    """Create the addon zip and optionally mirror it into Blender's addon dir."""
    if not (ADDON_SOURCE_DIR / "__init__.py").is_file():
        raise RuntimeError("Blender Addon/__init__.py is missing")

    version = get_version()
    suffix = "" if release else f"_{build_id()}"
    output_dir = RELEASE_DIR if release else BUILD_DIR
    zip_path = output_dir / f"{ARCHIVE_PREFIX}_{version}{suffix}.zip"

    output_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files()
    if not files:
        raise RuntimeError("No files collected for addon build")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for src, arc in files:
            archive.write(src, arc)

    print(f"Built {zip_path}")
    print(f"Files: {len(files)}")
    print(f"Size: {zip_path.stat().st_size / 1024:.1f} KB")

    if not release:
        install_to_blender(files)
    return zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release", action="store_true",
        help="Build a version-only archive in dist without installing into Blender.",
    )
    args = parser.parse_args()
    build(release=args.release)