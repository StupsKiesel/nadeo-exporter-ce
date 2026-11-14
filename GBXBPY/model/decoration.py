"""Decoration domain model.

A decoration (``CGameCtnDecoration``) is the environmental backdrop of a map:
sky, distant terrain, lighting mood, and terrain modifier references.

Subclasses loaded as separate assets:

* :class:`DecorationMood` — ``CGameCtnDecorationMood`` (``0x0303A000``)
* :class:`DecorationSize` — ``CGameCtnDecorationSize`` (``0x0303B000``)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .asset import Asset, AssetKind
from .common import Color, IsoLoc


@dataclass(slots=True)
class Mood:
    """Lighting / atmosphere settings of a decoration (legacy minimal struct)."""

    name: str = ""
    sun_direction: Color = field(default_factory=Color)
    ambient: Color = field(default_factory=Color)
    fog_color: Color = field(default_factory=Color)
    fog_distance: float = 0.0


@dataclass(slots=True)
class DecorationSolid:
    """A static piece of decoration geometry placed in the world."""

    solid_path: str = ""
    transform: IsoLoc = field(default_factory=IsoLoc)


@dataclass
class Decoration(Asset):
    """A Trackmania decoration definition (``CGameCtnDecoration``)."""

    collection: str = ""
    mood: Optional[Mood] = None
    solids: List[DecorationSolid] = field(default_factory=list)

    # 0x03038011 – 0x03038016 (external node refs → paths)
    deco_size_path: str = ""
    deco_audio_path: str = ""
    deco_mood_path: str = ""
    decorator_solid_warp_path: str = ""
    terrain_modifier_covered_path: str = ""
    terrain_modifier_base_path: str = ""

    # 0x03038017
    decoration_zone_frontier_id: str = ""
    is_water_outside_play_field: bool = False

    # 0x03038018
    vehicle_fx_skin_path: str = ""
    vehicle_fx_folder: str = ""

    # 0x0303801A
    deco_material_modifiers_path: str = ""

    # 0x0303801B: the map placing the surroundings (Stadium: Stade1536/4096
    # blocks of the Stadium256 collection); empty for vista decorations.
    deco_map_path: str = ""

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.DECORATION


@dataclass
class DecorationMood(Asset):
    """A Trackmania decoration mood (``CGameCtnDecorationMood``)."""

    # 0x0303A000 — solar position
    latitude: float = 0.0
    longitude: float = 0.0
    delta_gmt: float = 0.0
    time_sun_rise: int = 0   # ms
    time_sun_fall: int = 0   # ms

    # 0x0303A001 / 0x0303A012 — remapping skin (012 supersedes 001)
    remapped_start_day_time: float = 0.0
    remapping_path: str = ""
    remap_folder: str = ""
    is_night: bool = False
    enable_stars: bool = False

    # 0x0303A002
    shadow_count_car_human: int = 0
    shadow_count_car_opponent: int = 0
    shadow_car_intensity: float = 0.0
    shadow_scene: bool = False
    background_is_locally_lighted: bool = False

    # 0x0303A006 / 0x0303A007
    sun_moon_intensity: float = 0.0
    local_light_scale: float = 0.0

    # 0x0303A00F
    water_reflect_fake_cube: bool = False
    mood_blender_path: str = ""

    # 0x0303A013
    editor_helper_hdr_scale: float = 0.0

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.DECORATION


@dataclass
class DecorationSize(Asset):
    """A Trackmania decoration size (``CGameCtnDecorationSize``)."""

    # 0x0303B000
    edition_zone_min: tuple = field(default_factory=tuple)
    edition_zone_max: tuple = field(default_factory=tuple)

    # 0x0303B001 / 0x0303B002 (002 supersedes 001)
    base_height_base: int = 0
    size: tuple = field(default_factory=tuple)   # (x, y, z) in blocks
    offset_block_y: bool = False
    scene_path: str = ""

    # 0x0303B003 (skippable)
    base_height_offset: int = 0

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.DECORATION


__all__ = ["Decoration", "DecorationSolid", "Mood", "DecorationMood", "DecorationSize"]

