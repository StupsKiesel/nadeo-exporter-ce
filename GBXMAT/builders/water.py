"""Stadium water: ``Tech3_Water_*`` pools and ``Tech3 Block WaterWall``.

Both game shaders blend a reflection and a see-through layer by the
``InvFresnelPC3`` lookup (``Fresnel`` slot). Read in Blender texture space,
its V axis is cos(view angle): reflectance is 0.15 looking straight down
and 0.94 at grazing angles. The builders sample it at ``1 - Facing``.

``Water.Surface`` (pools, Sea shader):

* the see-through layer is tinted by the collection's
  ``WaterTransmittance.ImageGen.Gbx`` colour (``water_transmittance_*``);
* ``Water_SxSySz`` is a greyscale height map. The mesh UVs are zero (the
  game samples it in world space), so it is projected on world XY. The
  ``MultiH`` shader blends several height samples; two rotated layers
  (:data:`HEIGHT_LAYERS`) scroll in different directions to animate it.

``Water.Wall`` (``GlassWaterWall``/``GlassWaterStart``):

* ``Transmittance`` tints the see-through layer on the mesh UVs
  (``SeparateTcTransmit``: it does not scroll);
* ``Normal`` layer 0 scrolls down at ``Y0_Speed`` UV/s while swaying in U
  by ``X0_Scale * sin(2πt / X0_Period)``; layer 1 scrolls at ``Y1_Speed``
  and is blended in by ``Normal1_Weight``;
* ``RoughMetal`` red and the ``FoamOpacity`` foam streaks follow layer 0.

Approximations: the game's screen-space refraction and depth fog are
replaced by a tinted Transparent BSDF, and the pool ripples' world repeats,
scroll speeds and bump strength are not stored in the material (the
compiled shader holds them), so :data:`HEIGHT_LAYERS` is tuned against
in-game screenshots.
"""

from __future__ import annotations

import math

from ..images import load_image
from ..spec import TextureRole
from ._common import add_image_texture, add_output, find_texture, new_material

FRESNEL_ROLE = "Fresnel"
TRANSMITTANCE_ROLE = "Transmittance"
FOAM_ROLE = "FoamOpacity"
# (world repeat in metres, Z rotation in radians, scroll velocity in m/s)
HEIGHT_LAYERS = (
    (3.0, 0.0, (0.12, 0.06)),
    (2.2, 1.9, (-0.08, 0.10)),
)
HEIGHT_STRENGTH = 0.35
HEIGHT_DISTANCE = 0.1
SURFACE_ROUGHNESS = 0.02
_FALLBACK_F0 = 1.33  # IOR for the Fresnel node when the LUT is missing


def _set_blended(mat) -> None:
    for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:  # noqa: BLE001
                pass


class _Graph:
    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, kind, location, **inputs):
        node = self.nodes.new(kind)
        node.location = location
        for name, value in inputs.items():
            self.set(node.inputs[name], value)
        return node

    def set(self, socket, value):
        if hasattr(value, "is_linked") or hasattr(value, "links"):
            self.links.new(value, socket)
        else:
            socket.default_value = value

    def math(self, operation, a, b=None, location=(0, 0)):
        node = self.node("ShaderNodeMath", location)
        node.operation = operation
        self.set(node.inputs[0], a)
        if b is not None:
            self.set(node.inputs[1], b)
        return node.outputs[0]

    def texture(self, spec, role, load_role, vector, location, label):
        ref = find_texture(spec, role)
        image = load_image(ref.path, load_role, flip_normal_green=spec.flip_normal_green) if ref else None
        if image is None:
            return None, None
        tex = add_image_texture(self.tree, image, location=location, label=label)
        if vector is not None:
            self.links.new(vector, tex.inputs["Vector"])
        return tex, image


def _drive(socket_owner, index: int, expression: str) -> None:
    driver = socket_owner.driver_add("default_value", index).driver
    driver.type = "SCRIPTED"
    for name, path in (("fps", "render.fps"), ("fps_base", "render.fps_base")):
        var = driver.variables.new()
        var.name = name
        var.type = "CONTEXT_PROP"
        var.targets[0].context_property = "ACTIVE_SCENE"
        var.targets[0].data_path = path
    driver.expression = expression


def _seconds() -> str:
    return "frame * (fps_base / fps)"


