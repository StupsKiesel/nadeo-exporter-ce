"""Map domain model.

Represents ``CGameCtnChallenge`` (0x03043000): the placed blocks, anchored
items, free blocks, embedded items, and high-level metadata of a Trackmania
map. Coordinates and rotations are already Blender-oriented; flag bits are
already decoded; embedded item bytes are exposed verbatim so the addon's
asset layer can re-feed them into GBXBPY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .asset import Asset, AssetKind
from .common import Quat, Vec3


@dataclass(frozen=True, slots=True)
class MapInfo:
    """Header metadata: the ``0x03043005`` XML plus the author chunk ``0x03043008``.

    Medal times are milliseconds; ``-1`` means not set (unvalidated map).
    """

    uid: str = ""
    author_login: str = ""
    author_nickname: str = ""
    author_zone: str = ""
    title_id: str = ""
    exe_version: str = ""
    exe_build: str = ""
    environment: str = ""
    mood: str = ""
    map_type: str = ""
    map_style: str = ""
    validated: bool = False
    laps: int = 0
    display_cost: int = 0
    mod: str = ""
    has_ghost_blocks: bool = False
    bronze_time: int = -1
    silver_time: int = -1
    gold_time: int = -1
    author_time: int = -1
    author_score: int = 0
    comments: str = ""
    dependencies: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BlockSkin:
    """``CGameCtnBlockSkin`` (0x03059000) of a placed block.

    ``pack_path``/``foreground_path`` are relative to the game's ``Skins``
    root (e.g. ``Skins\\Any\\Advertisement4x1\\Blue.zip``). ``text`` is a
    free string; Nadeo deco walls use it to name a modifier folder such as
    ``PlatformGrass\\``.
    """

    text: str = ""
    pack_path: str = ""
    pack_url: str = ""
    foreground_path: str = ""
    foreground_url: str = ""

    def __bool__(self) -> bool:
        return any((self.text, self.pack_path, self.pack_url,
                    self.foreground_path, self.foreground_url))


@dataclass(slots=True)
class MapBlock:
    """A single block placed on the map grid."""

    name: str = ""
    direction: str = "North"          # "North" | "East" | "South" | "West"
    coords: Tuple[int, int, int] = (0, 0, 0)
    color: str = "Default"
    is_ground: bool = False
    is_free: bool = False
    is_ghost: bool = False
    is_clip: bool = False
    is_pillar: bool = False
    is_waypoint: bool = False
    is_skinnable: bool = False
    is_baked: bool = False            # came from BakedBlocks list
    block_variant_index: int = 0
    mobil_variant_index: int = 0
    mobil_index: int = 0
    skin: Optional[BlockSkin] = None


@dataclass(slots=True)
class MapFreeBlock:
    """A free-placement block with absolute world transform.

    ``position`` and ``rotation`` are already in Blender orientation
    (Z-up); the loader applies the GBX → Blender swizzle.
    """

    name: str = ""
    position: Vec3 = field(default_factory=Vec3)
    rotation: Quat = field(default_factory=Quat.identity)
    color: str = "Default"
    is_ground: bool = False
    is_ghost: bool = False
    block_variant_index: int = 0
    mobil_variant_index: int = 0
    mobil_index: int = 0
    is_baked: bool = False
    skin: Optional[BlockSkin] = None


@dataclass(slots=True)
class MapAnchoredItem:
    """An item placed on the map (anchored object)."""

    item_model_id: str = ""
    item_model_collection: str = ""
    item_model_author: str = ""
    position: Vec3 = field(default_factory=Vec3)
    rotation: Quat = field(default_factory=Quat.identity)
    pivot: Vec3 = field(default_factory=Vec3)
    block_unit_coord: Tuple[int, int, int] = (0, 0, 0)
    scale: float = 1.0
    variant_index: int = 0
    color: str = "Default"
    anim_phase_offset: int = 0
    anchor_tree_id: str = ""
    flags: int = 0


@dataclass
class Map(Asset):
    """A Trackmania map asset."""

    map_name: str = ""
    decoration: str = ""
    decoration_collection: str = ""
    map_size: Tuple[int, int, int] = (0, 0, 0)
    deco_base_height_offset: float = 0.0
    color_palette: str = "Classic"
    # 0x03043056: editor time of day in [0, 1); None keeps the mood default.
    day_time: Optional[float] = None
    dynamic_daylight: bool = False
    day_duration_ms: int = 0
    # 0x03043019: texture mod pack (path under the user/Packs folders, e.g.
    # ``Skins\Stadium\Mod\X.zip``) and the URL the game downloads it from.
    mod_pack_path: str = ""
    mod_pack_url: str = ""
    blocks: List[MapBlock] = field(default_factory=list)
    baked_blocks: List[MapBlock] = field(default_factory=list)
    free_blocks: List[MapFreeBlock] = field(default_factory=list)
    anchored_items: List[MapAnchoredItem] = field(default_factory=list)

    # Embedded resources kept as raw bytes; the addon re-feeds them into
    # GBXBPY via the loader registry to obtain Item / Mesh / etc. assets.
    embedded_items: Dict[str, bytes] = field(default_factory=dict, repr=False)
    thumbnail: bytes = field(default=b"", repr=False)
    xml_metadata: str = field(default="", repr=False)
    info: MapInfo = field(default_factory=MapInfo)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.MAP


__all__ = ["Map", "MapBlock", "MapFreeBlock", "MapAnchoredItem", "BlockSkin"]
