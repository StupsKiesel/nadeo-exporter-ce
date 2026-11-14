"""Loaders for ``CGameCtnBlockInfo`` family (EDClassic, EDFlat, EDFrontier, EDTransition).

These four file types share the ``CGameCtnBlockInfo`` body schema (the
``0x0304E*`` chunks) and only differ in their root class id. They are all
surfaced as :class:`~GBXBPY.model.Block`.

This is intentionally a *basic* loader: it walks the block-info body and
fills out the dataclass tree (variants → mobils + units → clip path
references). It does **not** resolve external prefab/mesh references or
evaluate variants beyond the data on disk — downstream code (the addon's
import operators, normalization passes) drives that.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from ..model import Block, BlockMobil, BlockUnit, BlockVariant
from ..registry import register
from ._gbx_helpers import (
    body_dict,
    extract_header_icon,
    extract_header_meta,
    file_stem,
    node_path,
)

log = logging.getLogger("gbxbpy.load.block")

# Class ids handled by this loader (all share the CGameCtnBlockInfo body).
CLASS_IDS = (
    0x0304F000,  # CGameCtnBlockInfoFlat   (EDFlat.Gbx)
    0x03050000,  # CGameCtnBlockInfoFrontier (EDFrontier.Gbx)
    0x03051000,  # CGameCtnBlockInfoClassic (EDClassic.Gbx)
    0x0314C000,  # CGameCtnBlockInfoTransition (EDTransition.Gbx)
)

# Body chunks
_CHUNK_NO_RESPAWN = 0x0304E00F
_CHUNK_WAYPOINT = 0x0304E026
_CHUNK_VARIANTS = 0x0304E023
_CHUNK_ADD_GROUND = 0x0304E027
_CHUNK_ADD_AIR = 0x0304E02C
_CHUNK_SYMMETRY = 0x0304E028
_CHUNK_PILLAR = 0x0304E02F
_CHUNK_BASE_BLOCK = 0x0304E031

_CHUNK_PAGE_PATH = 0x2E001009
_CHUNK_SKIN_DIR = 0x2E001010
_CHUNK_CATALOG = 0x2E001011

# Variant body chunks
_VAR_CHUNK_MULTI_DIR = 0x0315B002
_VAR_CHUNK_BASE_INFO = 0x0315B003
_VAR_CHUNK_MOBILS = 0x0315B005
_VAR_CHUNK_TRIGGERS = 0x0315B006
_VAR_CHUNK_PROBE = 0x0315B007
_VAR_CHUNK_BLOCK_UNITS = 0x0315B008
_VAR_CHUNK_PILLAR_PARAMS = 0x0315B009
_VAR_CHUNK_COMPOUND = 0x0315B00A
_VAR_CHUNK_WATER = 0x0315B00B
_VAR_CHUNK_AUTO_TERRAINS = 0x0315C001

# BlockUnitInfo body chunks
_UNIT_CHUNK_OFFSET = 0x03036000
_UNIT_CHUNK_CLIPS = 0x0303600C

# CGameCtnBlockInfoMobil body chunks (only the ones we need)
_MOBIL_CHUNK_GEOM = 0x03122003


# ---------------------------------------------------------------------------
#  Variant / mobil / unit parsing
# ---------------------------------------------------------------------------

def _node_body(node: Any) -> dict:
    body = getattr(node, "body", None)
    if isinstance(body, dict):
        return body
    if node is not None and isinstance(node, dict):
        return node
    return {}


def _parse_mobil(node: Any) -> BlockMobil:
    """Walk a CGameCtnBlockInfoMobil body and pull out the bits we care about."""
    mobil = BlockMobil()
    body = _node_body(node)
    if not body:
        return mobil

    # The chunks we need are not always at fixed indices, but the field
    # names are unique enough that we can scan body values for them.
    for chunk_data in body.values():
        if chunk_data is None:
            continue
        solid = getattr(chunk_data, "solid_fid", None)
        if solid is not None and not mobil.solid_path:
            mobil.solid_path = node_path(solid)
        prefab = getattr(chunk_data, "prefab_fid", None)
        if prefab is not None and not mobil.prefab_path:
            mobil.prefab_path = node_path(prefab)
        u03 = getattr(chunk_data, "u03", None)
        if u03 is not None:
            try:
                mobil.kind = int(u03)
            except (TypeError, ValueError):
                pass
        # GeomTransformation (GbxPose3D: x, y, z, yaw, pitch, roll) — only
        # present when ``HasGeomTransformation`` is set on the chunk.
        if getattr(chunk_data, "HasGeomTransformation", False) or getattr(
            chunk_data, "hasGeomTransformation", False
        ):
            gt = getattr(chunk_data, "GeomTransformation", None)
            if gt is None:
                gt = getattr(chunk_data, "geomTransformation", None)
            if gt is not None:
                try:
                    mobil.geom_transform = (
                        float(getattr(gt, "x", 0.0) or 0.0),
                        float(getattr(gt, "y", 0.0) or 0.0),
                        float(getattr(gt, "z", 0.0) or 0.0),
                        float(getattr(gt, "yaw", 0.0) or 0.0),
                        float(getattr(gt, "pitch", 0.0) or 0.0),
                        float(getattr(gt, "roll", 0.0) or 0.0),
                    )
                except (TypeError, ValueError):
                    pass

    # ``transform`` (IsoLoc) stays at identity — the placement transform we
    # actually need is in ``geom_transform`` above.
    return mobil


def _parse_unit(node: Any) -> BlockUnit:
    """Walk a CGameCtnBlockUnitInfo body."""
    unit = BlockUnit()
    body = _node_body(node)
    if not body:
        return unit

    chunk = body.get(_UNIT_CHUNK_OFFSET)
    if chunk is not None:
        off = getattr(chunk, "RelativeOffset", None)
        if off is not None:
            unit.relative_offset = (
                int(getattr(off, "x", 0) or 0),
                int(getattr(off, "y", 0) or 0),
                int(getattr(off, "z", 0) or 0),
            )

    chunk = body.get(_UNIT_CHUNK_CLIPS)
    if chunk is not None:
        unit.clips_north = _clip_paths(getattr(chunk, "clipsNorth", None))
        unit.clips_east = _clip_paths(getattr(chunk, "clipsEast", None))
        unit.clips_south = _clip_paths(getattr(chunk, "clipsSouth", None))
        unit.clips_west = _clip_paths(getattr(chunk, "clipsWest", None))
        unit.clips_top = _clip_paths(getattr(chunk, "clipsTop", None))
        unit.clips_bottom = _clip_paths(getattr(chunk, "clipsBottom", None))

    return unit


def _clip_paths(refs: Any) -> List[str]:
    if not refs:
        return []
    out: List[str] = []
    for ref in refs:
        if ref is None:
            continue
        if isinstance(ref, str):
            out.append(ref)
            continue
        path = node_path(ref)
        if path:
            out.append(path)
    return out


def _parse_auto_terrain_zones(raw_list: Any) -> List[str]:
    """Pull the genealogy zone-id strings out of an autoTerrains list."""
    zone_ids: List[str] = []
    if not raw_list:
        return zone_ids
    for at in raw_list:
        if at is None:
            continue
        # Look at the auto-terrain's body for a `genealogy` node ref, then
        # read its zoneIds list.
        zone_node = None
        for chunk in _node_body(at).values():
            if chunk is None:
                continue
            genealogy = getattr(chunk, "genealogy", None)
            if genealogy is None:
                continue
            for sub in _node_body(genealogy).values():
                if sub is not None:
                    zone_node = sub
                    break
            if zone_node is not None:
                break
        if zone_node is None:
            continue
        for zid in (getattr(zone_node, "zoneIds", None) or []):
            if zid and zid not in zone_ids:
                zone_ids.append(zid)
    return zone_ids


def parse_variant(raw: Any) -> BlockVariant:
    """Parse a CGameCtnBlockInfoVariant{Ground,Air} node into a BlockVariant.

    Reads every chunk in the 0x0315B* family that NICE knows about plus the
    ground-only 0x0315C001 chunk. Caller is responsible for setting
    ``variant.kind`` / ``variant.index`` afterwards (the chunk itself does
    not encode that information).
    """
    variant = BlockVariant()
    body = _node_body(raw)
    if not body:
        return variant

    # 0x0315B002 — multi_dir
    chunk = body.get(_VAR_CHUNK_MULTI_DIR)
    if chunk is not None:
        variant.multi_dir = _enum_str(getattr(chunk, "multi_dir", ""))

    # 0x0315B003 — base info (cardinal_dir, variant_base_type, ...)
    chunk = body.get(_VAR_CHUNK_BASE_INFO)
    if chunk is not None:
        variant.symmetrical_variant_index = _safe_int(
            getattr(chunk, "symmetrical_variant_index", -1), -1
        )
        cdir = getattr(chunk, "cardinal_dir", None)
        if cdir is not None:
            variant.cardinal_dir = _enum_str(cdir)
        vbt = getattr(chunk, "variant_base_type", None)
        if vbt is not None:
            variant.variant_base_type = _enum_str(vbt)
        variant.no_pillar_below_index = _safe_int(
            getattr(chunk, "no_pillar_below_index", -1), -1
        )

    # 0x0315B005 — mobils + helper solids (v2+)
    chunk = body.get(_VAR_CHUNK_MOBILS)
    if chunk is not None:
        for group in (getattr(chunk, "mobils", None) or []):
            mobil_group: List[BlockMobil] = []
            items = group if isinstance(group, (list, tuple)) else [group]
            for m in items:
                mobil_group.append(_parse_mobil(m))
            variant.mobils.append(mobil_group)
        variant.helper_solid_path = node_path(
            getattr(chunk, "helperSolidFid", None)
        )
        variant.facultative_helper_solid_path = node_path(
            getattr(chunk, "facultativeHelperSolidFid", None)
        )

    # 0x0315B006 — triggers / gameplay sub-models
    chunk = body.get(_VAR_CHUNK_TRIGGERS)
    if chunk is not None:
        variant.screen_interaction_trigger_solid = node_path(
            getattr(chunk, "screenInteractionTriggerSolid", None)
        )
        variant.screen_interaction_trigger_shape = node_path(
            getattr(chunk, "screenInteractionTriggerShape", None)
        )
        variant.waypoint_trigger_solid = node_path(
            getattr(chunk, "waypointTriggerSolid", None)
        )
        variant.waypoint_trigger_shape = node_path(
            getattr(chunk, "waypointTriggerShape", None)
        )
        variant.gate_path = node_path(getattr(chunk, "gate", None))
        variant.teleporter_path = node_path(getattr(chunk, "teleporter", None))
        variant.turbine_path = node_path(getattr(chunk, "turbine", None))
        variant.flock_model_path = node_path(getattr(chunk, "flockModel", None))
        variant.spawn_model_path = node_path(getattr(chunk, "spawnModel", None))
        variant.entity_spawner_paths = [
            node_path(es) for es in (getattr(chunk, "entitySpawners", None) or [])
            if node_path(es)
        ]

    # 0x0315B007 — probe
    chunk = body.get(_VAR_CHUNK_PROBE)
    if chunk is not None:
        variant.probe_path = node_path(getattr(chunk, "probe", None))

    # 0x0315B008 — block units + manual symmetry + spawn pose + name
    chunk = body.get(_VAR_CHUNK_BLOCK_UNITS)
    if chunk is not None:
        for u in (getattr(chunk, "blockUnitModels", None) or []):
            variant.units.append(_parse_unit(u))
        variant.has_manual_symmetry_h = bool(
            getattr(chunk, "hasManualSymmetryH", False)
        )
        variant.has_manual_symmetry_v = bool(
            getattr(chunk, "hasManualSymmetryV", False)
        )
        variant.has_manual_symmetry_d1 = bool(
            getattr(chunk, "hasManualSymmetryD1", False)
        )
        variant.has_manual_symmetry_d2 = bool(
            getattr(chunk, "hasManualSymmetryD2", False)
        )
        variant.name = str(getattr(chunk, "name", "") or "")
        spawn = getattr(chunk, "spawn", None)
        if spawn is not None:
            variant.spawn = _spawn_tuple(spawn)

    # 0x0315B009 — placed/replaced pillar params (path lists)
    chunk = body.get(_VAR_CHUNK_PILLAR_PARAMS)
    if chunk is not None:
        for entry in (getattr(chunk, "u01", None) or []):
            p = node_path(getattr(entry, "u01", None))
            if p:
                variant.placed_pillar_paths.append(p)
        for entry in (getattr(chunk, "u02", None) or []):
            p = node_path(getattr(entry, "u01", None))
            if p:
                variant.replaced_pillar_paths.append(p)

    # 0x0315B00A — compound physics model
    chunk = body.get(_VAR_CHUNK_COMPOUND)
    if chunk is not None:
        variant.compound_model_path = node_path(
            getattr(chunk, "compoundModel", None)
        )

    # 0x0315B00B — water volumes
    chunk = body.get(_VAR_CHUNK_WATER)
    if chunk is not None:
        for wv in (getattr(chunk, "waterVolumes", None) or []):
            entry = {
                "water_type": str(getattr(wv, "waterType", "") or ""),
            }
            cs = getattr(wv, "chunksSize", None)
            if cs is not None:
                # GbxBox -> store as ((min_xyz), (max_xyz)) when possible
                try:
                    entry["chunks_size"] = (
                        (cs.x, cs.y, cs.z),
                        (cs.x2, cs.y2, cs.z2),
                    )
                except AttributeError:
                    entry["chunks_size"] = None
            variant.water_volumes.append(entry)

    # 0x0315C001 — ground-only auto-terrain info
    chunk = body.get(_VAR_CHUNK_AUTO_TERRAINS)
    if chunk is not None:
        variant.auto_terrain_zone_ids = _parse_auto_terrain_zones(
            getattr(chunk, "autoTerrains", None)
        )
        variant.auto_terrain_height_offset = _safe_int(
            getattr(chunk, "autoTerrainHeightOffset", 0), 0
        )
        atpt = getattr(chunk, "autoTerrainPlaceType", None)
        if atpt is not None:
            variant.auto_terrain_place_type = _enum_str(atpt)

    return variant


def _enum_str(v: Any) -> str:
    """Coerce a NICE Enum value (which is usually an EnumIntegerString) to str."""
    if v is None:
        return ""
    s = str(v)
    # construct EnumIntegerString reprs as the symbolic name; just return that.
    return s


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _spawn_tuple(spawn: Any) -> Tuple:
    """Flatten a Pose3D / legacy spawn struct into a plain tuple."""
    # Pose3D usually has .position + .rotation; legacy has spawnTrans/Yaw/Pitch.
    pos = getattr(spawn, "position", None) or getattr(spawn, "spawnTrans", None)
    rot = getattr(spawn, "rotation", None) or getattr(spawn, "spawnYaw", None)
    if pos is None and rot is None:
        return ()
    def to_tuple(v):
        if v is None:
            return ()
        return tuple(getattr(v, c, 0.0) for c in ("x", "y", "z", "w") if hasattr(v, c))
    return (to_tuple(pos), to_tuple(rot))


# ---------------------------------------------------------------------------
#  Top-level block loader
# ---------------------------------------------------------------------------

def _populate_block_info(target: Any, parsed: Any) -> None:
    """Fill the shared CGameCtnBlockInfo metadata (used by Block + Clip)."""
    body = body_dict(parsed)

    # Ingame / catalog metadata.
    chunk = body.get(_CHUNK_PAGE_PATH)
    if chunk is not None:
        target.page_path = str(getattr(chunk, "pagePath", "") or "")

    chunk = body.get(_CHUNK_SKIN_DIR)
    if chunk is not None:
        target.skin_directory = str(getattr(chunk, "skinDirectory", "") or "")

    # Blocks and clips both redirect materials through modifiers.
    chunk = body.get(_CHUNK_BASE_BLOCK)
    if chunk is not None:
        target.material_modifier_path = node_path(getattr(chunk, "materialModifier", None))
        target.material_modifier2_path = node_path(getattr(chunk, "materialModifier2", None))

    chunk = body.get(_CHUNK_CATALOG)
    if chunk is not None:
        target.is_internal = bool(getattr(chunk, "isInternal", False))
        target.is_advanced = bool(getattr(chunk, "isAdvanced", False))
        target.catalog_position = int(getattr(chunk, "catalogPosition", 0) or 0)

    icon, _ = extract_header_meta(parsed)
    target.icon = icon


def _populate_variants(
    ground: List[BlockVariant],
    air: List[BlockVariant],
    parsed: Any,
) -> None:
    body = body_dict(parsed)

    chunk = body.get(_CHUNK_VARIANTS)
    if chunk is not None:
        vg = getattr(chunk, "variantBaseGround", None)
        va = getattr(chunk, "variantBaseAir", None)
        if vg:
            v = parse_variant(vg)
            v.kind = "ground"
            v.index = len(ground)
            ground.append(v)
        if va:
            v = parse_variant(va)
            v.kind = "air"
            v.index = len(air)
            air.append(v)

    chunk = body.get(_CHUNK_ADD_GROUND)
    if chunk is not None:
        for v_raw in (getattr(chunk, "additionalVariantsGround", None) or []):
            v = parse_variant(v_raw)
            v.kind = "ground"
            v.index = len(ground)
            ground.append(v)

    chunk = body.get(_CHUNK_ADD_AIR)
    if chunk is not None:
        for v_raw in (getattr(chunk, "additionalVariantsAir", None) or []):
            v = parse_variant(v_raw)
            v.kind = "air"
            v.index = len(air)
            air.append(v)


def _load_block(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,  # noqa: ARG001 - reserved for future external resolution
) -> Block:
    body = body_dict(parsed)
    block = Block(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    _populate_block_info(block, parsed)
    _populate_variants(block.ground_variants, block.air_variants, parsed)

    chunk = body.get(_CHUNK_NO_RESPAWN)
    if chunk is not None:
        block.no_respawn = bool(getattr(chunk, "no_respawn", False))

    chunk = body.get(_CHUNK_WAYPOINT)
    if chunk is not None:
        block.waypoint_type = str(getattr(chunk, "waypointType", "No") or "No")

    chunk = body.get(_CHUNK_SYMMETRY)
    if chunk is not None:
        block.symmetrical_block_id = str(
            getattr(chunk, "symmetricalBlockInfoId", "") or ""
        )

    chunk = body.get(_CHUNK_PILLAR)
    if chunk is not None:
        block.is_pillar = bool(getattr(chunk, "isPillar", False))
        psmd = getattr(chunk, "pillarShapeMultiDir", 0)
        try:
            block.pillar_shape_multi_dir = int(psmd or 0)
        except (TypeError, ValueError):
            block.pillar_shape_multi_dir = 0

    chunk = body.get(_CHUNK_BASE_BLOCK)
    if chunk is not None:
        block.base_block_path = node_path(getattr(chunk, "baseBlock", None))

    log.debug(
        "Loaded block %s (class=0x%08X) ground_variants=%d air_variants=%d "
        "is_pillar=%s waypoint=%r",
        block.name, block.class_id or 0,
        len(block.ground_variants), len(block.air_variants),
        block.is_pillar, block.waypoint_type,
    )
    return block


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

@register(0x0304F000)
def load_block_flat(parsed, *, source_path="", resolver=None):
    return _load_block(parsed, source_path=source_path, resolver=resolver)


@register(0x03050000)
def load_block_frontier(parsed, *, source_path="", resolver=None):
    return _load_block(parsed, source_path=source_path, resolver=resolver)


@register(0x03051000)
def load_block_classic(parsed, *, source_path="", resolver=None):
    return _load_block(parsed, source_path=source_path, resolver=resolver)


@register(0x0314C000)
def load_block_transition(parsed, *, source_path="", resolver=None):
    return _load_block(parsed, source_path=source_path, resolver=resolver)


__all__ = ["parse_variant", "CLASS_IDS"]
