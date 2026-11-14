"""Domain dataclasses — the stable public contract of GBXBPY.

Anything exported from :mod:`GBXBPY.model` is what the Blender addon and
external tools are allowed to depend on. These are plain Python dataclasses;
they must not contain ``construct.Container`` instances, NICE parser types,
or ``bpy`` types.

Loaders in :mod:`GBXBPY.load` produce these objects. Normalization passes in
:mod:`GBXBPY.normalize` operate on them. Everything downstream consumes them.

Coordinate convention: all geometry, placement, and rotation data is in
**Blender axes** (right-handed, Z-up). See :mod:`GBXBPY.coords`.
"""

from __future__ import annotations

from .common import Vec2, Vec3, Quat, Color, Box, IsoLoc
from .asset import Asset, AssetKind
from .material import Material, TextureSlot, ShaderParam
from .funcshader import FuncShader, FuncShaderKind
from .mesh import (
    Vertex,
    Submesh,
    MeshLOD,
    MaterialBinding,
    BoneJoint,
    LightAttachment,
    Mesh,
)
from .block import Block, BlockVariant, BlockMobil, BlockUnit
from .clip import Clip
from .mediatracker import (
    MediaTrackerBlock,
    MediaTrackerTrack,
    MediaTrackerClip,
    MediaTrackerClipDocument,
)
from .item import CustomBlock, Item, ItemPlacement, EntityVariant
from .geometry import CrystalLayer, MeshPart
from .prefab import DynaAnimDriver, EntityLink, PlacementGroup, Prefab, PrefabEntity
from .map import BlockSkin, Map, MapBlock, MapFreeBlock, MapAnchoredItem, MapInfo
from .decoration import Decoration, DecorationSolid, Mood, DecorationMood, DecorationSize
from .gameskin import GameSkin, SkinFid
from .terrain import TerrainModel, TerrainZone, TerrainModifier
from .vegetation import VegetationModel, VegetationVariant
from .zone import Zone, ZoneFlat, ZoneFrontier, ZoneTransition
from .particle import ParticleEmission, ParticleEmitterModel
from .fx import FxEmitter, FxSystem, FxVar, FxVarUpdate
from .kinematic import KinematicConstraint, AnimSubFunc
from .placement import PlacementParam, MagnetLoc

__all__ = [
    # primitives
    "Vec2", "Vec3", "Quat", "Color", "Box", "IsoLoc",
    # base
    "Asset", "AssetKind",
    # material
    "Material", "TextureSlot", "ShaderParam",
    "FuncShader", "FuncShaderKind",
    # mesh
    "Vertex", "Submesh", "MeshLOD", "MaterialBinding",
    "BoneJoint", "LightAttachment", "Mesh",
    # block
    "Block", "BlockVariant", "BlockMobil", "BlockUnit",
    # clip
    "Clip",
    # MediaTracker
    "MediaTrackerBlock", "MediaTrackerTrack", "MediaTrackerClip",
    "MediaTrackerClipDocument",
    # item
    "CustomBlock", "Item", "ItemPlacement", "EntityVariant", "MeshPart", "CrystalLayer",
    # prefab
    "Prefab", "PrefabEntity", "DynaAnimDriver", "EntityLink", "PlacementGroup",
    # map
    "Map", "MapBlock", "MapFreeBlock", "MapAnchoredItem", "BlockSkin", "MapInfo",
    # decoration
    "Decoration", "DecorationSolid", "Mood", "DecorationMood", "DecorationSize",
    # skin
    "GameSkin", "SkinFid",
    # terrain
    "TerrainModel", "TerrainZone", "TerrainModifier",
    # vegetation
    "VegetationModel", "VegetationVariant",
    # zone
    "Zone", "ZoneFlat", "ZoneFrontier", "ZoneTransition",
    # particle / effects
    "ParticleEmission", "ParticleEmitterModel",
    "FxEmitter", "FxSystem", "FxVar", "FxVarUpdate",
    # kinematic
    "KinematicConstraint", "AnimSubFunc",
    # placement
    "PlacementParam", "MagnetLoc",
]