def _fresnel(g: _Graph, spec, location):
    """Reflectance socket: the game's LUT, else Blender's Fresnel node."""
    weight = g.node("ShaderNodeLayerWeight", (location[0] - 600, location[1]), Blend=0.5)
    cos_view = g.math("SUBTRACT", 1.0, weight.outputs["Facing"], location=(location[0] - 420, location[1]))
    coord = g.node("ShaderNodeCombineXYZ", (location[0] - 250, location[1]), X=0.5, Y=cos_view)
    tex, _image = g.texture(spec, FRESNEL_ROLE, "Fresnel", coord.outputs[0], location, "Fresnel LUT")
    if tex is None:
        return g.node("ShaderNodeFresnel", location, IOR=_FALLBACK_F0).outputs["Fac"]
    tex.extension = "EXTEND"
    return tex.outputs["Color"]


def _normal_color(g: _Graph, color, image, location):
    """Signed BC5 X/Y -> full tangent-space normal colour."""
    if int(image.get("source_channels", 4) or 4) > 3:
        return color
    sep = g.node("ShaderNodeSeparateXYZ", location)
    g.links.new(color, sep.inputs[0])
    x = g.math("MULTIPLY_ADD", sep.outputs[0], 2.0, location=(location[0] + 180, location[1] + 60))
    x.node.inputs[2].default_value = -1.0
    y = g.math("MULTIPLY_ADD", sep.outputs[1], 2.0, location=(location[0] + 180, location[1] - 60))
    y.node.inputs[2].default_value = -1.0
    xx = g.math("MULTIPLY", x, x, location=(location[0] + 360, location[1] + 60))
    yy = g.math("MULTIPLY", y, y, location=(location[0] + 360, location[1] - 60))
    rest = g.math("SUBTRACT", 1.0, g.math("ADD", xx, yy, location=(location[0] + 540, location[1])),
                  location=(location[0] + 700, location[1]))
    rest.node.use_clamp = True
    z = g.math("SQRT", rest, location=(location[0] + 860, location[1]))
    z_color = g.math("MULTIPLY_ADD", z, 0.5, location=(location[0] + 1020, location[1]))
    z_color.node.inputs[2].default_value = 0.5
    comb = g.node("ShaderNodeCombineXYZ", (location[0] + 1180, location[1]))
    g.links.new(sep.outputs[0], comb.inputs[0])
    g.links.new(sep.outputs[1], comb.inputs[1])
    g.links.new(z_color, comb.inputs[2])
    return comb.outputs[0]


def _shell(spec, name):
    mat = new_material(spec.name_hint or name)
    _set_blended(mat)
    g = _Graph(mat.node_tree)
    out = add_output(mat.node_tree, location=(900, 0))
    return mat, g, out


def _water_layers(g: _Graph, spec, tint, roughness, normal, location=(300, 0)):
    """Mix a tinted see-through layer with a glossy reflection by Fresnel."""
    transparent = g.node("ShaderNodeBsdfTransparent", (location[0], location[1] + 150), Color=tint)
    glossy = g.node("ShaderNodeBsdfGlossy", (location[0], location[1] - 100), Roughness=roughness)
    if normal is not None:
        g.links.new(normal, glossy.inputs["Normal"])
    mix = g.node("ShaderNodeMixShader", (location[0] + 250, location[1]))
    g.links.new(_fresnel(g, spec, (location[0] - 100, location[1] + 400)), mix.inputs["Fac"])
    g.links.new(transparent.outputs[0], mix.inputs[1])
    g.links.new(glossy.outputs[0], mix.inputs[2])
    return mix.outputs[0]


def build_surface(spec):
    mat, g, out = _shell(spec, "TM_Water")
    p = spec.params
    tint = (
        float(p.get("water_transmittance_r", 1.0)),
        float(p.get("water_transmittance_g", 1.0)),
        float(p.get("water_transmittance_b", 1.0)),
        1.0,
    )
    position = g.node("ShaderNodeNewGeometry", (-1700, -300)).outputs["Position"]
    heights = []
    for index, (repeat, rotation, velocity) in enumerate(HEIGHT_LAYERS):
        y = -300 - 350 * index
        mapping = _ripple_mapping(g, position, repeat, rotation, velocity, (-1450, y), f"Ripple layer {index}")
        tex, _image = g.texture(spec, TextureRole.NORMAL, "Height", mapping, (-1200, y), f"Water height {index}")
        if tex is None:
            break
        heights.append(tex.outputs["Color"])
    normal = None
    if heights:
        height = heights[0]
        for extra in heights[1:]:
            height = g.math("ADD", height, extra, location=(-900, -400))
        bump = g.node("ShaderNodeBump", (-650, -300), Strength=HEIGHT_STRENGTH, Distance=HEIGHT_DISTANCE)
        g.links.new(height, bump.inputs["Height"])
        normal = bump.outputs["Normal"]
    shader = _water_layers(g, spec, tint, SURFACE_ROUGHNESS, normal)
    g.links.new(shader, out.inputs["Surface"])
    return mat


