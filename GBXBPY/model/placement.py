"""PlacementParam domain model.

``CGameItemPlacementParam`` (``0x2E020000``) defines how an item can be placed
in the editor: grid snapping, pivot points, fly mode, and magnet locations.
Files are named ``*.PlaceParam.Gbx``.

This data is used by the Blender addon to:
- Display correct snap increments in UI panels.
- Reconstruct pivot lists for rotation handles.
- Show magnet attachment points for procedural placement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from .asset import Asset, AssetKind

# Simple (x, y, z) tuple for positions that come from GbxVec3
Vec3Tuple = Tuple[float, float, float]
# Simple (x, y, z, w) tuple for quaternions from GbxQuat
QuatTuple = Tuple[float, float, float, float]


@dataclass(slots=True)
class MagnetLoc:
    """One magnet attachment location (from chunk 0x2E020004 GbxPose3D)."""

    position: Vec3Tuple = (0.0, 0.0, 0.0)
    rotation: QuatTuple = (0.0, 0.0, 0.0, 1.0)


@dataclass
class PlacementParam(Asset):
    """A ``CGameItemPlacementParam`` asset (``*.PlaceParam.Gbx``)."""

    # --- Flags (chunk 000) ---
    auto_rotation: bool = False
    yaw_only: bool = False
    not_on_object: bool = False
    switch_pivot_manually: bool = False
    ghost_mode: bool = False
    has_path: bool = False

    # --- Placement cube (chunk 000) ---
    cube_center: Vec3Tuple = (0.0, 0.0, 0.0)
    cube_size: float = 0.0

    # --- Grid snap (chunk 000) ---
    grid_snap_h_step: float = 0.0
    grid_snap_v_step: float = 0.0
    grid_snap_h_offset: float = 0.0
    grid_snap_v_offset: float = 0.0

    # --- Fly mode (chunk 000) ---
    fly_step: float = 0.0
    fly_offset: float = 0.0

    # --- Pivot (chunk 000 + 001) ---
    pivot_snap_distance: float = 0.0
    pivot_positions: List[Vec3Tuple] = field(default_factory=list)
    pivot_rotations: List[QuatTuple] = field(default_factory=list)

    # --- Magnets (chunk 004) ---
    magnet_locs: List[MagnetLoc] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.PLACEMENT


__all__ = ["PlacementParam", "MagnetLoc"]
