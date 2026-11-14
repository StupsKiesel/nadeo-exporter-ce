"""GBX Inspector — hijacks the Blender Preferences window to show parsed GBX data.

Usage:
    bpy.ops.trackmania_blender_bridge.inspector_open()

The operator opens the Preferences window, unregisters the default prefs UI,
and draws a custom navbar + treeview layout. The red Exit button restores
everything.

Ported from the legacy addon (Legacy/Blender Addon/gbx_inspector.py).
"""

from __future__ import annotations

import bpy
import os
import logging
from typing import Any

log = logging.getLogger("trackmania_blender_bridge.inspector")

# ---------------------------------------------------------------------------
#  Module state
# ---------------------------------------------------------------------------

_inspector_active: bool = False

# References to the original classes we unregistered so we can re-register
# them on exit. Populated in _hijack_preferences().
_saved_header_classes: list[type] = []
_saved_panel_classes: list[type] = []

# Per-tab Python-side state (keyed by tab index)
_tab_parsed: dict[int, Any] = {}       # tab_index -> parsed root
_tab_registry: dict[int, dict] = {}    # tab_index -> {uid: obj}
_tab_next_id: dict[int, int] = {}      # tab_index -> next uid counter


# ---------------------------------------------------------------------------
#  PropertyGroups (treeview rows)
# ---------------------------------------------------------------------------

class GBX_InspectorRow(bpy.types.PropertyGroup):
    label: bpy.props.StringProperty(name="Label", default="")
    value_str: bpy.props.StringProperty(name="Value", default="")
    depth: bpy.props.IntProperty(name="Depth", default=0)
    is_expanded: bpy.props.BoolProperty(name="Expanded", default=False)
    is_expandable: bpy.props.BoolProperty(name="Expandable", default=False)
    unique_id: bpy.props.IntProperty(name="ID", default=0)
    node_path: bpy.props.StringProperty(name="Path", default="")
    visible: bpy.props.BoolProperty(name="Visible", default=True)
    type_hint: bpy.props.StringProperty(name="Type", default="")


class GBX_InspectorTab(bpy.types.PropertyGroup):
    filename: bpy.props.StringProperty(name="File", default="")
    rows: bpy.props.CollectionProperty(type=GBX_InspectorRow)
    is_collapsed: bpy.props.BoolProperty(name="Collapsed", default=False)


class GBX_InspectorState(bpy.types.PropertyGroup):
    tabs: bpy.props.CollectionProperty(type=GBX_InspectorTab)
    active_tab: bpy.props.IntProperty(name="Active Tab", default=0)
    filepath: bpy.props.StringProperty(name="File", default="")


# ---------------------------------------------------------------------------
#  GBX parsing helpers
# ---------------------------------------------------------------------------

def _parse_gbx_file(filepath: str) -> Any:
    """Parse a .Gbx file using the NICE parser and return the raw Container.

    Relies on the addon's top-level sys.path setup
    (_ensure_bundled_packages_on_path in __init__.py) so NICE is importable.
    """
    from NICE.src.parser import parse_file
    return parse_file(filepath, recursive=True)


def _is_container(obj: Any) -> bool:
    return hasattr(obj, "items") and callable(getattr(obj, "items"))


def _is_list_like(obj: Any) -> bool:
    return isinstance(obj, (list, tuple)) and not isinstance(obj, (str, bytes, bytearray))


def _child_count(obj: Any) -> int:
    if _is_container(obj):
        return sum(1 for k in obj.keys() if not str(k).startswith("_"))
    if _is_list_like(obj):
        return len(obj)
    return 0


def _format_value(obj: Any) -> str:
    if obj is None:
        return "None"
    if isinstance(obj, bool):
        return str(obj)
    if isinstance(obj, int):
        if abs(obj) > 0xFFFF:
            return f"{obj} (0x{obj & 0xFFFFFFFF:08X})"
        return str(obj)
    if isinstance(obj, float):
        return f"{obj:.6g}"
    if isinstance(obj, bytes):
        if len(obj) <= 32:
            return obj.hex()
        return f"bytes[{len(obj)}] {obj[:32].hex()}..."
    if isinstance(obj, str):
        if len(obj) > 120:
            return repr(obj[:120]) + "..."
        return repr(obj)
    type_name = type(obj).__name__
    if "EnumInteger" in type_name:
        return str(obj)
    return str(obj)


