"""TriPlanar — terrain / cliff blending without UV unwrap.

A simple triplanar projection driven by world-space ``Object`` coords
through three image lookups on XY/YZ/XZ planes, blended by the world
normal. Uses Diffuse only for now; Normal can be added once the basic
projection is proven on a real terrain mesh.
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
    normal_map_color,
    wire_rough_metal,
)


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_TriPlanar")
    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(400, 0))
    out = add_output(tree, location=(700, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = spec.color
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.9

    # Shared mapping (one TexCoord + Mapping feeds every BOX-projected
    # texture — keeps tile-size in sync across Diffuse/Normal/RoughMetal).
    coord = tree.nodes.new("ShaderNodeTexCoord")
    coord.location = (-400, 0)
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.location = (-200, 0)
    scale = float(spec.params.get("triplanar_scale", 1.0))
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    vector_socket = mapping.outputs["Vector"]
    blend = float(spec.params.get("projection_blend", 0.25))

    # ---- Diffuse -------------------------------------------------------
    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(0, 200), label="DiffuseTri")
            tex.projection = "BOX"
            tex.projection_blend = blend
            links.new(vector_socket, tex.inputs["Vector"])
            links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

    # ---- Normal --------------------------------------------------------
    normal_ref = find_texture(spec, TextureRole.NORMAL)
    if normal_ref is not None:
        img = load_image(
            normal_ref.path, TextureRole.NORMAL,
            flip_normal_green=spec.flip_normal_green,
        )
        if img is not None:
            tex = add_image_texture(tree, img, location=(0, -200), label="NormalTri")
            tex.projection = "BOX"
            tex.projection_blend = blend
            links.new(vector_socket, tex.inputs["Vector"])
            nmap = tree.nodes.new("ShaderNodeNormalMap")
            nmap.location = (260, -200)
            links.new(normal_map_color(tree, tex.outputs["Color"], img), nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ---- RoughMetal ----------------------------------------------------
    rm_ref = find_texture(spec, TextureRole.ROUGH_METAL)
    if rm_ref is not None:
        img = load_image(rm_ref.path, TextureRole.ROUGH_METAL)
        if img is not None:
            wire_rough_metal(
                tree, bsdf, img,
                location=(0, -500),
                vector_socket=vector_socket,
            )

    return mat


__all__ = ["build"]
