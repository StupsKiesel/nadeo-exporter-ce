"""Fogger/sparkler particle effects (``CPlugFxSystem``) as Geometry Nodes emitters.

Each decoded :class:`GBXBPY.model.fx.FxSystem` becomes an Empty carrying the
effect graph as custom properties, with one child point object per particle
emitter. The child's ``TM FX Particles`` modifier simulates the GPU particles
analytically from the scene time (no bake, no simulation zone):

* slot ``i`` of ``N = ceil(rate * lifetime)`` spawns every ``N / rate`` s;
* position = ``dir * v * (1 - e^(-k*a)) / k + g * (a - (1 - e^(-k*a)) / k) / k``
  with drag ``k`` and world gravity ``g`` along +Z, age ``a``;
* size lerps from start to end; the material plays the texture's flipbook
  (``atlas_columns`` x ``atlas_rows``) over ``age / lifetime`` and fades;
* ``SpawnFreqModifierExpr = cos(Time/A - B)`` gates spawning (``Time`` in ms).

Each particle is three crossed quads with a random orientation instead of a
camera-facing sprite: prefab collections are instanced, and Geometry Nodes
cannot see the instance transform a camera-facing quad would need. The
shader fades quads seen edge-on, so the planes read as one soft sprite.

The modifier is disabled in the viewport by default (it is visible in
renders); field meanings are inferred from the stock files and the cone
spread is an estimate - see ``GBXBPY.model.particle``.
"""

from __future__ import annotations

import colorsys
import json
import logging
import math
import os
import re
import zlib

import bpy

from GBXBPY.model.fx import FxEmitter, FxSystem, constant_float, constant_float3

log = logging.getLogger("trackmania_blender_bridge.fx_particles")

GROUP_NAME = "TM FX Particles"
GROUP_VERSION = 1
MODIFIER_NAME = "TM FX Particles"
SPARK_SPREAD = 0.35         # cone spread (|random offset| added to the unit axis)
FOG_SPREAD = 0.25
SPARK_EMISSION = 6.0
FOG_OPACITY = 0.35
DEFAULT_MAX_PARTICLES = 400
# Emission axis: TM-local +Z (opposite DOVExpr float3(0,0,-1)) in Blender space.
EMIT_AXIS = (0.0, -1.0, 0.0)

_PULSE = re.compile(r"^\s*cos\(\s*Time\s*/\s*([\d.]+)\s*(?:-\s*([\d.]+))?\s*\)")

_INPUTS = (
    # name, socket type, default
    ("Count", "NodeSocketInt", 100),
    ("Period", "NodeSocketFloat", 1.0),
    ("Rate", "NodeSocketFloat", 50.0),
    ("Speed", "NodeSocketFloat", 1.0),
    ("Speed Variation", "NodeSocketFloat", 0.0),
    ("Lifetime", "NodeSocketFloat", 1.0),
    ("Lifetime Variation", "NodeSocketFloat", 0.0),
    ("Size Start", "NodeSocketFloat", 1.0),
    ("Size End", "NodeSocketFloat", 1.0),
    ("Gravity", "NodeSocketFloat", 0.0),
    ("Drag", "NodeSocketFloat", 0.0),
    ("Spread", "NodeSocketFloat", SPARK_SPREAD),
    ("Pulse Divisor", "NodeSocketFloat", 0.0),
    ("Pulse Phase", "NodeSocketFloat", 0.0),
    ("Seed", "NodeSocketInt", 0),
    ("Material", "NodeSocketMaterial", None),
)


def pulse(expr: str) -> tuple[float, float]:
    """``(A, B)`` of ``cos(Time/A - B)[*...]``; ``(0, 0)`` = constant spawning."""
    match = _PULSE.match(expr or "")
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2) or 0.0)


