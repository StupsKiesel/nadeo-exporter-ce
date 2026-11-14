"""GrassFence — grass fins of Stadium grass zones (``Tech3 GrassFence_VDepLight``).

The fins are rows of 0.25 m vertical cards over the grass tile; their
normals point up so they shade like the ground. ``FenceA`` (role
``Diffuse``) is a grey blade strip whose alpha is the blade silhouette.
The game tints the blades with the ground grass; here ``GrassGround``
(the material's ``Diffuse`` slot, ``Grass_D``) is sampled at the fin's
world XY with the ground's 32 m tiling, modulated by the same ``GrassX2``
overlay as the ground, and multiplied by the normalised blade shade.
Dithered alpha-cutout, two-sided.
"""

from __future__ import annotations

from ..spec import MaterialSpec, TextureRole
from ..images import load_image
from ._common import new_material, add_output, add_principled, add_image_texture, find_texture, apply_grass_x2
from .decal import _set_cutout_render

# Mean linear value of the blade texels (sRGB ~0.4); the blade shade is
# divided by it so the fins average the ground's colour.
_BLADE_MEAN = 0.14
GROUND_ROLE = "GrassGround"


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_GrassFence")
    tree = mat.node_tree
    links = tree.links
    _set_cutout_render(mat)
    mat.use_backface_culling = False

    bsdf = add_principled(tree, location=(300, 0))
    bsdf.inputs["Base Color"].default_value = spec.color
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["Metallic"].default_value = 0.0
    links.new(bsdf.outputs["BSDF"], add_output(tree, location=(600, 0)).inputs["Surface"])

    blade = None
    ref = find_texture(spec, TextureRole.DIFFUSE)
    image = load_image(ref.path, TextureRole.DIFFUSE) if ref is not None else None
    if image is not None:
        blade = add_image_texture(tree, image, location=(-500, 100), label="FenceA")
        clip = tree.nodes.new("ShaderNodeMath")
        clip.operation = "GREATER_THAN"
        clip.location = (-150, -150)
        clip.label = "Blade cutout"
        clip.inputs[1].default_value = 0.5
        links.new(blade.outputs["Alpha"], clip.inputs[0])
        links.new(clip.outputs[0], bsdf.inputs["Alpha"])

    ground = None
    ref = find_texture(spec, GROUND_ROLE)
    image = load_image(ref.path, TextureRole.DIFFUSE) if ref is not None else None
    if image is not None:
        geom = tree.nodes.new("ShaderNodeNewGeometry")
        geom.location = (-900, 400)
        scale = tree.nodes.new("ShaderNodeVectorMath")
        scale.operation = "SCALE"
        scale.location = (-700, 400)
        scale.inputs["Scale"].default_value = float(spec.params.get("ground_scale", 1.0 / 32.0))
        links.new(geom.outputs["Position"], scale.inputs[0])
        ground = add_image_texture(tree, image, location=(-500, 400), label="Ground grass")
        links.new(scale.outputs[0], ground.inputs["Vector"])

    ground_color = ground.outputs["Color"] if ground is not None else None
    ground_color = apply_grass_x2(tree, spec, ground_color, location=(-1900, 900))

    if blade is not None and ground is not None:
        gain = tree.nodes.new("ShaderNodeVectorMath")
        gain.operation = "SCALE"
        gain.location = (-200, 250)
        gain.inputs["Scale"].default_value = 1.0 / _BLADE_MEAN
        links.new(blade.outputs["Color"], gain.inputs[0])
        tint = tree.nodes.new("ShaderNodeVectorMath")
        tint.operation = "MULTIPLY"
        tint.location = (0, 300)
        tint.label = "Ground tint"
        links.new(ground_color, tint.inputs[0])
        links.new(gain.outputs[0], tint.inputs[1])
        links.new(tint.outputs[0], bsdf.inputs["Base Color"])
    elif blade is not None:
        links.new(blade.outputs["Color"], bsdf.inputs["Base Color"])
    elif ground is not None:
        links.new(ground_color, bsdf.inputs["Base Color"])
    return mat


__all__ = ["build"]
