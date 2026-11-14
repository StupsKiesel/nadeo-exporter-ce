"""Loaders for ``CGameCtnZoneFlat`` (ZoneFlat.Gbx) and
``CGameCtnZoneFrontier`` (ZoneFrontier.Gbx).

Both classes inherit ``CGameCtnZone``, so a shared helper
``_populate_zone_base`` reads the ``0x0305C*`` chunks into any :class:`Zone`
subclass before the subclass-specific chunks are read.
"""

from __future__ import annotations

import logging
from typing import Any

from ..model.zone import Zone, ZoneFlat, ZoneFrontier, ZoneTransition
from ..registry import register
from ._gbx_helpers import body_dict, file_stem, node_path

log = logging.getLogger("gbxbpy.load.zone")

# CGameCtnZone base chunks
_CHUNK_Z_MAIN       = 0x0305C003
_CHUNK_Z_DEPTH      = 0x0305C004  # skippable
_CHUNK_Z_HAS_WATER  = 0x0305C005
_CHUNK_Z_LARGE      = 0x0305C006
_CHUNK_Z_VIS_HEIGHT = 0x0305C007
_CHUNK_Z_HEIGHT_ALT = 0x0305C008
_CHUNK_Z_WATER_ID   = 0x0305C00B
_CHUNK_Z_FRONTIER   = 0x0305C00C

# CGameCtnZoneFlat-only chunks
_CHUNK_ZF_BLOCK_INFOS   = 0x0305D001
_CHUNK_ZF_GROUND_ONLY   = 0x0305D002  # skippable
_CHUNK_ZF_AUTO_SIMPLIFY = 0x0305D003  # skippable

# CGameCtnZoneFrontier-only chunks
_CHUNK_ZFR_MAIN      = 0x0305E001
_CHUNK_ZFR_OFFSET    = 0x0305E002
_CHUNK_ZFR_PYLONS_V1 = 0x0305E003
_CHUNK_ZFR_PYLONS_V2 = 0x0305E004  # supersedes 003, has 2 extra bools
_CHUNK_ZFR_COMPAT    = 0x0305E005  # skippable / deprecated

# CGameCtnZoneTransition-only chunks
_CHUNK_ZT_BLOCK_INFO    = 0x0314D000
_CHUNK_ZT_REPLACEMENT   = 0x0314D005
_CHUNK_ZT_U06           = 0x0314D006
_CHUNK_ZT_U07           = 0x0314D007
_CHUNK_ZT_BORDERS       = 0x0314D008


def _populate_zone_base(zone: Zone, body: dict) -> None:
    """Read all CGameCtnZone (0x0305C*) chunks into *zone* in-place."""
    chunk = body.get(_CHUNK_Z_MAIN)
    if chunk is not None:
        zone.height = int(getattr(chunk, "height", 0) or 0)
        zone.zone_id = str(getattr(chunk, "zoneId", "") or "")
        zone.surface_id = str(getattr(chunk, "surfaceId", "") or "")

    chunk = body.get(_CHUNK_Z_DEPTH)
    if chunk is not None:
        zone.depth = int(getattr(chunk, "depth", 0) or 0)
        zone.old_zone = bool(getattr(chunk, "oldZone", False))

    chunk = body.get(_CHUNK_Z_HAS_WATER)
    if chunk is not None:
        zone.has_water = bool(getattr(chunk, "hasWater", False))

    chunk = body.get(_CHUNK_Z_LARGE)
    if chunk is not None:
        zone.is_large_zone = bool(getattr(chunk, "isLargeZone", False))

    chunk = body.get(_CHUNK_Z_VIS_HEIGHT)
    if chunk is not None:
        zone.visual_top_ground_height = float(
            getattr(chunk, "visualTopGroundHeight", 0.0) or 0.0
        )

    chunk = body.get(_CHUNK_Z_HEIGHT_ALT)
    if chunk is not None:
        zone.height_alt = int(getattr(chunk, "height", 0) or 0)

    chunk = body.get(_CHUNK_Z_WATER_ID)
    if chunk is not None:
        zone.water_id = str(getattr(chunk, "waterId", "") or "")

    chunk = body.get(_CHUNK_Z_FRONTIER)
    if chunk is not None:
        zone.forced_parent_zone_frontier_id = str(
            getattr(chunk, "forcedParentZoneFrontierId", "") or ""
        )


