"""Blender representation of GBX semantic parts and item definitions."""

from dataclasses import asdict
import json
import logging

import bpy
from mathutils import Matrix

from GBXBPY.model.geometry import MeshPart

log = logging.getLogger("trackmania_blender_bridge.item_geometry")
_WAYPOINT_COLORS = {
    "No": "NONE", "None": "NONE", "Start": "COLOR_04",
    "Checkpoint": "COLOR_05", "StartFinish": "COLOR_03", "Finish": "COLOR_01",
}


def configure_part(obj, part):
    """Names are the Blendermania adapter; tm_* values retain source semantics."""
    name = part.name or obj.name
    for prefix in ("_ignore_", "_trigger_", "_notvisible_", "_notcollidable_"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    if not part.enabled or (not part.visible and not part.collidable and part.role != "trigger"):
        prefix = "_ignore_"
    elif part.role == "trigger":
        prefix = "_trigger_"
    elif not part.visible:
        prefix = "_notvisible_"
    elif not part.collidable:
        prefix = "_notcollidable_"
    else:
        prefix = ""
        if name.startswith("_"):
            name = "Geometry" + name
    obj.name = prefix + name
    obj["tm_part_role"] = part.role
    obj["tm_visible"] = part.visible
    obj["tm_collidable"] = part.collidable
    obj["tm_part_enabled"] = part.enabled
    obj["tm_layer_index"] = part.layer_index
    obj["tm_layer_id"] = part.layer_id
    obj["tm_layer_type"] = part.layer_type
    obj["tm_surface_type"] = part.surface_type
    obj["tm_surface_bone_index"] = part.bone_index
    obj.hide_render = not part.visible or not part.enabled
    obj.hide_viewport = not part.visible or not part.enabled
    if not part.visible:
        obj.display_type = "WIRE"
    if part.mesh is None:
        obj["tm_placeholder"] = True
        obj.name = "_ignore_" + obj.name.lstrip("_")
    elif part.role == "trigger":
        obj.data.materials.clear()
        for uv in list(obj.data.uv_layers):
            obj.data.uv_layers.remove(uv)
    elif part.role == "geometry":
        for name in ("BaseMaterial", "LightMap"):
            if not any(uv.name.casefold() == name.casefold() for uv in obj.data.uv_layers):
                obj.data.uv_layers.new(name=name)
    if part.mesh is not None and part.layer_index >= 0:
        obj["tm_material_indices"] = json.dumps(part.material_indices)
        for name, values in (("tm_face_group", part.face_groups), ("tm_source_face", part.face_sources)):
            if len(values) == len(obj.data.polygons):
                attr = obj.data.attributes.get(name) or obj.data.attributes.new(name, "INT", "FACE")
                attr.data.foreach_set("value", values)
            else:
                if values:
                    log.warning("%s: %s provenance count differs after mesh construction", obj.name, name)


def collision_material(binding):
    physics = binding.surface_physic_id
    gameplay = binding.surface_gameplay_id
    name = f"TM_Invisible_{physics or 'Unknown'}"
    if gameplay not in ("", "No", "None"):
        name += "_" + gameplay
    mat = next((candidate for candidate in bpy.data.materials
                if candidate.get("tm_collision_material") and
                candidate.get("tm_physics_id") == physics and candidate.get("tm_gameplay_id") == gameplay and
                candidate.get("tm_material_path") == binding.material_path), None)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        mat["tm_collision_material"] = True
    mat["tm_physics_id"] = physics
    mat["tm_gameplay_id"] = gameplay
    mat["tm_material_path"] = binding.material_path
    if all(hasattr(mat, prop) for prop in (
            "gameType", "environment", "link", "physicsId", "usePhysicsId", "gameplayId", "useGameplayId")):
        mat.gameType = "Trackmania2020"
        mat.environment = "Stadium"
        supported_physics = bool(physics) and not physics.lstrip("-").isdecimal()
        mat.link = "Invisible_" + physics if supported_physics else ""
        mat.physicsId = physics
        mat.usePhysicsId = bool(physics)
        mat.gameplayId = gameplay
        mat.useGameplayId = gameplay not in ("", "No", "None")
        if not supported_physics:
            log.warning("%s: collision physics has no exportable name; source ID retained", mat.name)
    return mat


def build_part(part, lod_level=1, *, resolver=None, source_path="", progress=None):
    if part.mesh is None:
        obj = bpy.data.objects.new(part.name, None)
        obj.empty_display_type = "CUBE"
    elif part.role in ("collision", "trigger"):
        mesh = part.mesh
        lod = mesh.lod(lod_level) or (mesh.lods[0] if mesh.lods else None)
        if lod is None:
            raise ValueError(f"{part.name}: surface has no geometry")
        data = bpy.data.meshes.new(part.name)
        faces, slots = [], []
        for submesh in lod.submeshes:
            for i in range(0, len(submesh.indices), 3):
                faces.append(submesh.indices[i:i + 3])
                slots.append(submesh.material_index)
        data.from_pydata([v.position.as_tuple() for v in lod.vertices], [], faces)
        if part.role == "collision":
            for binding in mesh.materials:
                data.materials.append(collision_material(binding))
            for face, slot in zip(data.polygons, slots):
                face.material_index = slot
        obj = bpy.data.objects.new(part.name, data)
    else:
        from . import _build_blender_mesh_from_gbxbpy
        obj = _build_blender_mesh_from_gbxbpy(
            part.mesh, lod_level, resolver=resolver, source_path=source_path, progress=progress,
        )
    configure_part(obj, part)
    obj.matrix_local = Matrix(part.matrix)
    return obj


def attach_collision_parts(obj, mesh, collection, *, lod_level=1, resolver=None, source_path=""):
    for part in mesh.collision_parts:
        child = build_part(part, lod_level, resolver=resolver, source_path=source_path)
        collection.objects.link(child)
        child.parent = obj


def configure_item_collection(collection, item, source_path):
    from . import _asset_panel
    _asset_panel.stamp(collection, "Item", source_path, item.name or "")
    _asset_panel.stamp_item_placement(collection, item.default_placement)
    collection["tm_item_type"] = item.item_type
    collection["tm_item_page"] = item.page_path
    collection["tm_item_waypoint_type"] = item.waypoint_type
    collection["tm_item_is_ingame"] = item.is_ingame
    collection["tm_item_is_custom"] = item.is_custom
    collection["tm_crystal_layers"] = json.dumps([asdict(layer) for layer in item.crystal_layers])
    collection["tm_item_entity_models"] = json.dumps([
        {"path": variant.entity_model_path, "tags": variant.tags,
         "hidden": variant.hidden_in_manual_cycle} for variant in item.entity_variants
    ])
    collection.color_tag = _WAYPOINT_COLORS.get(item.waypoint_type, "NONE")
    if item.waypoint_type not in _WAYPOINT_COLORS:
        log.warning("Waypoint type %s has no Blendermania collection color", item.waypoint_type)
    if hasattr(collection, "gbx_kind"):
        collection.gbx_kind = "ITEM"
        collection.gbx_item_data.source_file_path = source_path
    if item.spawn_transform is not None and item.waypoint_type in ("Start", "Checkpoint", "StartFinish"):
        obj = bpy.data.objects.new("_socket_start_", None)
        obj.empty_display_type = "ARROWS"
        obj.matrix_local = Matrix(item.spawn_transform)
        obj["tm_part_role"] = "spawn"
        collection.objects.link(obj)
    elif item.waypoint_type in ("Start", "Checkpoint", "StartFinish"):
        log.warning("%s has no decoded spawn transform; no synthetic socket created", item.name)


def build_item_collection(item, context, lod_level, *, resolver=None, source_path="", progress=None,
                          target_collection=None):
    from . import _build_prefab_object_impl
    collection = target_collection
    if collection is None:
        collection = bpy.data.collections.new(item.name or "Item")
        context.collection.children.link(collection)
    configure_item_collection(collection, item, source_path)
    if item.custom_block is not None:
        collection["tm_custom_block_archetype"] = item.custom_block.archetype_id
        collection["tm_custom_block_collection"] = item.custom_block.archetype_collection
        for key, parts in item.custom_block.variants.items():
            variant = bpy.data.collections.new(f"{item.name}_{key}")
            variant["tm_custom_block_variant_key"] = key
            collection.children.link(variant)
            for part in parts:
                variant.objects.link(build_part(part, lod_level, resolver=resolver, source_path=source_path))
    for i, part in enumerate(item.parts):
        reporter = progress.sub(i / max(1, len(item.parts)), (i + 1) / max(1, len(item.parts))) if progress else None
        obj = build_part(part, lod_level, resolver=resolver, source_path=source_path, progress=reporter)
        collection.objects.link(obj)
    if item.embedded_prefab is not None:
        _build_prefab_object_impl(
            item.embedded_prefab, context, lod_level, target_collection=collection,
            resolver=resolver, source_path=source_path, resolve_external_entities=True, progress=progress,
        )
    elif not item.parts and item.entity_variants:
        if resolver is None:
            raise ValueError("This item needs GameData to resolve its entity model")
        import GBXBPY
        from GBXBPY.model import Mesh, Prefab
        reference = item.entity_variants[0].entity_model_path
        path = resolver.resolve(reference, source_path=source_path)
        if path is None:
            raise ValueError(f"Item entity model not found: {reference}")
        payload = GBXBPY.load(path, resolver=resolver)
        if isinstance(payload, Mesh):
            obj = build_part(MeshPart(name=payload.name or "Geometry", mesh=payload,
                                      collidable=payload.collidable),
                             lod_level, resolver=resolver, source_path=path)
            collection.objects.link(obj)
            attach_collision_parts(obj, payload, collection, lod_level=lod_level,
                                   resolver=resolver, source_path=path)
        elif isinstance(payload, Prefab):
            _build_prefab_object_impl(payload, context, lod_level, target_collection=collection,
                                      resolver=resolver, source_path=path, resolve_external_entities=True)
        else:
            from GBXBPY.model import VegetationModel
            if isinstance(payload, VegetationModel) and payload.variants and payload.variants[0].mesh is not None:
                mesh = payload.variants[0].mesh
                obj = build_part(MeshPart(name=mesh.name or "Vegetation", mesh=mesh),
                                 lod_level, resolver=resolver, source_path=path)
                collection.objects.link(obj)
            else:
                raise ValueError(f"Unsupported item entity model: {type(payload).__name__}")
    if not item.parts and item.embedded_prefab is None and not item.entity_variants and item.custom_block is None:
        raise ValueError("Item has no supported geometry or entity model")
    return collection
