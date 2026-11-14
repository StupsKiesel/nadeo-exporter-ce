"""Selection-driven asset controls, alongside the original read-only panel."""

import json
import logging
import math
import os

import bpy
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
    IntProperty, IntVectorProperty, PointerProperty, StringProperty,
)

from . import ADDON_SHORT_NAME, _asset_panel, _map_panel, _progress


log = logging.getLogger("trackmania_blender_bridge.asset_controls")
KINDS = (
    ("BLOCK", "Block", "MESH_CUBE"),
    ("ITEM", "Item", "OUTLINER_OB_EMPTY"),
    ("CLIP", "Clip", "MOD_BOOLEAN"),
    ("PREFAB", "Prefab", "OUTLINER_COLLECTION"),
)
DEFAULT_FIELDS = (
    ("ghost_mode", "Ghost Mode"),
    ("cube_size", "Cube Size"),
    ("grid_snap_h_step", "Horizontal"),
    ("grid_snap_v_step", "Vertical"),
    ("grid_snap_h_offset", "Offset"),
    ("fly_step", "Fly Step"),
    ("pivot_snap_distance", "Pivot Distance"),
)


def selection(context):
    return _asset_panel.selected_asset(context)


def owners(obj):
    yield obj
    collection = obj.instance_collection
    if collection is None:
        from . import gbx_props
        instance = gbx_props.active_instance(obj)
        collection = instance.data_ref if instance is not None else None
    if collection is not None:
        yield collection


def value(obj, key, default=None):
    return next((owner[key] for owner in owners(obj) if key in owner), default)


def placement(obj):
    raw = value(obj, "tm_item_placement")
    if raw is None:
        return None
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Item defaults must contain a JSON object")
    for key, _label in DEFAULT_FIELDS:
        if key not in document:
            continue
        field = document[key]
        if key == "ghost_mode":
            valid = isinstance(field, bool)
        else:
            valid = (isinstance(field, (int, float)) and not isinstance(field, bool)
                     and math.isfinite(field))
        if not valid:
            raise ValueError(f"Invalid item default: {key}")
    pivots = document.get("pivot_positions", [])
    if not isinstance(pivots, list):
        raise ValueError("Pivot points must be a list")
    for pivot in pivots:
        if not isinstance(pivot, dict) or any(
            axis not in pivot or not isinstance(pivot[axis], (int, float))
            or not math.isfinite(pivot[axis]) for axis in ("x", "y", "z")
        ):
            raise ValueError("Invalid pivot coordinates")
    return document


def failure(obj, message):
    obj["tm_asset_controls_error"] = str(message)
    log.error("Asset %s: %s", obj.name, message)


def write_default(obj, key, new_value, index=None):
    document = placement(obj)
    if document is None:
        raise ValueError("Item defaults unavailable")
    if index is None:
        if key not in document:
            raise ValueError(f"Item default unavailable: {key}")
        document[key] = new_value
    else:
        pivots = document.get("pivot_positions", [])
        if not 0 <= index < len(pivots):
            raise ValueError("Pivot point unavailable")
        if not all(math.isfinite(component) for component in new_value):
            raise ValueError("Pivot coordinates must be finite")
        pivots[index] = dict(zip(("x", "y", "z"), new_value))
    # Store a placement-local override, never mutate shared GameData.
    obj["tm_item_placement"] = json.dumps(document, allow_nan=False)
    obj.pop("tm_asset_controls_error", None)


def metadata_get(key):
    return lambda self: str(value(self.id_data, key, ""))


def metadata_set(key):
    def set_value(self, new_value):
        self.id_data[key] = new_value
    return set_value


def name_get(self):
    obj = self.id_data
    return str(obj.get("tm_asset_name") or obj.get("tm_block_name")
               or obj.get("tm_item_model_id") or value(obj, "tm_asset_name", obj.name))


def default_get(key, fallback):
    def get_value(self):
        try:
            document = placement(self.id_data)
            return document.get(key, fallback) if document is not None else fallback
        except (ValueError, TypeError) as exc:
            failure(self.id_data, exc)
            return fallback
    return get_value


def default_set(key):
    def set_value(self, new_value):
        try:
            write_default(self.id_data, key, new_value)
        except (ValueError, TypeError) as exc:
            failure(self.id_data, exc)
    return set_value


