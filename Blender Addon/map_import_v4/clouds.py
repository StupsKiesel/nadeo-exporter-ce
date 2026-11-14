"""Sky clouds: the game's sprite clusters as camera-facing billboards, plus the
``Clouds.tga`` cloud shadows.

:func:`GBXBPY.map_import_v4.clouds.place_cloud_sprites` tiles the authored
clusters around the map. Here they become a point cloud whose geometry-nodes
modifier instances one quad per point, turned towards the active camera (yaw,
then pitch). Point attributes reach the material as instancer attributes:
``cell_min``/``cell_size`` select the ``SkyClouds.dds`` atlas cell and
``fog`` blends towards the fog colour. The sprite RGB (0.86..1) is the baked
shading between the mood's ``CloudsX2`` min and max colours.

Cloud shadows: a plane high above the map, visible to shadow rays only,
whose transparency is the scrolling ``Clouds.tga`` mask. The game projects
the mask in its surface shaders; its world scale is not decoded, so
:data:`SHADOW_TILE` is an estimate.
"""

from __future__ import annotations

import logging
import math

import bpy

from GBXBPY.map_import_v4.clouds import place_cloud_sprites

from .sky import _load_image as _image

log = logging.getLogger("trackmania_blender_bridge.map_import_v4")

# Cloud brightness: sun/moon light and ambient share of the CloudsX2 colours.
# "X2" colours are stored at half range and doubled by the shader.
COLOR_X2 = 2.0
SUN_SHARE = 0.15
AMBIENT_SHARE = 1.0
SHADOW_HEIGHT = 1200.0      # m above the map origin
SHADOW_EXTENT = 8000.0      # m, square around the map centre
SHADOW_TILE = 1024.0        # m per Clouds.tga repeat (estimate)
SHADOW_MAX = 0.6            # sunlight blocked under the densest clouds
CLIP_MARGIN = 5000.0


def _node_group(name: str, material):
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group["tm_clouds"] = True
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")

    # Unit quad (-1..1) standing in XZ, facing +Y, image upright.
    grid = nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = 2.0
    grid.inputs["Size Y"].default_value = 2.0
    grid.inputs["Vertices X"].default_value = 2
    grid.inputs["Vertices Y"].default_value = 2
    store_uv = nodes.new("GeometryNodeStoreNamedAttribute")
    store_uv.data_type = "FLOAT2"
    store_uv.domain = "CORNER"
    store_uv.inputs["Name"].default_value = "UVMap"
    links.new(grid.outputs["Mesh"], store_uv.inputs["Geometry"])
    links.new(grid.outputs["UV Map"], store_uv.inputs["Value"])
    stand = nodes.new("GeometryNodeTransform")
    stand.inputs["Rotation"].default_value = (math.pi / 2, 0.0, 0.0)
    stand.inputs["Scale"].default_value = (-1.0, 1.0, 1.0)
    links.new(store_uv.outputs["Geometry"], stand.inputs["Geometry"])
    set_material = nodes.new("GeometryNodeSetMaterial")
    set_material.inputs["Material"].default_value = material
    links.new(stand.outputs["Geometry"], set_material.inputs["Geometry"])

    camera = nodes.new("GeometryNodeInputActiveCamera")
    camera_info = nodes.new("GeometryNodeObjectInfo")
    camera_info.transform_space = "RELATIVE"
    links.new(camera.outputs[0], camera_info.inputs["Object"])
    position = nodes.new("GeometryNodeInputPosition")
    towards = nodes.new("ShaderNodeVectorMath")
    towards.operation = "SUBTRACT"
    links.new(camera_info.outputs["Location"], towards.inputs[0])
    links.new(position.outputs["Position"], towards.inputs[1])
    yaw = nodes.new("FunctionNodeAlignRotationToVector")
    yaw.axis, yaw.pivot_axis = "Y", "Z"
    links.new(towards.outputs["Vector"], yaw.inputs["Vector"])
    pitch = nodes.new("FunctionNodeAlignRotationToVector")
    pitch.axis, pitch.pivot_axis = "Y", "X"
    links.new(yaw.outputs["Rotation"], pitch.inputs["Rotation"])
    links.new(towards.outputs["Vector"], pitch.inputs["Vector"])

    size = nodes.new("GeometryNodeInputNamedAttribute")
    size.data_type = "FLOAT"
    size.inputs["Name"].default_value = "half_size"
    instance = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(group_in.outputs[0], instance.inputs["Points"])
    links.new(set_material.outputs["Geometry"], instance.inputs["Instance"])
    links.new(pitch.outputs["Rotation"], instance.inputs["Rotation"])
    links.new(size.outputs["Attribute"], instance.inputs["Scale"])
    links.new(instance.outputs["Instances"], group_out.inputs[0])
    return group


