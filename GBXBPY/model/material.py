"""Material domain model.

A :class:`Material` is the GBXBPY representation of a Trackmania material
(``CPlugMaterial`` / ``CPlugMaterialUserInst``) after inheritance and shader
binding resolution. It is what :mod:`GBXBPY.normalize.materials` produces
and what the addon's ``materials/`` layer consumes when building Blender
materials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .asset import Asset, AssetKind
from .common import Color
from .funcshader import FuncShader


@dataclass(slots=True)
class TextureSlot:
    """A single texture binding inside a material.

    Texture paths are GameData-relative; resolution to absolute paths
    happens in :mod:`GBXBPY.resolve`.
    """

    name: str = ""               # e.g. "Diffuse", "Normal", "RoughMetal", "SelfIllum"
    texture_path: str = ""       # GameData-relative .Texture.Gbx path
    tiling_u: str = "Wrap"       # "Wrap" | "Mirror" | "Clamp"
    tiling_v: str = "Wrap"
    texarray_layer: Optional[int] = None  # vista terrain & similar; None when N/A


@dataclass(slots=True)
class ShaderParam:
    """A named shader parameter (float, vector, or color)."""

    name: str = ""
    value: Any = None


@dataclass
class Material(Asset):
    """A fully resolved Trackmania material, ready for the addon.

    All fields here represent the *effective* material after inheritance and
    user-instance overlays have been collapsed by :mod:`GBXBPY.normalize`.
    """

    base_material_path: str = ""  # GameData-relative path to the base .Material.Gbx, if any
    shader_family: str = ""       # e.g. "TDSN", "TDLm"; populated by normalization
    surface_physic_id: str = ""
    surface_gameplay_id: str = ""
    color_modifier: Color = field(default_factory=Color)
    texture_size: float = 1.0
    features: List[str] = field(default_factory=list)
    flags: Dict[str, bool] = field(default_factory=dict)
    texture_slots: List[TextureSlot] = field(default_factory=list)
    shader_params: List[ShaderParam] = field(default_factory=list)
    func_shader: Optional[FuncShader] = None  # animated UV / atlas / driver
    # Referenced ``*.ColorTable.gbx.json`` (relative reference-table path);
    # empty when the material uses the default table.
    color_table_path: str = ""

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.MATERIAL

    def slot(self, name: str) -> Optional[TextureSlot]:
        """Look up a texture slot by case-insensitive name."""
        target = name.lower()
        for s in self.texture_slots:
            if s.name.lower() == target:
                return s
        return None


__all__ = ["Material", "TextureSlot", "ShaderParam"]
