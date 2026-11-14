"""Loader for ``CPlugKinematicConstraint`` (``KinematicConstraint.Gbx``)."""

from __future__ import annotations

import logging
from typing import Any

from ..model.kinematic import AnimSubFunc, KinematicConstraint
from ..registry import register
from ._gbx_helpers import body_dict, file_stem

log = logging.getLogger("gbxbpy.load.kinematic")

_CHUNK_MAIN = 0x2F0CA000  # only chunk — all data in one struct


def _parse_sub_funcs(anim_func: Any) -> list[AnimSubFunc]:
    """Convert a parsed GbxAnimFunc container to a list of AnimSubFunc."""
    if anim_func is None:
        return []
    sub_funcs_raw = getattr(anim_func, "SubFuncs", None) or []
    out: list[AnimSubFunc] = []
    for sf in sub_funcs_raw:
        ease_raw = getattr(sf, "ease", 0)
        # ease may be an Enum-like construct object; coerce to int
        try:
            ease_int = int(ease_raw)
        except (TypeError, ValueError):
            ease_int = 0
        out.append(AnimSubFunc(
            ease=ease_int,
            reverse=bool(getattr(sf, "reverse", False)),
            duration_ms=int(getattr(sf, "duration", 0) or 0),
        ))
    return out


@register(0x2F0CA000)
def load_kinematic_constraint(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> KinematicConstraint:
    body = body_dict(parsed)

    kc = KinematicConstraint(
        class_id=0x2F0CA000,
        source_path=source_path or None,
        name=file_stem(source_path),
    )

    chunk = body.get(_CHUNK_MAIN)
    if chunk is None and "TransAnimFunc" in body:
        chunk = body
    if chunk is None:
        log.debug("KinematicConstraint %s: no main chunk", kc.name)
        return kc

    # Translation
    trans_axis_raw = getattr(chunk, "TransAxis", 0)
    try:
        kc.trans_axis = int(trans_axis_raw)
    except (TypeError, ValueError):
        kc.trans_axis = 0
    kc.trans_min = float(getattr(chunk, "TransMin", 0.0) or 0.0)
    kc.trans_max = float(getattr(chunk, "TransMax", 0.0) or 0.0)
    kc.trans_sub_funcs = _parse_sub_funcs(getattr(chunk, "TransAnimFunc", None))

    # Rotation
    rot_axis_raw = getattr(chunk, "RotAxis", 0)
    try:
        kc.rot_axis = int(rot_axis_raw)
    except (TypeError, ValueError):
        kc.rot_axis = 0
    kc.angle_min_deg = float(getattr(chunk, "AngleMinDeg", 0.0) or 0.0)
    kc.angle_max_deg = float(getattr(chunk, "AngleMaxDeg", 0.0) or 0.0)
    kc.rot_sub_funcs = _parse_sub_funcs(getattr(chunk, "RotAnimFunc", None))

    # Shader TC type
    shader_tc_raw = getattr(chunk, "ShaderTcType", 0)
    try:
        kc.shader_tc_type = int(shader_tc_raw)
    except (TypeError, ValueError):
        kc.shader_tc_type = 0

    log.debug(
        "KinematicConstraint %s: trans_axis=%d [%.2f..%.2f] "
        "rot_axis=%d [%.1f..%.1f deg] sub_funcs(t=%d r=%d)",
        kc.name, kc.trans_axis, kc.trans_min, kc.trans_max,
        kc.rot_axis, kc.angle_min_deg, kc.angle_max_deg,
        len(kc.trans_sub_funcs), len(kc.rot_sub_funcs),
    )
    return kc
