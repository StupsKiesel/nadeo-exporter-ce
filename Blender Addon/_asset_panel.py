"""Read-only, selection-driven metadata for imported GBX assets."""

import math
import os
import json
from dataclasses import asdict

import bpy

from . import ADDON_SHORT_NAME
from . import gbx_props
from ._map_panel import _row


_KINDS = {
    "ED_CLASSIC": "Block", "BLOCK": "Block", "ED_CLIP": "Clip",
    "ED_HORIZONTAL_CLIP": "Clip", "ED_VERTICAL_CLIP": "Clip",
    "ED_FRONTIER": "Clip", "ED_FLAT": "Clip", "ED_TRANSITION": "Clip",
    "ITEM": "Item", "PREFAB": "Prefab", "VEGET_TREE_MODEL": "Vegetation",
    "STATIC_OBJECT": "Mesh", "DYNA_OBJECT": "Mesh",
}

_FIELDS = (
    ("tm_block_is_ground", "Ground"),
    ("tm_block_is_free", "Free Placement"),
    ("tm_block_coord", "Grid Position"),
    ("tm_block_dir", "Direction"),
    ("tm_block_variant_index", "Variant"),
    ("tm_block_mobil_index", "Mobil"),
    ("tm_block_mobil_variant_index", "Mobil Variant"),
    ("tm_block_is_ghost", "Ghost"),
    ("tm_block_is_baked", "Baked"),
    ("tm_block_color", "Color"),
    ("tm_var_kind", "Ground / Air"),
    ("tm_var_index", "Variant"),
    ("tm_clip_face", "Face"),
    ("tm_item_author", "Author"),
    ("tm_item_collection", "Collection"),
    ("tm_item_variant_index", "Variant"),
    ("tm_item_color", "Color"),
    ("tm_item_type", "Type"),
    ("tm_item_page", "Page"),
    ("tm_item_animated", "Animated"),
    ("tm_item_is_ingame", "In-game"),
    ("tm_item_is_custom", "Custom"),
    ("tm_prefab_version", "Version"),
    ("tm_prefab_entity_count", "Entities"),
    ("tm_prefab_url", "URL"),
    ("tm_ed_page", "Page"),
    ("tm_ed_total_ground", "Ground Variants"),
    ("tm_ed_total_air", "Air Variants"),
    ("tm_ed_clip_type", "Clip Type"),
    ("tm_lod_level", "LOD"),
)


def stamp(owner, kind: str, source: str, name: str = "") -> None:
    owner["tm_asset_type"] = kind
    owner["tm_source_path"] = source
    owner["tm_asset_name"] = name or os.path.basename(source.replace("\\", "/")) or owner.name


def stamp_item_placement(owner, placement) -> None:
    owner["tm_item_placement"] = json.dumps(asdict(placement))


def item_placement(context):
    selected = selected_asset(context)
    if selected is None or selected[1] != "Item":
        return None
    obj = selected[0]
    collection = obj.instance_collection if obj.instance_type == "COLLECTION" else None
    raw = obj.get("tm_item_placement")
    if raw is None and collection is not None:
        raw = collection.get("tm_item_placement")
    if raw is None:
        for owner in obj.users_collection:
            if owner.get("tm_asset_type") == "Item":
                raw = owner.get("tm_item_placement")
                break
    return json.loads(raw) if raw is not None else None


def _kind(owner) -> str:
    explicit = owner.get("tm_asset_type", "")
    if explicit:
        return explicit
    if "tm_block_name" in owner or owner.get("tm_ed_kind") == "block":
        return "Block"
    if "tm_item_model_id" in owner or "tm_item_type" in owner:
        return "Item"
    if "tm_clip_source" in owner or owner.get("tm_ed_kind") == "clip":
        return "Clip"
    if "tm_prefab_version" in owner:
        return "Prefab"
    return _KINDS.get(getattr(owner, "gbx_kind", "NONE"), "")


def selected_asset(context):
    """Resolve children to the outer owning placement, not a nested definition."""
    obj = context.active_object
    found = None
    while obj is not None:
        kind = _kind(obj)
        collection = obj.instance_collection if obj.instance_type == "COLLECTION" else None
        if not kind and collection is not None:
            kind = _kind(collection)
        if collection is None:
            owner_collection = next((owner for owner in obj.users_collection
                               if owner.get("tm_asset_type") == "Item"), None)
            if owner_collection is not None:
                collection = owner_collection
                kind = "Item"
        if kind:
            found = (obj, kind, collection)
        obj = obj.parent
    if found is None:
        return None
    obj, kind, collection = found
    instance = gbx_props.active_instance(obj)
    if collection is None and instance is not None:
        collection = instance.data_ref
    data = gbx_props.active_data(collection)
    source = obj.get("tm_source_path", "") or obj.get("tm_clip_source", "")
    if not source and collection is not None:
        source = collection.get("tm_source_path", "")
    if not source and data is not None:
        source = data.source_file_path
    return obj, kind, source, data, instance