def _socket(sockets, identifier: str):
    """``ShaderNodeMix`` repeats socket names per data type; pick by identifier."""
    return next(s for s in sockets if s.identifier == identifier)


def _sprite_material(name: str, layer, environment):
    mat = bpy.data.materials.new(name)
    mat["tm_clouds"] = layer.atlas_path
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()

    def attribute(attr_name: str, kind: str = "INSTANCER"):
        node = nodes.new("ShaderNodeAttribute")
        node.attribute_type = kind
        node.attribute_name = attr_name
        return node

    uv = attribute("UVMap", "GEOMETRY")
    atlas_uv = nodes.new("ShaderNodeVectorMath")
    atlas_uv.operation = "MULTIPLY_ADD"
    links.new(uv.outputs["Vector"], atlas_uv.inputs[0])
    links.new(attribute("cell_size").outputs["Vector"], atlas_uv.inputs[1])
    links.new(attribute("cell_min").outputs["Vector"], atlas_uv.inputs[2])
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = _image(layer.atlas_path)
    tex.extension = "EXTEND"
    links.new(atlas_uv.outputs["Vector"], tex.inputs["Vector"])

    shade = nodes.new("ShaderNodeMapRange")
    shade.inputs["From Min"].default_value = 0.85
    shade.inputs["From Max"].default_value = 1.0
    links.new(tex.outputs["Color"], shade.inputs["Value"])
    light = tuple(
        (environment.light_color[i] * environment.light_strength * SUN_SHARE
         + environment.ambient[i] * AMBIENT_SHARE) * COLOR_X2
        for i in range(3)
    )
    lit = nodes.new("ShaderNodeMix")
    lit.data_type = "RGBA"
    _socket(lit.inputs, "A_Color").default_value = (
        *(c * l for c, l in zip(layer.shading.min_color, light)), 1.0)
    _socket(lit.inputs, "B_Color").default_value = (
        *(c * l for c, l in zip(layer.shading.max_color, light)), 1.0)
    links.new(shade.outputs["Result"], _socket(lit.inputs, "Factor_Float"))
    fogged = nodes.new("ShaderNodeMix")
    fogged.data_type = "RGBA"
    links.new(attribute("fog").outputs["Fac"], _socket(fogged.inputs, "Factor_Float"))
    links.new(_socket(lit.outputs, "Result_Color"), _socket(fogged.inputs, "A_Color"))
    _socket(fogged.inputs, "B_Color").default_value = (*(environment.fog or environment.ambient), 1.0)

    emission = nodes.new("ShaderNodeEmission")
    links.new(_socket(fogged.outputs, "Result_Color"), emission.inputs["Color"])
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
    links.new(transparent.outputs[0], mix.inputs[1])
    links.new(emission.outputs[0], mix.inputs[2])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(mix.outputs[0], output.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    if hasattr(mat, "use_backface_culling"):
        mat.use_backface_culling = False
    return mat


def _camera_only(obj, *, shadow: bool = False) -> None:
    for flag in ("visible_diffuse", "visible_glossy", "visible_transmission", "visible_volume_scatter"):
        if hasattr(obj, flag):
            setattr(obj, flag, False)
    if hasattr(obj, "visible_shadow"):
        obj.visible_shadow = shadow


def _build_sprites(layer, environment, center, name: str):
    placed = place_cloud_sprites(layer, center)
    if not placed:
        return None
    mesh = bpy.data.meshes.new(f"TM Clouds {name}")
    mesh.vertices.add(len(placed))
    mesh.vertices.foreach_set("co", [c for sprite in placed for c in sprite.position])
    cells = layer.cells
    cell_min, cell_size = [], []
    for sprite in placed:
        u0, v0, u1, v1 = cells[sprite.cell] if sprite.cell < len(cells) else (0.0, 0.0, 1.0, 1.0)
        # Atlas cells are in image space (v down); Blender's v points up.
        cell_min += (u0, 1.0 - v1, 0.0)
        cell_size += (u1 - u0, v1 - v0, 0.0)
    for attr_name, kind, values in (
        ("half_size", "FLOAT", [s.half_size for s in placed]),
        ("fog", "FLOAT", [s.fog for s in placed]),
        ("cell_min", "FLOAT_VECTOR", cell_min),
        ("cell_size", "FLOAT_VECTOR", cell_size),
    ):
        mesh.attributes.new(attr_name, kind, "POINT").data.foreach_set(
            "vector" if kind == "FLOAT_VECTOR" else "value", values,
        )
    mesh.materials.append(_sprite_material(f"TM Clouds {name}", layer, environment))
    obj = bpy.data.objects.new("TM Clouds", mesh)
    obj["tm_cloud_sprites"] = len(placed)
    modifier = obj.modifiers.new("Clouds", "NODES")
    modifier.node_group = _node_group(f"TM Clouds {name}", mesh.materials[0])
    _camera_only(obj)
    if hasattr(obj, "visible_glossy"):
        obj.visible_glossy = True  # clouds mirrored in water
    return obj


def _scroll_driver(mapping, speed, fps: float) -> None:
    for axis, value in enumerate(speed[:2]):
        if not value:
            continue
        driver = mapping.inputs["Location"].driver_add("default_value", axis).driver
        driver.type = "SCRIPTED"
        driver.expression = f"frame * {value / max(fps, 1.0):.9g}"


def _build_shadows(layer, center, name: str, fps: float):
    image = _image(layer.shadow_path, non_color=True)
    if image is None:
        return None
    half = SHADOW_EXTENT / 2
    cx, cy = center
    mesh = bpy.data.meshes.new(f"TM Cloud Shadows {name}")
    mesh.from_pydata(
        [(cx - half, cy - half, SHADOW_HEIGHT), (cx + half, cy - half, SHADOW_HEIGHT),
         (cx + half, cy + half, SHADOW_HEIGHT), (cx - half, cy + half, SHADOW_HEIGHT)],
        [], [(0, 1, 2, 3)],
    )
    repeat = SHADOW_EXTENT / SHADOW_TILE
    mesh.uv_layers.new(name="UVMap").data.foreach_set("uv", [0, 0, repeat, 0, repeat, repeat, 0, repeat])
    mat = bpy.data.materials.new(f"TM Cloud Shadows {name}")
    mat["tm_clouds"] = layer.shadow_path
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()
    coords = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    _scroll_driver(mapping, layer.shadow_speed, fps)
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    density = nodes.new("ShaderNodeMapRange")
    density.interpolation_type = "SMOOTHSTEP"
    density.inputs["From Min"].default_value = 0.45
    density.inputs["From Max"].default_value = 0.75
    density.inputs["To Max"].default_value = SHADOW_MAX
    links.new(tex.outputs["Color"], density.inputs["Value"])
    blocker = nodes.new("ShaderNodeBsdfDiffuse")
    blocker.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(density.outputs["Result"], mix.inputs["Fac"])
    links.new(transparent.outputs[0], mix.inputs[1])
    links.new(blocker.outputs[0], mix.inputs[2])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(mix.outputs[0], output.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    if hasattr(mat, "use_transparent_shadow"):
        mat.use_transparent_shadow = True
    mesh.materials.append(mat)
    obj = bpy.data.objects.new("TM Cloud Shadows", mesh)
    obj.visible_camera = False
    _camera_only(obj, shadow=True)
    # A plane over the whole map would hide it in solid mode.
    obj.display_type = "WIRE"
    return obj


def _extend_clip(distance: float) -> None:
    """Far clouds sit up to ``distance`` away; raise 3D-view clip ends below it.

    Depth precision depends on the near plane, so a far clip end is harmless.
    """
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D" and space.clip_end < distance:
                    space.clip_end = distance


def build_clouds(environment, collection, center, name: str, fps: float = 24.0) -> int:
    """Add cloud sprites and cloud shadows to ``collection``; returns the sprite count."""
    layer = environment.sky.clouds if environment.sky is not None else None
    if layer is None:
        return 0
    sprites = _build_sprites(layer, environment, center, name)
    count = 0
    if sprites is not None:
        collection.objects.link(sprites)
        count = sprites["tm_cloud_sprites"]
        radius = layer.param.points[-1][0] if layer.param.points else 30000.0
        _extend_clip(radius + CLIP_MARGIN)
    shadows = _build_shadows(layer, center, name, fps)
    if shadows is not None:
        collection.objects.link(shadows)
    log.info("V4 clouds: %d sprites, shadows %s", count, shadows is not None)
    return count
