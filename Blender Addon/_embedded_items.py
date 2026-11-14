"""Typed map placement for embedded Item source collections."""

import logging

import bpy
from bpy.props import StringProperty

from . import _asset_panel

log = logging.getLogger("trackmania_blender_bridge.embedded_items")


def context_collection(context):
    clicked = getattr(context, "id", None)
    if clicked is not None:
        return clicked if isinstance(clicked, bpy.types.Collection) else None
    return getattr(context, "collection", None)


def item_owners(definition):
    if definition is None or not definition.get("tm_embedded"):
        return []
    if definition.gbx_kind != "ITEM" and definition.get("tm_asset_type") != "Item":
        return []
    owners = []
    for scene in bpy.data.scenes:
        if scene.get("tm_scene_role") != "MAP":
            continue
        for root in scene.collection.children:
            if root.get("tm_import_version") != 4:
                continue
            for library in (*root.children, *scene.collection.children):
                if not library.get("tm_embedded") or definition.name not in library.children:
                    continue
                if (root.get("tm_embedded_asset_collection") == library.name
                        or (library.get("tm_map_id") and library.get("tm_map_id") == root.get("tm_map_id"))):
                    owners.append((scene, root))
    return owners


class TMBRIDGE_OT_PlaceEmbeddedItem(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.place_embedded_item"
    bl_label = "Place in Map"
    bl_description = "Place this embedded Item at its map's 3D cursor and select the new placement"
    bl_options = {"REGISTER", "UNDO"}

    source_collection: StringProperty(options={"HIDDEN"})  # pyright: ignore[reportInvalidTypeForm]

    @classmethod
    def poll(cls, context):
        return context.window is not None and bool(item_owners(context_collection(context)))

    def execute(self, context):
        definition = (bpy.data.collections.get(self.source_collection)
                      if self.source_collection else context_collection(context))
        owners = item_owners(definition)
        active = [owner for owner in owners if owner[0] == context.scene]
        choices = active or owners
        if len(choices) != 1:
            self.report({"ERROR"}, "Select an embedded Item belonging to one map, or activate its Map scene")
            return {"CANCELLED"}
        scene, root = choices[0]
        if root.get("tm_import_status") not in {"COMPLETE", "PARTIAL"}:
            self.report({"ERROR"}, "The map must finish importing before placing an item")
            return {"CANCELLED"}
        original_scene = context.window.scene
        section = next((coll for coll in root.children if coll.get("tm_section") == "AnchoredItems"), None)
        created_section = section is None
        obj = None
        try:
            if section is None:
                section = bpy.data.collections.new("AnchoredItems")
                section["tm_section"] = "AnchoredItems"
                root.children.link(section)
            obj = bpy.data.objects.new(definition.get("tm_asset_name", definition.name), None)
            section.objects.link(obj)
            obj.instance_type = "COLLECTION"
            obj.instance_collection = definition
            obj.empty_display_type = "PLAIN_AXES"
            obj.empty_display_size = 0.5
            obj.location = scene.cursor.location
            obj.gbx_kind = "ITEM"
            obj.gbx_item_instance.data_ref = definition
            obj.gbx_item_instance.variant_index = definition.get("tm_item_variant_index", 0)
            obj["tm_map_root"] = root["tm_map_id"]
            _asset_panel.stamp(obj, "Item", definition.get("tm_source_path", ""),
                               definition.get("tm_asset_name", definition.name))
            context.window.scene = scene
            view_layer = context.window.view_layer
            view_layer.update()
            if obj.name not in view_layer.objects or obj.hide_get(view_layer=view_layer):
                raise ValueError("Enable the map's AnchoredItems placement collection before placing an item")
            for selected in list(view_layer.objects.selected):
                selected.select_set(False, view_layer=view_layer)
            obj.select_set(True, view_layer=view_layer)
            view_layer.objects.active = obj
            root["tm_placement_count"] = root.get("tm_placement_count", 0) + 1
        except Exception as exc:
            log.exception("Failed to place embedded Item %s", definition.name)
            context.window.scene = original_scene
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
            if created_section and section is not None:
                bpy.data.collections.remove(section)
            self.report({"ERROR"}, f"Item placement failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Placed {obj.name} in {scene.name}")
        return {"FINISHED"}


def collection_context_menu(self, context):
    definition = context_collection(context)
    if item_owners(definition):
        self.layout.separator()
        op = self.layout.operator(TMBRIDGE_OT_PlaceEmbeddedItem.bl_idname, icon="OUTLINER_OB_EMPTY")
        op.source_collection = definition.name


def register():
    bpy.types.OUTLINER_MT_collection.append(collection_context_menu)


def unregister():
    bpy.types.OUTLINER_MT_collection.remove(collection_context_menu)


CLASSES = (TMBRIDGE_OT_PlaceEmbeddedItem,)