def _display(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if hasattr(value, "to_list"):
        return ", ".join(str(v) for v in value.to_list())
    if not isinstance(value, (str, int, float)) and hasattr(value, "__iter__"):
        return ", ".join(str(v) for v in value)
    return value


class TMBRIDGE_PT_Asset(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_asset"
    bl_label = "Selected Asset"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_order = 60

    @classmethod
    def poll(cls, context):
        return selected_asset(context) is not None

    def draw(self, context):
        selected = selected_asset(context)
        if selected is None:
            return
        obj, kind, source, data, instance = selected
        layout = self.layout
        layout.label(text=kind, icon="INFO")
        name = (obj.get("tm_block_name") or obj.get("tm_item_model_id")
                or obj.get("tm_asset_name") or obj.name)
        _row(layout, "Name", name)
        _row(layout, "Source GBX", source)
        owners = [obj]
        # A selected variant contributes its own metadata without replacing
        # the outer block's identity.
        child = context.active_object
        while child is not None and child != obj:
            owners.append(child)
            child = child.parent
        shown = set()
        for owner in owners:
            for key, label in _FIELDS:
                if key in owner and label not in shown:
                    _row(layout, label, _display(owner[key]))
                    shown.add(label)
        for group in (data, instance):
            if group is None:
                continue
            for key, label in (
                ("coord", "Grid Position"), ("direction", "Direction"),
                ("is_ground", "Ground"), ("is_free", "Free Placement"),
                ("block_variant_index", "Variant"), ("item_type", "Type"),
                ("page_path", "Page"), ("prefab_version", "Version"),
            ):
                if label not in shown and hasattr(group, key):
                    _row(layout, label, _display(getattr(group, key)))
                    shown.add(label)
        if "tm_block_is_free" in obj or kind == "Item":
            _row(layout, "Position (Blender)", ", ".join(f"{v:.3f}" for v in obj.matrix_world.translation))
            _row(layout, "Rotation (Blender)", ", ".join(
                f"{math.degrees(v):.1f}" for v in obj.matrix_world.to_euler()
            ))
        if obj.get("tm_placeholder"):
            layout.label(text="Geometry unavailable; see import warnings", icon="ERROR")
        if obj.instance_type == "COLLECTION":
            layout.operator("trackmania_blender_bridge.make_blocks_editable",
                            text="Make Editable", icon="MESH_DATA")


class TMBRIDGE_PT_ItemPlacement(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_item_placement"
    bl_label = "Placement"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_Asset.bl_idname
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return item_placement(context) is not None

    def draw(self, context):
        placement = item_placement(context)
        if placement is None:
            return
        layout = self.layout
        layout.label(text="Item defaults (not current transform)")
        for key, label in (
            ("ghost_mode", "Ghost Mode"), ("cube_size", "Cube Size"),
            ("grid_snap_h_step", "Grid Horizontal Step"),
            ("grid_snap_v_step", "Grid Vertical Step"),
            ("grid_snap_h_offset", "Grid Horizontal Offset"),
            ("fly_step", "Fly Step"), ("pivot_snap_distance", "Pivot Snap Distance"),
        ):
            _row(layout, label, _display(placement[key]))
        pivots = placement["pivot_positions"]
        _row(layout, "Pivots", len(pivots))
        layout.label(text="Local coordinates (Blender, metres)")
        for index, pivot in enumerate(pivots, 1):
            _row(layout, f"Pivot {index}", ", ".join(f"{pivot[axis]:.3f}" for axis in ("x", "y", "z")))
        if not pivots:
            layout.label(text="No explicit pivots stored")


CLASSES = (TMBRIDGE_PT_Asset, TMBRIDGE_PT_ItemPlacement)


def register_scene_props():
    bpy.types.Scene.tm_imported_material = bpy.props.PointerProperty(type=bpy.types.Material)


def unregister_scene_props():
    if hasattr(bpy.types.Scene, "tm_imported_material"):
        del bpy.types.Scene.tm_imported_material
