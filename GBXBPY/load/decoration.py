"""Loaders for decoration asset types.

Handles three GBX class families:

* ``CGameCtnDecoration``     (``0x03038000``) — ``*.Decoration.Gbx``
* ``CGameCtnDecorationMood`` (``0x0303A000``) — ``*.DecorationMood.Gbx``
* ``CGameCtnDecorationSize`` (``0x0303B000``) — ``*.DecorationSize.Gbx``
"""

from __future__ import annotations

import logging
from typing import Any

from ..model.decoration import Decoration, DecorationMood, DecorationSize
from ..registry import register
from ._gbx_helpers import body_dict, file_stem, node_path

log = logging.getLogger("gbxbpy.load.decoration")

# CGameCtnDecoration chunks
_CHUNK_D_DECO_SIZE          = 0x03038011
_CHUNK_D_DECO_AUDIO         = 0x03038012
_CHUNK_D_DECO_MOOD          = 0x03038013
_CHUNK_D_SOLID_WARP         = 0x03038014
_CHUNK_D_TERRAIN_COVERED    = 0x03038015
_CHUNK_D_TERRAIN_BASE       = 0x03038016
_CHUNK_D_ZONE_FRONTIER      = 0x03038017
_CHUNK_D_VEHICLE_FX         = 0x03038018
_CHUNK_D_DECO_AUDIO2        = 0x03038019
_CHUNK_D_MATERIAL_MODS      = 0x0303801A
_CHUNK_D_DECO_MAP           = 0x0303801B

# CGameCtnDecorationMood chunks
_CHUNK_M_SOLAR      = 0x0303A000
_CHUNK_M_REMAP_V1   = 0x0303A001
_CHUNK_M_SHADOWS    = 0x0303A002
_CHUNK_M_SKY_SCALE  = 0x0303A006
_CHUNK_M_SKY_FULL   = 0x0303A007
_CHUNK_M_TECH3      = 0x0303A00F
_CHUNK_M_REMAP_V2   = 0x0303A012  # supersedes 0x0303A001
_CHUNK_M_HDR_SCALE  = 0x0303A013

# CGameCtnDecorationSize chunks
_CHUNK_S_EDITION_ZONE = 0x0303B000
_CHUNK_S_SIZE_V1      = 0x0303B001
_CHUNK_S_SIZE_V2      = 0x0303B002  # supersedes v1, adds offsetBlockY
_CHUNK_S_HEIGHT_OFFS  = 0x0303B003  # skippable


