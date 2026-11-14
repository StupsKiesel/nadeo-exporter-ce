"""World shader for the decoration mood's sky: dome, sun, moon and stars.

All layers are computed from the view direction (world ``Generated``
coordinates), so the sky works in Cycles and EEVEE without extra objects:

* dome: ``SkyColor.dds`` (float HDR). Its u axis is the azimuth away from
  the sun (0 = sun side with the baked glow, 1 = opposite), mirrored on both
  sides of the sun, and v the elevation;
* sun: ``pow(cos, HdrSun.Power)`` disc plus the ``Atmo1``/``Atmo2`` halos;
* moon: ``Moon.tga`` projected on a plane facing the moon direction;
* stars: thresholded Voronoi points, night moods only.

Clouds are real sprite objects (see :mod:`.clouds`), so they cover the sun,
moon and stars by occlusion. Sun disc, moon and stars only reach camera rays
so they do not double the sun lamp's light or add noise.
"""

from __future__ import annotations

import logging
import math
import os

import bpy

log = logging.getLogger("trackmania_blender_bridge.map_import_v4")

SUN_DISC_STRENGTH = 40.0
MOON_RADIUS = 0.05          # half-size of the moon texture, radians
MOON_STRENGTH = 1.5
STAR_SCALE = 220.0
STAR_STRENGTH = 2.0


def _load_image(path: str, non_color: bool = False):
    if not path or not os.path.isfile(path):
        return None
    try:
        image = bpy.data.images.load(path, check_existing=True)
    except RuntimeError as exc:
        log.warning("Sky texture unreadable: %s (%s)", path, exc)
        return None
    if non_color:
        image.colorspace_settings.name = "Non-Color"
    return image


class _Graph:
    """Small helper for wiring shader nodes with sockets or constants."""

    def __init__(self, tree) -> None:
        self.tree = tree
        self.x = -2400

    def node(self, kind: str, **props):
        node = self.tree.nodes.new(kind)
        for key, value in props.items():
            setattr(node, key, value)
        node.location = (self.x, 0)
        self.x += 40
        return node

    def _set(self, socket, value) -> None:
        if isinstance(value, bpy.types.NodeSocket):
            self.tree.links.new(value, socket)
        elif isinstance(value, (tuple, list)):
            socket.default_value = tuple(value)[:len(socket.default_value)]
        else:
            socket.default_value = value

    def math(self, op: str, a, b=0.0, clamp: bool = False):
        node = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        self._set(node.inputs[0], a)
        self._set(node.inputs[1], b)
        return node.outputs[0]

    def vec(self, op: str, a, b=(0.0, 0.0, 0.0), scale=None):
        node = self.node("ShaderNodeVectorMath", operation=op)
        self._set(node.inputs[0], a)
        self._set(node.inputs[1], b)
        if scale is not None:
            self._set(node.inputs[3], scale)
        return node.outputs["Value" if op == "DOT_PRODUCT" else "Vector"]

    def smoothstep(self, value, low: float, high: float):
        node = self.node("ShaderNodeMapRange", interpolation_type="SMOOTHSTEP")
        self._set(node.inputs["Value"], value)
        node.inputs["From Min"].default_value = low
        node.inputs["From Max"].default_value = high
        return node.outputs["Result"]

    def combine(self, x, y, z=0.0):
        node = self.node("ShaderNodeCombineXYZ")
        for socket, value in zip(node.inputs, (x, y, z)):
            self._set(socket, value)
        return node.outputs[0]

    def image(self, image, vector, extension: str = "REPEAT"):
        node = self.node("ShaderNodeTexImage", image=image, extension=extension, interpolation="Linear")
        self._set(node.inputs["Vector"], vector)
        return node


def _basis(direction):
    """Right/up vectors of a plane facing ``direction`` (up stays towards +Z)."""
    from mathutils import Vector

    forward = Vector(direction).normalized()
    right = forward.cross(Vector((0.0, 0.0, 1.0)))
    if right.length < 1e-4:
        right = Vector((1.0, 0.0, 0.0))
    right.normalize()
    return tuple(right), tuple(right.cross(forward).normalized())