def tm_to_blender(vec: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vec
    return x, -z, y


class _Nodes:
    """Wires Geometry Nodes math from sockets or constants."""

    def __init__(self, tree) -> None:
        self.tree = tree
        self.x = 0

    def new(self, kind: str, **props):
        node = self.tree.nodes.new(kind)
        for key, value in props.items():
            setattr(node, key, value)
        node.location = (self.x, 0)
        self.x += 30
        return node

    def put(self, socket, value) -> None:
        if hasattr(value, "is_output"):
            self.tree.links.new(value, socket)
        else:
            socket.default_value = value

    @staticmethod
    def socket(sockets, name: str):
        """Available socket ``name`` (typed nodes keep hidden duplicates)."""
        return next(s for s in sockets if s.name == name and getattr(s, "enabled", True))

    def math(self, op: str, a, b=0.0, c=0.0):
        node = self.new("ShaderNodeMath", operation=op)
        for socket, value in zip(node.inputs, (a, b, c)):
            self.put(socket, value)
        return node.outputs[0]

    def vec(self, op: str, a, b=None, scale=None):
        node = self.new("ShaderNodeVectorMath", operation=op)
        self.put(node.inputs[0], a)
        if b is not None:
            self.put(node.inputs[1], b)
        if scale is not None:
            self.put(self.socket(node.inputs, "Scale"), scale)
        return node.outputs["Vector"]

    def random(self, data_type: str, ident, seed, low, high):
        node = self.new("FunctionNodeRandomValue", data_type=data_type)
        self.put(self.socket(node.inputs, "Min"), low)
        self.put(self.socket(node.inputs, "Max"), high)
        self.put(node.inputs["ID"], ident)
        self.put(node.inputs["Seed"], seed)
        return self.socket(node.outputs, "Value")

    def store(self, geometry, name: str, data_type: str, value):
        node = self.new("GeometryNodeStoreNamedAttribute", data_type=data_type, domain="POINT")
        self.put(node.inputs["Geometry"], geometry)
        node.inputs["Name"].default_value = name
        self.put(self.socket(node.inputs, "Value"), value)
        return node.outputs["Geometry"]

    def attribute(self, name: str, data_type: str):
        node = self.new("GeometryNodeInputNamedAttribute", data_type=data_type)
        node.inputs["Name"].default_value = name
        return self.socket(node.outputs, "Attribute")


def _cross(n: _Nodes, material):
    """Three crossed unit quads (-1..1) with a ``UVMap``."""
    join = n.new("GeometryNodeJoinGeometry")
    for rotation in ((0.0, 0.0, 0.0), (math.pi / 2, 0.0, 0.0), (0.0, math.pi / 2, 0.0)):
        grid = n.new("GeometryNodeMeshGrid")
        for key, value in (("Size X", 2.0), ("Size Y", 2.0), ("Vertices X", 2), ("Vertices Y", 2)):
            grid.inputs[key].default_value = value
        uv = n.new("GeometryNodeStoreNamedAttribute", data_type="FLOAT2", domain="CORNER")
        uv.inputs["Name"].default_value = "UVMap"
        n.put(uv.inputs["Geometry"], grid.outputs["Mesh"])
        n.put(n.socket(uv.inputs, "Value"), grid.outputs["UV Map"])
        turn = n.new("GeometryNodeTransform")
        turn.inputs["Rotation"].default_value = rotation
        n.put(turn.inputs["Geometry"], uv.outputs["Geometry"])
        n.tree.links.new(turn.outputs["Geometry"], join.inputs[0])
    set_material = n.new("GeometryNodeSetMaterial")
    n.put(set_material.inputs["Geometry"], join.outputs[0])
    n.put(set_material.inputs["Material"], material)
    return set_material.outputs["Geometry"]


def _node_group():
    group = bpy.data.node_groups.get(GROUP_NAME)
    if group is not None and group.get("tm_fx_version") == GROUP_VERSION:
        return group
    if group is not None:
        group.name = f"{GROUP_NAME} (old)"
    group = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    group["tm_fx_version"] = GROUP_VERSION
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    for name, kind, default in _INPUTS:
        socket = group.interface.new_socket(name, in_out="INPUT", socket_type=kind)
        if default is not None:
            socket.default_value = default
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    n = _Nodes(group)
    g_in = n.new("NodeGroupInput").outputs
    g_out = n.new("NodeGroupOutput")

    points = n.new("GeometryNodePoints")
    n.put(points.inputs["Count"], g_in["Count"])
    index = n.new("GeometryNodeInputIndex").outputs[0]
    now = n.new("GeometryNodeInputSceneTime").outputs["Seconds"]

    # Slot i: first spawn at i / rate, then every Period seconds.
    since = n.math("SUBTRACT", now, n.math("DIVIDE", index, g_in["Rate"]))
    cycle = n.math("FLOOR", n.math("DIVIDE", since, g_in["Period"]))
    age = n.math("SUBTRACT", since, n.math("MULTIPLY", cycle, g_in["Period"]))
    ident = n.math("ADD", index, n.math("MULTIPLY", cycle, g_in["Count"]))

    def rand(offset: int, low=0.0, high=1.0, data_type="FLOAT"):
        return n.random(data_type, ident, n.math("ADD", g_in["Seed"], offset), low, high)

    life = n.math("MAXIMUM", n.math(
        "MULTIPLY", g_in["Lifetime"],
        n.math("SUBTRACT", 1.0, n.math("MULTIPLY", g_in["Lifetime Variation"], rand(1))),
    ), 0.01)
    speed = n.math("MAXIMUM", n.math(
        "MULTIPLY_ADD", g_in["Speed Variation"], n.math("MULTIPLY_ADD", rand(2), 2.0, -1.0), g_in["Speed"],
    ), 0.0)
    jitter = rand(3, (-1.0, -1.0, -1.0), (1.0, 1.0, 1.0), "FLOAT_VECTOR")
    direction = n.vec("NORMALIZE", n.vec("ADD", EMIT_AXIS, n.vec("SCALE", jitter, scale=g_in["Spread"])))

    k = n.math("MAXIMUM", g_in["Drag"], 1e-3)
    decay = n.math("EXPONENT", n.math("MULTIPLY", k, n.math("MULTIPLY", age, -1.0)))
    reach = n.math("DIVIDE", n.math("SUBTRACT", 1.0, decay), k)
    fall = n.math("DIVIDE", n.math("SUBTRACT", age, reach), k)

    # World gravity (+Z up) in the emitter's local space.
    self_info = n.new("GeometryNodeObjectInfo", transform_space="ORIGINAL")
    n.put(self_info.inputs["Object"], n.new("GeometryNodeSelfObject").outputs[0])
    invert = n.new("FunctionNodeInvertRotation")
    n.put(invert.inputs[0], self_info.outputs["Rotation"])
    gravity_world = n.new("ShaderNodeCombineXYZ")
    n.put(gravity_world.inputs["Z"], g_in["Gravity"])
    gravity = n.new("FunctionNodeRotateVector")
    n.put(gravity.inputs["Vector"], gravity_world.outputs[0])
    n.put(gravity.inputs["Rotation"], invert.outputs[0])

    position = n.vec(
        "ADD",
        n.vec("SCALE", direction, scale=n.math("MULTIPLY", speed, reach)),
        n.vec("SCALE", gravity.outputs[0], scale=fall),
    )
    age01 = n.math("DIVIDE", age, life)
    size = n.math("MULTIPLY_ADD", n.math("SUBTRACT", g_in["Size End"], g_in["Size Start"]), age01, g_in["Size Start"])

    # SpawnFreqModifierExpr cos(Time/A - B): spawn with probability max(cos, 0).
    spawn_ms = n.math("MULTIPLY", n.math("SUBTRACT", now, age), 1000.0)
    wave = n.math("COSINE", n.math("SUBTRACT", n.math("DIVIDE", spawn_ms, g_in["Pulse Divisor"]), g_in["Pulse Phase"]))
    no_pulse = n.math("COMPARE", g_in["Pulse Divisor"], 0.0, 1e-6)
    gate = n.math("MAXIMUM", wave, no_pulse)
    dead = n.math("MAXIMUM", n.math("GREATER_THAN", age, life), n.math("GREATER_THAN", rand(4), gate))

    geometry = points.outputs["Points"]
    set_position = n.new("GeometryNodeSetPosition")
    n.put(set_position.inputs["Geometry"], geometry)
    n.put(set_position.inputs["Position"], position)
    geometry = set_position.outputs["Geometry"]
    geometry = n.store(geometry, "age01", "FLOAT", age01)
    geometry = n.store(geometry, "half_size", "FLOAT", n.math("MULTIPLY", size, 0.5))
    geometry = n.store(geometry, "spin", "FLOAT_VECTOR", rand(5, (0.0, 0.0, 0.0), (math.tau,) * 3, "FLOAT_VECTOR"))
    delete = n.new("GeometryNodeDeleteGeometry", domain="POINT")
    n.put(delete.inputs["Geometry"], geometry)
    n.put(delete.inputs["Selection"], dead)

    instance = n.new("GeometryNodeInstanceOnPoints")
    n.put(instance.inputs["Points"], delete.outputs[0])
    n.put(instance.inputs["Instance"], _cross(n, g_in["Material"]))
    n.put(instance.inputs["Rotation"], n.attribute("spin", "FLOAT_VECTOR"))
    n.put(instance.inputs["Scale"], n.attribute("half_size", "FLOAT"))
    n.put(g_out.inputs[0], instance.outputs["Instances"])
    return group


def _load_image(path: str):
    if not path or not os.path.isfile(path):
        return None
    try:
        return bpy.data.images.load(path, check_existing=True)
    except RuntimeError as exc:
        log.warning("FX texture unreadable: %s (%s)", path, exc)
        return None


def _emitter_color(emitter: FxEmitter, variables: dict[str, float]) -> tuple[float, float, float, float]:
    emission = emitter.model.emission if emitter.model is not None else None
    if emission is None or not emission.use_hue_shift:
        return (1.0, 1.0, 1.0, 1.0)

    def scalar(expr: str, fallback: float) -> float:
        value = constant_float(expr)
        if value is None:
            value = variables.get(expr.strip())
        if value is None:
            log.warning("%s: unsupported colour expression %r; using %s", emitter.name, expr, fallback)
            return fallback
        return value

    hue = emission.hue
    if emission.use_emitter_hue and emitter.linear_hue:
        override = scalar(emitter.linear_hue, hue)
        if override >= 0.0:
            hue = override
    lightness = scalar(emitter.hue_lightness, 0.5) if emitter.hue_lightness else 0.5
    return (*colorsys.hls_to_rgb(hue % 1.0, max(0.0, min(lightness, 1.0)), 1.0), 1.0)


def _material(image_path: str, fog: bool, columns: int = 1, rows: int = 1, *,
              color=(1.0, 1.0, 1.0, 1.0)):
    stem = os.path.basename(image_path).split(".", 1)[0] if image_path else "Untextured"
    key = f"{image_path}|{columns}x{rows}|{color}"
    name = f"TM FX {stem}{' Fog' if fog else ''} {zlib.crc32(key.encode()):08x}"
    mat = bpy.data.materials.get(name)
    if mat is not None and mat.get("tm_fx_texture") == key:
        return mat
    mat = bpy.data.materials.new(name)
    mat["tm_fx_texture"] = key
    if bpy.app.version < (5, 0, 0) and not mat.use_nodes:
        mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()

    def attribute(attr_name: str, kind: str):
        node = nodes.new("ShaderNodeAttribute")
        node.attribute_type = kind
        node.attribute_name = attr_name
        return node

    def math(op: str, a, b=0.0):
        node = nodes.new("ShaderNodeMath")
        node.operation = op
        for socket, value in zip(node.inputs, (a, b)):
            if hasattr(value, "is_output"):
                links.new(value, socket)
            else:
                socket.default_value = value
        return node.outputs[0]

    age = attribute("age01", "INSTANCER").outputs["Fac"]
    # Flipbook: frame = age * frames, row 0 at the top of the image.
    frames = columns * rows
    frame = math("MINIMUM", math("FLOOR", math("MULTIPLY", age, frames)), frames - 1)
    cell = nodes.new("ShaderNodeCombineXYZ")
    links.new(math("FLOORED_MODULO", frame, columns), cell.inputs["X"])
    links.new(math("SUBTRACT", rows - 1, math("FLOOR", math("DIVIDE", frame, columns))), cell.inputs["Y"])
    uv = nodes.new("ShaderNodeVectorMath")
    uv.operation = "ADD"
    links.new(attribute("UVMap", "GEOMETRY").outputs["Vector"], uv.inputs[0])
    links.new(cell.outputs[0], uv.inputs[1])
    atlas_uv = nodes.new("ShaderNodeVectorMath")
    atlas_uv.operation = "DIVIDE"
    links.new(uv.outputs["Vector"], atlas_uv.inputs[0])
    atlas_uv.inputs[1].default_value = (columns, rows, 1.0)

    tex = nodes.new("ShaderNodeTexImage")
    tex.image = _load_image(image_path)
    tex.extension = "CLIP"
    links.new(atlas_uv.outputs["Vector"], tex.inputs["Vector"])
    # Quick fade-in; the flipbook frames dissipate, the fade-out only softens the end.
    fade = math("MULTIPLY", math("MINIMUM", math("MULTIPLY", age, 8.0), 1.0),
                math("SUBTRACT", 1.0, math("MULTIPLY", age, age)))
    # Fade quads seen edge-on so the crossed planes read as one soft sprite.
    facing = nodes.new("ShaderNodeLayerWeight")
    facing.inputs["Blend"].default_value = 0.5
    edge_fade = math("POWER", math("SUBTRACT", 1.0, facing.outputs["Facing"]), 2.0)
    alpha = math("MULTIPLY", math("MULTIPLY", tex.outputs["Alpha"], fade), edge_fade)
    if fog:
        alpha = math("MULTIPLY", alpha, FOG_OPACITY)
        surface = nodes.new("ShaderNodeBsdfDiffuse")
    else:
        surface = nodes.new("ShaderNodeEmission")
        surface.inputs["Strength"].default_value = SPARK_EMISSION
    tint = nodes.new("ShaderNodeMixRGB")
    tint.blend_type = "MULTIPLY"
    tint.inputs[0].default_value = 1.0
    tint.inputs[2].default_value = color
    links.new(tex.outputs["Color"], tint.inputs[1])
    links.new(tint.outputs[0], surface.inputs["Color"])
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(alpha, mix.inputs["Fac"])
    links.new(transparent.outputs[0], mix.inputs[1])
    links.new(surface.outputs[0], mix.inputs[2])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(mix.outputs[0], output.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED" if fog else "DITHERED"
    if hasattr(mat, "use_backface_culling"):
        mat.use_backface_culling = False
    return mat


def _points_mesh():
    mesh = bpy.data.meshes.get("TM FX Points")
    return mesh if mesh is not None else bpy.data.meshes.new("TM FX Points")


def _set_input(modifier, name: str, value) -> None:
    identifier = modifier.node_group.interface.items_tree[name].identifier
    properties = getattr(modifier, "properties", None)
    if properties is not None:  # Blender 5.2+: typed interface properties
        getattr(properties.inputs, identifier).value = value
    else:
        modifier[identifier] = value


def _emitter_object(emitter: FxEmitter, index: int, collection, show_viewport: bool, variables: dict[str, float]):
    emission = emitter.model.emission if emitter.model is not None else None
    obj = bpy.data.objects.new(emitter.name or f"Emitter{index:02d}", _points_mesh())
    obj["tm_fx_emitter"] = emitter.name
    obj["tm_fx_particle_model"] = emitter.model_path
    for key in ("condition", "local_offset", "world_offset", "spawn_freq", "scale",
                "opacity", "up", "dov", "linear_hue", "hue_lightness"):
        value = getattr(emitter, key)
        if value:
            obj[f"tm_fx_{key}"] = value
    offset = constant_float3(emitter.local_offset)
    if offset is not None:
        obj.location = tm_to_blender(offset)
    collection.objects.link(obj)
    if emission is None:
        obj["tm_fx_unreadable"] = True
        return obj

    color = _emitter_color(emitter, variables)
    obj["tm_fx_color"] = color
    obj["tm_fx_emission"] = json.dumps({
        key: getattr(emission, key) for key in emission.__dataclass_fields__
    })
    modifier = obj.modifiers.new(MODIFIER_NAME, "NODES")
    modifier.node_group = _node_group()
    max_particles = emission.max_particles or DEFAULT_MAX_PARTICLES
    rate = max(emission.rate, 1e-3)
    count = max(1, min(math.ceil(rate * max(emission.lifetime, 0.01)), max_particles))
    divisor, phase = pulse(emitter.spawn_freq)
    values = {
        "Count": count, "Period": count / rate, "Rate": rate,
        "Speed": emission.speed, "Speed Variation": emission.speed_variation,
        "Lifetime": emission.lifetime, "Lifetime Variation": min(emission.lifetime_variation, 0.95),
        "Size Start": emission.size_start, "Size End": emission.size_end,
        "Gravity": emission.gravity, "Drag": emission.drag,
        "Spread": FOG_SPREAD if emission.is_fog_emitter else SPARK_SPREAD,
        "Pulse Divisor": divisor, "Pulse Phase": phase,
        "Seed": index * 7919 + (zlib.crc32(emitter.name.encode()) & 0xFFFF),
        "Material": _material(
            emission.texture_image, emission.is_fog_emitter, emission.atlas_columns, emission.atlas_rows,
            color=color,
        ),
    }
    for name, value in values.items():
        _set_input(modifier, name, value)
    modifier.show_viewport = show_viewport
    return obj


def build_fx_object(fx: FxSystem, label: str, collection, *, show_viewport: bool = False):
    """Empty for ``fx`` with one child emitter per particle emitter.

    The Empty is returned unlinked (the caller places and links it); the
    emitters are linked to ``collection`` and parented to it.
    """
    root = bpy.data.objects.new(label, None)
    root.empty_display_type = "SPHERE"
    root.empty_display_size = 0.5
    root["tm_fx_system"] = fx.name
    if fx.source_path:
        root["tm_fx_source"] = fx.source_path
    if fx.vars:
        root["tm_fx_vars"] = json.dumps([vars(v) for v in fx.vars])
    if fx.var_updates:
        root["tm_fx_var_updates"] = json.dumps([vars(u) for u in fx.var_updates])
    if fx.sound_emitter_count:
        root["tm_fx_sound_emitters"] = fx.sound_emitter_count
    variables = {var.name: var.value for var in fx.vars if var.value is not None}
    for index, emitter in enumerate(fx.emitters):
        child = _emitter_object(emitter, index, collection, show_viewport, variables)
        child.parent = root
    return root


def fx_modifiers(objects):
    for obj in objects:
        modifier = obj.modifiers.get(MODIFIER_NAME) if obj.type == "MESH" else None
        if modifier is not None:
            yield modifier


__all__ = ["build_fx_object", "fx_modifiers", "pulse", "tm_to_blender"]
