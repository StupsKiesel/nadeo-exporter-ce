"""Additive — screen-space additive emissive material.

Used for TM2020 ``TAdd*`` shaders (energy shields, portals, glow FX,
weapon effects). The geometry adds its colour to whatever is already in
the frame buffer; there is no depth write.

Common TM shader names that route here:
  * ``Tech3 Block TAdd.Shader.Gbx``
  * ``Tech3 Block TAddLighted.Shader.Gbx``
  * ``Tech3 Block TAddShield.Shader.Gbx``
  * ``Tech3 Block TAddSwitch.Shader.Gbx``
  * ``Tech3_Dyna_TAdd.Shader.Gbx``

Blender equivalent: Emission shader with additive blend (BLEND mode with
the material's alpha set low, or — better — an Emission node wired to
an Add shader so depth-sorting artefacts are minimised).  We use a simple
Emission + Transparent mix with ``surface_render_method = "BLENDED"``
and zero roughness / metallic so the surface is pure light.
"""

from __future__ import annotations

from ..spec import MaterialSpec, TextureRole
from ..images import load_image
from ._common import (
    new_material,
    add_output,
    add_image_texture,
    find_texture,
    attach_funcshader_uv,
)


def _set_additive_blend(mat) -> None:
    """Switch ``mat`` to additive-style blended rendering."""
    # Blender 4.2+ uses ``surface_render_method`` on the material.
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
    if hasattr(mat, "show_transparent_back"):
        try:
            mat.show_transparent_back = False
        except Exception:  # noqa: BLE001
            pass
    if hasattr(mat, "shadow_method"):
        try:
            mat.shadow_method = "NONE"
        except Exception:  # noqa: BLE001
            pass


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_Additive")
    _set_additive_blend(mat)

    tree = mat.node_tree
    links = tree.links

    # Emission node for the additive colour.
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.location = (-100, 50)

    # Transparent node so the background shows through.
    transp = tree.nodes.new("ShaderNodeBsdfTransparent")
    transp.location = (-100, -80)

    add_sh = tree.nodes.new("ShaderNodeAddShader")
    add_sh.location = (150, 0)

    out = add_output(tree, location=(380, 0))

    links.new(emit.outputs["Emission"], add_sh.inputs[0])
    links.new(transp.outputs["BSDF"], add_sh.inputs[1])
    links.new(add_sh.outputs["Shader"], out.inputs["Surface"])

    # Default colour: white (full-intensity glow). If the spec carries a
    # non-white colour it will tint the emission.
    r, g, b, _a = spec.color
    emit.inputs["Color"].default_value = (r, g, b, 1.0)
    emit.inputs["Strength"].default_value = 1.0

    # ---- Diffuse / emission texture -----------------------------------
    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-400, 50), label="Diffuse")
            attach_funcshader_uv(tree, tex, spec, TextureRole.DIFFUSE)
            links.new(tex.outputs["Color"], emit.inputs["Color"])

    # ---- Emissive texture (if separate) --------------------------------
    em_ref = find_texture(spec, TextureRole.EMISSIVE)
    if em_ref is not None and em_ref != diffuse_ref:
        img = load_image(em_ref.path, TextureRole.EMISSIVE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-400, 50), label="Emissive")
            attach_funcshader_uv(tree, tex, spec, TextureRole.EMISSIVE)
            links.new(tex.outputs["Color"], emit.inputs["Color"])

    # ---- Mask / alpha --------------------------------------------------
    mask_ref = find_texture(spec, TextureRole.MASK)
    if mask_ref is not None:
        m_img = load_image(mask_ref.path, role="Mask")
        if m_img is not None:
            m_tex = add_image_texture(tree, m_img, location=(-400, -200), label="Mask")
            # Use mask luminance to scale emission strength.
            sep = tree.nodes.new("ShaderNodeSeparateColor")
            sep.location = (-200, -200)
            links.new(m_tex.outputs["Color"], sep.inputs["Color"])
            emit.inputs["Strength"].default_value = 1.0
            links.new(sep.outputs[0], emit.inputs["Strength"])

    return mat


__all__ = ["build"]
