"""Loader for ``CGameCtnChallenge`` (0x03043000) — Trackmania map files.

Reads the map body chunks NICE knows about and produces a
:class:`~GBXBPY.model.Map` with placed blocks, baked blocks, anchored
items, free blocks, embedded items and a handful of metadata fields.

Coordinate convention: grid block coordinates stay as raw integer cell
indices (the addon multiplies them by the Stadium grid 32 m × 8 m × 32 m
and applies the Y/Z swizzle when materializing). Absolute-world
placements (anchored objects, free blocks, pivots) are converted to
Blender orientation here via :mod:`GBXBPY.coords` so downstream code
can use them without any further swizzle — matching the rest of the
GBXBPY model layer.
"""

from __future__ import annotations

import io
import logging
import struct
import zipfile
from typing import Any, Iterable, List, Optional, Tuple

from ..coords import gbx_to_blender_quat, gbx_to_blender_vec3
from ..model import (
    BlockSkin,
    Map,
    MapAnchoredItem,
    MapBlock,
    MapFreeBlock,
    MapInfo,
)
from ..model.common import Quat
from ..registry import register
from ._gbx_helpers import body_dict, file_stem

log = logging.getLogger("gbxbpy.load.map")


# Body chunk IDs we read.
_CHUNK_MOD_PACK = 0x03043019
_CHUNK_CORE = 0x0304301F
_CHUNK_ANCHORED = 0x03043040
_CHUNK_BAKED = 0x03043048
_CHUNK_DECO_HEIGHT = 0x03043052
_CHUNK_EMBEDDED = 0x03043054
_CHUNK_LIGHT_SETTINGS = 0x03043056
_CHUNK_FREE_BLOCKS = 0x0304305F
_CHUNK_COLORS = 0x03043062
_CHUNK_ANIM_PHASE = 0x03043063
_CHUNK_COLOR_PALETTE = 0x0304306C

# Anchored-object body chunks.
_ANCHORED_CHUNK = 0x03101002


# ---------------------------------------------------------------------------
#  Public loader
# ---------------------------------------------------------------------------

