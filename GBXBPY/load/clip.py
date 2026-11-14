"""Loaders for ``CGameCtnBlockInfoClip*`` family (EDClip, EDVerticalClip, EDHorizontalClip).

All three clip types share the ``CGameCtnBlockInfo`` body schema (variants,
mobils, units) plus a small set of clip-specific 0x03053xxx chunks.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..model import Clip
from ..registry import register
from ._gbx_helpers import body_dict, file_stem
from .block import (
    _CHUNK_VARIANTS,
    _populate_block_info,
    parse_variant,
)

log = logging.getLogger("gbxbpy.load.clip")

# Clip-specific body chunks (CGameCtnBlockInfoClip).
_CHUNK_SYMMETRY = 0x03053002
_CHUNK_FREE_FLAGS = 0x03053004
_CHUNK_TYPE = 0x03053005
_CHUNK_PROPS = 0x03053006
_CHUNK_PASSING_POINT = 0x03053007
_CHUNK_GROUP = 0x03053008
_CHUNK_H_GROUP = 0x0335B000
_CHUNK_V_GROUP = 0x03340000


def _load_clip(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,  # noqa: ARG001 - reserved for future external resolution
) -> Clip:
    body = body_dict(parsed)
    clip = Clip(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    _populate_block_info(clip, parsed)

    chunk = body.get(_CHUNK_VARIANTS)
    if chunk is not None:
        vg = getattr(chunk, "variantBaseGround", None)
        va = getattr(chunk, "variantBaseAir", None)
        if vg:
            v = parse_variant(vg)
            v.kind = "ground"
            v.index = 0
            clip.variant_ground = v
        if va:
            v = parse_variant(va)
            v.kind = "air"
            v.index = 0
            clip.variant_air = v

    chunk = body.get(_CHUNK_SYMMETRY)
    if chunk is not None:
        clip.symmetrical_clip_id = str(
            getattr(chunk, "aSymmetricalClipId", "") or ""
        )

    chunk = body.get(_CHUNK_FREE_FLAGS)
    if chunk is not None:
        clip.is_full_free_clip = bool(getattr(chunk, "isFullFreeClip", False))
        clip.is_exclusive_free_clip = bool(
            getattr(chunk, "isExclusiveFreeClip", False)
        )

    chunk = body.get(_CHUNK_TYPE)
    if chunk is not None:
        clip.clip_type = str(getattr(chunk, "clipType", "") or "")

    chunk = body.get(_CHUNK_PROPS)
    if chunk is not None:
        clip.can_be_deleted_by_full_free = bool(
            getattr(chunk, "canBeDeletedByFullFreeClip", False)
        )
        clip.top_bottom_multi_dir = str(
            getattr(chunk, "topBottomMultiDir", "") or ""
        )

    chunk = body.get(_CHUNK_PASSING_POINT)
    if chunk is not None:
        clip.has_passing_point = bool(getattr(chunk, "hasPassingPoint", False))
        if clip.has_passing_point:
            pos = getattr(chunk, "passingPointPos", None)
            if pos is not None:
                clip.passing_point_pos = (
                    float(getattr(pos, "x", 0.0) or 0.0),
                    float(getattr(pos, "y", 0.0) or 0.0),
                )
            clip.passing_point_roll = float(
                getattr(chunk, "passingPointRoll", 0.0) or 0.0
            )
            clip.passing_point_pitch = float(
                getattr(chunk, "passingPointPitch", 0.0) or 0.0
            )

    chunk = body.get(_CHUNK_GROUP)
    if chunk is not None:
        clip.clip_group_id = str(getattr(chunk, "clipGroupId", "") or "")
        clip.symmetrical_clip_group_id = str(
            getattr(chunk, "symmetricalClipGroupId", "") or ""
        )

    chunk = body.get(_CHUNK_H_GROUP)
    if chunk is not None:
        clip.horizontal_clip_group_id = str(
            getattr(chunk, "horizontalClipGroupId", "") or ""
        )

    chunk = body.get(_CHUNK_V_GROUP)
    if chunk is not None:
        clip.vertical_clip_group_id = str(
            getattr(chunk, "verticalClipGroupId", "") or ""
        )

    log.debug(
        "Loaded clip %s (class=0x%08X) type=%r group=%r ground=%s air=%s",
        clip.name, clip.class_id or 0,
        clip.clip_type, clip.clip_group_id,
        clip.variant_ground is not None, clip.variant_air is not None,
    )
    return clip


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

@register(0x03053000)
def load_clip(parsed, *, source_path="", resolver=None):
    return _load_clip(parsed, source_path=source_path, resolver=resolver)


@register(0x03340000)
def load_clip_vertical(parsed, *, source_path="", resolver=None):
    return _load_clip(parsed, source_path=source_path, resolver=resolver)


@register(0x0335B000)
def load_clip_horizontal(parsed, *, source_path="", resolver=None):
    return _load_clip(parsed, source_path=source_path, resolver=resolver)


__all__: list[str] = []