def _type_hint(obj: Any) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "bool_true" if obj else "bool_false"
    if isinstance(obj, int):
        return "int"
    if isinstance(obj, float):
        return "float"
    if isinstance(obj, str):
        return "str"
    if isinstance(obj, bytes):
        return f"bytes[{len(obj)}]"
    if _is_container(obj):
        return "object"
    if _is_list_like(obj):
        return f"array[{len(obj)}]"
    return type(obj).__name__


# ---------------------------------------------------------------------------
#  Lazy-expansion row builder
# ---------------------------------------------------------------------------

def _new_id(tab_idx: int) -> int:
    _tab_next_id[tab_idx] = _tab_next_id.get(tab_idx, 0) + 1
    return _tab_next_id[tab_idx]


def _reset_registry(tab_idx: int | None = None) -> None:
    if tab_idx is not None:
        _tab_registry.pop(tab_idx, None)
        _tab_next_id.pop(tab_idx, None)
    else:
        _tab_registry.clear()
        _tab_next_id.clear()


def _reg(tab_idx: int) -> dict:
    if tab_idx not in _tab_registry:
        _tab_registry[tab_idx] = {}
    return _tab_registry[tab_idx]


def _build_top_level_rows(tab: Any, tab_idx: int, parsed: Any) -> None:
    tab.rows.clear()
    _reset_registry(tab_idx)
    if parsed is None:
        return
    _add_children_to_rows(tab, tab_idx, parsed, depth=0, parent_path="")


def _add_children_to_rows(
    tab: Any,
    tab_idx: int,
    obj: Any,
    depth: int,
    insert_after: int = -1,
    parent_path: str = "",
) -> int:
    added = 0
    entries: list[tuple[str, Any]] = []

    if _is_container(obj):
        for k, v in obj.items():
            ks = str(k)
            if ks.startswith("_"):
                continue
            entries.append((ks, v))
    elif _is_list_like(obj):
        for i, v in enumerate(obj):
            entries.append((f"[{i}]", v))
    else:
        return 0

    for label, value in entries:
        uid = _new_id(tab_idx)
        _reg(tab_idx)[uid] = value

        expandable = _child_count(value) > 0
        val_str = "" if expandable else _format_value(value)
        th = _type_hint(value)

        is_numeric = False
        try:
            int(label, 0)
            is_numeric = True
        except (ValueError, TypeError):
            pass

        if label.startswith("["):
            path = f"{parent_path}{label}"
        elif is_numeric:
            hex_label = label
            try:
                num = int(label, 0)
                if num > 0xFFFF:
                    hex_label = f"0x{num:08X}"
            except (ValueError, TypeError):
                pass
            path = f"{parent_path}[{hex_label}]"
        elif parent_path:
            path = f"{parent_path}.{label}"
        else:
            path = label

        if insert_after >= 0:
            idx = insert_after + added + 1
            row = _insert_row_at(tab, idx)
        else:
            row = tab.rows.add()

        display_label = label
        if label.isdigit() or label.startswith(("0x", "0X")):
            try:
                num = int(label, 0)
                if num > 0xFFFF:
                    display_label = f"0x{num:08X}"
            except ValueError:
                pass

        row.label = display_label
        row.value_str = val_str
        row.depth = depth
        row.is_expanded = False
        row.is_expandable = expandable
        row.unique_id = uid
        row.visible = True
        row.type_hint = th
        row.node_path = path
        added += 1

    return added


def _insert_row_at(tab: Any, index: int) -> Any:
    row = tab.rows.add()
    last = len(tab.rows) - 1
    if last != index:
        tab.rows.move(last, index)
    return tab.rows[index]


def _toggle_expand(tab: Any, tab_idx: int, row_index: int) -> None:
    if row_index < 0 or row_index >= len(tab.rows):
        return
    row = tab.rows[row_index]
    if not row.is_expandable:
        return
    if row.is_expanded:
        _collapse_children(tab, tab_idx, row_index)
        row.is_expanded = False
    else:
        obj = _reg(tab_idx).get(row.unique_id)
        if obj is not None:
            _add_children_to_rows(
                tab, tab_idx, obj,
                depth=row.depth + 1,
                insert_after=row_index,
                parent_path=row.node_path,
            )
        row.is_expanded = True


def _collapse_children(tab: Any, tab_idx: int, parent_index: int) -> None:
    parent_depth = tab.rows[parent_index].depth
    start = parent_index + 1
    end = start
    registry = _reg(tab_idx)
    while end < len(tab.rows) and tab.rows[end].depth > parent_depth:
        uid = tab.rows[end].unique_id
        registry.pop(uid, None)
        end += 1
    for i in range(end - 1, start - 1, -1):
        tab.rows.remove(i)


