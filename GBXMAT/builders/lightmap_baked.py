"""LightmapBaked — diffuse multiplied by a baked lightmap.

The Lightmap texture uses the second UV channel ("Lightmap" in the
addon's naming). The result is Diffuse * Lightmap fed straight into
Base Color of an unlit-ish Principled BSDF (roughness=1, no metal).
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
)


def _add_uvmap(tree, uv_name: str, location):
    n = tree.nodes.new("ShaderNodeUVMap")
    n.uv_map = uv_name
    n.location = location
    return n


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_LightmapBaked")
    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = spec.color
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 1.0

    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    lm_ref = find_texture(spec, TextureRole.LIGHTMAP)

    diffuse_tex = None
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            diffuse_tex = add_image_texture(
                tree, img, location=(-700, 200), label="Diffuse"
            )

    lm_tex = None
    if lm_ref is not None:
        img = load_image(lm_ref.path, TextureRole.LIGHTMAP)
        if img is not None:
            lm_tex = add_image_texture(
                tree, img, location=(-700, -200), label="Lightmap"
            )
            uv = _add_uvmap(tree, "Lightmap", (-1000, -200))
            links.new(uv.outputs["UV"], lm_tex.inputs["Vector"])

    if diffuse_tex and lm_tex:
        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.location = (-300, 0)
        # Blender 4.x mix node: factor = 1, A = diffuse, B = lightmap
        if "Factor" in mix.inputs:
            mix.inputs["Factor"].default_value = 1.0
        a_in = mix.inputs.get("A") or mix.inputs[6]
        b_in = mix.inputs.get("B") or mix.inputs[7]
        result_out = mix.outputs.get("Result") or mix.outputs[2]
        links.new(diffuse_tex.outputs["Color"], a_in)
        links.new(lm_tex.outputs["Color"], b_in)
        links.new(result_out, bsdf.inputs["Base Color"])
    elif diffuse_tex:
        links.new(diffuse_tex.outputs["Color"], bsdf.inputs["Base Color"])
    elif lm_tex:
        links.new(lm_tex.outputs["Color"], bsdf.inputs["Base Color"])

    return mat


__all__ = ["build"]
