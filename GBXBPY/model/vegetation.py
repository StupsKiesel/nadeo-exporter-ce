"""Vegetation domain model.

Parametric vegetation (trees, bushes) that uses ``CPlugVegetTreeModel``
and similar plug classes. Variants here are random-seedable LOD/visual
options the editor can pick from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .asset import Asset, AssetKind
from .mesh import Mesh


@dataclass(slots=True)
class VegetationVariant:
    """One visual variant of a vegetation model."""

    name: str = ""
    mesh: Mesh = field(default_factory=Mesh)
    weight: float = 1.0


@dataclass
class VegetationModel(Asset):
    """A Trackmania parametric vegetation asset."""

    variants: List[VegetationVariant] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.VEGETATION


__all__ = ["VegetationModel", "VegetationVariant"]