def _rekey_tabs(removed_idx: int) -> None:
    new_parsed: dict[int, Any] = {}
    new_registry: dict[int, dict] = {}
    new_next_id: dict[int, int] = {}
    for old_idx in sorted(_tab_parsed.keys()):
        if old_idx < removed_idx:
            new_parsed[old_idx] = _tab_parsed[old_idx]
        elif old_idx > removed_idx:
            new_parsed[old_idx - 1] = _tab_parsed[old_idx]
    for old_idx in sorted(_tab_registry.keys()):
        if old_idx < removed_idx:
            new_registry[old_idx] = _tab_registry[old_idx]
            new_next_id[old_idx] = _tab_next_id.get(old_idx, 0)
        elif old_idx > removed_idx:
            new_registry[old_idx - 1] = _tab_registry[old_idx]
            new_next_id[old_idx - 1] = _tab_next_id.get(old_idx, 0)
    _tab_parsed.clear(); _tab_parsed.update(new_parsed)
    _tab_registry.clear(); _tab_registry.update(new_registry)
    _tab_next_id.clear(); _tab_next_id.update(new_next_id)


# ---------------------------------------------------------------------------
#  Operators
# ---------------------------------------------------------------------------

class TMBRIDGE_OT_InspectorOpen(bpy.types.Operator):
    """Open the GBX Inspector window (hijacks Preferences)"""
    bl_idname = "trackmania_blender_bridge.inspector_open"
    bl_label = "GBX Inspector"

    def execute(self, context):
        global _inspector_active
        if _inspector_active:
            self.report({"INFO"}, "Inspector already open")
            return {"CANCELLED"}

        bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
        bpy.app.timers.register(_deferred_hijack, first_interval=0.05)
        return {"FINISHED"}


class TMBRIDGE_OT_InspectorExit(bpy.types.Operator):
    """Close the GBX Inspector and restore Blender Preferences"""
    bl_idname = "trackmania_blender_bridge.inspector_exit"
    bl_label = "Exit Inspector"

    def execute(self, context):
        _restore_preferences()
        if context.window and len(context.window.screen.areas) == 1:
            bpy.ops.wm.window_close("INVOKE_DEFAULT")
        return {"FINISHED"}


class TMBRIDGE_OT_InspectorOpenFile(bpy.types.Operator):
    """Open a GBX file in the inspector"""
    bl_idname = "trackmania_blender_bridge.inspector_open_file"
    bl_label = "Open GBX"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(
        default="*.Gbx;*.gbx",
        options={"HIDDEN"},
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "No valid file selected")
            return {"CANCELLED"}
        try:
            parsed = _parse_gbx_file(self.filepath)
        except Exception as e:  # noqa: BLE001 - report to user
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, f"Failed to parse: {e}")
            return {"CANCELLED"}

        state = context.window_manager.gbx_inspector
        tab = state.tabs.add()
        tab_idx = len(state.tabs) - 1
        tab.filename = os.path.basename(self.filepath)
        state.active_tab = tab_idx
        state.filepath = tab.filename

        _tab_parsed[tab_idx] = parsed
        _build_top_level_rows(tab, tab_idx, parsed)

        for area in context.screen.areas:
            area.tag_redraw()

        self.report({"INFO"}, f"Loaded {os.path.basename(self.filepath)}")
        return {"FINISHED"}


class TMBRIDGE_OT_InspectorToggleRow(bpy.types.Operator):
    """Expand or collapse a row in the inspector treeview"""
    bl_idname = "trackmania_blender_bridge.inspector_toggle_row"
    bl_label = "Toggle Row"
    bl_options = {"INTERNAL"}

    row_index: bpy.props.IntProperty()
    tab_index: bpy.props.IntProperty()

    def execute(self, context):
        state = context.window_manager.gbx_inspector
        tab_idx = self.tab_index
        if tab_idx < 0 or tab_idx >= len(state.tabs):
            return {"CANCELLED"}
        tab = state.tabs[tab_idx]
        _toggle_expand(tab, tab_idx, self.row_index)
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class TMBRIDGE_OT_InspectorCopyPath(bpy.types.Operator):
    """Copy the data path of this row to the clipboard"""
    bl_idname = "trackmania_blender_bridge.inspector_copy_path"
    bl_label = "Copy Path"
    bl_options = {"INTERNAL"}

    path: bpy.props.StringProperty()

    def execute(self, context):
        context.window_manager.clipboard = self.path
        self.report({"INFO"}, f"Copied: {self.path}")
        return {"FINISHED"}