@register(0x03043000)
def load_map(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,  # noqa: ARG001 - reserved
) -> Map:
    """Convert a parsed ``CGameCtnChallenge`` container into a :class:`Map`."""
    body = body_dict(parsed)
    m = Map(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    _read_core(m, body)
    _read_anchored(m, body)
    _read_baked(m, body)
    _read_colors(m, body)
    _read_color_palette(m, body)
    _read_light_settings(m, body)
    _read_free_blocks(m, body)
    _read_anim_phase(m, body)
    _read_deco_height(m, body)
    _read_embedded(m, body)
    _read_mod_pack(m, body)
    _read_header(m, parsed)

    log.debug(
        "Loaded map %s: %d blocks, %d baked, %d items, %d free, %d embedded",
        m.map_name or m.name,
        len(m.blocks), len(m.baked_blocks)
        if hasattr(m, "baked_blocks") else 0,
        len(m.anchored_items),
        len(m.free_blocks),
        len(m.embedded_items),
    )
    return m


# ---------------------------------------------------------------------------
#  Chunk readers
# ---------------------------------------------------------------------------

def _read_core(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_CORE)
    if chunk is None:
        return
    m.map_name = str(getattr(chunk, "mapName", "") or "")
    m.map_size = _to_int3(getattr(chunk, "size", None))
    deco = getattr(chunk, "decoration", None)
    if deco is not None:
        m.decoration = str(getattr(deco, "id", "") or "")
        m.decoration_collection = str(getattr(deco, "collection", "") or "")
    for raw in (getattr(chunk, "Blocks", None) or []):
        pb = _parse_block(raw, baked=False)
        if pb is not None:
            m.blocks.append(pb)


def _read_anchored(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_ANCHORED)
    if chunk is None:
        return
    for obj in (getattr(chunk, "anchoredObjects", None) or []):
        item = _unwrap_anchored(obj)
        if item is None:
            continue
        ao = MapAnchoredItem()
        model = getattr(item, "itemModel", None)
        if model is not None:
            ao.item_model_id = str(getattr(model, "id", "") or "")
            ao.item_model_collection = str(getattr(model, "collection", "") or "")
            ao.item_model_author = str(getattr(model, "author", "") or "")

        pos = getattr(item, "absolutePositionInMap", None)
        if pos is not None:
            ao.position = gbx_to_blender_vec3(
                float(getattr(pos, "x", 0.0) or 0.0),
                float(getattr(pos, "y", 0.0) or 0.0),
                float(getattr(pos, "z", 0.0) or 0.0),
            )

        pivot = getattr(item, "pivotPosition", None)
        if pivot is not None:
            ao.pivot = gbx_to_blender_vec3(
                float(getattr(pivot, "x", 0.0) or 0.0),
                float(getattr(pivot, "y", 0.0) or 0.0),
                float(getattr(pivot, "z", 0.0) or 0.0),
            )

        rot = getattr(item, "rot", None)
        if rot is not None:
            yaw = float(getattr(rot, "yaw", 0.0) or 0.0)
            pitch = float(getattr(rot, "pitch", 0.0) or 0.0)
            roll = float(getattr(rot, "roll", 0.0) or 0.0)
            ao.rotation = _yaw_pitch_roll_to_blender_quat(yaw, pitch, roll)

        ao.block_unit_coord = _to_int3(getattr(item, "blockUnitCoord", None))
        ao.scale = float(getattr(item, "scale", 1.0) or 1.0)
        ao.flags = int(getattr(item, "flags", 0) or 0)
        ao.variant_index = (ao.flags >> 8) & 0xFF
        ao.anchor_tree_id = str(getattr(item, "anchorTreeId", "") or "")
        m.anchored_items.append(ao)


def _read_baked(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_BAKED)
    if chunk is None:
        return
    # Map model carries baked blocks under the same blocks list when there
    # is no dedicated bucket. Keep them separate via ``is_baked`` so the
    # importer can choose which list to materialize.
    for raw in (getattr(chunk, "BakedBlocks", None) or []):
        pb = _parse_block(raw, baked=True)
        if pb is not None:
            # Stash on a dynamic attribute when the dataclass doesn't have
            # a dedicated field (model already exposes ``is_baked``).
            if hasattr(m, "baked_blocks"):
                m.baked_blocks.append(pb)  # type: ignore[attr-defined]
            else:
                m.blocks.append(pb)


def _read_free_blocks(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_FREE_BLOCKS)
    if chunk is None:
        return
    # The 0x0304305F chunk holds positions/rotations for every block in the
    # regular Blocks followed by BakedBlocks whose flags say isFree.
    free_meta = [b for b in (*m.blocks, *m.baked_blocks) if b.is_free]
    raw_list = list(getattr(chunk, "freeBlocks", None) or [])
    for i, fb in enumerate(raw_list):
        pos = getattr(fb, "pos", None) or getattr(fb, "position", None) or fb
        rot = getattr(fb, "rot", None) or getattr(fb, "rotation", None) or fb
        out = MapFreeBlock()
        if i < len(free_meta):
            meta = free_meta[i]
            out.name = meta.name
            out.color = meta.color
            out.is_ground = meta.is_ground
            out.is_ghost = meta.is_ghost
            out.block_variant_index = meta.block_variant_index
            out.mobil_variant_index = meta.mobil_variant_index
            out.mobil_index = meta.mobil_index
            out.is_baked = meta.is_baked
            out.skin = meta.skin
        if pos is not None:
            out.position = gbx_to_blender_vec3(
                float(getattr(pos, "x", 0.0) or 0.0),
                float(getattr(pos, "y", 0.0) or 0.0),
                float(getattr(pos, "z", 0.0) or 0.0),
            )
        if rot is not None:
            yaw = float(getattr(rot, "yaw", 0.0) or 0.0)
            pitch = float(getattr(rot, "pitch", 0.0) or 0.0)
            roll = float(getattr(rot, "roll", 0.0) or 0.0)
            out.rotation = _yaw_pitch_roll_to_blender_quat(yaw, pitch, roll)
        m.free_blocks.append(out)


def _read_colors(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_COLORS)
    if chunk is None:
        return
    for i, c in enumerate(getattr(chunk, "blocksColors", None) or []):
        if i < len(m.blocks):
            m.blocks[i].color = str(c) if c else "Default"
    if hasattr(m, "baked_blocks"):
        for i, c in enumerate(getattr(chunk, "bakedBlocksColors", None) or []):
            if i < len(m.baked_blocks):  # type: ignore[attr-defined]
                m.baked_blocks[i].color = str(c) if c else "Default"  # type: ignore[attr-defined]
    for i, c in enumerate(getattr(chunk, "itemsColors", None) or []):
        if i < len(m.anchored_items):
            m.anchored_items[i].color = str(c) if c else "Default"


def _read_color_palette(m: Map, body: dict) -> None:
    """Map-wide palette (``EMapElemColorPalette``); absent on older maps."""
    chunk = body.get(_CHUNK_COLOR_PALETTE)
    palette = getattr(chunk, "palette", None) if chunk is not None else None
    if palette is not None and not isinstance(palette, int):
        m.color_palette = str(palette)


def _read_light_settings(m: Map, body: dict) -> None:
    """Editor time of day; ``dayTime`` -1 means the decoration mood's default."""
    chunk = body.get(_CHUNK_LIGHT_SETTINGS)
    day_time = getattr(chunk, "dayTime", None) if chunk is not None else None
    if day_time is None:
        return
    if day_time >= 0:
        m.day_time = (int(day_time) % 65536) / 65536.0
    m.dynamic_daylight = bool(getattr(chunk, "dynamicDaylight", False))
    m.day_duration_ms = int(getattr(chunk, "dayDuration", 0) or 0)


def _read_anim_phase(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_ANIM_PHASE)
    if chunk is None:
        return
    for i, off in enumerate(getattr(chunk, "itemsAnimPhaseOffsets", None) or []):
        if i < len(m.anchored_items):
            try:
                m.anchored_items[i].anim_phase_offset = int(off)
            except (TypeError, ValueError):
                pass


def _read_deco_height(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_DECO_HEIGHT)
    if chunk is None:
        return
    m.deco_base_height_offset = float(
        getattr(chunk, "DecoBaseHeightOffset", 0) or 0
    )


def parse_pack_desc(data: bytes) -> tuple[str, str]:
    """Decode a raw ``PackDesc`` (``u8 version``, ``[32-byte checksum]``,
    ``FilePath``, ``[LocatorUrl]``) into ``(file_path, locator_url)``."""
    pos = 0

    def string() -> str:
        nonlocal pos
        if pos + 4 > len(data):
            raise ValueError("truncated PackDesc")
        size = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if size > len(data) - pos:
            raise ValueError("truncated PackDesc string")
        text = data[pos:pos + size].decode("utf-8", "replace")
        pos += size
        return text

    if not data:
        return "", ""
    version = data[0]
    pos = 1 + (32 if version >= 3 else 0)
    path = string()
    url = string() if (path and version >= 1) or version >= 3 else ""
    return path, url


def _read_mod_pack(m: Map, body: dict) -> None:
    # NICE leaves this chunk undecoded; its raw body is a PackDesc.
    chunk = body.get(_CHUNK_MOD_PACK)
    if chunk is None:
        return
    desc = getattr(chunk, "modPackDesc", None)
    if desc is not None:
        m.mod_pack_path = str(getattr(desc, "FilePath", "") or "")
        m.mod_pack_url = str(getattr(desc, "LocatorUrl", "") or "")
        return
    raw = getattr(chunk, "_unknownChunkId", None)
    if not isinstance(raw, (bytes, bytearray)):
        return
    try:
        m.mod_pack_path, m.mod_pack_url = parse_pack_desc(bytes(raw))
    except ValueError as exc:
        log.warning("Unreadable texture mod descriptor: %s", exc)


def _read_embedded(m: Map, body: dict) -> None:
    chunk = body.get(_CHUNK_EMBEDDED)
    if chunk is None:
        return
    embed = getattr(chunk, "embeddedData", None)
    if embed is None:
        return
    files_meta = getattr(embed, "filesMeta", None) or []
    zf = getattr(embed, "zip", None)
    if zf is None or not files_meta:
        return
    try:
        names = list(zf.namelist())
    except Exception:  # noqa: BLE001
        return
    for i, fm in enumerate(files_meta):
        item_id = str(getattr(fm, "id", "") or "")
        if not item_id or i >= len(names):
            continue
        try:
            m.embedded_items[item_id] = bytes(zf.read(names[i]))
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to read embedded item %s: %s", item_id, e)


def _read_header(m: Map, parsed: Any) -> None:
    """Header chunks: ids are on ``header.entries``, payloads in ``header.data``."""
    header = getattr(parsed, "header", None)
    if header is None:
        return
    entries = getattr(header, "entries", None) or []
    data = getattr(header, "data", None) or []
    author: dict[str, str] = {}
    comments = ""
    for entry, payload in zip(entries, data):
        cid = int(getattr(entry, "id", 0) or getattr(entry, "chunkId", 0) or 0)
        try:
            if cid == 0x03043007 and isinstance(payload, (bytes, bytearray)):
                m.thumbnail, comments = parse_thumbnail_chunk(bytes(payload))
            elif cid == 0x03043005:
                m.xml_metadata = str(getattr(payload, "xml", "") or "")
            elif cid == 0x03043008 and isinstance(payload, (bytes, bytearray)):
                author = parse_author_chunk(bytes(payload))
        except (ValueError, struct.error, UnicodeDecodeError) as exc:
            log.warning("Map header chunk %08X unreadable: %s", cid, exc)
    m.info = parse_header_info(m.xml_metadata, author=author, comments=comments)


def _gbx_string(data: bytes, offset: int) -> tuple[str, int]:
    (size,) = struct.unpack_from("<I", data, offset)
    offset += 4
    if size > len(data) - offset:
        raise ValueError("string runs past the chunk")
    return data[offset:offset + size].decode("utf-8", errors="replace"), offset + size


def parse_author_chunk(data: bytes) -> dict[str, str]:
    """``0x03043008``: version, author version, login, nickname, zone, extra info."""
    offset = 8
    values = {}
    for key in ("login", "nickname", "zone"):
        values[key], offset = _gbx_string(data, offset)
    return values


def parse_thumbnail_chunk(data: bytes) -> tuple[bytes, str]:
    """``0x03043007``: version, JPEG size, ``<Thumbnail.jpg>`` JPEG ``</Thumbnail.jpg>``,
    then ``<Comments>`` string ``</Comments>``. The JPEG is stored upside down."""
    if len(data) < 8 or struct.unpack_from("<I", data, 0)[0] == 0:
        return b"", ""
    (size,) = struct.unpack_from("<I", data, 4)
    start = 8 + len(b"<Thumbnail.jpg>")
    jpeg = data[start:start + size] if size else b""
    offset = start + size + len(b"</Thumbnail.jpg>") + len(b"<Comments>")
    comments = ""
    if offset + 4 <= len(data):
        comments, _ = _gbx_string(data, offset)
    return jpeg, comments


def _int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def parse_header_info(xml: str, *, author: Optional[dict] = None, comments: str = "") -> MapInfo:
    """Build :class:`MapInfo` from the header XML; unreadable XML gives the author chunk only."""
    import xml.etree.ElementTree as ET

    author = author or {}
    root = None
    if xml:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            log.warning("Map header XML unreadable: %s", exc)

    def attrs(tag: str) -> dict:
        element = root.find(tag) if root is not None else None
        return dict(element.attrib) if element is not None else {}

    head = dict(root.attrib) if root is not None else {}
    ident, desc, times = attrs("ident"), attrs("desc"), attrs("times")
    deps = tuple(
        dep.get("file", "") for dep in (root.iter("dep") if root is not None else ()) if dep.get("file")
    )
    return MapInfo(
        uid=ident.get("uid", ""),
        author_login=author.get("login") or ident.get("author", ""),
        author_nickname=author.get("nickname", ""),
        author_zone=author.get("zone") or ident.get("authorzone", ""),
        title_id=head.get("title", ""),
        exe_version=head.get("exever", ""),
        exe_build=head.get("exebuild", ""),
        environment=desc.get("envir", ""),
        mood=desc.get("mood", ""),
        map_type=desc.get("maptype", ""),
        map_style=desc.get("mapstyle", ""),
        validated=desc.get("validated") == "1",
        laps=_int(desc.get("nblaps"), 0),
        display_cost=_int(desc.get("displaycost"), 0),
        mod=desc.get("mod", ""),
        has_ghost_blocks=desc.get("hasghostblocks") == "1",
        bronze_time=_int(times.get("bronze"), -1),
        silver_time=_int(times.get("silver"), -1),
        gold_time=_int(times.get("gold"), -1),
        author_time=_int(times.get("authortime"), -1),
        author_score=_int(times.get("authorscore"), 0),
        comments=comments,
        dependencies=deps,
    )


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _to_int3(v: Any) -> Tuple[int, int, int]:
    if v is None:
        return (0, 0, 0)
    try:
        return (int(getattr(v, "x", 0) or 0),
                int(getattr(v, "y", 0) or 0),
                int(getattr(v, "z", 0) or 0))
    except (TypeError, ValueError):
        return (0, 0, 0)


def _unwrap_anchored(obj: Any) -> Any:
    """Return the chunk-0x03101002 struct from a parsed anchored object."""
    body = getattr(obj, "body", None)
    if isinstance(body, dict):
        item = body.get(_ANCHORED_CHUNK)
        if item is not None:
            return item
    return obj


def _parse_block(raw: Any, *, baked: bool) -> Optional[MapBlock]:
    name = str(getattr(raw, "name", "") or "")
    if not name:
        return None
    flags = getattr(raw, "flags", None)
    pb = MapBlock()
    pb.name = name
    pb.direction = str(getattr(raw, "dir", "North") or "North")
    pb.coords = _to_int3(getattr(raw, "coords", None))
    pb.is_baked = baked
    if flags is not None and not isinstance(flags, int):
        pb.is_ground = bool(getattr(flags, "isGround", False))
        pb.is_free = bool(getattr(flags, "isFree", False))
        pb.is_ghost = bool(getattr(flags, "isGhost", False))
        pb.is_clip = bool(getattr(flags, "isClip", False))
        pb.is_pillar = bool(getattr(flags, "isPillar", False))
        pb.is_waypoint = bool(getattr(flags, "isWaypoint", False))
        pb.is_skinnable = bool(getattr(flags, "isSkinnable", False))
        pb.block_variant_index = int(getattr(flags, "blockVariantIndex", 0) or 0)
        pb.mobil_variant_index = int(getattr(flags, "mobilVariantIndex", 0) or 0)
        pb.mobil_index = int(getattr(flags, "mobilIndex", 0) or 0)
    pb.skin = _parse_block_skin(getattr(raw, "skinParams", None))
    return pb


_CHUNK_SKIN_PACK = 0x03059002
_CHUNK_SKIN_FOREGROUND = 0x03059003


def _parse_block_skin(params: Any) -> Optional[BlockSkin]:
    """Decode ``CGameCtnBlockSkin`` chunks; ``None`` when the block has no skin."""
    skin = getattr(params, "skin", None) if params is not None else None
    body = getattr(skin, "body", None)
    if not isinstance(body, dict):
        return None

    def pack(desc: Any) -> tuple[str, str]:
        return (str(getattr(desc, "FilePath", "") or ""),
                str(getattr(desc, "LocatorUrl", "") or ""))

    main = body.get(_CHUNK_SKIN_PACK)
    fore = body.get(_CHUNK_SKIN_FOREGROUND)
    pack_path, pack_url = pack(getattr(main, "packDesc", None))
    fore_path, fore_url = pack(getattr(fore, "foregroundPackDesc", None))
    result = BlockSkin(
        text=str(getattr(main, "text", "") or ""),
        pack_path=pack_path, pack_url=pack_url,
        foreground_path=fore_path, foreground_url=fore_url,
    )
    return result if result else None


def _yaw_pitch_roll_to_blender_quat(yaw: float, pitch: float, roll: float) -> Quat:
    """Convert TM Euler angles to a Blender-oriented quaternion.

    Trackmania stores rotations as yaw-around-Y (up), pitch-around-X,
    roll-around-Z, composed as yaw * roll * pitch (YZX). We compose in GBX space then run the same axis
    swizzle the rest of GBXBPY uses (:func:`gbx_to_blender_vec3`) so the
    returned quaternion is ready to drop into ``rotation_quaternion`` on
    a Blender object without any further fixup.
    """
    import math as _math

    cy = _math.cos(yaw * 0.5)
    sy = _math.sin(yaw * 0.5)
    cp = _math.cos(pitch * 0.5)
    sp = _math.sin(pitch * 0.5)
    cr = _math.cos(roll * 0.5)
    sr = _math.sin(roll * 0.5)

    # YZX order: verified against Blue Lagoon's pitched and rolled supports.
    qx = cy * sp * cr + sy * cp * sr
    qy = sy * cp * cr + cy * sp * sr
    qz = cy * cp * sr - sy * sp * cr
    qw = cy * cp * cr - sy * sp * sr
    return gbx_to_blender_quat(qx, qy, qz, qw)


__all__: list[str] = []
