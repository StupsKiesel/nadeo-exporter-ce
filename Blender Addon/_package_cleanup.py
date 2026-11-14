"""Remove only retired map-import implementation paths from addon installs."""

from pathlib import Path
import shutil


def remove_retired_importers(addon_root: Path) -> list[Path]:
    if not (addon_root / "__init__.py").is_file():
        raise ValueError(f"Not an addon package: {addon_root}")
    removed = []
    for relative in ("_map_import_bridge.py", "gbx_import", "GBXBPY\\map_import"):
        path = addon_root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(addon_root.resolve()):
            raise ValueError(f"Refusing to remove linked retired package: {path}")
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.is_file():
            path.unlink()
            removed.append(path)
    return removed