def placement_value(obj, key, attribute):
    result = value(obj, key)
    if result is not None:
        return result
    from . import gbx_props
    instance = gbx_props.active_instance(obj)
    if instance is not None and hasattr(instance, attribute):
        return getattr(instance, attribute)
    return None


def placement_bool(key, attribute):
    return lambda self: bool(placement_value(self.id_data, key, attribute))


def grid_get(self):
    coord = placement_value(self.id_data, "tm_block_coord", "coord")
    return tuple(coord) if coord is not None else (0, 0, 0)


class TMBRIDGE_AssetPreviewSettings(bpy.types.PropertyGroup):
    placement_ground: BoolProperty(name="Ground", get=placement_bool("tm_block_is_ground", "is_ground"))
    placement_free: BoolProperty(name="Free", get=placement_bool("tm_block_is_free", "is_free"))
    placement_ghost: BoolProperty(name="Ghost", get=placement_bool("tm_block_is_ghost", "is_ghost"))
    placement_grid: IntVectorProperty(name="Grid", size=3, get=grid_get)
    asset_name: StringProperty(
        name="Name", description="Blender display name; does not change the source asset ID",
        get=name_get, set=metadata_set("tm_asset_name"),
    )
    author: StringProperty(
        name="Author", get=metadata_get("tm_item_author"), set=metadata_set("tm_item_author"),
    )
    item_type: StringProperty(
        name="Type", get=metadata_get("tm_item_type"), set=metadata_set("tm_item_type"),
    )
    ghost_mode: BoolProperty(
        name="Ghost Mode", get=default_get("ghost_mode", False), set=default_set("ghost_mode"),
    )
    cube_size: FloatProperty(
        name="Cube Size", unit="LENGTH", min=0,
        get=default_get("cube_size", 0.0), set=default_set("cube_size"),
    )
    grid_snap_h_step: FloatProperty(
        name="Horizontal", unit="LENGTH", min=0,
        get=default_get("grid_snap_h_step", 0.0), set=default_set("grid_snap_h_step"),
    )
    grid_snap_v_step: FloatProperty(
        name="Vertical", unit="LENGTH", min=0,
        get=default_get("grid_snap_v_step", 0.0), set=default_set("grid_snap_v_step"),
    )
    grid_snap_h_offset: FloatProperty(
        name="Offset", unit="LENGTH",
        get=default_get("grid_snap_h_offset", 0.0), set=default_set("grid_snap_h_offset"),
    )
    fly_step: FloatProperty(
        name="Fly Step", unit="LENGTH", min=0,
        get=default_get("fly_step", 0.0), set=default_set("fly_step"),
    )
    pivot_snap_distance: FloatProperty(
        name="Pivot Distance", unit="LENGTH", min=-1,
        get=default_get("pivot_snap_distance", -1.0), set=default_set("pivot_snap_distance"),
    )


def editable(context, obj):
    return context.mode == "OBJECT" and obj.is_editable and not _progress.active()


class TMBRIDGE_OT_AssetPreviewAction(bpy.types.Operator):
    """Source actions for the active owning asset"""

    bl_idname = "trackmania_blender_bridge.asset_preview_action"
    bl_label = "Asset Action"

    action: EnumProperty(items=[
        ("COPY", "Copy Source Path", ""),
        ("OPEN", "Open Source Folder", ""),
        ("ERROR", "Show Asset Error", ""),
    ])

    @classmethod
    def poll(cls, context):
        return selection(context) is not None and not _progress.active()

    @classmethod
    def description(cls, context, properties):
        return {"COPY": "Copy source GBX path", "OPEN": "Open source folder",
                "ERROR": "Show asset editing error"}.get(properties.action, "Asset action")

    def execute(self, context):
        obj, _kind, source, _data, _instance = selection(context)
        if self.action == "ERROR":
            message = obj.get("tm_asset_controls_error")
            if not message:
                try:
                    placement(obj)
                except (ValueError, TypeError) as exc:
                    message = str(exc)
                    log.error("Asset %s: %s", obj.name, exc)
            self.report({"ERROR"}, message or "No asset error")
            return {"FINISHED"}
        if not source:
            self.report({"ERROR"}, "Source GBX unavailable")
            return {"CANCELLED"}
        if self.action == "COPY":
            context.window_manager.clipboard = source
            return {"FINISHED"}
        path = bpy.path.abspath(source)
        if not os.path.isfile(path):
            # Embedded resources have virtual paths inside their owning map.
            root = _map_panel.map_root(context.scene)
            if root is not None and obj.get("tm_map_root") == root.get("tm_map_id"):
                path = bpy.path.abspath(root.get("tm_source_path", ""))
        folder = os.path.dirname(path)
        if not path or not os.path.isdir(folder):
            self.report({"ERROR"}, "Source folder unavailable")
            return {"CANCELLED"}
        return bpy.ops.wm.path_open(filepath=folder)


