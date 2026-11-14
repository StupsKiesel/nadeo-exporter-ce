"""Vista terrain 3D decals: the camera-facing grass tufts on flat ground.

A terrain layer's top texture ``<env>/Media/Texture/<Layer>_D.Texture.gbx``
may reference a ``*.TextureDecals.Gbx`` (class ``0x09078000``). That file
lists ``CPlugDecal3d`` files (``*3D.Decal.Gbx``) and a fixed pattern of
entries in the texture's UV space; the game repeats the pattern with the
layer's world tile size, like the texture itself. Each decal references a
sprite atlas texture whose ``CPlugBitmap`` carries a sprite table
(``0x090A8002``) addressing cells of the atlas image.

NICE does not decode these chunks, so this module reads the raw bodies.

``0x09078000`` body::

    u32 version (4), u32 (10), u32 decalCount, decalCount x u32 node index,
    u32 (3), u32 (1), u32 entryCount, u32 (0), entries...

Entry: lookback string sprite name, 16 floats (size x/y/z in texture UV
units, 1.0, u, v, 3x3 matrix holding a yaw, 0.0), u32 decal index. The
size is uniform (``x == y``, ``z == x / 2``); ``z`` times the tile size
gives a plausible billboard height in metres.

Sprite record (``0x090A8002``, after ``u32 version, u32 count``): lookback
name, lookback group, u32 grid width/height, u32 cell x/y/w/h (y from the
image bottom), float pivot x/y, float scale, u32.

``CPlugDecal3d`` chunk ``0x090A7004.f01`` is the cosine of the steepest
slope a decal grows on.
"""

from __future__ import annotations

import logging
import math
import os
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .. import library

log = logging.getLogger("gbxbpy.textures.terrain_decals")

TEXTURE_DECALS_CHUNK = 0x09078000
SPRITE_TABLE_CHUNK = 0x090A8002
DECAL3D_CHUNK = 0x090A7004

_ENTRY_SIZE = 16 * 4 + 4
_MAX_COUNT = 1_000_000


@dataclass(frozen=True)
class Sprite:
    """One atlas sprite; ``x``/``y``/``w``/``h`` are fractions of the image."""

    name: str
    x: float
    y: float
    w: float
    h: float
    pivot_x: float = 0.5
    pivot_y: float = 0.0
    scale: float = 1.0


@dataclass(frozen=True)
class SpriteAtlas:
    image: Optional[str]
    sprites: Dict[str, Sprite]


@dataclass(frozen=True)
class Decal3D:
    path: str
    name: str
    min_up: float
    atlas: Optional[SpriteAtlas]


@dataclass(frozen=True)
class DecalEntry:
    sprite: str
    decal: int
    size: float
    u: float
    v: float
    yaw: float


@dataclass(frozen=True)
class TextureDecals:
    path: str
    decals: Tuple[Optional[str], ...]
    entries: Tuple[DecalEntry, ...]


@dataclass(frozen=True)
class LayerDecals:
    """Resolved decal pattern of one terrain layer."""

    layer: str
    texture_decals: TextureDecals
    decals: Tuple[Optional[Decal3D], ...]


