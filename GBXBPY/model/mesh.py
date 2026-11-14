"""Mesh domain model.

Represents a Trackmania visual mesh (``CPlugSolid2Model`` / ``CPlugMesh``)
in a Blender-ready form: positions and normals are already in Z-up space
(see :mod:`GBXBPY.coords`), triangle indices are flattened, and per-LOD
material assignments are precomputed.

The addon consumes this structure to build a Blender mesh datablock; it
must not need to interpret raw NICE chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .asset import Asset, AssetKind
from .common import Box, Color, Quat, Vec2, Vec3
from .geometry import MeshPart


@dataclass(slots=True)
class Vertex:
    """A single mesh vertex.

    ``layer_idx_a`` / ``layer_idx_b`` carry per-vertex texture-array layer
    indices used by vista terrain meshes; both default to ``-1`` when the
    mesh has no such stream.
    """

    position: Vec3 = field(default_factory=Vec3)
    normal: Vec3 = field(default_factory=lambda: Vec3(0.0, 0.0, 1.0))
    uv0: Vec2 = field(default_factory=Vec2)
    uv1: Vec2 = field(default_factory=Vec2)
    uv2: Vec2 = field(default_factory=Vec2)
    color: Optional[Color] = None     # None when the vertex has no color stream
    bone_index: int = -1              # skeleton bone index; -1 when unrigged
    layer_idx_a: int = -1
    layer_idx_b: int = -1
    bone_weights: Tuple[Tuple[int, float], ...] = ()


@dataclass(slots=True)
class Submesh:
    """A contiguous triangle range that shares a single material."""

    material_index: int = 0
    indices: List[int] = field(default_factory=list)  # triangle indices into the parent vertex list


@dataclass(slots=True)
class MeshLOD:
    """One LOD level of a mesh.

    ``lod`` matches the Trackmania convention: 1 = highest detail, then 2,
    4, 8, 16. ``distance`` is the switch distance in meters (0 if unknown).

    ``pose_blocks`` carries alternative vertex positions for ``VertexTween``
    meshes (animated flags, bushes, banners…). When non-empty, each inner
    list has the same length as ``vertices`` and represents one full
    animation pose. The addon turns these into Blender shape keys so the
    in-engine wind animation can be reproduced on the timeline.
    """

    lod: int = 1
    distance: float = 0.0
    vertices: List[Vertex] = field(default_factory=list)
    submeshes: List[Submesh] = field(default_factory=list)
    bounding_box: Box = field(default_factory=Box)
    pose_blocks: List[List[Vec3]] = field(default_factory=list)


@dataclass(slots=True)
class MaterialBinding:
    """A material slot on a mesh.

    Either ``material_path`` (link to an external .Material.Gbx) or
    ``inline_material`` (an embedded :class:`~GBXBPY.model.material.Material`)
    will be set, depending on whether the mesh uses linked or inline materials.
    """

    material_path: str = ""
    inline_material: object = None    # Material; `object` to avoid cyclic import
    inline_textures: Dict[str, str] = field(default_factory=dict)
    surface_physic_id: str = ""
    surface_gameplay_id: str = ""

    # ---- CPlugMaterialUserInst extras -------------------------------------
    # Internal shader model name as declared by the game on the per-mesh
    # material instance (e.g. "TDSN", "TDiffABlend", "TAdd", "TSelfIllum").
    # When present, this is the most reliable render-mode signal — far more
    # trustworthy than guessing from the .Material.Gbx filename.
    shader_model: str = ""
    # Internal material name from the user-instance (may differ from filename).
    material_name: str = ""
    # Per-instance RGB tint. Empty tuple means "no tint" (pure white).
    # When set, contains 3 floats in linear RGB.
    color_tint: tuple = ()
    # Linear RGB ``TargetColor`` declared by the user instance (item custom
    # colour for ``Material_BlockCustom`` materials). Empty when not declared;
    # black is a valid colour here.
    target_color: tuple = ()

    # ---- Per-binding tiling (from CPlugMaterialUserInst chunk 0x090FD001) -
    # World-space length (in metres) of one texture repetition. The game
    # multiplies UVs by ``1 / texture_size`` before sampling, which is
    # how Tech3 triplanar materials get their per-mesh tiling. Default
    # ``1.0`` means "no scaling" (UVs sampled as authored).
    texture_size: float = 1.0
    tiling_u: str = "Wrap"   # "Wrap" | "Mirror" | "Clamp"
    tiling_v: str = "Wrap"
    # Prefab entity TextureId selects a layer of CPlugBitmap texture arrays.
    texture_array_index: int = 0


@dataclass(slots=True)
class BoneJoint:
    """A single joint of a mesh skeleton, in Blender axes."""

    name: str = ""
    parent: int = -1
    translation: Vec3 = field(default_factory=Vec3)
    rotation: Quat = field(default_factory=Quat.identity)


@dataclass(slots=True)
class LightAttachment:
    """A light placement attached to a mesh.

    ``transform`` is the raw TM ``Iso4`` matrix (3×3 rotation + 3-vec
    translation) stored as 12 floats in row-major order::

        [XX, XY, XZ, YX, YY, YZ, ZX, ZY, ZZ, TX, TY, TZ]

    The TM→Blender swizzle is intentionally **not** applied in the
    loader because converting a 3×3 rotation matrix to a Blender
    quaternion requires ``mathutils`` (which is only available inside
    Blender). The addon performs the conversion when materializing the
    light.
    """

    name: str = ""
    light_path: str = ""              # GameData-relative .Light.Gbx, if any
    transform: List[float] = field(default_factory=list)
    light_type: str = ""              # Embedded CGx light, otherwise a file reference
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    intensity: Optional[float] = None
    distance: Optional[float] = None


@dataclass
class Mesh(Asset):
    """A complete mesh asset: LODs, materials, optional skeleton, optional lights."""

    lods: List[MeshLOD] = field(default_factory=list)
    materials: List[MaterialBinding] = field(default_factory=list)
    skeleton: List[BoneJoint] = field(default_factory=list)
    lights: List[LightAttachment] = field(default_factory=list)
    icon: bytes = field(default=b"", repr=False)
    # Vista terrain: vertex material slot (``layer_idx_a/b``) -> collision
    # material name -> vote count, matched against the static object's
    # collision shape. Empty for non-terrain meshes.
    terrain_slot_votes: Dict[int, Dict[str, int]] = field(default_factory=dict)
    # Object-model wrappers may use a separate, non-rendered collision surface.
    collidable: bool = True
    collision_parts: List[MeshPart] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.MESH

    def lod_levels(self) -> List[int]:
        """Sorted unique LOD level numbers present on this mesh."""
        return sorted({lod.lod for lod in self.lods})

    def lod(self, level: int = 1) -> Optional[MeshLOD]:
        """Return the LOD with the given level number, or ``None``."""
        for lod in self.lods:
            if lod.lod == level:
                return lod
        return None


__all__ = [
    "Vertex",
    "Submesh",
    "MeshLOD",
    "MaterialBinding",
    "BoneJoint",
    "LightAttachment",
    "Mesh",
]
