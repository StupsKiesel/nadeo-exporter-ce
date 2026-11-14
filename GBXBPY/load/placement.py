"""Loader for ``CGameItemPlacementParam`` (``PlaceParam.Gbx``)."""

from __future__ import annotations

import logging
from typing import Any

from ..model.placement import MagnetLoc, PlacementParam
from ..registry import register
from ._gbx_helpers import body_dict, file_stem

log = logging.getLogger("gbxbpy.load.placement")

_CHUNK_MAIN    = 0x2E020000  # flags, cube, grid, fly, pivot_snap_distance
_CHUNK_PIVOTS  = 0x2E020001  # pivotPositions[], pivotRotations[]
_CHUNK_MAGNETS = 0x2E020004  # magnetLocs[] (GbxPose3D)
# 0x2E020003 is parsed by NICE; placement-class rules are not used by this loader.
# 0x2E020005 is an item_placement NodeRef — skipped


def _vec3(node: Any) -> tuple[float, float, float]:
    if node is None:
        return (0.0, 0.0, 0.0)
    try:
        return (float(node.x), float(node.y), float(node.z))
    except AttributeError:
        try:
            return (float(node[0]), float(node[1]), float(node[2]))
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, 0.0)


def _quat(node: Any) -> tuple[float, float, float, float]:
    if node is None:
        return (0.0, 0.0, 0.0, 1.0)
    try:
        return (float(node.x), float(node.y), float(node.z), float(node.w))
    except AttributeError:
        try:
            return (float(node[0]), float(node[1]), float(node[2]), float(node[3]))
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, 0.0, 1.0)


@register(0x2E020000)
def load_placement_param(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> PlacementParam:
    body = body_dict(parsed)

    pp = PlacementParam(
        class_id=0x2E020000,
        source_path=source_path or None,
        name=file_stem(source_path),
    )

    # Chunk 000 — main placement params
    main = body.get(_CHUNK_MAIN)
    if main is not None:
        flags = getattr(main, "flags", None)
        if flags is not None:
            pp.auto_rotation = bool(getattr(flags, "AutoRotation", False))
            pp.yaw_only = bool(getattr(flags, "YawOnly", False))
            pp.not_on_object = bool(getattr(flags, "NotOnObject", False))
            pp.switch_pivot_manually = bool(getattr(flags, "SwitchPivotManually", False))
            pp.ghost_mode = bool(getattr(flags, "GhostMode", False))
            pp.has_path = bool(getattr(flags, "HasPath", False))

        pp.cube_center = _vec3(getattr(main, "cubeCenter", None))
        pp.cube_size = float(getattr(main, "cubeSize", 0.0) or 0.0)
        pp.grid_snap_h_step = float(getattr(main, "gridSnap_HStep", 0.0) or 0.0)
        pp.grid_snap_v_step = float(getattr(main, "gridSnap_VStep", 0.0) or 0.0)
        pp.grid_snap_h_offset = float(getattr(main, "gridSnap_HOffset", 0.0) or 0.0)
        pp.grid_snap_v_offset = float(getattr(main, "gridSnap_VOffset", 0.0) or 0.0)
        pp.fly_step = float(getattr(main, "flyStep", 0.0) or 0.0)
        pp.fly_offset = float(getattr(main, "flyOffset", 0.0) or 0.0)
        pp.pivot_snap_distance = float(getattr(main, "pivotSnapDistance", 0.0) or 0.0)

    # Chunk 001 — pivot lists
    pivots = body.get(_CHUNK_PIVOTS)
    if pivots is not None:
        for pos in getattr(pivots, "pivotPositions", None) or []:
            pp.pivot_positions.append(_vec3(pos))
        for rot in getattr(pivots, "pivotRotations", None) or []:
            pp.pivot_rotations.append(_quat(rot))

    # Chunk 004 — magnet locations (GbxPose3D = position + rotation)
    magnets = body.get(_CHUNK_MAGNETS)
    if magnets is not None:
        for ml in getattr(magnets, "magnetLocs", None) or []:
            pos = _vec3(getattr(ml, "position", None) or getattr(ml, "pos", None))
            rot = _quat(getattr(ml, "rotation", None) or getattr(ml, "rot", None))
            pp.magnet_locs.append(MagnetLoc(position=pos, rotation=rot))

    log.debug(
        "PlacementParam %s: grid=(%.2f,%.2f) pivots=%d magnets=%d "
        "flags=autoRot:%s yawOnly:%s",
        pp.name, pp.grid_snap_h_step, pp.grid_snap_v_step,
        len(pp.pivot_positions), len(pp.magnet_locs),
        pp.auto_rotation, pp.yaw_only,
    )
    return pp
