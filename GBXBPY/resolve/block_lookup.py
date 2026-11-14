"""Block-name → ED*.Gbx path resolution.

Maps reference blocks by short name (``"Beach"``, ``"RoadTechStraight"``).
The actual file lives somewhere under
``GameData/<Collection>/GameCtnBlockInfo/<Kind>/<Name>.<Ext>.Gbx`` and the
``<Collection>`` and ``<Kind>`` aren't stored on the block instance — we
have to probe.

Lookup order (first hit wins):
1. The map's collection root (e.g. ``BlueBay``) — when supplied.
2. ``Stadium`` — the default TM2020 collection root.
3. Any other collection roots discovered on disk under ``GameData``.

Each root is searched completely (direct path, then nested folders) before
the next one. Vista collections override Stadium blocks from a nested
folder (``BlueBay/GameCtnBlockInfo/GameCtnBlockInfoClassic/Stadium/
RoadIceStraight.EDClassic.Gbx``); a direct Stadium hit must not win over it.

Within each root, we try every block-info subdirectory in this order:
``Classic``, ``Flat``, ``Frontier``, ``Transition``, ``Pillar``,
``Clip``, ``HorizontalClip``, ``VerticalClip``.

Results are cached on the resolver instance.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional, Tuple

from .. import library

log = logging.getLogger("gbxbpy.resolve.block_lookup")


# (subdir-suffix, file-extension) pairs to probe.
_BLOCK_KINDS: Tuple[Tuple[str, str], ...] = (
    ("GameCtnBlockInfoClassic", "EDClassic"),
    ("GameCtnBlockInfoFlat", "EDFlat"),
    ("GameCtnBlockInfoFrontier", "EDFrontier"),
    ("GameCtnBlockInfoTransition", "EDTransition"),
    # Pillar block-infos use the .EDClassic extension on disk, even though
    # they live under the GameCtnBlockInfoPillar/ subdir.
    ("GameCtnBlockInfoPillar", "EDClassic"),
)

_CLIP_KINDS: Tuple[Tuple[str, str], ...] = (
    ("GameCtnBlockInfoClip", "EDClip"),
    ("GameCtnBlockInfoClip", "EDHorizontalClip"),
    ("GameCtnBlockInfoClip", "EDVerticalClip"),
)


# Some collections used in maps don't match the on-disk folder name 1:1.
# In particular, the TM2020 collection ``Stadium2020`` lives under
# ``GameData/Stadium`` — the same folder as legacy stadium content.
_COLLECTION_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("Stadium2020", "Stadium"),
)


def _aliases_for(collection: str) -> List[str]:
    """Return ``collection`` plus any known on-disk aliases for it."""
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
    """Return absolute paths of likely collection roots, preferred first."""
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
    # Discover further collection roots that contain a GameCtnBlockInfo dir.
    try:
        for name in sorted(library.listdir(gamedata_dir), key=str.casefold):
            if library.isdir(os.path.join(gamedata_dir, name, "GameCtnBlockInfo")):
                add(name)
    except OSError:
        pass
    return seen


def find_block_file(
    block_name: str,
    *,
    gamedata_dir: str,
    collection: str = "",
    is_clip: bool = False,
) -> Optional[str]:
    """Locate the ED*.Gbx file for ``block_name`` on disk.

    Args:
        block_name: e.g. ``"Beach"``, ``"RoadTechStraight"``.
        gamedata_dir: GameData root.
        collection: Map's ``decoration_collection`` (preferred root).
        is_clip: When True, search clip kinds instead of block kinds.

    Returns:
        Absolute path or ``None`` when no file matches.
    """
    return BlockResolver(gamedata_dir, collection=collection).find(block_name, is_clip=is_clip)


class BlockResolver:
    """Cached block-name → file-path lookup.

    Use this from map-import code to avoid hammering the filesystem when
    a map references the same block hundreds of times.
    """

    def __init__(self, gamedata_dir: str, *, collection: str = "") -> None:
        self.gamedata_dir = gamedata_dir
        self.collection = collection
        self._cache: dict[Tuple[str, bool], Optional[str]] = {}
        # Lazily-built lowercase-filename → absolute-path index per
        # (root, subdir). Walking ``GameCtnBlockInfo/<subdir>`` once is
        # cheaper than `os.walk` per miss.
        self._index: dict[Tuple[str, str], dict[str, str]] = {}

    def _index_for(self, root: str, subdir: str) -> dict[str, str]:
        key = (root, subdir)
        if key in self._index:
            return self._index[key]
        out: dict[str, str] = {}
        qualified: dict[str, str] = {}
        base = os.path.join(root, "GameCtnBlockInfo", subdir)
        if library.isdir(base):
            for dirpath, _dirs, filenames in library.walk(base):
                folder = os.path.relpath(dirpath, base).split(os.sep)[0]
                for fn in filenames:
                    path = os.path.join(dirpath, fn)
                    out.setdefault(fn.lower(), path)
                    # Baked vista IDs include the nested collection prefix:
                    # StadiumStructurePillarToFlatACB -> Stadium/StructurePillarToFlatACB.
                    if folder != ".":
                        qualified.setdefault((folder + fn).lower(), path)
        for name, path in qualified.items():
            out.setdefault(name, path)
        self._index[key] = out
        return out

    def _find_in_root(
        self, root: str, block_name: str, kinds: Tuple[Tuple[str, str], ...]
    ) -> Optional[str]:
        # Fast direct hit first, then nested folders (Theme/, Stadium/).
        for subdir, ext in kinds:
            direct = os.path.join(
                root, "GameCtnBlockInfo", subdir, f"{block_name}.{ext}.Gbx"
            )
            if library.exists(direct):
                return direct
        # Subdir and extension are not always in lock-step on disk — e.g.
        # ``*.EDClassic.Gbx`` files can sit inside ``GameCtnBlockInfoPillar``.
        # The indexed walk tries every extension against every subdir.
        exts = [ext for _sd, ext in kinds]
        subdirs: list[str] = []
        for sd, _ext in kinds:
            if sd not in subdirs:
                subdirs.append(sd)
        lname = block_name.lower()
        for subdir in subdirs:
            idx = self._index_for(root, subdir)
            for ext in exts:
                hit = idx.get(f"{lname}.{ext.lower()}.gbx")
                if hit:
                    return hit
        return None

    def candidates(self, block_name: str, *, is_clip: bool = False) -> List[str]:
        """One definition per collection root that has ``block_name``, preferred first.

        Vista collections override Stadium blocks, but the overrides can
        reference geometry absent from a GameData extract; callers use the
        later entries as fallbacks.
        """
        if not block_name:
            return []
        roots = _candidate_collection_roots(
            self.gamedata_dir, preferred=_aliases_for(self.collection)
        )
        kinds = _CLIP_KINDS if is_clip else _BLOCK_KINDS
        found = (self._find_in_root(root, block_name, kinds) for root in roots)
        return [path for path in found if path]

    def find(self, block_name: str, *, is_clip: bool = False) -> Optional[str]:
        key = (block_name, is_clip)
        if key in self._cache:
            return self._cache[key]
        hit = None
        if block_name:
            roots = _candidate_collection_roots(
                self.gamedata_dir, preferred=_aliases_for(self.collection)
            )
            kinds = _CLIP_KINDS if is_clip else _BLOCK_KINDS
            for root in roots:
                hit = self._find_in_root(root, block_name, kinds)
                if hit:
                    break
        self._cache[key] = hit
        return hit

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<BlockResolver gamedata={self.gamedata_dir!r} "
            f"collection={self.collection!r} cached={len(self._cache)}>"
        )

    def prewarm(self, *, include_clips: bool = True) -> None:
        """Eagerly build the on-disk index for every collection root.

        After this returns, ``find()`` only pays the cost of a dict
        lookup per miss — no ``os.walk`` per call. Useful before a
        large map import where thousands of names will be resolved.
        """
        preferred = _aliases_for(self.collection)
        roots = _candidate_collection_roots(
            self.gamedata_dir, preferred=preferred
        )
        kinds = list(_BLOCK_KINDS)
        if include_clips:
            kinds += list(_CLIP_KINDS)
        seen: set[tuple[str, str]] = set()
        for root in roots:
            for subdir, _ext in kinds:
                if (root, subdir) in seen:
                    continue
                seen.add((root, subdir))
                self._index_for(root, subdir)


__all__ = ["BlockResolver", "find_block_file"]
