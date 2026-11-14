"""Material-modifier folder resolution.

A *Material Modifier* (``CPlugGameSkinAndFolder`` — class id ``0x0915D000``)
is a tiny ``.Modifier.Gbx`` file referenced by blocks/items via the
``materialModifier`` / ``materialModifier2`` node refs. Its body chunk
``0x0915D000`` carries a single string field, ``RemapFolder``, which
points at a sibling folder full of skinned ``.Material.Gbx`` files.

When a block has a modifier, the engine looks for each material it would
normally load (e.g. ``Stadium/Media/Material/SpecialFXTurbo.Material.Gbx``)
inside the remap folder *first*; the override has precedence over the
default material. Only materials listed by the modifier's ``Remapping``
GameSkin are redirected: ``TrackWallToDecoCliff`` remaps ``TrackWall`` but
leaves ``PlatformTech`` alone even though ``PlatformGrass\\`` contains it.

This module exposes two helpers:

    :func:`resolve_modifier_folder` — given a GameData-relative modifier
    path and a :class:`~GBXBPY.resolve.Resolver`, parse the modifier
    file via NICE and return the :pyattr:`RemapFolder` string (an empty
    string when the file is missing, unparseable, or carries no folder).

    :func:`resolve_modifier_materials` — the remapped material stems, or
    ``None`` when the GameSkin could not be read.

Results are cached on the resolver via ``_modifier_folder_cache`` so
repeated lookups during a single import are O(1).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..load.gameskin import skin_fids_chunk

log =  logging.getLogger("gbxbpy.resolve.modifier_lookup")


_MODIFIER_CHUNK_ID = 0x0915D000
_CACHE_ATTR = "_modifier_folder_cache"


def _body_chunk(parsed: Any, chunk_id: int) -> Any:
    """Pull a chunk by id from a NICE-parsed container, or ``None``."""
    body = getattr(parsed, "body", None)
    if isinstance(body, dict):
        chunk = body.get(chunk_id)
        if chunk is not None:
            return chunk
        # Some files normalise leading nibble; try the lower-bit alias
        # (e.g. 0x0915D000 vs 0x915D000) the same way the legacy reader did.
        return body.get(chunk_id & 0x0FFFFFFF)
    return None


def _remapped_materials(chunk: Any) -> Optional[tuple[str, ...]]:
    """Material stems listed by the modifier's ``Remapping`` GameSkin.

    The engine only redirects materials named in this skin (for example
    ``TrackWallToDecoCliff`` lists just ``TrackWall`` although its folder
    also holds ``PlatformTech``). ``None`` means the skin was unreadable.
    """
    skin = getattr(chunk, "Remapping", None) if chunk is not None else None
    if skin is None:
        return None
    fids_chunk = skin_fids_chunk(skin)
    if fids_chunk is None:
        return None
    names: list[str] = []
    for fid in getattr(fids_chunk, "fids", None) or getattr(fids_chunk, "customizableFids", None) or ():
        path = str(getattr(fid, "filePath", "") or getattr(fid, "directory", "") or "")
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower().endswith(".material.gbx"):
            names.append(base[: -len(".material.gbx")])
    return tuple(names)


def _material_renames(chunk: Any) -> tuple[tuple[str, str], ...]:
    """``(original stem, remap file stem)`` pairs whose names differ.

    Each GameSkin fid's ``type`` names the replacement file inside the
    ``RemapFolder``. Item modifiers rename it (``ItemObstacleLevel2``
    replaces ``ItemObstacle``); ``*TrackWall`` style types keep the
    original name.
    """
    skin = getattr(chunk, "Remapping", None) if chunk is not None else None
    fids_chunk = skin_fids_chunk(skin) if skin is not None else None
    pairs: list[tuple[str, str]] = []
    for fid in getattr(fids_chunk, "fids", None) or ():
        name = str(getattr(fid, "type", "") or "")
        path = str(getattr(fid, "filePath", "") or "")
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or name.startswith("*") or not base.lower().endswith(".material.gbx"):
            continue
        stem = base[: -len(".material.gbx")]
        if stem.casefold() != name.casefold():
            pairs.append((stem, name))
    return tuple(pairs)


def resolve_modifier_folder(
    modifier_path: str,
    resolver: Any,
) -> str:
    """Return the ``RemapFolder`` declared by a ``.Modifier.Gbx`` file.

    Args:
        modifier_path: GameData-relative path to a ``CPlugGameSkinAndFolder``
            modifier (typically taken from ``Block.material_modifier_path``
            or ``material_modifier2_path``).
        resolver: A :class:`~GBXBPY.resolve.Resolver` used to turn the
            relative path into an absolute filesystem path.

    Returns:
        The remap folder string in its original encoding (TM uses
        backslashes), or an empty string when the modifier is missing,
        empty, or fails to parse.
    """
    return _read_modifier(modifier_path, resolver)[0]


def resolve_modifier_materials(modifier_path: str, resolver: Any) -> Optional[tuple[str, ...]]:
    """Return the material stems a modifier remaps, or ``None`` if unknown."""
    return _read_modifier(modifier_path, resolver)[1]


def resolve_modifier_renames(modifier_path: str, resolver: Any) -> tuple[tuple[str, str], ...]:
    """Return ``(original stem, remap file stem)`` pairs of a modifier."""
    return _read_modifier(modifier_path, resolver)[2]


def _read_modifier(
    modifier_path: str, resolver: Any,
) -> tuple[str, Optional[tuple[str, ...]], tuple[tuple[str, str], ...]]:
    if not modifier_path or resolver is None:
        return "", None, ()
    cache = getattr(resolver, _CACHE_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(resolver, _CACHE_ATTR, cache)
        except Exception:  # noqa: BLE001 - resolver may forbid attribute set
            cache = None

    if cache is not None and modifier_path in cache:
        return cache[modifier_path]

    folder = ""
    materials: Optional[tuple[str, ...]] = None
    renames: tuple[tuple[str, str], ...] = ()
    abs_path: Optional[str] = None
    try:
        abs_path = resolver.resolve(modifier_path)
    except Exception as e:  # noqa: BLE001
        log.debug("resolver failed for modifier %r: %s", modifier_path, e)

    if abs_path:
        try:
            # Local import: keeps top-level GBXBPY package import light
            # and avoids circular issues with the resolve package.
            from ..io import read_gbx

            parsed = read_gbx(abs_path, recursive=False)
        except Exception as e:  # noqa: BLE001
            log.debug("failed to parse modifier %s: %s", abs_path, e)
            parsed = None

        if parsed is not None:
            chunk = _body_chunk(parsed, _MODIFIER_CHUNK_ID)
            raw = getattr(chunk, "RemapFolder", "") if chunk is not None else ""
            folder = str(raw or "").strip()
            materials = _remapped_materials(chunk)
            renames = _material_renames(chunk)

    result = (folder, materials, renames)
    if cache is not None:
        cache[modifier_path] = result
    log.debug("modifier %r -> folder %r, materials %r, renames %r", modifier_path, folder, materials, renames)
    return result


_TERRAIN_MODIFIER = "{collection}\\Media\\Modifier\\StadiumOnTerrain.TerrainModifier.Gbx"
_TERRAIN_MODIFIER_FOLDER = "{collection}\\Media\\Modifier\\StadiumOnTerrain\\"
_TERRAIN_MODIFIER_SKIN = "Stadium\\GameSkin\\StadiumOnTerrain.GameSkin.gbx"


def _header_remap_folder(parsed: Any) -> str:
    """Read the ``RemapFolder`` string from the 0x01001000 header chunk."""
    header = getattr(parsed, "header", None)
    entries = list(getattr(header, "entries", None) or ())
    data = list(getattr(header, "data", None) or ())
    for entry, raw in zip(entries, data):
        if getattr(entry, "id", None) != 0x01001000 or not isinstance(raw, (bytes, bytearray)):
            continue
        if len(raw) < 8:
            return ""
        size = int.from_bytes(raw[4:8], "little")
        return raw[8:8 + size].decode("utf-8", "replace")
    return ""


def _external_refs(parsed: Any) -> list[str]:
    """GameData-relative paths of a file's external node references."""
    table = getattr(parsed, "referenceTable", None)
    roots = getattr(getattr(table, "externalFolders", None), "folders", None) or ()
    folders: list[str] = []

    def walk(nodes: Any, prefix: str) -> None:
        for node in nodes or ():
            path = f"{prefix}{node.name}\\"
            folders.append(path)
            walk(getattr(node, "folders", None), path)

    walk(roots, "")
    refs: list[str] = []
    for node in getattr(table, "externalNodes", None) or ():
        index = int(getattr(node, "folderIndex", 0) or 0)
        folder = folders[index - 1] if 0 < index <= len(folders) else ""
        refs.append(folder + str(getattr(node, "ref", "") or ""))
    return refs


