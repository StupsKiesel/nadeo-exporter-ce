"""Public dataclasses describing an input to GBXMAT.

These types are intentionally pure-Python (no ``bpy``, no GBXBPY) so
callers can construct them in any environment and so they are trivially
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Tuple


# Canonical texture-role tags. Builders dispatch on these strings, so
# spelling matters. ``str`` (not Enum) keeps the public API friction-free
# for callers — they may also pass non-canonical roles, which builders
# are free to ignore.
class TextureRole:
    DIFFUSE = "Diffuse"          # sRGB base color
    NORMAL = "Normal"            # tangent-space normal map (DirectX Y)
    ROUGH_METAL = "RoughMetal"   # R = roughness, G = metallic (BC4 = R only)
    DETAIL = "Detail"            # tiled overlay (often modulated)
    LIGHTMAP = "Lightmap"        # baked lighting; uses second UV channel
    EMISSIVE = "Emissive"        # sRGB self-illumination
    OCCLUSION = "Occlusion"      # AO, single channel
    HUE_MASK = "HueMask"         # team/skin color mask
    SPECULAR = "Specular"        # legacy spec-only path
    MASK = "Mask"                # generic single-channel mask


@dataclass(frozen=True)
class TextureRef:
    """One texture slot referenced by a material.

    ``path`` must be an absolute path to a file Blender or Pillow can
    decode. The caller is responsible for resolving game-relative paths
    (e.g. via a project-specific resolver) before constructing this.
    """

    role: str                    # one of :class:`TextureRole` (or custom)
    path: str                    # absolute filesystem path


@dataclass(frozen=True)
class MaterialSpec:
    """Everything GBXMAT needs to build one Trackmania material.

    Construct one of these per unique material; pass it to
    :func:`gbxmat.get_or_create_material`. GBXMAT dedups internally based
    on a stable signature derived from this spec, so identical specs
    always return the same ``bpy.types.Material`` datablock.
    """

    family: str                                    # shader family, e.g. "Stadium.Default"
    name_hint: str = "TM_Material"                 # base name for the Material datablock
    color: Tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    textures: Tuple[TextureRef, ...] = ()
    params: Mapping[str, float] = field(default_factory=dict)
    physics_id: str = ""                           # TM surface physic id (string tag)
    gameplay_id: str = ""                          # TM surface gameplay id (string tag)

    # Optional — caller may set to ``False`` for normal maps that are
    # already in OpenGL Y convention (rare).
    flip_normal_green: bool = True

    # When True, the diffuse texture's alpha channel is wired into the
    # BSDF Alpha input. Most TM materials carry alpha in the diffuse for
    # reasons unrelated to surface opacity (e.g. detail masking) — only
    # set this when the source material's ``OpacityIsDiffuseAlpha`` flag
    # is on.
    opacity_from_diffuse_alpha: bool = False

    # When True, the material uses the diffuse alpha as a binary cutout
    # mask (vegetation billboards, fences). Builders threshold diffuse
    # alpha at 0.5 and use alpha-clip / dithered coverage. DITHERED alone
    # is not binary clipping. Implies opacity from the diffuse alpha.
    alpha_cutout: bool = False

    # When True, the material blends with the frame buffer using standard
    # over-compositing (src-alpha / inv-src-alpha). Used for semitransparent
    # surfaces such as TDiffABlend road markings, glass overlays, and HUD
    # decals that need depth-sorted blending rather than binary clip.
    alpha_blend: bool = False

    # When True, the material adds its colour to the frame buffer without
    # depth-writing (TAdd* shaders: energy shields, portals, glow effects).
    # Builders should use additive-blend mode with BSDF emission.
    additive: bool = False

    # Shader / base-material name in the TM game-data hierarchy
    # (e.g. "Tech3_Block_TDiffABlend_Spec_Norm_PC3"). Supplied
    # best-effort from the material chain; may be empty when the chain
    # is not loaded. Builders may inspect this for fine-grained decisions
    # that the family string alone can't encode.
    shader_name: str = ""

    # Animated material parameters from a paired ``.FuncShader.Gbx``.
    # Each tuple entry describes one animation:
    #   (kind, layer_name, period, input_val_id,
    #    atlas_cols, atlas_rows, atlas_frames,
    #    uv_offset_x, uv_offset_y,
    #    uv_scale_x, uv_scale_y,
    #    uv_speed_x, uv_speed_y)
    # ``kind`` is one of ``'Atlas'``, ``'UvScroll'``, ``'Composite'``.
    # The tuple form keeps :class:`MaterialSpec` hashable and dedup-safe.
    func_shaders: Tuple[
        Tuple[str, str, float, str, int, int, int,
              float, float, float, float, float, float],
        ...,
    ] = ()


__all__ = ["TextureRole", "TextureRef", "MaterialSpec"]
