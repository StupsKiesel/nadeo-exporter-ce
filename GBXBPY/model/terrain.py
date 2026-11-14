"""Terrain domain model.

Trackmania terrain is **not** "just another block family". It carries its
own zones, modifier blocks, and (for Vista environments) per-vertex
texture-array layer info. This module exposes the addon-ready shape; the
heavy lifting that produces it lives in :mod:`GBXBPY.normalize` and
:mod:`GBXBPY.load.terrain`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .asset import Asset, AssetKind
from .mesh import Mesh


@dataclass(slots=True)
class TerrainZone:
    """A named terrain region (e.g. dirt, grass, asphalt)."""

    zone_id: str = ""
    material_path: str = ""              # GameData-relative .Material.Gbx
    texarray_layer: Optional[int] = None
    color_modifier: str = "Default"


@dataclass(slots=True)
class TerrainModifier:
    """A block-shaped modifier that overrides terrain zones in its footprint."""

    name: str = ""
    target_zone_id: str = ""
    replacement_zone_id: str = ""


@dataclass
class TerrainModel(Asset):
    """A baked terrain mesh with its zone and modifier metadata."""

    mesh: Optional[Mesh] = None
    zones: List[TerrainZone] = field(default_factory=list)
    modifiers: List[TerrainModifier] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.TERRAIN


__all__ = ["TerrainModel", "TerrainZone", "TerrainModifier"]
