"""Semantic geometry shared by item definitions and object-model surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mesh import Mesh

Matrix4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]
IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0),
)


@dataclass(slots=True)
class MeshPart:
    name: str = ""
    mesh: Mesh | None = None
    role: str = "geometry"
    visible: bool = True
    collidable: bool = True
    enabled: bool = True
    matrix: Matrix4 = IDENTITY
    layer_index: int = -1
    layer_id: str = ""
    layer_type: str = ""
    surface_type: str = ""
    bone_index: int = -1
    face_groups: list[int] = field(default_factory=list)
    material_indices: list[int] = field(default_factory=list)
    face_sources: list[int] = field(default_factory=list)


@dataclass(slots=True)
class CrystalLayer:
    """Source stack identity, including layers which do not produce meshes."""

    index: int
    id: str
    name: str
    type: str
    enabled: bool