class TMBRIDGE_OT_InspectorCloseTab(bpy.types.Operator):
    """Close an inspector tab"""
    bl_idname = "trackmania_blender_bridge.inspector_close_tab"
    bl_label = "Close Tab"
    bl_options = {"INTERNAL"}

    tab_index: bpy.props.IntProperty()

    def execute(self, context):
        state = context.window_manager.gbx_inspector
        idx = self.tab_index
        if idx < 0 or idx >= len(state.tabs):
            return {"CANCELLED"}

        _tab_parsed.pop(idx, None)
        _reset_registry(idx)
        state.tabs.remove(idx)
        _rekey_tabs(idx)

        if len(state.tabs) == 0:
            state.active_tab = 0
            state.filepath = ""
        else:
            if state.active_tab >= len(state.tabs):
                state.active_tab = len(state.tabs) - 1
            state.filepath = state.tabs[state.active_tab].filename

        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  Custom Preferences Header (navbar)
# ---------------------------------------------------------------------------

class GBX_HT_InspectorHeader(bpy.types.Header):
    bl_space_type = "PREFERENCES"

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.scale_x = 1.4
        row.operator(
            TMBRIDGE_OT_InspectorOpenFile.bl_idname,
            text="Open GBX",
            icon="FILEBROWSER",
        )

        layout.separator_spacer()
        layout.label(text="GBX Inspector", icon="VIEWZOOM")
        layout.separator_spacer()

        row = layout.row(align=True)
        row.alert = True
        row.scale_x = 1.6
        row.operator(
            TMBRIDGE_OT_InspectorExit.bl_idname,
            text="EXIT",
            icon="PANEL_CLOSE",
        )


# ---------------------------------------------------------------------------
#  Custom Preferences Panel (treeview)
# ---------------------------------------------------------------------------

_MAX_VISIBLE_ROWS = 500


def _icon_for_type(type_hint: str) -> str:
    if type_hint == "object":
        return "OUTLINER_OB_GROUP_INSTANCE"
    if type_hint.startswith("array"):
        return "THREE_DOTS"
    if type_hint == "int":
        return "DRIVER_TRANSFORM"
    if type_hint == "float":
        return "EMPTY_SINGLE_ARROW"
    if type_hint == "str":
        return "SYNTAX_OFF"
    if type_hint == "bool_true":
        return "CHECKBOX_HLT"
    if type_hint == "bool_false":
        return "CHECKBOX_DEHLT"
    if type_hint.startswith("bytes"):
        return "FILE_BLANK"
    if type_hint == "null":
        return "X"
    return "NONE"


class GBX_PT_InspectorTree(bpy.types.Panel):
    bl_label = "Data Tree"
    bl_space_type = "PREFERENCES"
    bl_region_type = "WINDOW"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        state = context.window_manager.gbx_inspector

        if len(state.tabs) == 0:
            box = layout.box()
            col = box.column()
            col.scale_y = 3.0
            col.label(text="No file loaded.", icon="INFO")
            col.label(text="Click 'Open GBX' to load a .Gbx file.")
            return

        for tab_idx, tab in enumerate(state.tabs):
            file_box = layout.box()
            header_row = file_box.row(align=True)

            header_row.prop(
                tab, "is_collapsed",
                text="",
                icon="TRIA_RIGHT" if tab.is_collapsed else "TRIA_DOWN",
                emboss=False,
            )
            header_row.label(text=tab.filename, icon="FILE")

            sub = header_row.row()
            sub.alignment = "RIGHT"
            sub.active = False
            sub.label(text=f"{len(tab.rows)} entries")

            close_op = header_row.operator(
                TMBRIDGE_OT_InspectorCloseTab.bl_idname,
                text="",
                icon="X",
                emboss=False,
            )
            close_op.tab_index = tab_idx

            if tab.is_collapsed:
                continue

            if len(tab.rows) == 0:
                col = file_box.column()
                col.label(text="Empty file.", icon="INFO")
                continue

            col = file_box.column(align=True)
            visible_count = 0
            for i, row in enumerate(tab.rows):
                if visible_count >= _MAX_VISIBLE_ROWS:
                    remaining = len(tab.rows) - i
                    col.label(text=f"... {remaining} more rows (expand fewer nodes)")
                    break

                main_row = col.row(align=True)

                if row.depth > 0:
                    indent = main_row.row()
                    indent.ui_units_x = row.depth * 0.25
                    indent.label(text="")

                if row.is_expandable:
                    op = main_row.operator(
                        TMBRIDGE_OT_InspectorToggleRow.bl_idname,
                        text="",
                        icon="TRIA_DOWN" if row.is_expanded else "TRIA_RIGHT",
                        emboss=False,
                    )
                    op.row_index = i
                    op.tab_index = tab_idx
                else:
                    sub = main_row.row()
                    sub.ui_units_x = 1.0
                    sub.label(text="", icon="DOT")

                icon = _icon_for_type(row.type_hint)
                label_row = main_row.row()
                label_row.label(text=row.label, icon=icon)

                if row.value_str:
                    val_row = main_row.row()
                    val_row.alignment = "RIGHT"
                    val_row.label(text=row.value_str)
                elif row.is_expandable:
                    val_row = main_row.row()
                    val_row.alignment = "RIGHT"
                    val_row.active = False
                    val_row.label(text=f"({row.type_hint})")

                cp = main_row.operator(
                    TMBRIDGE_OT_InspectorCopyPath.bl_idname,
                    text="",
                    icon="COPYDOWN",
                    emboss=False,
                )
                cp.path = row.node_path

                visible_count += 1