class _Body:
    def __init__(self, data: bytes, pos: int) -> None:
        self.data = data
        self.pos = pos
        self.strings: List[str] = []
        self.lookback_started = False

    def u32(self) -> int:
        (value,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return value

    def f32(self) -> float:
        (value,) = struct.unpack_from("<f", self.data, self.pos)
        self.pos += 4
        return value

    def lookback(self) -> str:
        value = self.u32()
        if not self.lookback_started:
            self.lookback_started = True
            if value == 3:
                value = self.u32()
        if value == 0xFFFFFFFF:
            return ""
        index = value & 0x3FFFFFFF
        if value & 0xC0000000 and index == 0:
            (length,) = struct.unpack_from("<I", self.data, self.pos)
            if length > 1024:
                raise ValueError(f"implausible string length {length}")
            raw = self.data[self.pos + 4:self.pos + 4 + length]
            self.pos += 4 + length
            text = raw.decode("utf-8", "replace")
            self.strings.append(text)
            return text
        if value & 0xC0000000 and 0 < index <= len(self.strings):
            return self.strings[index - 1]
        raise ValueError(f"bad lookback string 0x{value:08X}")


def _read_file(path: str) -> Optional[bytes]:
    from ..library.validate import gbx_body

    library.ensure(path)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        log.warning("decal file unreadable: %s (%s)", path, exc)
        return None
    body = gbx_body(data)
    if body is None:
        log.warning("decal file body unreadable: %s", path)
    return body


def _external_nodes(path: str) -> Dict[int, str]:
    """External node index -> absolute path."""
    from ..io import read_gbx

    try:
        parsed = read_gbx(path, recursive=False)
    except Exception as exc:  # noqa: BLE001 - corrupt files must not abort imports
        log.warning("decal file unreadable: %s (%s)", path, exc)
        return {}
    folder = os.path.dirname(path)
    nodes: Dict[int, str] = {}
    table = getattr(parsed, "referenceTable", None)
    for node in getattr(table, "externalNodes", None) or []:
        rel = getattr(node, "_computedRelativePath", "") or getattr(node, "ref", "") or ""
        if rel:
            nodes[int(node.nodeIndex)] = os.path.normpath(os.path.join(folder, rel))
    return nodes


def parse_texture_decals(body: bytes) -> Tuple[Tuple[int, ...], Tuple[DecalEntry, ...]]:
    """Decode a ``0x09078000`` body: (decal node indices, entries)."""
    if body[:4] != TEXTURE_DECALS_CHUNK.to_bytes(4, "little"):
        raise ValueError("body does not start with chunk 0x09078000")
    r = _Body(body, 4)
    r.u32()  # version
    r.u32()
    count = r.u32()
    if count > 256:
        raise ValueError(f"implausible decal count {count}")
    nodes = tuple(r.u32() for _ in range(count))
    r.u32()
    r.u32()
    entry_count = r.u32()
    r.u32()
    if entry_count > _MAX_COUNT:
        raise ValueError(f"implausible entry count {entry_count}")
    entries = []
    for _ in range(entry_count):
        sprite = r.lookback()
        values = struct.unpack_from("<16fI", body, r.pos)
        r.pos += _ENTRY_SIZE
        decal = values[16]
        if decal >= count:
            raise ValueError(f"decal index {decal} out of range")
        entries.append(DecalEntry(
            sprite=sprite,
            decal=decal,
            size=values[2],
            u=values[4],
            v=values[5],
            yaw=math.atan2(values[7], values[6]),
        ))
    return nodes, tuple(entries)


@lru_cache(maxsize=32)
def read_texture_decals(path: str) -> Optional[TextureDecals]:
    body = _read_file(path)
    if body is None:
        return None
    try:
        nodes, entries = parse_texture_decals(body)
    except (ValueError, struct.error) as exc:
        log.warning("texture decals unreadable: %s (%s)", path, exc)
        return None
    refs = _external_nodes(path)
    return TextureDecals(path, tuple(refs.get(n) for n in nodes), entries)


def parse_sprite_table(body: bytes) -> Dict[str, Sprite]:
    """Decode the ``0x090A8002`` sprite table found anywhere in ``body``."""
    marker = SPRITE_TABLE_CHUNK.to_bytes(4, "little")
    pos = body.find(marker)
    if pos < 0:
        return {}
    r = _Body(body, pos + 4)
    r.u32()  # version
    count = r.u32()
    if count > 65536:
        raise ValueError(f"implausible sprite count {count}")
    sprites: Dict[str, Sprite] = {}
    for _ in range(count):
        name = r.lookback()
        r.lookback()  # group
        grid_w, grid_h, x, y, w, h = (r.u32() for _ in range(6))
        pivot_x, pivot_y, scale = r.f32(), r.f32(), r.f32()
        r.u32()
        if not grid_w or not grid_h:
            raise ValueError(f"sprite {name!r} has an empty grid")
        sprites[name] = Sprite(
            name, x / grid_w, y / grid_h, w / grid_w, h / grid_h, pivot_x, pivot_y, scale,
        )
    return sprites


@lru_cache(maxsize=32)
def read_sprite_atlas(texture_path: str) -> Optional[SpriteAtlas]:
    body = _read_file(texture_path)
    if body is None:
        return None
    try:
        sprites = parse_sprite_table(body)
    except (ValueError, struct.error) as exc:
        log.warning("sprite table unreadable: %s (%s)", texture_path, exc)
        return None
    if not sprites:
        return None
    image = next(
        (p for p in _external_nodes(texture_path).values() if p.lower().endswith(".dds")), None,
    )
    if image is not None and not library.ensure(image):
        image = None
    return SpriteAtlas(image, sprites)


@lru_cache(maxsize=64)
def read_decal3d(path: str) -> Optional[Decal3D]:
    from ..io import read_gbx

    if not path or not library.ensure(path):
        return None
    try:
        parsed = read_gbx(path, recursive=False)
        chunk = parsed.body[DECAL3D_CHUNK]
        name = str(getattr(chunk, "name", "") or "")
        min_up = float(getattr(chunk, "f01", 0.0))
    except Exception as exc:  # noqa: BLE001 - corrupt decals are skipped
        log.warning("3D decal unreadable: %s (%s)", path, exc)
        return None
    atlas = None
    for ref in _external_nodes(path).values():
        if ref.lower().endswith(".texture.gbx") and library.ensure(ref):
            atlas = read_sprite_atlas(ref)
            break
    return Decal3D(path, name, min_up, atlas)


def _find_file(folder: str, name: str) -> Optional[str]:
    candidate = os.path.join(folder, name)
    if library.isfile(candidate):
        return candidate
    try:
        listing = library.listdir(folder)
    except OSError:
        return None
    lowered = name.lower()
    match = next((entry for entry in listing if entry.lower() == lowered), None)
    return os.path.join(folder, match) if match else None


@lru_cache(maxsize=64)
def layer_decals(media_dir: str, layer: str) -> Optional[LayerDecals]:
    """Decal pattern of terrain ``layer`` in ``<env>/Media``, or ``None``."""
    texture = _find_file(os.path.join(media_dir, "Texture"), f"{layer}_D.Texture.gbx")
    if texture is None:
        return None
    source = next(
        (p for p in _external_nodes(texture).values() if p.lower().endswith(".texturedecals.gbx")),
        None,
    )
    if source is None or not library.ensure(source):
        return None
    decals = read_texture_decals(source)
    if decals is None or not decals.entries:
        return None
    return LayerDecals(layer, decals, tuple(read_decal3d(p) if p else None for p in decals.decals))


__all__ = [
    "Decal3D",
    "DecalEntry",
    "LayerDecals",
    "Sprite",
    "SpriteAtlas",
    "TextureDecals",
    "layer_decals",
    "parse_sprite_table",
    "parse_texture_decals",
    "read_decal3d",
    "read_sprite_atlas",
    "read_texture_decals",
]
