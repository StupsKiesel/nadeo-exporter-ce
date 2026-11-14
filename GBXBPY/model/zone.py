"""Zone domain model.

``CGameCtnZone`` (``0x0305C000``) is the base zone class, carrying zone /
surface / water identity and height metadata.

Subclasses:

* :class:`ZoneFlat`     — ``CGameCtnZoneFlat``     (``0x0305D000``)
* :class:`ZoneFrontier` — ``CGameCtnZoneFrontier``  (``0x0305E000``)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .asset import Asset, AssetKind


@dataclass
class Zone(Asset):
    """Base CGameCtnZone asset — shared by ZoneFlat and ZoneFrontier."""

    # 0x0305C003
    height: int = 0
    zone_id: str = ""
    surface_id: str = ""

    # 0x0305C004 (skippable)
    depth: int = 0
    old_zone: bool = False

    # 0x0305C005
    has_water: bool = False

    # 0x0305C006
    is_large_zone: bool = False

    # 0x0305C007
    visual_top_ground_height: float = 0.0

    # 0x0305C008 (alternative height — may override 0x0305C003.height)
    height_alt: int = 0

    # 0x0305C00B
    water_id: str = ""

    # 0x0305C00C
    forced_parent_zone_frontier_id: str = ""

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.ZONE


@dataclass
class ZoneFlat(Zone):
    """CGameCtnZoneFlat (``0x0305D000``) — flat terrain zone."""

    # 0x0305D001 — the four linked block-info assets (external node refs)
    block_info_flat_path: str = ""     # CGameCtnBlockInfoFlat
    block_info_clip_path: str = ""     # CGameCtnBlockInfoClip
    block_info_road_path: str = ""     # CGameCtnBlockInfoRoad
    block_info_pylon_path: str = ""    # CGameCtnBlockInfoPylon

    # 0x0305D002 (skippable)
    ground_only: bool = False

    # 0x0305D003 (skippable)
    auto_simplify_genealogy: bool = False


@dataclass
class ZoneFrontier(Zone):
    """CGameCtnZoneFrontier (``0x0305E000``) — boundary zone between two zones."""

    # 0x0305E001
    block_info_frontier_path: str = ""  # CGameCtnBlockInfoFrontier
    parent_zone_id: str = ""
    child_zone_id: str = ""

    # 0x0305E002
    block_y_offset_from_parent: int = 0

    # 0x0305E003 / 0x0305E004 (004 supersedes 003 and adds two extra bools)
    frontier_parent_border_accept_pylons: bool = False
    frontier_child_border_accept_pylons: bool = False
    frontier_transition_middle_accept_pylons: bool = False  # 004 only
    frontier_straight_middle_accept_pylons: bool = False    # 004 only


@dataclass
class ZoneTransition(Zone):
    """CGameCtnZoneTransition (``0x0314D000``) — a blend zone between two zone types."""

    # 0x0314D000
    block_info_transition_path: str = ""  # CGameCtnBlockInfoTransition

    # 0x0314D005
    replacement_zone_id: str = ""

    # 0x0314D008 — cardinal border types (EBorderType enum as int)
    border_north: int = 0
    border_east: int = 0
    border_south: int = 0
    border_west: int = 0


__all__ = ["Zone", "ZoneFlat", "ZoneFrontier", "ZoneTransition"]
