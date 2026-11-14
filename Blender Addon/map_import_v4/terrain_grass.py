"""Vista terrain grass: camera-facing 3D decal billboards on terrain layers.

Terrain meshes carry ``tm_terrain_layers`` (vertex slot -> top layer) and
``TMLayer<slot>`` corner weights (see the mesh builder). After the map's
placements exist, every visible terrain instance in the map scene's
depsgraph contributes its up-facing triangles; hidden placements are not
evaluated, so occupied terrain grows no grass. Each layer with a decal
pattern (:mod:`GBXBPY.textures.terrain_decals`) is scattered by
:func:`GBXBPY.map_import_v4.terrain_grass.scatter` into one point cloud,
instanced by a geometry-nodes modifier with one textured quad per atlas
sprite, turned about Z to face the scene's active camera. Vista ground
under water is often Land-weighted and level with the water plane, so
points at or below a visible ``Water*`` material face are dropped.
"""

from __future__ import annotations

import json
import logging
import os

import bpy
import numpy as np

from GBXBPY.map_import_v4.terrain_grass import below_surfaces, grass_pattern, scatter
from GBXBPY.textures.terrain_decals import layer_decals

log = logging.getLogger("trackmania_blender_bridge.map_import_v4")

GROUP_NAME = "TM Terrain Grass"


