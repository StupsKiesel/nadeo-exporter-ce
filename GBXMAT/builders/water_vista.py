"""Vista sea water: depth-fog tinted, partially see-through surface.

The game's ``Tech3_Water_MultiH`` shader composites screen-space
refraction with a per-environment depth-fog ramp (``WaterFog.dds`` /
``WaterSea_Fog.dds``). EEVEE can't measure water depth through a blended
surface, so the view angle stands in for it: looking straight down sees
shallow, light water; grazing views read as deep, dark and opaque.

Ramp layout: a tall texture runs shallow (v = 1) to deep (v = 0); a wide
one runs shallow (u = 0) to deep (u = 1).

Blender approximation: the depth proxy and opacity curve are heuristics
tuned for appearance. ``depth_facing``/``depth_grazing`` and
``alpha_facing``/``alpha_grazing`` params override them: the defaults suit
BlueBay's open sea, while vista lakes (RedIsland, GreenCoast, WhiteShore)
are shallow, so the adapter passes a lighter, clearer profile that keeps
their lake bed and submerged shore visible. The sea material has no height
map, so ripples are 4D noise on world position, drifting and evolving over
time via drivers.
"""

from __future__ import annotations

from ..images import load_image
from ._common import new_material, add_output, add_principled, add_image_texture, find_texture
from .water import _drive, _seconds

FOG_ROLE = "WaterFog"
_FALLBACK_COLOR = (0.02, 0.25, 0.3, 1.0)
# Ramp position (0 shallow .. 1 deep) at facing and grazing view angles.
_DEPTH_FACING = 0.25
_DEPTH_GRAZING = 0.85
_ALPHA_FACING = 0.55
_ALPHA_GRAZING = 0.95
_ROUGHNESS = 0.05
_IOR = 1.33
_WAVE_SCALE = 1.2
_WAVE_STRENGTH = 0.15
_WAVE_DISTANCE = 0.1
_WAVE_EVOLVE = 0.4  # noise W units per second
_WAVE_DRIFT = (0.25, 0.15)  # m/s


def _set_blended(mat) -> None:
    for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:  # noqa: BLE001
                pass


def build(spec):
    mat = new_material(spec.name_hint or "TM_Water")
    _set_blended(mat)
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links
    p = spec.params

    bsdf = add_principled(tree, location=(300, 0))
    out = add_output(tree, location=(650, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    bsdf.inputs["Roughness"].default_value = _ROUGHNESS
    bsdf.inputs["IOR"].default_value = _IOR
    bsdf.inputs["Base Color"].default_value = _FALLBACK_COLOR

    weight = nodes.new("ShaderNodeLayerWeight")
    weight.location = (-1100, 0)
    weight.inputs["Blend"].default_value = 0.5
    facing = weight.outputs["Facing"]

    def remap(lo, hi, label):
        node = nodes.new("ShaderNodeMapRange")
        node.label = label
        node.clamp = True
        node.inputs["To Min"].default_value = lo
        node.inputs["To Max"].default_value = hi
        links.new(facing, node.inputs["Value"])
        return node

    depth = remap(p.get("depth_facing", _DEPTH_FACING), p.get("depth_grazing", _DEPTH_GRAZING), "Depth proxy")
    depth.location = (-850, 100)
    alpha = remap(p.get("alpha_facing", _ALPHA_FACING), p.get("alpha_grazing", _ALPHA_GRAZING), "Opacity")
    alpha.location = (-850, -200)
    links.new(alpha.outputs["Result"], bsdf.inputs["Alpha"])

    ref = find_texture(spec, FOG_ROLE)
    img = load_image(ref.path, "Diffuse") if ref is not None else None
    if img is not None:
        width, height = img.size
        coord = nodes.new("ShaderNodeCombineXYZ")
        coord.location = (-600, 200)
        if height > width:
            flip = nodes.new("ShaderNodeMath")
            flip.operation = "SUBTRACT"
            flip.location = (-750, 250)
            flip.inputs[0].default_value = 1.0
            links.new(depth.outputs["Result"], flip.inputs[1])
            coord.inputs["X"].default_value = 0.5
            links.new(flip.outputs["Value"], coord.inputs["Y"])
        else:
            links.new(depth.outputs["Result"], coord.inputs["X"])
            coord.inputs["Y"].default_value = 0.5
        tex = add_image_texture(tree, img, location=(-350, 200), label="Water fog")
        tex.extension = "EXTEND"
        links.new(coord.outputs["Vector"], tex.inputs["Vector"])
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

    position = nodes.new("ShaderNodeNewGeometry")
    position.location = (-1100, -450)
    drift = nodes.new("ShaderNodeMapping")
    drift.location = (-850, -450)
    drift.label = "Wave drift"
    links.new(position.outputs["Position"], drift.inputs["Vector"])
    for axis, speed in enumerate(_WAVE_DRIFT):
        _drive(drift.inputs["Location"], axis, f"{-speed!r} * {_seconds()}")
    noise = nodes.new("ShaderNodeTexNoise")
    noise.location = (-600, -450)
    noise.noise_dimensions = "4D"
    noise.inputs["Scale"].default_value = _WAVE_SCALE
    _drive(noise.inputs["W"], -1, f"{_WAVE_EVOLVE!r} * {_seconds()}")
    links.new(drift.outputs["Vector"], noise.inputs["Vector"])
    bump = nodes.new("ShaderNodeBump")
    bump.location = (-350, -450)
    bump.inputs["Strength"].default_value = _WAVE_STRENGTH
    bump.inputs["Distance"].default_value = _WAVE_DISTANCE
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


__all__ = ["build", "FOG_ROLE"]
