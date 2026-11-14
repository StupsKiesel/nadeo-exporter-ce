"""Material short-name → ``.Material.Gbx`` path resolution.

TM2020 mesh material bindings often store only the bare stem of a
material (``"RoadTech"``, ``"LightSpot"``) in the ``.link`` chunk — not
a GameData-relative path. :class:`MaterialResolver` walks each
collection's ``Media/Material`` tree once and indexes files by their
lowercased stem, so subsequent lookups are O(1).

Lookup order (first hit wins):
1. The map's collection root (e.g. ``BlueBay``) — when supplied.
2. ``Stadium`` — the default TM2020 collection root.
3. Any other collection roots discovered on disk under ``GameData``.
4. A stem declared in ``GameData\\MaterialLib_<Collection>.txt`` but missing
   from disk and the game index is requested from the plugin by its exact
   ``<Collection>\\Media\\Material`` path.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List, Optional, Tuple

from .. import library

log = logging.getLogger("gbxbpy.resolve.material_lookup")


_MATERIAL_EXT = ".Material.Gbx"
_MEDIA_MATERIAL_SUBDIR = os.path.join("Media", "Material")
_DECLARED_MATERIAL = re.compile(r"^\s*DMaterial\(\s*([^)\s]+)\s*\)", re.MULTILINE)


# Mirror block_lookup's collection aliases.
_COLLECTION_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("Stadium2020", "Stadium"),
)


def _aliases_for(collection: str) -> List[str]:
    if not collection:
        return []
    out = [collection]
    for src, dst in _COLLECTION_ALIASES:
        if collection == src and dst not in out:
            out.append(dst)
        elif collection == dst and src not in out:
            out.append(src)
    return out


def _candidate_collection_roots(
    gamedata_dir: str,
    *,
    preferred: Iterable[str] = (),
) -> List[str]:
    if not gamedata_dir or not library.isdir(gamedata_dir):
        return []
    seen: List[str] = []
    seen_lower: set[str] = set()

    def add(name: str) -> None:
        if not name:
            return
        candidate = os.path.join(gamedata_dir, name)
        if not library.isdir(candidate):
            return
        key = candidate.lower()
        if key in seen_lower:
            return
        seen.append(candidate)
        seen_lower.add(key)

    for name in preferred:
        add(name)
    add("Stadium")
    try:
        for name in sorted(library.listdir(gamedata_dir), key=str.casefold):
            if library.isdir(os.path.join(gamedata_dir, name, _MEDIA_MATERIAL_SUBDIR)):
                add(name)
    except OSError:
        pass
    return seen


class MaterialResolver:
    """Cached material short-name → file-path lookup.

    Args:
        gamedata_dir: GameData root.
        collection: Map's collection name (preferred root).
    """

    def __init__(self, gamedata_dir: str, *, collection: str = "") -> None:
        self.gamedata_dir = gamedata_dir
        self.collection = collection
        self._cache: dict[str, Optional[str]] = {}
        # root -> { lowercased-stem -> abs-path }
        self._index: dict[str, dict[str, str]] = {}
        # root -> { lowercased-stem -> MaterialLib name }
        self._declared: dict[str, dict[str, str]] = {}

    def _index_for(self, root: str) -> dict[str, str]:
        if root in self._index:
            return self._index[root]
        out: dict[str, str] = {}
        base = os.path.join(root, _MEDIA_MATERIAL_SUBDIR)
        if library.isdir(base):
            ext_lower = _MATERIAL_EXT.lower()
            for dirpath, _dirs, filenames in library.walk(base):
                for fn in filenames:
                    fl = fn.lower()
                    if fl.endswith(ext_lower):
                        stem = fn[: -len(_MATERIAL_EXT)].lower()
                        out.setdefault(stem, os.path.join(dirpath, fn))
        self._index[root] = out
        return out

    def find(self, material_name: str) -> Optional[str]:
        """Return the absolute path of ``<name>.Material.Gbx``, or ``None``.

        ``material_name`` may be a bare stem (``"RoadTech"``), a name
        with extension (``"RoadTech.Material.Gbx"``), or a path
        containing slashes — only the basename's stem is used.
        """
        if not material_name:
            return None
        # Reduce to a bare stem.
        base = os.path.basename(material_name.replace("\\", "/"))
        stem = base
        low = stem.lower()
        if low.endswith(_MATERIAL_EXT.lower()):
            stem = stem[: -len(_MATERIAL_EXT)]
        else:
            # Strip any other extension (.gbx alone, etc.)
            dot = stem.find(".")
            if dot > 0:
                stem = stem[:dot]
        key = stem.lower()
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        preferred = _aliases_for(self.collection)
        roots = _candidate_collection_roots(
            self.gamedata_dir, preferred=preferred
        )
        for root in roots:
            idx = self._index_for(root)
            hit = idx.get(key)
            if hit:
                self._cache[key] = hit
                return hit
        # The plugin's game index omits files from packs the game has not
        # mounted yet; a material the collection's MaterialLib declares
        # can still be extracted by its exact path.
        for root in roots:
            name = self._declared_for(root).get(key)
            if not name:
                continue
            path = os.path.join(root, _MEDIA_MATERIAL_SUBDIR, name + _MATERIAL_EXT)
            if library.ensure(path):
                self._index_for(root).setdefault(key, path)
                self._cache[key] = path
                return path
        self._cache[key] = None
        return None

    def _declared_for(self, root: str) -> dict[str, str]:
        """``{lower stem: name}`` from ``GameData\\MaterialLib_<Collection>.txt``."""
        cached = self._declared.get(root)
        if cached is not None:
            return cached
        out: dict[str, str] = {}
        lib = os.path.join(self.gamedata_dir, f"MaterialLib_{os.path.basename(root)}.txt")
        try:
            if library.isfile(lib):
                with open(lib, encoding="utf-8", errors="replace") as handle:
                    for match in _DECLARED_MATERIAL.finditer(handle.read()):
                        out.setdefault(match.group(1).lower(), match.group(1))
        except OSError:
            pass
        self._declared[root] = out
        return out

    def prewarm(self) -> None:
        """Eagerly build the on-disk index for every collection root."""
        preferred = _aliases_for(self.collection)
        roots = _candidate_collection_roots(
            self.gamedata_dir, preferred=preferred
        )
        for root in roots:
            self._index_for(root)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<MaterialResolver gamedata={self.gamedata_dir!r} "
            f"collection={self.collection!r} cached={len(self._cache)}>"
        )


__all__ = ["MaterialResolver"]
