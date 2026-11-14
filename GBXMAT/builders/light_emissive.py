"""LightEmissive — neon, light glows, screen panels.

Diffuse drives both Base Color and Emission. Strong emission strength
(default 5.0) so it visibly "glows" in Cycles/Eevee bloom passes.
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
)


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_LightEmissive")

    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    em_socket = (
        bsdf.inputs.get("Emission Color")
        or bsdf.inputs.get("Emission")
    )

    bsdf.inputs["Base Color"].default_value = spec.color
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 1.0
    if em_socket is not None:
        em_socket.default_value = spec.color
    if "Emission Strength" in bsdf.inputs:
        bsdf.inputs["Emission Strength"].default_value = float(
            spec.params.get("emission_strength", 5.0)
        )

    # Prefer dedicated Emissive texture; otherwise fall back to Diffuse.
    em_ref = find_texture(spec, TextureRole.EMISSIVE) or find_texture(spec, TextureRole.DIFFUSE)
    if em_ref is not None:
        img = load_image(em_ref.path, TextureRole.EMISSIVE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, 0), label="Emissive")
            # SelfIllum / Emissive is the dominant FuncShader target on
            # neon panels and torch flames; try the emissive role first
            # then fall back to diffuse so the single-animation case
            # (one FuncShader, ambiguous layer) still wires correctly.
            if not attach_funcshader_uv(tree, tex, spec, TextureRole.EMISSIVE):
                attach_funcshader_uv(tree, tex, spec, TextureRole.DIFFUSE)
            links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            if em_socket is not None:
                links.new(tex.outputs["Color"], em_socket)

    return mat


__all__ = ["build"]
