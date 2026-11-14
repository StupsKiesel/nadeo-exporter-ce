"""Glass / transparent — alpha-blended surface.

Trackmania glass (``TDiffABlend``) has no refraction: the diffuse colour
is blended over the background by its alpha, with specular reflections on
top. The builder mirrors that with a non-transmissive Principled BSDF in
blended mode. Roughness/metallic come from the ``_R`` texture when the
material has one. Stadium water and water walls use :mod:`.water` instead.
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
    wire_rough_metal,
)


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_Glass")
    if hasattr(mat, "blend_method"):
        mat.blend_method = "BLEND"
    if hasattr(mat, "shadow_method"):
        mat.shadow_method = "HASHED"

    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = spec.color
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.05
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = 1.45
    if "Alpha" in bsdf.inputs:
        bsdf.inputs["Alpha"].default_value = max(0.05, spec.color[3] * 0.6)

    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, 200), label="Diffuse")
            attach_funcshader_uv(tree, tex, spec, TextureRole.DIFFUSE)
            links.new(apply_hue_recolour(tree, spec, tex.outputs["Color"], source_tex=tex), bsdf.inputs["Base Color"])
            if "Alpha" in bsdf.inputs:
                links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])

    rough_ref = find_texture(spec, TextureRole.ROUGH_METAL)
    if rough_ref is not None:
        img = load_image(rough_ref.path, TextureRole.ROUGH_METAL)
        if img is not None:
            tex = wire_rough_metal(tree, bsdf, img, location=(-600, -400))
            attach_funcshader_uv(tree, tex, spec, TextureRole.ROUGH_METAL)

    normal_ref = find_texture(spec, TextureRole.NORMAL)
    if normal_ref is not None:
        img = load_image(
            normal_ref.path, TextureRole.NORMAL,
            flip_normal_green=spec.flip_normal_green,
        )
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -100), label="Normal")
            attach_funcshader_uv(tree, tex, spec, TextureRole.NORMAL)
            nmap = tree.nodes.new("ShaderNodeNormalMap")
            nmap.location = (-300, -100)
            links.new(normal_map_color(tree, tex.outputs["Color"], img), nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    return mat


__all__ = ["build"]