@register(0x0305D000)
def load_zone_flat(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    zone = ZoneFlat(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )
    _populate_zone_base(zone, body)

    chunk = body.get(_CHUNK_ZF_BLOCK_INFOS)
    if chunk is not None:
        zone.block_info_flat_path  = node_path(getattr(chunk, "blockInfoFlat",  None))
        zone.block_info_clip_path  = node_path(getattr(chunk, "blockInfoClip",  None))
        zone.block_info_road_path  = node_path(getattr(chunk, "blockInfoRoad",  None))
        zone.block_info_pylon_path = node_path(getattr(chunk, "blockInfoPylon", None))

    chunk = body.get(_CHUNK_ZF_GROUND_ONLY)
    if chunk is not None:
        zone.ground_only = bool(getattr(chunk, "groundOnly", False))

    chunk = body.get(_CHUNK_ZF_AUTO_SIMPLIFY)
    if chunk is not None:
        zone.auto_simplify_genealogy = bool(
            getattr(chunk, "autoSimplifyGenealogy", False)
        )

    log.debug(
        "Loaded zone_flat %s zone_id=%r flat=%s clip=%s",
        zone.name, zone.zone_id,
        bool(zone.block_info_flat_path), bool(zone.block_info_clip_path),
    )
    return zone


@register(0x0305E000)
def load_zone_frontier(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    zone = ZoneFrontier(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )
    _populate_zone_base(zone, body)

    chunk = body.get(_CHUNK_ZFR_MAIN)
    if chunk is not None:
        zone.block_info_frontier_path = node_path(
            getattr(chunk, "blockInfoFrontier", None)
        )
        zone.parent_zone_id = str(getattr(chunk, "parentZoneId", "") or "")
        zone.child_zone_id  = str(getattr(chunk, "childZoneId",  "") or "")

    chunk = body.get(_CHUNK_ZFR_OFFSET)
    if chunk is not None:
        zone.block_y_offset_from_parent = int(
            getattr(chunk, "blockYOffsetFromParent", 0) or 0
        )

    # 004 supersedes 003 — check 004 first, fall back to 003
    chunk = body.get(_CHUNK_ZFR_PYLONS_V2) or body.get(_CHUNK_ZFR_PYLONS_V1)
    if chunk is not None:
        zone.frontier_parent_border_accept_pylons = bool(
            getattr(chunk, "frontierParentBorderAcceptPylons", False)
        )
        zone.frontier_child_border_accept_pylons = bool(
            getattr(chunk, "frontierChildBorderAcceptPylons", False)
        )
        zone.frontier_transition_middle_accept_pylons = bool(
            getattr(chunk, "frontierTransitionMiddleAcceptPylons", False)
        )
        zone.frontier_straight_middle_accept_pylons = bool(
            getattr(chunk, "frontierStraightMiddleAcceptPylons", False)
        )

    log.debug(
        "Loaded zone_frontier %s zone_id=%r parent=%r child=%r",
        zone.name, zone.zone_id, zone.parent_zone_id, zone.child_zone_id,
    )
    return zone


@register(0x0314D000)
def load_zone_transition(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    zone = ZoneTransition(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )
    _populate_zone_base(zone, body)

    chunk = body.get(_CHUNK_ZT_BLOCK_INFO)
    if chunk is not None:
        zone.block_info_transition_path = node_path(
            getattr(chunk, "blockInfoTransition", None)
        )

    chunk = body.get(_CHUNK_ZT_REPLACEMENT)
    if chunk is not None:
        zone.replacement_zone_id = str(
            getattr(chunk, "replacementZoneId", "") or ""
        )

    chunk = body.get(_CHUNK_ZT_BORDERS)
    if chunk is not None:
        zone.border_north = int(getattr(chunk, "borderNorth", 0) or 0)
        zone.border_east  = int(getattr(chunk, "borderEast",  0) or 0)
        zone.border_south = int(getattr(chunk, "borderSouth", 0) or 0)
        zone.border_west  = int(getattr(chunk, "borderWest",  0) or 0)

    log.debug(
        "Loaded zone_transition %s zone_id=%r replacement=%r",
        zone.name, zone.zone_id, zone.replacement_zone_id,
    )
    return zone


__all__: list[str] = []
