"""Opt-in hosting in a Blender editor; importing GPUUI registers nothing."""

import logging
from uuid import uuid4

import bpy

from ._browse import Browse
from ._runtime import Runtime
from ._support import _shared
from .components import Root


log = logging.getLogger("gpuui")
_hosts: dict[str, "BlenderHost"] = {}


class BlenderHost:
    def __init__(self, root: Root, *, on_close=None):
        if not isinstance(root, Root):
            raise TypeError("BlenderHost requires a GPUUI Root")
        self.root = root
        self._token = uuid4().hex
        self._runtime = None
        self._dirty = True
        self.on_close = on_close

    @property
    def is_open(self) -> bool:
        return self._runtime is not None and not self._runtime._closed

    def open(self, context):
        if self.is_open:
            raise RuntimeError("This GPUUI host is already open")
        if context.window is None or context.area is None or context.area.type not in {
            "VIEW_3D", "IMAGE_EDITOR", "NODE_EDITOR",
        }:
            raise ValueError("Open GPUUI in a 3D View, Image Editor or Node Editor")
        register()
        _hosts[self._token] = self
        try:
            with context.temp_override(window=context.window, area=context.area):
                result = bpy.ops.gpuui.open("INVOKE_DEFAULT", token=self._token)
            if "RUNNING_MODAL" not in result:
                raise RuntimeError(f"GPUUI host could not open: {result}")
        except Exception:
            _hosts.pop(self._token, None)
            raise
        return self

    def invalidate(self):
        """Call after structural or unbound document changes."""
        self._dirty = True
        if self.is_open:
            self._runtime._area.tag_redraw()

    def close(self):
        if self._runtime is not None:
            self._runtime._finish()
        _hosts.pop(self._token, None)


class GPUUI_OT_Browse(Browse, bpy.types.Operator):
    bl_idname = "gpuui.browse"
    bl_label = "Choose Path"
    bl_options = {"INTERNAL"}
    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={"SKIP_SAVE"})
    filter_glob: bpy.props.StringProperty(default="*", options={"HIDDEN", "SKIP_SAVE"})


class GPUUI_OT_Open(Runtime, bpy.types.Operator):
    bl_idname = "gpuui.open"
    bl_label = "Open GPU UI"
    bl_options = {"INTERNAL"}
    token: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    browse_operator = "gpuui.browse"
    _instances = {}
    _controls = Runtime._controls | {"custom"}

    @staticmethod
    def _context():
        return bpy.context

    def invoke(self, context, event):
        self.host = _hosts[self.token]
        self.editor_type = context.area.type
        # A host owns one area; opening another root there replaces it, not the new root.
        for instance in list(self._instances.values()):
            if instance._window == context.window and instance._area == context.area:
                instance._finish()
        self.host._runtime = self
        self._custom_capture = None
        return super().invoke(context, event)

    def execute(self, context):
        return self.invoke(context, None)

    def build_ui(self, _context):
        self.host._dirty = False
        return self.host.root

    def _poll_changes(self):
        if self.host._dirty:
            return True
        if self._theme != self._tree.theme.resolve(bpy.context):
            return True
        for widget in self._widgets.values():
            if widget.getter is not None and self._read(widget, bpy.context) != widget.value:
                return True
            kind = getattr(widget, "theme_kind", widget.kind)
            key = (kind, id(getattr(widget, "style", None)))
            if self._widget_themes[key] != self._tree.theme.resolve(bpy.context, kind, getattr(widget, "style", None)):
                return True
            if widget.kind in ("spinner",) or (widget.kind == "custom" and widget.needs_redraw()):
                return True
        size = (self._region.width, self._region.height, bpy.context.preferences.system.ui_scale)
        changed = size != getattr(self, "_last_size", None)
        self._last_size = size
        return changed or bool(_shared.get("_gpu_toast_store"))

    def _finish(self):
        try:
            if getattr(self, "_custom_capture", None) is not None:
                self._custom_capture.cancel_interaction(bpy.context)
        except Exception:
            log.exception("Could not roll back custom GPUUI interaction while closing")
        finally:
            self._custom_capture = None
            super()._finish()
        pending = _shared.get("_gpu_browse_pending")
        if pending and pending["overlay"] == self:
            _shared.pop("_gpu_browse_pending")
        _hosts.pop(self.token, None)
        self.host._runtime = None
        callback, self.host.on_close = self.host.on_close, None
        if callback is not None:
            callback()

    def _refresh(self):
        super()._refresh()
        capture = self._custom_capture
        if capture is not None and (self._buttons.get(capture.key) is not capture or not capture.enabled):
            capture.cancel_interaction(bpy.context)
            self._custom_capture = None

    def _render(self):
        if self.host._dirty:
            self._refresh()
        super()._render()

    def _cancel_custom_capture(self):
        if self._custom_capture is not None:
            self._custom_capture.cancel_interaction(bpy.context)
            self._custom_capture = None

    def _custom_event(self, event, x, y):
        if any((self._popup, self._picker, self._command, self._confirm, self._hotkey, self._edit)):
            return None
        widget = self._custom_capture
        if widget is None:
            kind, candidate = self._hit(x, y)
            if kind == "button" and candidate.kind == "custom":
                widget = candidate
            elif event.value == "PRESS" and event.type not in {"LEFTMOUSE", "RIGHTMOUSE", "MIDDLEMOUSE"}:
                candidate = self._buttons.get(self._focus)
                if candidate is not None and candidate.kind == "custom":
                    widget = candidate
        if widget is None or not widget.enabled:
            return None
        handled = widget.handle_event(self, event, x, y)
        if handled:
            self._area.tag_redraw()
            return {"RUNNING_MODAL"}
        return None

    def modal(self, context, event):
        try:
            return super().modal(context, event)
        except Exception:
            log.exception("GPUUI host failed")
            self._finish()
            return {"CANCELLED"}


@bpy.app.handlers.persistent
def _before_load(_unused):
    close_all()


def register():
    if not GPUUI_OT_Open.is_registered:
        bpy.utils.register_class(GPUUI_OT_Browse)
        bpy.utils.register_class(GPUUI_OT_Open)
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)


def close_all():
    for host in list(_hosts.values()):
        host.close()


def unregister():
    close_all()
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    for cls in (GPUUI_OT_Open, GPUUI_OT_Browse):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