def _skin_fids(skin_path: str) -> list[tuple[str, str]]:
    """``(remap name, original GameData path)`` pairs of a GameSkin file."""
    from ..io import read_gbx

    try:
        skin = read_gbx(skin_path, recursive=False)
    except Exception as e:  # noqa: BLE001
        log.debug("failed to parse game skin %s: %s", skin_path, e)
        return []
    chunk = skin_fids_chunk(skin)
    pairs: list[tuple[str, str]] = []
    for fid in getattr(chunk, "fids", None) or ():
        name = str(getattr(fid, "type", "") or "")
        path = str(getattr(fid, "filePath", "") or "")
        if name and path:
            pairs.append((name, path))
    return pairs


def resolve_terrain_material_remap(collection: str, resolver: Any) -> dict[str, str]:
    """Material redirection a vista applies to the Stadium blocks placed in it.

    Each vista ships ``<Vista>\\Media\\Modifier\\StadiumOnTerrain.TerrainModifier.Gbx``.
    It names the remap folder in its header and references the external
    ``Stadium\\GameSkin\\StadiumOnTerrain.GameSkin.gbx``, whose fids pair a
    remap file name with the original material path (``TrackWallClipsInWorld``
    replaces ``Stadium\\Media\\Material\\TrackWallClips.Material.Gbx``;
    ``Deco`` also replaces the ``PlatformDirt``/``PlatformIce`` modifier
    copies). Returns ``{normcased absolute original: absolute remap}``. The
    result is empty for Stadium, when the files are missing, or when no
    remap file exists.
    """
    import os

    if not collection or resolver is None:
        return {}
    cache = getattr(resolver, _CACHE_ATTR, None)
    key = ("terrain", collection.casefold())
    if isinstance(cache, dict) and key in cache:
        return dict(cache[key])

    def find(path: str) -> str:
        try:
            return resolver.resolve(path) or ""
        except Exception as e:  # noqa: BLE001
            log.debug("resolver failed for %r: %s", path, e)
            return ""

    remap: dict[str, str] = {}
    modifier_path = find(_TERRAIN_MODIFIER.format(collection=collection))
    if modifier_path:
        from ..io import read_gbx

        try:
            parsed = read_gbx(modifier_path, recursive=False)
        except Exception as e:  # noqa: BLE001
            log.debug("failed to parse terrain modifier %s: %s", modifier_path, e)
            parsed = None
        folder = _header_remap_folder(parsed) or _TERRAIN_MODIFIER_FOLDER.format(collection=collection)
        skins = [r for r in _external_refs(parsed) if r.lower().endswith(".gameskin.gbx")]
        skin_path = find(skins[0] if skins else _TERRAIN_MODIFIER_SKIN)
        for name, original in _skin_fids(skin_path) if skin_path else ():
            target = find(folder.rstrip("\\/") + "\\" + name + ".Material.Gbx")
            source = find(original)
            if target and source:
                remap[os.path.normcase(os.path.abspath(source))] = target
    if isinstance(cache, dict):
        cache[key] = dict(remap)
    log.debug("terrain modifier for %r: %d material remap(s)", collection, len(remap))
    return remap


__all__ = [
    "resolve_modifier_folder", "resolve_modifier_materials", "resolve_modifier_renames",
    "resolve_terrain_material_remap",
]