def _ripple_mapping(g: _Graph, position, repeat, rotation, velocity, location, label):
    """World position -> height-map texture space, scrolling at ``velocity`` m/s."""
    mapping = g.node("ShaderNodeMapping", location)
    mapping.label = label
    g.links.new(position, mapping.inputs["Vector"])
    mapping.inputs["Scale"].default_value = (1.0 / repeat, 1.0 / repeat, 1.0)
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, rotation)
    for axis, speed in enumerate(velocity):
        if speed:
            _drive(mapping.inputs["Location"], axis, f"{float(speed) / repeat!r} * {_seconds()}")
    return mapping.outputs["Vector"]


def _scroll_mapping(g: _Graph, uv, speed_v, sway_scale, sway_period, location, label):
    mapping = g.node("ShaderNodeMapping", location)
    mapping.label = label
    g.links.new(uv, mapping.inputs["Vector"])
    location_input = mapping.inputs["Location"]
    if speed_v:
        _drive(location_input, 1, f"{float(speed_v)!r} * {_seconds()}")
    if sway_scale and sway_period > 0.0:
        _drive(location_input, 0,
               f"{float(sway_scale)!r} * sin({2.0 * math.pi / float(sway_period)!r} * {_seconds()})")
    return mapping.outputs["Vector"]


def build_wall(spec):
    mat, g, out = _shell(spec, "TM_WaterWall")
    p = spec.params
    uv = g.node("ShaderNodeTexCoord", (-1900, 0)).outputs["UV"]
    layer0 = _scroll_mapping(
        g, uv, p.get("water_y0_speed", 0.0), p.get("water_x0_scale", 0.0),
        float(p.get("water_x0_period", 0.0)), (-1650, 100), "Water layer 0",
    )
    layer1 = _scroll_mapping(g, uv, p.get("water_y1_speed", 0.0), 0.0, 0.0, (-1650, -250), "Water layer 1")

    tint = (1.0, 1.0, 1.0, 1.0)
    tex, _image = g.texture(spec, TRANSMITTANCE_ROLE, "Diffuse", uv, (-400, 500), "Transmittance")
    if tex is not None:
        tint = tex.outputs["Color"]

    roughness = SURFACE_ROUGHNESS
    tex, _image = g.texture(spec, TextureRole.ROUGH_METAL, TextureRole.ROUGH_METAL, layer0, (-1300, 300), "RoughMetal")
    if tex is not None:
        sep = g.node("ShaderNodeSeparateColor", (-1000, 300))
        g.links.new(tex.outputs["Color"], sep.inputs["Color"])
        roughness = sep.outputs["Red"]

    normal = None
    tex0, image = g.texture(spec, TextureRole.NORMAL, TextureRole.NORMAL, layer0, (-1300, 0), "Normal layer 0")
    if tex0 is not None:
        color = _normal_color(g, tex0.outputs["Color"], image, (-1050, 0))
        tex1, _image1 = g.texture(spec, TextureRole.NORMAL, TextureRole.NORMAL, layer1, (-1300, -300), "Normal layer 1")
        if tex1 is not None:
            mix = g.node("ShaderNodeMix", (-80, -150), Factor=float(p.get("water_normal1_weight", 0.0)))
            mix.data_type = "RGBA"
            a, b = (next(s for s in mix.inputs if s.name == n and s.enabled) for n in ("A", "B"))
            g.links.new(color, a)
            g.links.new(_normal_color(g, tex1.outputs["Color"], image, (-1050, -300)), b)
            color = next(s for s in mix.outputs if s.name == "Result" and s.enabled)
        nmap = g.node("ShaderNodeNormalMap", (120, -150))
        g.links.new(color, nmap.inputs["Color"])
        normal = nmap.outputs["Normal"]

    shader = _water_layers(g, spec, tint, roughness, normal, location=(350, 0))
    foam_tex, _image = g.texture(spec, FOAM_ROLE, "Mask", layer0, (-400, 800), "Foam opacity")
    if foam_tex is not None:
        foam = g.node("ShaderNodeBsdfDiffuse", (600, 300), Color=(1.0, 1.0, 1.0, 1.0))
        if normal is not None:
            g.links.new(normal, foam.inputs["Normal"])
        mix = g.node("ShaderNodeMixShader", (750, 100))
        g.links.new(foam_tex.outputs["Color"], mix.inputs["Fac"])
        g.links.new(shader, mix.inputs[1])
        g.links.new(foam.outputs[0], mix.inputs[2])
        shader = mix.outputs[0]
    g.links.new(shader, out.inputs["Surface"])
    return mat


__all__ = ["build_surface", "build_wall", "FRESNEL_ROLE", "TRANSMITTANCE_ROLE", "FOAM_ROLE"]