# ---------------------------------------------------------------------------
#  Hijack / Restore preferences
# ---------------------------------------------------------------------------

def _deferred_hijack():
    global _inspector_active
    if _inspector_active:
        return None

    _hijack_preferences()
    _inspector_active = True

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "PREFERENCES":
                space = area.spaces.active
                if hasattr(space, "show_region_header"):
                    space.show_region_header = True
                area.tag_redraw()

    return None


def _hijack_preferences() -> None:
    global _saved_header_classes, _saved_panel_classes
    _saved_header_classes.clear()
    _saved_panel_classes.clear()

    our_classes = {GBX_HT_InspectorHeader, GBX_PT_InspectorTree}

    for cls in list(bpy.types.__dir__()):
        try:
            klass = getattr(bpy.types, cls)
        except Exception:
            continue
        if not isinstance(klass, type):
            continue

        if issubclass(klass, bpy.types.Header) and klass is not bpy.types.Header:
            if (
                getattr(klass, "bl_space_type", "") == "PREFERENCES"
                and klass not in our_classes
            ):
                try:
                    bpy.utils.unregister_class(klass)
                    _saved_header_classes.append(klass)
                except Exception:
                    pass

        if issubclass(klass, bpy.types.Panel) and klass is not bpy.types.Panel:
            if (
                getattr(klass, "bl_space_type", "") == "PREFERENCES"
                and klass not in our_classes
            ):
                try:
                    bpy.utils.unregister_class(klass)
                    _saved_panel_classes.append(klass)
                except Exception:
                    pass

    for cls in our_classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass


def _restore_preferences() -> None:
    global _inspector_active
    if not _inspector_active:
        return

    for cls in (GBX_HT_InspectorHeader, GBX_PT_InspectorTree):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    for cls in _saved_header_classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass

    for cls in _saved_panel_classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass

    _saved_header_classes.clear()
    _saved_panel_classes.clear()
    _inspector_active = False

    _tab_parsed.clear()
    _tab_registry.clear()
    _tab_next_id.clear()

    wm = bpy.context.window_manager
    if hasattr(wm, "gbx_inspector"):
        state = wm.gbx_inspector
        state.tabs.clear()
        state.active_tab = 0
        state.filepath = ""


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

CLASSES = (
    GBX_InspectorRow,
    GBX_InspectorTab,
    GBX_InspectorState,
    TMBRIDGE_OT_InspectorOpen,
    TMBRIDGE_OT_InspectorExit,
    TMBRIDGE_OT_InspectorOpenFile,
    TMBRIDGE_OT_InspectorToggleRow,
    TMBRIDGE_OT_InspectorCopyPath,
    TMBRIDGE_OT_InspectorCloseTab,
    # Header and Panel are registered/unregistered dynamically by hijack.
)


def register() -> None:
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)

    bpy.types.WindowManager.gbx_inspector = bpy.props.PointerProperty(
        type=GBX_InspectorState,
    )


def unregister() -> None:
    if _inspector_active:
        _restore_preferences()

    if hasattr(bpy.types.WindowManager, "gbx_inspector"):
        del bpy.types.WindowManager.gbx_inspector

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