def _mesh_layers(mesh, cache: dict):
    """Per decal layer key: (local triangles (n, 3, 3), corner weights (n, 3))."""
    key = mesh.as_pointer()
    if key in cache:
        return cache[key]
    result = {}
    cache[key] = result
    try:
        slots = json.loads(mesh.get("tm_terrain_layers") or "{}")
    except ValueError:
        return result
    by_layer: dict = {}
    for slot, info in slots.items():
        media, layer = info.get("media") or "", info.get("layer") or ""
        if not media or not layer or layer_decals(media, layer) is None:
            continue
        if mesh.attributes.get(f"TMLayer{slot}") is None:
            continue
        tile = tuple(float(t) for t in info.get("tile") or (64.0, 64.0))
        by_layer.setdefault((media, layer, tile), []).append(slot)
    if not by_layer:
        return result
    mesh.calc_loop_triangles()
    count = len(mesh.loop_triangles)
    if not count:
        return result
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    verts = np.empty(count * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", verts)
    loops = np.empty(count * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", loops)
    tris = co[verts.reshape(-1, 3)]
    corner = np.empty(len(mesh.loops), dtype=np.float64)
    for layer_key, layer_slots in by_layer.items():
        weights = np.zeros(count * 3, dtype=np.float64)
        for slot in layer_slots:
            mesh.attributes[f"TMLayer{slot}"].data.foreach_get("value", corner)
            weights += corner[loops]
        weights = np.clip(weights.reshape(-1, 3), 0.0, 1.0)
        used = weights.max(axis=1) > 0.0
        if used.any():
            result[layer_key] = (tris[used], weights[used])
    return result


def _water_triangles(mesh, cache: dict):
    """Local triangles of faces using a ``Water*`` GBXMAT material, or ``None``."""
    key = mesh.as_pointer()
    if key in cache:
        return cache[key]
    slots = [
        i for i, mat in enumerate(mesh.materials)
        if mat is not None and str(mat.get("gbxmat_family", "")).startswith("Water")
    ]
    result = None
    if slots:
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        material = np.empty(count, dtype=np.int64)
        mesh.loop_triangles.foreach_get("material_index", material)
        chosen = np.isin(material, slots)
        if chosen.any():
            co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
            mesh.vertices.foreach_get("co", co)
            verts = np.empty(count * 3, dtype=np.int64)
            mesh.loop_triangles.foreach_get("vertices", verts)
            result = co.reshape(-1, 3)[verts.reshape(-1, 3)[chosen]]
    cache[key] = result
    return result


def collect_terrain(depsgraph) -> tuple[dict, np.ndarray]:
    """Visible terrain and water in world space.

    Returns (layer key -> (triangles, corner weights), water triangles).
    """
    cache: dict = {}
    water_cache: dict = {}
    parts: dict = {}
    water = []
    for inst in depsgraph.object_instances:
        obj = inst.object
        if obj.type != "MESH":
            continue
        mesh = obj.original.data
        if mesh is None:
            continue
        matrix = None
        if "tm_terrain_layers" in mesh:
            layers = _mesh_layers(mesh, cache)
            if layers:
                matrix = np.array(inst.matrix_world, dtype=np.float64)
            for layer_key, (tris, weights) in layers.items():
                world = tris @ matrix[:3, :3].T + matrix[:3, 3]
                parts.setdefault(layer_key, []).append((world, weights))
        surface = _water_triangles(mesh, water_cache)
        if surface is not None:
            if matrix is None:
                matrix = np.array(inst.matrix_world, dtype=np.float64)
            water.append(surface @ matrix[:3, :3].T + matrix[:3, 3])
    terrain = {
        key: (np.concatenate([w for w, _ in chunks]), np.concatenate([g for _, g in chunks]))
        for key, chunks in parts.items()
    }
    return terrain, (np.concatenate(water) if water else np.zeros((0, 3, 3)))


def _atlas_material(image_path: str):
    from GBXMAT.images import load_image

    name = f"TM Terrain Grass {os.path.splitext(os.path.basename(image_path))[0]}"
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat["tm_terrain_grass_atlas"] = image_path
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links
    for node in list(nodes):
        nodes.remove(node)
    output = nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = load_image(image_path, "Diffuse")
    tex.interpolation = "Closest" if tex.image is None else "Linear"
    # Grass lit like the ground it stands on, not like a vertical card.
    up = nodes.new("ShaderNodeCombineXYZ")
    up.inputs[2].default_value = 1.0
    clip = nodes.new("ShaderNodeMath")
    clip.operation = "GREATER_THAN"
    clip.inputs[1].default_value = 0.5
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    links.new(tex.outputs["Alpha"], clip.inputs[0])
    links.new(clip.outputs[0], bsdf.inputs["Alpha"])
    links.new(up.outputs[0], bsdf.inputs["Normal"])
    bsdf.inputs["Roughness"].default_value = 0.9
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = "CLIP"
        except TypeError:
            pass
    if hasattr(mat, "use_backface_culling"):
        mat.use_backface_culling = False
    return mat


def _sprite_quad(name: str, sprite, material):
    """Unit-height quad facing +Y, base at the sprite pivot, atlas UVs."""
    width = sprite.w / sprite.h if sprite.h else 1.0
    left, right = sprite.pivot_x * width, -(1.0 - sprite.pivot_x) * width
    bottom, top = -sprite.pivot_y, 1.0 - sprite.pivot_y
    mesh = bpy.data.meshes.new(name)
    # Seen from +Y the image's left edge is at +X.
    mesh.from_pydata(
        [(left, 0.0, bottom), (right, 0.0, bottom), (right, 0.0, top), (left, 0.0, top)],
        [], [(0, 1, 2, 3)],
    )
    uv = mesh.uv_layers.new(name="UVMap")
    u0, u1 = sprite.x, sprite.x + sprite.w
    v0, v1 = sprite.y, sprite.y + sprite.h
    uv.data.foreach_set("uv", [u0, v0, u1, v0, u1, v1, u0, v1])
    mesh.materials.append(material)
    return bpy.data.objects.new(name, mesh)


def _node_group(sprites_collection):
    group = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")

    sprites = nodes.new("GeometryNodeCollectionInfo")
    sprites.transform_space = "ORIGINAL"
    sprites.inputs["Collection"].default_value = sprites_collection
    sprites.inputs["Separate Children"].default_value = True
    sprites.inputs["Reset Children"].default_value = True

    sprite_index = nodes.new("GeometryNodeInputNamedAttribute")
    sprite_index.data_type = "INT"
    sprite_index.inputs["Name"].default_value = "sprite"
    height = nodes.new("GeometryNodeInputNamedAttribute")
    height.data_type = "FLOAT"
    height.inputs["Name"].default_value = "height"

    camera = nodes.new("GeometryNodeInputActiveCamera")
    camera_info = nodes.new("GeometryNodeObjectInfo")
    camera_info.transform_space = "RELATIVE"
    position = nodes.new("GeometryNodeInputPosition")
    towards = nodes.new("ShaderNodeVectorMath")
    towards.operation = "SUBTRACT"
    align = nodes.new("FunctionNodeAlignRotationToVector")
    align.axis = "Y"
    align.pivot_axis = "Z"

    instance = nodes.new("GeometryNodeInstanceOnPoints")
    instance.inputs["Pick Instance"].default_value = True

    links.new(camera.outputs[0], camera_info.inputs["Object"])
    links.new(camera_info.outputs["Location"], towards.inputs[0])
    links.new(position.outputs["Position"], towards.inputs[1])
    links.new(towards.outputs["Vector"], align.inputs["Vector"])
    links.new(group_in.outputs[0], instance.inputs["Points"])
    links.new(sprites.outputs[0], instance.inputs["Instance"])
    links.new(sprite_index.outputs["Attribute"], instance.inputs["Instance Index"])
    links.new(align.outputs["Rotation"], instance.inputs["Rotation"])
    links.new(height.outputs["Attribute"], instance.inputs["Scale"])
    links.new(instance.outputs["Instances"], group_out.inputs[0])
    return group


def build_terrain_grass(plan, depsgraph, root, library, density: float = 1.0):
    """Scatter grass for every visible terrain layer.

    Adds a ``Terrain Grass`` collection under ``root`` and the sprite quads
    under ``library``; returns the sprite collection, or ``None`` when no
    terrain grows grass.
    """
    terrain, water = collect_terrain(depsgraph)
    if not terrain:
        return None
    sprites: list = []
    positions, sprite_ids, heights = [], [], []
    for (media, layer, tile), (tris, weights) in sorted(terrain.items()):
        pattern = grass_pattern(layer_decals(media, layer), tile, sprites)
        if pattern is None:
            continue
        pos, entries = scatter(tris, weights, pattern, density)
        dry = ~below_surfaces(pos, water)
        log.info(
            "terrain grass: %s %d triangles -> %d billboards (%d under water dropped)",
            layer, len(tris), int(dry.sum()), int((~dry).sum()),
        )
        pos, entries = pos[dry], entries[dry]
        if len(pos):
            positions.append(pos)
            sprite_ids.append(pattern.sprite[entries])
            heights.append(pattern.height[entries])
    if not positions:
        return None
    positions = np.concatenate(positions)
    sprite_ids = np.concatenate(sprite_ids).astype(np.int32)
    heights = np.concatenate(heights).astype(np.float32)

    from .._text import plain_name

    sprites_collection = bpy.data.collections.new(f"Terrain Grass Sprites {plain_name(plan.name)}")
    library.children.link(sprites_collection)
    materials: dict = {}
    for index, key in enumerate(sprites):
        material = materials.get(key.atlas) or materials.setdefault(key.atlas, _atlas_material(key.atlas))
        sprites_collection.objects.link(_sprite_quad(f"{index:04d} {key.sprite}", key.record, material))

    mesh = bpy.data.meshes.new(f"TM Terrain Grass {plan.name}")
    mesh.vertices.add(len(positions))
    mesh.vertices.foreach_set("co", positions.astype(np.float32).ravel())
    mesh.attributes.new("sprite", "INT", "POINT").data.foreach_set("value", sprite_ids)
    mesh.attributes.new("height", "FLOAT", "POINT").data.foreach_set("value", heights)
    obj = bpy.data.objects.new("TM Terrain Grass", mesh)
    obj["tm_terrain_grass_count"] = len(positions)
    modifier = obj.modifiers.new("Terrain Grass", "NODES")
    modifier.node_group = _node_group(sprites_collection)
    collection = bpy.data.collections.new("Terrain Grass")
    collection["tm_section"] = "TerrainGrass"
    root.children.link(collection)
    collection.objects.link(obj)
    root["tm_terrain_grass_count"] = len(positions)
    return sprites_collection
