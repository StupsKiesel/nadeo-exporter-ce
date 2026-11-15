"""Build a Blender addon zip including NICE, GBXBPY, GBXMAT and gpuui.

Usage:
    python build.py
    python build.py --release
    python build.py --release --pdf

Output:
    build/nadeo_exporter_ce_<version>_<timestamp>.zip
    dist/nadeo_exporter_ce_<version>.zip (--release, no live install)
    dist/Nadeo_Exporter_CE_User_Guide_<version>.pdf (--release --pdf)

PDF generation requires Markdown from requirements-dev.txt and Chrome,
Edge or Chromium. ZIP-only builds need only the Python standard library.
"""

from __future__ import annotations

import argparse
from html import escape
import os
import importlib.util
import re
import shutil
import subprocess
import tempfile
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


def render_readme_pdf(output_dir: Path, version: str) -> Path:
    """Render the current README and its local images as a separate user guide."""
    try:
        import markdown
    except ModuleNotFoundError as exc:
        if exc.name != "markdown":
            raise
        raise RuntimeError(
            "PDF generation requires Markdown; install requirements-dev.txt first."
        ) from exc

    candidates = []
    for name in ("chrome", "msedge", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for directory in (
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ):
        candidates.extend((
            Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(directory) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ))
    browser = next((path for path in candidates if path.is_file()), None)
    if browser is None:
        raise RuntimeError("PDF generation requires Chrome, Edge or Chromium on PATH.")

    source = (ROOT / "README.md").read_text(encoding="utf-8")
    source = re.sub(
        r"^> \[!([A-Z]+)\]",
        lambda match: f"> **{match[1].title()}**",
        source, flags=re.MULTILINE,
    )
    source = re.sub(r"</?details(?:\s[^>]*)?>", "", source)
    source = re.sub(r"<summary>(.*?)</summary>", r"\n**\1**\n", source)
    body = markdown.markdown(source, extensions=["extra", "toc", "sane_lists"])

    def image_uri(match: re.Match[str]) -> str:
        image = ROOT / match[1]
        if not image.is_file():
            raise RuntimeError(f"README image does not exist: {image}")
        return f'src="{escape(image.resolve().as_uri(), quote=True)}"'

    body = re.sub(r'src="([^"]+)"', image_uri, body)
    css = """
@page { size: A4; margin: 17mm 16mm 18mm; }
* { box-sizing: border-box; }
body { font: 10pt/1.45 "Segoe UI", Arial, sans-serif; color: #202936; margin: 0; }
h1, h2, h3 { color: #12385a; line-height: 1.2; break-after: avoid; }
h1 { font-size: 25pt; margin: 0 0 6mm; }
h2 { font-size: 17pt; margin: 8mm 0 3mm; border-bottom: 1px solid #c7d7e6; padding-bottom: 2mm; }
h3 { font-size: 12pt; margin: 5mm 0 2mm; }
p { margin: 2.4mm 0; orphans: 3; widows: 3; }
ul, ol { padding-left: 6mm; margin: 2.5mm 0; }
li { margin: 1mm 0; }
a { color: #145b92; text-decoration: none; overflow-wrap: anywhere; }
img { display: block; max-width: 100%; max-height: 170mm; width: auto; height: auto;
      margin: 4mm auto; object-fit: contain; break-inside: avoid; }
blockquote { border-left: 3px solid #4b83ad; background: #f0f5fa; margin: 4mm 0;
             padding: 2mm 4mm; break-inside: avoid; }
table { width: 100%; border-collapse: collapse; margin: 4mm 0; font-size: 9pt; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td { border: 1px solid #cdd7e1; padding: 2mm 2.5mm; vertical-align: top; }
th { background: #e9f1f8; text-align: left; }
td:first-child { min-width: 28mm; }
code { font: 8.8pt Consolas, monospace; background: #eef2f6; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; padding: 3mm; background: #eef2f6; break-inside: avoid; }
hr { border: 0; border-top: 1px solid #cdd7e1; margin: 6mm 0; }
"""
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"Nadeo_Exporter_CE_User_Guide_{version}.pdf"
    with tempfile.TemporaryDirectory(prefix="nadeo-readme-", dir=output_dir) as folder:
        temporary = Path(folder)
        html_path = temporary / "readme.html"
        temporary_pdf = temporary / "guide.pdf"
        html_path.write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f"<title>Nadeo Exporter CE - User Guide {escape(version)}</title>"
            f"<style>{css}</style></head><body>{body}</body></html>",
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(browser), "--headless", "--disable-gpu", "--disable-extensions",
                "--disable-background-networking", "--no-first-run",
                "--no-default-browser-check", "--no-pdf-header-footer",
                f"--user-data-dir={temporary / 'browser-profile'}",
                f"--print-to-pdf={temporary_pdf}", html_path.as_uri(),
            ],
            check=True, capture_output=True, timeout=120,
        )
        if not temporary_pdf.is_file():
            raise RuntimeError("The browser did not produce a README PDF.")
        data = temporary_pdf.read_bytes()
        if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
            raise RuntimeError("The browser produced an invalid README PDF.")
        os.replace(temporary_pdf, pdf_path)
    print(f"Built {pdf_path}")
    print(f"Size: {pdf_path.stat().st_size / 1024:.1f} KB")
    return pdf_path


def build(*, release: bool = False, pdf: bool = False) -> Path:
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

    if pdf:
        render_readme_pdf(output_dir, version)
    if not release:
        install_to_blender(files)
    return zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release", action="store_true",
        help="Build a version-only archive in dist without installing into Blender.",
    )
    parser.add_argument(
        "--pdf", action="store_true",
        help="Render the current README as a separate PDF alongside the addon ZIP.",
    )
    args = parser.parse_args()
    build(release=args.release, pdf=args.pdf)