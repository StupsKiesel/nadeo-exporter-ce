"""AlphaBlend — semitransparent depth-sorted surface.

Used for TM2020 ``TDiffABlend*`` shaders and similar materials that
blend with the background using standard over-compositing (src-alpha /
inv-src-alpha) rather than binary clipping. Unlike the Decal family
these surfaces are genuine semi-transparent geometry — banners, signage
glass, coloured mesh overlays — and require proper depth sorting.

Common TM shader names:
  * ``Tech3 Block TDiffABlend_Spec_Norm PC3``
  * ``Tech3 Block TDiffA_Spec_Norm PC3``
  * ``Tech3_Block_TDiffABlend_SpecNorm_CubeOut``
  * ``Tech3_Block_TDiffA_SpecNorm_CubeOut``

Blender equivalent: Principled BSDF with ``surface_render_method =
"BLENDED"`` and the diffuse alpha fed directly into BSDF Alpha. Normal
and roughness/metallic slots wired as usual.
"""

from __future__ import annotations

from ..spec import MaterialSpec, TextureRole
from ..images import load_image
from ._common import (
    new_material,
    add_output,
    add_principled,
    add_image_texture,
    find_texture,
    attach_funcshader_uv,
    apply_hue_recolour,
    normal_map_color,
)


def _set_alpha_blend(mat) -> None:
    if hasattr(mat, "surface_render_method"):
        try:
            mat.surface_render_method = "BLENDED"
        except Exception:  # noqa: BLE001
            pass
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = "BLEND"
        except Exception:  # noqa: BLE001
            pass
    if hasattr(mat, "shadow_method"):
        try:
            mat.shadow_method = "CLIP"
        except Exception:  # noqa: BLE001
            pass


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_AlphaBlend")
    _set_alpha_blend(mat)

    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = spec.color
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.4

    # ---- Diffuse -------------------------------------------------------
    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, 200), label="Diffuse")
            attach_funcshader_uv(tree, tex, spec, TextureRole.DIFFUSE)
            links.new(apply_hue_recolour(tree, spec, tex.outputs["Color"], source_tex=tex), bsdf.inputs["Base Color"])
            # Alpha from the diffuse texture IS the opacity for these mats.
            if "Alpha" in bsdf.inputs:
                links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])

    # ---- Normal --------------------------------------------------------
    normal_ref = find_texture(spec, TextureRole.NORMAL)
    if normal_ref is not None:
        img = load_image(
            normal_ref.path, TextureRole.NORMAL,
            flip_normal_green=spec.flip_normal_green,
        )
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -200), label="Normal")
            attach_funcshader_uv(tree, tex, spec, TextureRole.NORMAL)
            nmap = tree.nodes.new("ShaderNodeNormalMap")
            nmap.location = (-300, -200)
            links.new(normal_map_color(tree, tex.outputs["Color"], img), nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ---- RoughMetal ----------------------------------------------------
    rm_ref = find_texture(spec, TextureRole.ROUGH_METAL)
    if rm_ref is not None:
        img = load_image(rm_ref.path, TextureRole.ROUGH_METAL)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -500), label="RoughMetal")
            sep = tree.nodes.new("ShaderNodeSeparateColor")
            sep.location = (-300, -500)
            links.new(tex.outputs["Color"], sep.inputs["Color"])
            if "Roughness" in bsdf.inputs:
                links.new(sep.outputs[0], bsdf.inputs["Roughness"])
            if "Metallic" in bsdf.inputs:
                src_channels = getattr(img, "source_channels", None) or \
                    (img.get("source_channels") if hasattr(img, "get") else None)
                if src_channels != 1:
                    links.new(sep.outputs[1], bsdf.inputs["Metallic"])

    # ---- Emissive ------------------------------------------------------
    em_ref = find_texture(spec, TextureRole.EMISSIVE)
    if em_ref is not None:
        img = load_image(em_ref.path, TextureRole.EMISSIVE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -800), label="Emissive")
            attach_funcshader_uv(tree, tex, spec, TextureRole.EMISSIVE)
            em_socket = (
                bsdf.inputs.get("Emission Color")
                or bsdf.inputs.get("Emission")
            )
            if em_socket is not None:
                links.new(tex.outputs["Color"], em_socket)
                if "Emission Strength" in bsdf.inputs:
                    bsdf.inputs["Emission Strength"].default_value = 1.0

    return mat


__all__ = ["build"]