@register(0x03038000)
def load_decoration(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    deco = Decoration(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    chunk = body.get(_CHUNK_D_DECO_SIZE)
    if chunk is not None:
        deco.deco_size_path = node_path(getattr(chunk, "decoSize", None))

    chunk = body.get(_CHUNK_D_DECO_AUDIO)
    if chunk is not None:
        deco.deco_audio_path = node_path(getattr(chunk, "decoAudio", None))

    chunk = body.get(_CHUNK_D_DECO_MOOD)
    if chunk is not None:
        deco.deco_mood_path = node_path(getattr(chunk, "decoMood", None))

    chunk = body.get(_CHUNK_D_SOLID_WARP)
    if chunk is not None:
        deco.decorator_solid_warp_path = node_path(
            getattr(chunk, "decoratorSolidWarp", None)
        )

    chunk = body.get(_CHUNK_D_TERRAIN_COVERED)
    if chunk is not None:
        deco.terrain_modifier_covered_path = node_path(
            getattr(chunk, "terrainModifierCovered", None)
        )

    chunk = body.get(_CHUNK_D_TERRAIN_BASE)
    if chunk is not None:
        deco.terrain_modifier_base_path = node_path(
            getattr(chunk, "terrainModifierBase", None)
        )

    chunk = body.get(_CHUNK_D_ZONE_FRONTIER)
    if chunk is not None:
        deco.decoration_zone_frontier_id = str(
            getattr(chunk, "decorationZoneFrontierId", "") or ""
        )
        deco.is_water_outside_play_field = bool(
            getattr(chunk, "isWaterOutsidePlayField", False)
        )

    chunk = body.get(_CHUNK_D_VEHICLE_FX)
    if chunk is not None:
        deco.vehicle_fx_skin_path = node_path(
            getattr(chunk, "vehicleFxSkin", None)
        )
        deco.vehicle_fx_folder = str(
            getattr(chunk, "vehicleFxFolder", "") or ""
        )

    # 0x03038019 has the same audio reference as 0x03038012 but versioned
    chunk = body.get(_CHUNK_D_DECO_AUDIO2)
    if chunk is not None and not deco.deco_audio_path:
        deco.deco_audio_path = node_path(getattr(chunk, "decoAudio2", None))

    chunk = body.get(_CHUNK_D_MATERIAL_MODS)
    if chunk is not None:
        deco.deco_material_modifiers_path = node_path(
            getattr(chunk, "decoMaterialModifiers", None)
        )

    chunk = body.get(_CHUNK_D_DECO_MAP)
    if chunk is not None:
        deco.deco_map_path = node_path(getattr(chunk, "decoMap", None))

    log.debug(
        "Loaded decoration %s mood=%s size=%s terrain_base=%s",
        deco.name,
        bool(deco.deco_mood_path),
        bool(deco.deco_size_path),
        bool(deco.terrain_modifier_base_path),
    )
    return deco


@register(0x0303A000)
def load_decoration_mood(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    mood = DecorationMood(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    chunk = body.get(_CHUNK_M_SOLAR)
    if chunk is not None:
        mood.latitude       = float(getattr(chunk, "latitude",    0.0) or 0.0)
        mood.longitude      = float(getattr(chunk, "longitude",   0.0) or 0.0)
        mood.delta_gmt      = float(getattr(chunk, "deltaGMT",    0.0) or 0.0)
        mood.time_sun_rise  = int(getattr(chunk, "timeSunRise",   0) or 0)
        mood.time_sun_fall  = int(getattr(chunk, "timeSunFall",   0) or 0)

    # 0x0303A012 supersedes 0x0303A001 — prefer 012 if present
    chunk = body.get(_CHUNK_M_REMAP_V2) or body.get(_CHUNK_M_REMAP_V1)
    if chunk is not None:
        mood.remapped_start_day_time = float(
            getattr(chunk, "remappedStartDayTime", 0.0) or 0.0
        )
        mood.remapping_path = node_path(getattr(chunk, "remapping", None))
        mood.remap_folder   = str(getattr(chunk, "remapFolder", "") or "")
        mood.is_night       = bool(getattr(chunk, "isNight",      False))
        mood.enable_stars   = bool(getattr(chunk, "enableStars",  False))

    chunk = body.get(_CHUNK_M_SHADOWS)
    if chunk is not None:
        mood.shadow_count_car_human     = int(getattr(chunk, "shadowCountCarHuman",     0) or 0)
        mood.shadow_count_car_opponent  = int(getattr(chunk, "shadowCountCarOpponent",  0) or 0)
        mood.shadow_car_intensity       = float(getattr(chunk, "shadowCarIntensity",    0.0) or 0.0)
        mood.shadow_scene               = bool(getattr(chunk, "shadowScene",            False))
        mood.background_is_locally_lighted = bool(
            getattr(chunk, "backgroundIsLocallyLighted", False)
        )

    # 0x0303A007 supersedes 0x0303A006 — prefer 007
    chunk = body.get(_CHUNK_M_SKY_FULL) or body.get(_CHUNK_M_SKY_SCALE)
    if chunk is not None:
        mood.sun_moon_intensity = float(
            getattr(chunk, "sunMoonIntensity", 0.0) or 0.0
        )
        mood.local_light_scale = float(
            getattr(chunk, "localLightScale", 0.0) or 0.0
        )

    chunk = body.get(_CHUNK_M_TECH3)
    if chunk is not None:
        mood.water_reflect_fake_cube = bool(
            getattr(chunk, "waterReflectFakeCube", False)
        )
        mood.mood_blender_path = node_path(getattr(chunk, "moodBlender", None))

    chunk = body.get(_CHUNK_M_HDR_SCALE)
    if chunk is not None:
        mood.editor_helper_hdr_scale = float(
            getattr(chunk, "editorHelperHdrScale", 0.0) or 0.0
        )

    log.debug(
        "Loaded decoration_mood %s lat=%.2f lon=%.2f night=%s",
        mood.name, mood.latitude, mood.longitude, mood.is_night,
    )
    return mood


@register(0x0303B000)
def load_decoration_size(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    size = DecorationSize(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    chunk = body.get(_CHUNK_S_EDITION_ZONE)
    if chunk is not None:
        mn = getattr(chunk, "editionZoneMin", None)
        mx = getattr(chunk, "editionZoneMax", None)
        if mn is not None:
            size.edition_zone_min = (
                float(getattr(mn, "x", 0.0) or 0.0),
                float(getattr(mn, "y", 0.0) or 0.0),
            )
        if mx is not None:
            size.edition_zone_max = (
                float(getattr(mx, "x", 0.0) or 0.0),
                float(getattr(mx, "y", 0.0) or 0.0),
            )

    # 0x0303B002 supersedes 0x0303B001 — prefer 002
    chunk = body.get(_CHUNK_S_SIZE_V2) or body.get(_CHUNK_S_SIZE_V1)
    if chunk is not None:
        size.base_height_base = int(getattr(chunk, "baseHeightBase", 0) or 0)
        s = getattr(chunk, "size", None)
        if s is not None:
            size.size = (
                int(getattr(s, "x", 0) or 0),
                int(getattr(s, "y", 0) or 0),
                int(getattr(s, "z", 0) or 0),
            )
        size.offset_block_y = bool(getattr(chunk, "offsetBlockY", False))
        size.scene_path = node_path(getattr(chunk, "scene", None))

    chunk = body.get(_CHUNK_S_HEIGHT_OFFS)
    if chunk is not None:
        size.base_height_offset = int(
            getattr(chunk, "baseHeightOffset", 0) or 0
        )

    log.debug(
        "Loaded decoration_size %s size=%s base_height=%d",
        size.name, size.size, size.base_height_base,
    )
    return size


__all__: list[str] = []