def build_sky_world(world, environment) -> None:
    """Replace ``world``'s node tree with the mood sky of ``environment``."""
    sky = environment.sky
    if hasattr(world, "use_nodes") and world.node_tree is None:
        world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    g = _Graph(tree)

    direction = g.vec("NORMALIZE", g.node("ShaderNodeTexCoord").outputs["Generated"])
    xyz = g.node("ShaderNodeSeparateXYZ")
    tree.links.new(direction, xyz.inputs[0])
    x, y, z = xyz.outputs
    sun = environment.light_direction
    # At night the light is the moon; the sky glow still marks the (set) sun.
    sun_azimuth = math.atan2(sun[1], sun[0]) + (math.pi if environment.is_moon else 0.0)

    sky_image = _load_image(sky.sky_color_path)
    if sky_image is not None:
        # SkyColor is sun-relative: u is the azimuth away from the sun
        # (0 = sun side, 1 = opposite), mirrored on both sides of the sun.
        cos_s, sin_s = math.cos(sun_azimuth), math.sin(sun_azimuth)
        along = g.math("ADD", g.math("MULTIPLY", x, cos_s), g.math("MULTIPLY", y, sin_s))
        across = g.math("SUBTRACT", g.math("MULTIPLY", y, cos_s), g.math("MULTIPLY", x, sin_s))
        u = g.math("MULTIPLY", g.math("ABSOLUTE", g.math("ARCTAN2", across, along)), 1.0 / math.pi)
        v = g.math("ADD", g.math("MULTIPLY", g.math("ARCSINE", z), 1.0 / math.pi), 0.5)
        base = g.image(sky_image, g.combine(u, v), extension="EXTEND").outputs["Color"]
    else:
        base = g.vec("ADD", tuple(environment.ambient), (0.0, 0.0, 0.0))

    above = g.smoothstep(z, -0.02, 0.02)

    extras = []
    if not environment.is_moon:
        cosine = g.math("MAXIMUM", g.vec("DOT_PRODUCT", direction, tuple(sun)), 0.0)
        disc = g.math("POWER", cosine, sky.sun_power)
        sun_rgb = g.vec("SCALE", tuple(c * SUN_DISC_STRENGTH for c in environment.light_color), scale=disc)
        for halo in sky.sun_halos:
            glow = g.math("MULTIPLY", g.math("POWER", cosine, halo.power), halo.scale)
            sun_rgb = g.vec("ADD", sun_rgb, g.vec("SCALE", tuple(halo.color), scale=glow))
        extras.append(g.vec("SCALE", sun_rgb, scale=above))

    moon_image = _load_image(sky.moon_path)
    if moon_image is not None and sky.moon_hdr_scale > 0 and sky.moon_direction[2] > -0.05:
        right, up = _basis(sky.moon_direction)
        size = 1.0 / (2 * MOON_RADIUS)
        mu = g.math("ADD", g.math("MULTIPLY", g.vec("DOT_PRODUCT", direction, right), size), 0.5)
        mv = g.math("ADD", g.math("MULTIPLY", g.vec("DOT_PRODUCT", direction, up), size), 0.5)
        moon = g.image(moon_image, g.combine(mu, mv), extension="CLIP")
        front = g.math("GREATER_THAN", g.vec("DOT_PRODUCT", direction, tuple(sky.moon_direction)), 0.0)
        mask = g.math("MULTIPLY", g.math("MULTIPLY", moon.outputs["Alpha"], front), above)
        extras.append(g.vec(
            "SCALE", moon.outputs["Color"], scale=g.math("MULTIPLY", mask, sky.moon_hdr_scale * MOON_STRENGTH),
        ))

    if sky.star_visibility > 0:
        voronoi = g.node("ShaderNodeTexVoronoi", voronoi_dimensions="3D", feature="F1")
        tree.links.new(direction, voronoi.inputs["Vector"])
        voronoi.inputs["Scale"].default_value = STAR_SCALE
        random = g.node("ShaderNodeSeparateXYZ")
        tree.links.new(voronoi.outputs["Color"], random.inputs[0])
        rand = random.outputs[0]
        point = g.math("LESS_THAN", voronoi.outputs["Distance"], 0.07)
        bright = g.math("MULTIPLY", g.math("SUBTRACT", rand, 0.6), 5.0, clamp=True)
        star = g.math("MULTIPLY", point, bright)
        star = g.math("MULTIPLY", star, g.smoothstep(z, 0.0, 0.3))
        star = g.math("MULTIPLY", star, sky.star_visibility * STAR_STRENGTH)
        extras.append(g.vec("SCALE", (0.9, 0.95, 1.0), scale=star))

    if extras:
        total = extras[0]
        for extra in extras[1:]:
            total = g.vec("ADD", total, extra)
        camera = g.node("ShaderNodeLightPath").outputs["Is Camera Ray"]
        base = g.vec("ADD", base, g.vec("SCALE", total, scale=camera))

    background = g.node("ShaderNodeBackground")
    tree.links.new(base, background.inputs["Color"])
    background.inputs["Strength"].default_value = 1.0
    output = g.node("ShaderNodeOutputWorld")
    tree.links.new(background.outputs["Background"], output.inputs["Surface"])
    world["tm_sky_color"] = sky.sky_color_path
    world["tm_clouds"] = sky.clouds_path
    world["tm_moon"] = sky.moon_path
    world["tm_stars"] = sky.star_visibility
