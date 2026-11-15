"""Opt-in developer sidebar panels.

The user-facing Tools panel is registered separately. Developer panels follow
``developer_ui`` (``_sync_developer_ui`` in ``__init__``). Operators are
referenced by idname to avoid circular imports.
"""

from __future__ import annotations

import bpy
from . import ADDON_SHORT_NAME

CATEGORY = ADDON_SHORT_NAME
OP = "trackmania_blender_bridge."


class _DevPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = "TMBRIDGE_PT_developer"
    bl_options = {"DEFAULT_CLOSED"}


class TMBRIDGE_PT_Developer(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_developer"
    bl_label = "Developer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_order = 100

    def draw(self, _context):
        pass


def _addon():
    from . import ADDON_ID

    return bpy.context.preferences.addons.get(ADDON_ID)


class TMBRIDGE_PT_DevPreferences(_DevPanel, bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_dev_preferences"
    bl_label = "Paths"
    bl_order = 0

    def draw(self, _context):
        from . import _draw_paths

        addon = _addon()
        if addon is None:
            self.layout.label(text="Addon preferences not available", icon="ERROR")
            return
        _draw_paths(self.layout, addon.preferences)
        self.layout.prop(addon.preferences, "developer_ui")


class TMBRIDGE_PT_DevQuickActions(_DevPanel, bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_dev_quick_actions"
    bl_label = "Quick Actions"
    bl_order = 40

    def draw(self, _context):
        col = self.layout.column(align=True)
        row = col.row(align=True)
        row.operator(OP + "import_map_v4", text="Import Map", icon="IMPORT")
        row.operator(OP + "render_from_view", text="Render View", icon="RENDER_STILL")
        addon = _addon()
        if addon is not None and addon.preferences.developer_ui:
            row = col.row(align=True)
            row.operator(OP + "clean_blend_file", text="Clear Blend", icon="TRASH")
            if hasattr(bpy.ops.wm, "console_toggle"):
                row.operator("wm.console_toggle", text="Console", icon="CONSOLE")
            from . import _openplanet_bridge

            box = col.box()
            box.label(text="Openplanet Developer Tools", icon="LINKED")
            _openplanet_bridge.draw_tools(box, addon.preferences)


class TMBRIDGE_PT_DevMediaTracker(_DevPanel, bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_dev_mediatracker"
    bl_label = "MediaTracker"
    bl_order = 45

    def draw(self, context):
        from . import _mediatracker_ui

        opened = _mediatracker_ui.is_open(context)
        self.layout.operator(
            OP + "toggle_mediatracker_gpu",
            text="Close GPU Editor" if opened else "Open GPU Editor",
            icon="TIME", depress=opened,
        )
        self.layout.label(text="Experimental | blend-file edits only")


class TMBRIDGE_PT_DevUpgrade(_DevPanel, bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_dev_upgrade"
    bl_label = "Upgrade"
    bl_order = 20
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, _context):
        from . import _tools_panel

        _tools_panel.draw_build_upgrade_button(self.layout)

    def draw(self, _context):
        from . import _draw_upgrade

        addon = _addon()
        if addon is None:
            self.layout.label(text="Addon preferences not available", icon="ERROR")
            return
        _draw_upgrade(self.layout.column(align=True), addon.preferences)


class TMBRIDGE_PT_DevImport(_DevPanel, bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_dev_import"
    bl_label = "Import"
    bl_parent_id = "TMBRIDGE_PT_dev_stats"
    bl_order = 1
    bl_options = {"DEFAULT_CLOSED"}

    ENTRIES = (
        ("import_map_v4", "Map", "FILE"),
        ("import_replay", "Replay (Motion Preview)", "ANIM"),
        ("import_material", "Material", "MATERIAL"),
        ("import_mesh", "Mesh", "MESH_DATA"),
        ("import_veget_tree", "Vegetation Tree", "MESH_DATA"),
        ("import_item", "Item", "MESH_CUBE"),
        ("import_prefab", "Prefab", "OUTLINER_OB_GROUP_INSTANCE"),
        ("import_ed", "Block/Clip", "MESH_GRID"),
    )

    def draw(self, _context):
        col = self.layout.column(align=True)
        for suffix, text, icon in self.ENTRIES:
            col.operator(OP + suffix, text=text, icon=icon)


class TMBRIDGE_PT_DevStats(_DevPanel, bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_dev_stats"
    bl_label = "Debug"
    bl_order = 60
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column(align=True)
        active = context.active_object
        is_mesh = active is not None and active.type == "MESH"
        in_edit = is_mesh and active.mode == "EDIT"
        col.label(
            text=f"Active: {active.name}" if active else "Active: (none)",
            icon="OBJECT_DATA" if active else "BLANK1",
        )
        for suffix, icon, enabled in (
            ("print_active_object_stats", "COPYDOWN", active is not None),
            ("print_active_object_vertex_data", "VERTEXSEL", is_mesh),
            ("print_selected_verts_stats", "VERTEXSEL", in_edit),
            ("distance_from_origin", "DRIVER_DISTANCE", in_edit),
            ("print_outliner_tree", "OUTLINER", True),
        ):
            row = col.row()
            row.enabled = enabled
            row.operator(OP + suffix, text={
                "print_active_object_stats": "Print Active Stats",
                "print_active_object_vertex_data": "Print Vertex Data",
                "print_selected_verts_stats": "Print Selected Vertices",
                "distance_from_origin": "Distance from Origin",
                "print_outliner_tree": "Print Outliner Tree",
            }[suffix], icon=icon)


PUBLIC_PANELS = ()


def panels(gbxmat_panel) -> tuple:
    """Developer registration order: parent panels before their children."""
    return (
        TMBRIDGE_PT_Developer,
        TMBRIDGE_PT_DevPreferences,
        TMBRIDGE_PT_DevUpgrade,
        TMBRIDGE_PT_DevQuickActions,
        TMBRIDGE_PT_DevMediaTracker,
        TMBRIDGE_PT_DevStats,
        TMBRIDGE_PT_DevImport,
        gbxmat_panel,
    )
