"""Convert BlenderMania collections into map-owned Blender Item definitions."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
import sys
import tempfile
import uuid

import bpy
from bpy.props import EnumProperty, StringProperty
from mathutils import Vector

from . import _asset_panel, _map_panel
from .map_import_v4.bridge import collection_layer, embedded_assets_collection, exclude_embedded_assets

log = logging.getLogger("trackmania_blender_bridge.blendermania_items")
_scene_items: list[tuple[str, str, str]] = []
_target_items: list[tuple[str, str, str]] = []


def enabled_blendermania():
    for addon in bpy.context.preferences.addons:
        module = sys.modules.get(addon.module)
        if getattr(module, "bl_info", {}).get("name", "").casefold() == "blendermania":
            return module
    return None


def context_collection(context):
    clicked = getattr(context, "id", None)
    if clicked is not None:
        return clicked if isinstance(clicked, bpy.types.Collection) else None
    return getattr(context, "collection", None)


def is_item_collection(collection) -> bool:
    module = enabled_blendermania()
    if module is None or collection is None or collection.gbx_kind != "NONE":
        return False
    export = importlib.import_module(module.__name__ + ".utils.ItemsExport")
    # Eligibility must not depend on which scene/view layer is currently active.
    return collection.name.lower() not in export.NOT_ALLOWED_COLLECTION_NAMES and any(
        obj.type == "MESH" and export._is_real_object(obj.name) for obj in collection.objects)


def _datablocks():
    return [block for prop in bpy.data.bl_rna.properties if prop.type == "COLLECTION"
            for block in getattr(bpy.data, prop.identifier)
            if isinstance(block, bpy.types.ID)]


def _enable_source_layer(layer):
    layer.exclude = False
    for child in layer.children:
        _enable_source_layer(child)


def existing_definitions(source, scene):
    if source is None or scene is None:
        return []
    root = _map_panel.map_root(scene)
    name = root.get("tm_embedded_asset_collection") if root else None
    library = (root.children.get(name) or scene.collection.children.get(name)) if name else None
    if library is None and root is not None:
        library = next((coll for coll in (*root.children, *scene.collection.children)
                        if coll.get("tm_embedded") and coll.get("tm_map_id") == root.get("tm_map_id")), None)
    if library is None:
        return []
    return [coll for coll in library.children if coll.gbx_kind == "ITEM"
            and coll.get("tm_embedded") and (
                coll.get("tm_blendermania_source_ref") == source
                or (coll.get("tm_blendermania_source_ref") is None
                    and coll.get("tm_blendermania_source_collection") == source.name))]


def _properties(owner):
    return {key: value.to_dict() if hasattr(value, "to_dict")
            else value.to_list() if hasattr(value, "to_list") else value
            for key, value in owner.items()}


def _replace_contents(target, staged, scene, library):
    old_objects, old_children = list(target.objects), list(target.children)
    old_properties, old_offset = _properties(target), target.instance_offset.copy()
    asset_id = target["tm_asset_id"]
    try:
        for obj in old_objects:
            target.objects.unlink(obj)
        for coll in old_children:
            target.children.unlink(coll)
        for obj in staged.objects:
            target.objects.link(obj)
        for coll in staged.children:
            target.children.link(coll)
        for key in list(target.keys()):
            del target[key]
        for key, value in _properties(staged).items():
            target[key] = value
        target["tm_asset_id"] = asset_id
        target.instance_offset = staged.instance_offset
        for view_layer in scene.view_layers:
            view_layer.update()
            _enable_source_layer(collection_layer(view_layer.layer_collection, library).children[target.name])
        exclude_embedded_assets(scene, library)
    except Exception:
        for obj in list(target.objects):
            target.objects.unlink(obj)
        for coll in list(target.children):
            target.children.unlink(coll)
        for obj in old_objects:
            target.objects.link(obj)
        for coll in old_children:
            target.children.link(coll)
        for key in list(target.keys()):
            del target[key]
        for key, value in old_properties.items():
            target[key] = value
        target.instance_offset = old_offset
        raise
    return old_objects, old_children


def _remove_replaced_sources(objects, collections):
    candidates = set(objects) | set(collections)
    for coll in collections:
        candidates.update(coll.children_recursive)
        candidates.update(coll.all_objects)
    for block in list(candidates):
        if isinstance(block, bpy.types.Object):
            if block.data is not None:
                candidates.add(block.data)
                candidates.update(mat for mat in getattr(block.data, "materials", ()) if mat)
            if block.animation_data and block.animation_data.action:
                candidates.add(block.animation_data.action)
    while True:
        unused = {block for block in candidates if block.users == 0 and not block.use_fake_user}
        if not unused:
            break
        candidates -= unused
        bpy.data.batch_remove(unused)


def convert_collection(source, scene, *, update=None):
    root = _map_panel.map_root(scene)
    if root is None or scene.get("tm_scene_role") != "MAP":
        raise ValueError("Choose an imported Map scene")
    if not is_item_collection(source):
        raise ValueError("Choose an exportable BlenderMania item collection")
    matches = existing_definitions(source, scene)
    if update is not None and update not in matches:
        raise ValueError("Choose an embedded item from this source in the selected Map scene")
    module = enabled_blendermania()
    assert module is not None
    functions = importlib.import_module(module.__name__ + ".utils.Functions")
    origin = functions.get_offset_from_item_origin(list(source.objects))
    if len(origin) != 3:
        raise ValueError("BlenderMania item has no usable origin")
    before = {block.as_pointer() for block in _datablocks()}
    metadata_key = "tm_embedded_asset_collection"
    previous_names = [(owner, owner.get(metadata_key)) for owner in (root, scene)]
    name = root.get(metadata_key, "")
    old_library = root.children.get(name) or scene.collection.children.get(name)
    previous_exclusions = []
    if old_library is not None:
        for view_layer in scene.view_layers:
            layer = collection_layer(view_layer.layer_collection, old_library)
            previous_exclusions.append((layer, layer.exclude))
    try:
        # Blender's native append remaps the complete dependency graph: parents,
        # constraints, modifiers, materials, nested collections and animation.
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "item.blend")
            bpy.data.libraries.write(path, {source}, path_remap="ABSOLUTE")
            with bpy.data.libraries.load(path, link=False) as (_available, loaded):
                loaded.collections = [source.name]
            definition = loaded.collections[0]
        if definition is None:
            raise RuntimeError("Blender did not append the item source collection")
        definition.name = f"Embedded Item {source.name}"
        definition.gbx_kind = "ITEM"
        definition.gbx_item_data.source_file_path = ""
        definition.instance_offset = Vector(origin)
        _asset_panel.stamp(definition, "Item", "", source.name)
        definition["tm_asset_id"] = uuid.uuid4().hex
        definition["tm_embedded"] = True
        definition["tm_map_id"] = root["tm_map_id"]
        definition["tm_map_scene"] = scene.name
        definition["tm_blendermania_source_collection"] = source.name
        definition["tm_blendermania_source_ref"] = source
        definition["tm_blendermania_source_id"] = next(
            (coll["tm_blendermania_source_id"] for coll in matches if coll.get("tm_blendermania_source_id")),
            uuid.uuid4().hex)
        library = embedded_assets_collection(scene, root)
        library.children.link(definition)
        for view_layer in scene.view_layers:
            view_layer.update()
            _enable_source_layer(collection_layer(view_layer.layer_collection, library).children[definition.name])
        exclude_embedded_assets(scene, library)
        if update is not None:
            old_objects, old_children = _replace_contents(update, definition, scene, library)
    except Exception:
        log.exception("BlenderMania item conversion failed: %s", source.name)
        bpy.data.batch_remove([block for block in _datablocks() if block.as_pointer() not in before])
        for owner, name in previous_names:
            if name is None:
                if metadata_key in owner:
                    del owner[metadata_key]
            else:
                owner[metadata_key] = name
        for layer, excluded in previous_exclusions:
            layer.exclude = excluded
        raise
    if update is not None:
        bpy.data.collections.remove(definition)
        definition = update
        _remove_replaced_sources(old_objects, old_children)
    log.info("Converted BlenderMania item %s to %s in %s", source.name, definition.name, scene.name)
    return definition


def map_scenes(_self, _context):
    _scene_items[:] = [(scene.name, scene.name, "Map scene owning the embedded item")
                      for scene in bpy.data.scenes
                      if scene.get("tm_scene_role") == "MAP" and _map_panel.map_root(scene) is not None]
    return _scene_items


def update_targets(self, _context):
    source = bpy.data.collections.get(self.source_collection)
    scene = bpy.data.scenes.get(self.map_scene)
    _target_items[:] = [(coll["tm_asset_id"], coll.name, "Replace this embedded item's source contents")
                       for coll in existing_definitions(source, scene)]
    return _target_items


class TMBRIDGE_OT_ConvertBlenderManiaItem(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.convert_blendermania_item"
    bl_label = "Convert to Embedded Item"
    bl_description = "Copy this BlenderMania item into a Map scene as an editable Item source, without a placement"
    bl_options = {"REGISTER", "UNDO"}

    source_collection: StringProperty(options={"HIDDEN"})  # pyright: ignore[reportInvalidTypeForm]
    map_scene: EnumProperty(name="Map Scene", items=map_scenes)  # pyright: ignore[reportInvalidTypeForm]
    mode: EnumProperty(name="Action", items=[  # pyright: ignore[reportInvalidTypeForm]
        ("NEW", "Create New Copy", "Create an independent embedded definition"),
        ("UPDATE", "Update Existing", "Replace source contents while preserving placements"),
    ], default="NEW")
    update_target: EnumProperty(name="Embedded Item", items=update_targets)  # pyright: ignore[reportInvalidTypeForm]

    @classmethod
    def poll(cls, context):
        return is_item_collection(context_collection(context))

    def invoke(self, context, event):
        source = context_collection(context)
        if not self.source_collection and source is not None:
            self.source_collection = source.name
        maps = map_scenes(self, context)
        if not maps:
            self.report({"ERROR"}, "Import a map before converting a BlenderMania item")
            return {"CANCELLED"}
        self.map_scene = context.scene.name if any(item[0] == context.scene.name for item in maps) else maps[0][0]
        targets = update_targets(self, context)
        if targets:
            self.mode = "UPDATE"
            self.update_target = targets[0][0]
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        self.layout.label(text=self.source_collection, icon="OUTLINER_COLLECTION")
        self.layout.prop(self, "map_scene")
        self.layout.prop(self, "mode")
        if self.mode == "UPDATE":
            self.layout.prop(self, "update_target")
            warning = self.layout.column()
            warning.alert = True
            warning.label(text="Replaces edits made directly to the embedded item.")
            warning.label(text="Existing placements and their transforms are preserved.")
        self.layout.label(text="Independent source copy only; original stays unchanged.")

    def execute(self, context):
        source = bpy.data.collections.get(self.source_collection) if self.source_collection else context_collection(context)
        scene = bpy.data.scenes.get(self.map_scene)
        if source is None or scene is None:
            self.report({"ERROR"}, "The source collection or target Map scene no longer exists")
            return {"CANCELLED"}
        try:
            update = None
            if self.mode == "UPDATE":
                update = next((coll for coll in existing_definitions(source, scene)
                               if coll.get("tm_asset_id") == self.update_target), None)
                if update is None:
                    self.report({"ERROR"}, "Choose an existing embedded item in this Map scene")
                    return {"CANCELLED"}
            definition = convert_collection(source, scene, update=update)
        except Exception as exc:
            self.report({"ERROR"}, f"Item conversion failed: {exc}")
            return {"CANCELLED"}
        action = "Updated" if self.mode == "UPDATE" else "Added"
        self.report({"INFO"}, f"{action} {definition.name} in {scene.name}; no placement created")
        return {"FINISHED"}


def collection_context_menu(self, context):
    source = context_collection(context)
    if is_item_collection(source):
        self.layout.separator()
        op = self.layout.operator(TMBRIDGE_OT_ConvertBlenderManiaItem.bl_idname, icon="OUTLINER_COLLECTION")
        op.source_collection = source.name


def register():
    bpy.types.OUTLINER_MT_collection.append(collection_context_menu)


def unregister():
    bpy.types.OUTLINER_MT_collection.remove(collection_context_menu)


CLASSES = (TMBRIDGE_OT_ConvertBlenderManiaItem,)
