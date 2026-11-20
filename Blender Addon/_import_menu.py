"""``File > Import > Nadeo Exporter CE`` submenu.

V4 is the sole map importer. All standalone asset entries are always listed.
"""

from __future__ import annotations

import os

import bpy
from . import ADDON_SHORT_NAME

OPERATOR_PREFIX = "trackmania_blender_bridge."

_ICON_FILE = os.path.join(os.path.dirname(__file__), "icons", "nadeo_ce.png")
_preview_collection = None
_item_icon = (
    "GEOMETRY_SET"
    if "GEOMETRY_SET" in bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
    else "OUTLINER_DATA_MESH"
)


def get_submenu_icon_id() -> int:
    """Return the custom NE/CE submenu icon id (0 if unavailable)."""
    global _preview_collection
    if _preview_collection is None:
        try:
            import bpy.utils.previews
            pcoll = bpy.utils.previews.new()
            pcoll.load("nadeo_ce", _ICON_FILE, "IMAGE")
            _preview_collection = pcoll
        except Exception:  # noqa: BLE001 - menu must draw even without the icon
            return 0
    return _preview_collection["nadeo_ce"].icon_id


def unregister_icons() -> None:
    global _preview_collection
    if _preview_collection is not None:
        try:
            import bpy.utils.previews
            bpy.utils.previews.remove(_preview_collection)
        except Exception:  # noqa: BLE001
            pass
        _preview_collection = None

# (operator idname suffix, menu text, icon)
RELEASE_ENTRIES = (
    ("import_map_v4", "Map (.Map.Gbx)", "FILE_VOLUME"),
    ("import_replay", "Replay (.Replay.Gbx)", "ANIM"),
    ("import_item", "Item / Crystal (.Gbx)", _item_icon),
    ("import_prefab", "Prefab (.Prefab.Gbx)", "NODE_COMPOSITING"),
    ("import_mesh", "Mesh (.Mesh.Gbx)", "OUTLINER_DATA_MESH"),
    ("import_veget_tree", "Vegetation Tree (.VegetTreeModel.Gbx)", "MOD_DATA_TRANSFER"),
    ("import_ed", "Block/Clip (.ED*.Gbx)", "MOD_EXPLODE"),
    ("import_material", "Material (.Material.Gbx)", "SHADING_RENDERED"),
    ("import_media_clip", "MediaTracker Clip (.Clip.Gbx)", "NLA"),
)

class TMBRIDGE_MT_Import(bpy.types.Menu):
    bl_idname = "TMBRIDGE_MT_import"
    bl_label = ADDON_SHORT_NAME

    def draw(self, _context):
        layout = self.layout
        layout.operator_context = "INVOKE_DEFAULT"
        for suffix, text, icon in RELEASE_ENTRIES:
            layout.operator(OPERATOR_PREFIX + suffix, text=text, icon=icon)


def menu_func_import(self, _context) -> None:
    self.layout.menu(TMBRIDGE_MT_Import.bl_idname, icon_value=get_submenu_icon_id())


CLASSES = (TMBRIDGE_MT_Import,)