class TMBRIDGE_OT_AssetMakeEditable(bpy.types.Operator):
    """Make only the active owning placement editable, including attached clips"""

    bl_idname = "trackmania_blender_bridge.asset_make_editable"
    bl_label = "Make Asset Editable"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        selected = selection(context)
        return (selected is not None and editable(context, selected[0])
                and selected[0].instance_type == "COLLECTION"
                and selected[0].instance_collection is not None)

    def execute(self, context):
        obj = selection(context)[0]
        previous = [other.name for other in context.selected_objects]
        active = context.active_object.name if context.active_object else None
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        result = {"CANCELLED"}
        try:
            result = bpy.ops.trackmania_blender_bridge.make_blocks_editable()
        except RuntimeError as exc:
            log.exception("Could not make owning asset editable")
            self.report({"ERROR"}, str(exc))
        finally:
            if result != {"FINISHED"}:
                for other in context.selected_objects:
                    other.select_set(False)
                previous_active = context.view_layer.objects.get(active) if active else None
                context.view_layer.objects.active = previous_active
            for name in previous:
                other = context.view_layer.objects.get(name)
                if other is not None:
                    other.select_set(True)
        return result


class TMBRIDGE_OT_AssetPivot(bpy.types.Operator):
    """Edit a placement-local pivot without changing the object origin"""

    bl_idname = "trackmania_blender_bridge.asset_pivot"
    bl_label = "Edit Pivot (Local XYZ)"
    bl_options = {"REGISTER", "UNDO"}

    target: StringProperty(options={"HIDDEN"})
    index: IntProperty(min=0, options={"HIDDEN"})
    position: FloatVectorProperty(name="Position", size=3, subtype="TRANSLATION", unit="LENGTH")

    @classmethod
    def poll(cls, context):
        selected = selection(context)
        return selected is not None and editable(context, selected[0])

    def invoke(self, context, event):
        selected = selection(context)
        self.target = selected[0].name
        try:
            document = placement(selected[0])
            if document is None or self.index >= len(document.get("pivot_positions", [])):
                raise ValueError("Pivot point unavailable")
            self.position = tuple(document["pivot_positions"][self.index][axis] for axis in ("x", "y", "z"))
        except (ValueError, TypeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        selected = selection(context)
        obj = selected[0]
        if self.target and obj.name != self.target:
            self.report({"ERROR"}, "Active asset changed; reopen the pivot editor")
            return {"CANCELLED"}
        try:
            write_default(obj, "pivot_positions", tuple(self.position), self.index)
        except (ValueError, TypeError) as exc:
            failure(obj, exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def _action(layout, icon, action):
    layout.operator(TMBRIDGE_OT_AssetPreviewAction.bl_idname, text="", icon=icon).action = action


def _fields(layout, settings, names):
    col = layout.column(align=True)
    col.use_property_split = True
    col.use_property_decorate = False
    for name in names:
        col.prop(settings, name)


def _metadata(layout, context, keys):
    obj, _kind, _source, data, instance = selection(context)
    chain = []
    child = context.active_object
    while child is not None and child != obj:
        chain.append(child)
        child = child.parent
    shown = set()
    for key, label in _asset_panel._FIELDS:
        if key not in keys or label in shown:
            continue
        result = value(obj, key)
        if result is None:
            result = next((child[key] for child in chain if key in child), None)
        if result is not None:
            _map_panel._row(layout, label, _asset_panel._display(result))
            shown.add(label)
    # Older standalone imports use RNA placement/data groups instead of stamps.
    fallback = {
        "tm_block_coord": ("coord", "Grid Position"),
        "tm_block_dir": ("direction", "Direction"),
        "tm_block_is_ground": ("is_ground", "Ground"),
        "tm_block_is_free": ("is_free", "Free Placement"),
        "tm_block_is_ghost": ("is_ghost", "Ghost"),
        "tm_block_variant_index": ("block_variant_index", "Variant"),
        "tm_block_mobil_index": ("mobil_index", "Mobil"),
        "tm_block_mobil_variant_index": ("mobil_variant_index", "Mobil Variant"),
        "tm_block_color": ("color", "Color"),
        "tm_item_variant_index": ("variant_index", "Variant"),
        "tm_item_color": ("color", "Color"),
        "tm_item_type": ("item_type", "Type"),
        "tm_item_page": ("page_path", "Page"),
        "tm_prefab_version": ("prefab_version", "Version"),
        "tm_prefab_url": ("url", "URL"),
    }
    for key, (attribute, label) in fallback.items():
        if key not in keys or label in shown:
            continue
        for group in (instance, data):
            if group is not None and hasattr(group, attribute):
                _map_panel._row(layout, label, _asset_panel._display(getattr(group, attribute)))
                shown.add(label)
                break


class TMBRIDGE_PT_AssetPreview(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_asset_preview"
    bl_label = ""
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_order = 61

    @classmethod
    def poll(cls, context):
        return selection(context) is not None

    def draw_header(self, context):
        obj, kind, _source, _data, _instance = selection(context)
        self.layout.label(
            text=f"Active: {obj.tm_bridge_asset_controls.asset_name}",
            icon=next((icon for _key, label, icon in KINDS if label == kind), "OBJECT_DATA"),
        )

    def draw_header_preset(self, context):
        obj = selection(context)[0]
        if obj.get("tm_asset_controls_error"):
            _action(self.layout, "ERROR", "ERROR")
        row = self.layout.row(align=True)
        row.enabled = editable(context, obj)
        if obj.instance_type == "COLLECTION":
            row.operator(TMBRIDGE_OT_AssetMakeEditable.bl_idname, text="", icon="MESH_DATA")

    def draw(self, context):
        obj, kind, _source, _data, _instance = selection(context)
        col = self.layout.column()
        col.enabled = editable(context, obj)
        if kind == "Item":
            for key, name in (("tm_item_author", "author"), ("tm_item_type", "item_type")):
                if value(obj, key) is not None:
                    _fields(col, obj.tm_bridge_asset_controls, (name,))
        if obj.get("tm_placeholder"):
            self.layout.label(text="Geometry unavailable", icon="ERROR")


class TMBRIDGE_PT_AssetPreviewPlacement(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_asset_preview_placement"
    bl_label = "Placement"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_AssetPreview.bl_idname
    bl_order = 0

    def draw(self, context):
        obj = selection(context)[0]
        settings = obj.tm_bridge_asset_controls
        row = self.layout.row(align=True)
        row.enabled = False
        for key, attribute, prop in (
            ("tm_block_is_ground", "is_ground", "placement_ground"),
            ("tm_block_is_free", "is_free", "placement_free"),
            ("tm_block_is_ghost", "is_ghost", "placement_ghost"),
        ):
            if placement_value(obj, key, attribute) is not None:
                row.prop(settings, prop)
        direction = placement_value(obj, "tm_block_dir", "direction")
        if direction is not None:
            row = self.layout.row(align=True)
            row.enabled = False
            row.label(text="Direction")
            icon = {
                "NORTH": "TRIA_UP", "EAST": "TRIA_RIGHT",
                "SOUTH": "TRIA_DOWN", "WEST": "TRIA_LEFT",
            }.get(str(direction), "ORIENTATION_GLOBAL")
            row.operator(TMBRIDGE_OT_AssetPreviewAction.bl_idname, text=str(direction).title(), icon=icon)
        if placement_value(obj, "tm_block_coord", "coord") is not None:
            row = self.layout.row(align=True)
            row.enabled = False
            row.label(text="Grid")
            for index, axis in enumerate("XYZ"):
                row.prop(settings, "placement_grid", index=index, text=axis, slider=True)
        _metadata(self.layout, context, {"tm_clip_face"})
        col = self.layout.column()
        col.enabled = editable(context, obj)
        col.label(text="Local Transform", icon="ORIENTATION_LOCAL")
        rotation = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}.get(obj.rotation_mode, "rotation_euler")
        for prop, label in (("location", "Location"), (rotation, "Rotation"), ("scale", "Scale")):
            row = col.row(align=True)
            row.label(text=label)
            axes = "WXYZ" if prop == "rotation_quaternion" else "AXYZ" if prop == "rotation_axis_angle" else "XYZ"
            for index, axis in enumerate(axes):
                row.prop(obj, prop, index=index, text=axis)


class TMBRIDGE_PT_AssetPreviewVariants(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_asset_preview_variants"
    bl_label = "Variants"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_AssetPreview.bl_idname
    bl_order = 1

    @classmethod
    def poll(cls, context):
        selected = selection(context)
        return selected is not None and selected[1] in {"Block", "Item"}

    def draw(self, context):
        _metadata(self.layout, context, {
            "tm_block_variant_index", "tm_block_mobil_index", "tm_block_mobil_variant_index",
            "tm_block_color", "tm_var_kind", "tm_var_index", "tm_item_variant_index", "tm_item_color",
        })


class TMBRIDGE_PT_AssetPreviewDefaults(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_asset_preview_defaults"
    bl_label = "Item Defaults"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_AssetPreview.bl_idname
    bl_order = 2
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        selected = selection(context)
        return selected is not None and selected[1] == "Item" and value(selected[0], "tm_item_placement") is not None

    def draw(self, context):
        obj = selection(context)[0]
        try:
            document = placement(obj)
        except (ValueError, TypeError):
            row = self.layout.row(align=True)
            row.label(text="Invalid item defaults", icon="ERROR")
            _action(row, "INFO", "ERROR")
            return
        layout = self.layout.column()
        layout.enabled = editable(context, obj)
        for key, _label in DEFAULT_FIELDS:
            if key == "grid_snap_h_step" and any(name in document for name in (
                "grid_snap_h_step", "grid_snap_v_step", "grid_snap_h_offset",
            )):
                layout.label(text="Grid Snap", icon="SNAP_GRID")
            if key in document:
                _fields(layout, obj.tm_bridge_asset_controls, (key,))
        pivots = document.get("pivot_positions", [])
        row = layout.row()
        row.label(text="Pivot Points", icon="PIVOT_CURSOR")
        row.label(text=str(len(pivots)))
        layout.label(text="Local XYZ (m)")
        header = layout.row(align=True)
        for label in ("#", "X", "Y", "Z", ""):
            header.label(text=label)
        for index, pivot in enumerate(pivots):
            row = layout.row(align=True)
            row.label(text=str(index + 1))
            for axis in ("x", "y", "z"):
                row.label(text=f"{pivot[axis]:.3f}")
            row.operator(TMBRIDGE_OT_AssetPivot.bl_idname, text="", icon="GREASEPENCIL").index = index


class TMBRIDGE_PT_AssetPreviewDetails(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_asset_preview_details"
    bl_label = "Details"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_AssetPreview.bl_idname
    bl_order = 3
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        obj, _kind, source, _data, _instance = selection(context)
        row = self.layout.row(align=True)
        row.label(text=source or "-", icon="FILE")
        actions = row.row(align=True)
        actions.enabled = bool(source) and not _progress.active()
        _action(actions, "COPYDOWN", "COPY")
        _action(actions, "FILE_FOLDER", "OPEN")
        identifier = obj.get("tm_block_name") or obj.get("tm_item_model_id") or value(obj, "tm_asset_id")
        if identifier is not None:
            _map_panel._row(self.layout, "ID", identifier)
        _metadata(self.layout, context, {
            key for key, _label in _asset_panel._FIELDS
            if key.startswith(("tm_prefab_", "tm_ed_", "tm_lod_")) or key in {
                "tm_item_collection", "tm_item_page", "tm_item_animated", "tm_item_is_ingame",
                "tm_item_is_custom", "tm_block_is_baked",
            }
        })


CLASSES = (
    TMBRIDGE_AssetPreviewSettings,
    TMBRIDGE_OT_AssetPreviewAction,
    TMBRIDGE_OT_AssetMakeEditable,
    TMBRIDGE_OT_AssetPivot,
    TMBRIDGE_PT_AssetPreview,
    TMBRIDGE_PT_AssetPreviewPlacement,
    TMBRIDGE_PT_AssetPreviewVariants,
    TMBRIDGE_PT_AssetPreviewDefaults,
    TMBRIDGE_PT_AssetPreviewDetails,
)


def register_scene_props() -> None:
    bpy.types.Object.tm_bridge_asset_controls = PointerProperty(type=TMBRIDGE_AssetPreviewSettings)


def unregister_scene_props() -> None:
    del bpy.types.Object.tm_bridge_asset_controls
