"""Blender draw-handler and modal lifecycle."""

from ._support import (
    _GPU_DRAG_KINDS, _GPU_ITEM_KINDS, _GPU_LIST_KINDS, _GPU_SCALAR_KINDS, _GPU_SEQUENCE_SIZES, _GPU_VALUE_KINDS, _gpu_finite, _shared, blf, bpy, gpu, time, traceback,
)
from ._arrangement import Arrangement
from ._drawing import Drawing
from ._interaction import Interaction

class Runtime(Arrangement, Drawing, Interaction):
    _font_users = {}


    _controls = set(_GPU_VALUE_KINDS) | {"button", "collapsible", "imagebutton", "confirm", "console", "overflow",
                                              "link", "banner"}


    def _release_font(self):
        if self._font_id == 0:
            return
        font_id, users = self._font_users[self._font_path]
        if users == 1:
            blf.unload(font_id)
            del self._font_users[self._font_path]
        else:
            self._font_users[self._font_path] = (font_id, users - 1)
        self._font_id = 0
        self._font_path = ""


    @staticmethod
    def _contains(rect, x, y):
        left, bottom, width, height = rect
        return width > 0 and height > 0 and left <= x < left + width and bottom <= y < bottom + height


    @staticmethod
    def _intersection(first, second):
        x = max(first[0], second[0])
        y = max(first[1], second[1])
        right = min(first[0] + first[2], second[0] + second[2])
        top = min(first[1] + first[3], second[1] + second[3])
        return (x, y, max(0, right - x), max(0, top - y))


    @classmethod
    def close_all(cls):
        for instance in list(cls._instances.values()):
            instance._finish()


    def _alive(self):
        try:
            return (
                self._window in bpy.context.window_manager.windows[:]
                and self._area in self._window.screen.areas[:]
                and self._area.type == self.editor_type
                and self._region in self._area.regions[:]
            )
        except ReferenceError:
            return False


    def _finish(self):
        if self._closed:
            return
        self._closed = True
        if self._slide is not None and self._alive():
            try:
                self._cancel_slide()
            except (ReferenceError, RuntimeError, AttributeError, TypeError, ValueError) as error:
                traceback.print_exc()
                self.report({"WARNING"}, "GPU UI slider rollback failed: " + str(error))
        if self._picker is not None and self._alive():
            try:
                self._cancel_picker()
            except (ReferenceError, RuntimeError, AttributeError, TypeError, ValueError) as error:
                traceback.print_exc()
                self.report({"WARNING"}, "GPU UI color rollback failed: " + str(error))
        self._instances.pop(self._instance_key, None)
        if self._handle is not None:
            try:
                self._space.draw_handler_remove(self._handle, "WINDOW")
            except (ReferenceError, RuntimeError) as error:
                self.report({"WARNING"}, "GPU UI draw handler cleanup: " + str(error))
            self._handle = None
        if self._timer is not None:
            try:
                self._manager.event_timer_remove(self._timer)
            except (ReferenceError, RuntimeError) as error:
                self.report({"WARNING"}, "GPU UI timer cleanup: " + str(error))
            self._timer = None
        self._batches.clear()
        self._release_font()
        if self._alive():
            self._area.tag_redraw()


    def cancel(self, context):
        self._finish()


    def _refresh(self):
        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
            context = bpy.context
            self._scale = max(0.5, context.preferences.system.ui_scale)
            self._tree = self.build_ui(context)
            self._theme = self._tree.theme.resolve(context)
            self._widget_themes = {}
            font_path = bpy.path.abspath(self._theme["font"]) if self._theme["font"] else ""
            if font_path != self._font_path:
                cached = self._font_users.get(font_path)
                font_id = cached[0] if cached else blf.load(font_path) if font_path else 0
                if font_id < 0:
                    raise ValueError("GPU UI could not load font: " + font_path)
                self._release_font()
                if font_path:
                    self._font_users[font_path] = (font_id, cached[1] + 1 if cached else 1)
                self._font_id = font_id
                self._font_path = font_path
        keys = set()
        self._buttons = {}
        self._widgets = {}
        self._panels = [child for child in self._tree.children if child.kind == "panel"]
        self._palettes = [child for child in self._tree.children if child.kind == "commands"]

        def collect(widget):
            if widget.key in keys:
                raise ValueError("GPU UI node used more than once: " + widget.key)
            keys.add(widget.key)
            self._widgets[widget.key] = widget
            kind = getattr(widget, "theme_kind", widget.kind)
            style = getattr(widget, "style", None)
            style_key = (kind, id(style))
            if style_key not in self._widget_themes:
                self._widget_themes[style_key] = self._tree.theme.resolve(bpy.context, kind, style)
            if widget.kind == "tree":
                widget.items, widget.meta = [], []

                def flatten(nodes, depth):
                    for identifier, label, children in nodes:
                        expanded = self._open.get((widget.key, identifier), False)
                        widget.items.append((identifier, label))
                        widget.meta.append((depth, bool(children), expanded))
                        if children and expanded:
                            flatten(children, depth + 1)
                flatten(widget.tree, 0)
            if widget.getter is not None:
                with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                    widget.value = self._read(widget, bpy.context)
                if widget.kind == "tabs":
                    indices = [child.index for child in widget.children]
                    if len(set(indices)) != len(indices) or any(index >= len(widget.items) for index in indices):
                        raise ValueError("GPU Tab pages require unique indices within the Options list.")
            if widget.kind == "collapsible":
                widget.value = self._expanded.setdefault(widget.key, widget.expanded)
            if widget.kind == "accordion":
                opened = [child for child in widget.children if self._expanded.setdefault(child.key, child.expanded)]
                for child in opened[1:]:
                    self._expanded[child.key] = False
            if widget.kind == "splitter":
                widget.ratio = self._ratios.get(widget.key, widget.ratio)
            if widget.kind in ("flow", "richtext"):
                widget.flow_width = self._flow_widths.get(widget.key)
            for child in widget.children:
                if child.kind == "banner":
                    child.hidden = (child.key, child.text) in self._dismissed
            if widget.kind == "console":
                bottom = max(0, len(widget.value) - widget.rows)
                if self._tail.setdefault(widget.key, widget.auto):
                    widget.scroll = bottom
                else:
                    widget.scroll = max(0, min(int(self._scroll.get(widget.key, 0)), bottom))
                self._scroll[widget.key] = widget.scroll
            if widget.kind in ("image", "imagebutton") and widget.image is not None and not hasattr(widget.image, "size"):
                raise ValueError("GPU Image requires a Blender Image or None.")
            if widget.kind in self._controls and (widget.kind != "banner" or widget.closable):
                self._buttons[widget.key] = widget
            if widget.kind == "scroll":
                widget.scroll = self._scroll.get(widget.key, 0)
            if widget.kind in _GPU_LIST_KINDS:
                widget.scroll = max(0, min(int(self._scroll.get(widget.key, 0)), len(widget.items) - widget.rows))
                self._scroll[widget.key] = widget.scroll
            if widget.kind == "reorder":
                index = self._components.get(widget.key, -1)
                if widget.selected_get is not None:
                    with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                        index = widget.selected_get(bpy.context)
                    if isinstance(index, bool) or not isinstance(index, int):
                        raise ValueError("GPU Reorderable List Active Index must be an integer property.")
                self._components[widget.key] = index if 0 <= index < len(widget.items) else -1
            if widget.kind == "multilist":
                self._components[widget.key] = max(0, min(self._components.get(widget.key, 0), len(widget.items) - 1))
            if widget.kind == "textarea":
                lines = (self._edit["text"] if self._edit and self._edit["key"] == widget.key else widget.value).count("\n") + 1
                widget.scroll = max(0, min(int(self._scroll.get(widget.key, 0)), lines - widget.rows))
                self._scroll[widget.key] = widget.scroll
            for child in widget.visible_children():
                collect(child)

        for panel in self._panels:
            before = set(self._buttons)
            collect(panel)
            if panel.mode == "hud":
                # HUDs are click-through, so their controls must not take focus or clicks.
                for key in set(self._buttons) - before:
                    del self._buttons[key]
            if panel.mode == "attached":
                panel.anchor = self._anchor_world(panel.target)
            if panel.mode in ("drawer", "dock") and panel.key not in self._seen:
                self._seen.add(panel.key)
                if not panel.expanded:
                    self._collapsed.add(panel.key)
        for palette in self._palettes:
            for command in [palette] + palette.children:
                if command.key in keys:
                    raise ValueError("GPU UI node used more than once: " + command.key)
                keys.add(command.key)
                self._widgets[command.key] = command
        if self._command is not None and self._command["key"] not in self._widgets:
            self._command = None
        self._arrange()
        if self._pressed not in self._buttons:
            self._pressed = None
        if self._focus not in self._buttons or (self._focus and not self._buttons[self._focus].enabled):
            self._focus = None
            self._edit = None
        if self._edit is not None and self._edit["key"] != self._focus:
            self._edit = None
        if self._popup is not None:
            widget = self._buttons.get(self._popup)
            if widget is None or not widget.enabled or widget.kind == "overflow" and not widget.items:
                self._popup = None
            else:
                self._popup_index = max(0, min(self._popup_index, len(self._popup_items(widget)) - 1))
        for state, kind in (("_hotkey", "hotkey"), ("_confirm", "confirm")):
            widget = self._buttons.get(getattr(self, state))
            if getattr(self, state) is not None and (widget is None or widget.kind != kind or not widget.enabled):
                setattr(self, state, None)
        if self._picker is not None:
            widget = self._buttons.get(self._picker["key"])
            if widget is None or widget.kind != "color" or not widget.enabled:
                self._cancel_picker()
        if self._reorder is not None:
            widget = self._buttons.get(self._reorder["key"])
            if widget is None or widget.kind != "reorder" or not widget.enabled or \
                    self._reorder["from"] >= len(widget.items):
                self._reorder = None
        if self._slide is not None:
            widget = self._buttons.get(self._slide[0])
            if widget is None or widget.kind not in _GPU_DRAG_KINDS or not widget.enabled:
                self._cancel_slide()


    def _read(self, widget, context):
        value = widget.getter(context)
        kind = widget.kind
        if kind in ("checkbox", "toggle") and not isinstance(value, bool):
            raise ValueError("GPU checkbox/toggle requires a Boolean property.")
        if kind in _GPU_SCALAR_KINDS + ("dial",) and not _gpu_finite(value):
            raise ValueError("GPU numeric control requires a finite numeric property.")
        if kind in ("dropdown", "radio", "search", "segmented") and value not in [item[0] for item in widget.items]:
            raise ValueError("GPU Dropdown/Radio/Search/Segmented property value must match an option identifier.")
        if kind in ("flags", "multilist"):
            try:
                value = set(value) if not isinstance(value, str) else None
            except TypeError:
                value = None
            if value is None or not value <= {item[0] for item in widget.items}:
                raise ValueError("GPU " + ("Multi-Toggle" if kind == "flags" else "Multi-select List") +
                                 " requires an enum-flag (set) property whose items match the Options.")
        if kind == "reorder":
            try:
                value = list(value) if not isinstance(value, (str, bytes)) and value is not None else None
            except TypeError:
                value = None
            if value is None:
                raise ValueError("GPU Reorderable List requires a list or a collection property.")
            widget.items = [(str(index), self._entry_label(widget, entry)) for index, entry in enumerate(value)]
        if kind == "tabs" and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < len(widget.items)):
            raise ValueError("GPU Tabs requires an integer property matching a tab index.")
        if kind in ("text", "list", "hotkey", "textarea", "tree", "filepath") and not isinstance(value, str):
            raise ValueError("GPU Text Field/Text Area/File Path/List View/Tree/Hotkey requires a String property.")
        if kind == "table":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("GPU Table requires an integer property (-1 means no selection).")
            # Rows can disappear while a selection is stored; show that as "no selection".
            if not 0 <= value < len(widget.items):
                value = -1
        if kind in _GPU_SEQUENCE_SIZES:
            try:
                value = tuple(value) if not isinstance(value, str) else ()
            except TypeError:
                value = ()
            if len(value) not in _GPU_SEQUENCE_SIZES[kind] or not all(_gpu_finite(item) for item in value):
                raise ValueError({
                    "color": "GPU Color Swatch requires a finite RGB or RGBA property.",
                    "palette": "GPU Color Palette requires a finite RGB or RGBA property.",
                    "range": "GPU Range Slider requires two finite numeric properties.",
                    "vector": "GPU Vector requires a finite numeric vector with 2 to 4 components.",
                    "xypad": "GPU XY Pad requires a finite numeric property with 2 components.",
                }[kind])
        return value


    def _cancel_picker(self):
        picker, self._picker = self._picker, None
        widget = picker["widget"]
        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
            if tuple(widget.getter(bpy.context)) != picker["initial"]:
                widget.setter(bpy.context, picker["initial"])


    def _cancel_slide(self):
        _key, initial = self._slide
        self._slide = None
        self._pressed = None
        self._capture = False
        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
            self._slide_control.setter(bpy.context, initial)


    def _draw(self):
        if self._closed:
            return
        context = bpy.context
        if context.window != self._window or context.area != self._area or context.region != self._region:
            return
        try:
            if self._shader is None:
                self._shader = gpu.shader.from_builtin("UNIFORM_COLOR")
            blend = gpu.state.blend_get()
            depth = gpu.state.depth_test_get()
            mask = gpu.state.depth_mask_get()
            try:
                gpu.state.blend_set("ALPHA")
                gpu.state.depth_test_set("NONE")
                gpu.state.depth_mask_set(False)
                attached = [panel for panel in self._panels if panel.mode == "attached"]
                if any(self._project(panel.anchor) != panel.screen for panel in attached):
                    self._arrange()
                self._render()
            finally:
                gpu.state.depth_mask_set(mask)
                gpu.state.depth_test_set(depth)
                gpu.state.blend_set(blend)
        except Exception as error:
            traceback.print_exc()
            self.report({"ERROR"}, "GPU UI drawing failed: " + str(error))
            self._finish()


    def invoke(self, context, event):
        region = next((region for region in context.area.regions if region.type == "WINDOW"), None)
        if region is None:
            self.report({"ERROR"}, "This editor has no WINDOW region for GPU UI.")
            return {"CANCELLED"}
        self._instance_key = (context.window.as_pointer(), context.area.as_pointer(), region.as_pointer())
        existing = self._instances.get(self._instance_key)
        if existing is not None:
            existing._finish()
            return {"FINISHED"}
        self._window = context.window
        self._area = context.area
        self._region = region
        self._manager = context.window_manager
        self._space = type(context.space_data)
        self._closed = False
        self._handle = None
        self._timer = None
        self._shader = None
        self._batches = {}
        self._font_id = 0
        self._font_path = ""
        self._positions = {}
        self._sizes = {}
        self._minimized = set()
        self._maximized = set()
        self._window_hover = None
        self._resize = None
        self._split_drag = None
        self._ratios = {}
        self._flow_widths = {}
        self._tail = {}
        self._dismissed = set()
        self._collapsed = set()
        self._seen = set()
        self._edge_drag = None
        self._command = None
        self._palettes = []
        self._pressed = None
        self._pressed_item = None
        self._picker = None
        self._expanded = {}
        self._open = {}
        self._handles = {}
        self._components = {}
        self._image_shader = None
        self._drag = None
        self._hover = None
        self._capture = False
        self._focus = None
        self._edit = None
        self._popup = None
        self._popup_offset = 0
        self._popup_index = 0
        self._popup_keyboard = False
        self._query = ""
        self._hotkey = None
        self._confirm = None
        self._scroll = {}
        self._slide = None
        self._reorder = None
        self._swallow_release = False
        self._mouse = None
        self._hover_since = time.monotonic()
        self._next_refresh = time.monotonic() + 0.1
        self._instances[self._instance_key] = self
        try:
            self._refresh()
            self._handle = self._space.draw_handler_add(self._draw, (), "WINDOW", "POST_PIXEL")
            self._timer = self._manager.event_timer_add(0.1, window=self._window)
            self._manager.modal_handler_add(self)
            self._area.tag_redraw()
        except Exception as error:
            traceback.print_exc()
            self.report({"ERROR"}, "GPU UI could not open: " + str(error))
            self._finish()
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


    def modal(self, context, event):
        if self._closed:
            return {"CANCELLED"}
        if not self._alive():
            self._finish()
            return {"CANCELLED"}
        try:
            if event.type == "WINDOW_DEACTIVATE":
                self._cancel_custom_capture()
                self._mouse = None
                self._update_hover()
                return {"PASS_THROUGH"}
            if event.type == "TIMER":
                now = time.monotonic()
                if now >= self._next_refresh:
                    if self._poll_changes():
                        self._refresh()
                        self._area.tag_redraw()
                    self._next_refresh = now + 0.1
                return {"PASS_THROUGH"}

            x = event.mouse_x - self._region.x
            y = event.mouse_y - self._region.y
            self._mouse = (x, y)
            self._update_hover()
            result = self._custom_event(event, x, y)
            if result is not None:
                return result
            if self._swallow_release and event.type == "LEFTMOUSE" and event.value == "RELEASE":
                self._swallow_release = False
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                for toast, rect, _alpha in self._toasts(time.monotonic()):
                    if self._contains(rect, x, y):
                        _shared["_gpu_toast_store"].remove(toast)
                        self._swallow_release = True
                        self._area.tag_redraw()
                        return {"RUNNING_MODAL"}
            for state, handler in (("_confirm", self._confirm_event), ("_hotkey", self._hotkey_event)):
                if getattr(self, state) is None:
                    continue
                result = handler(event, x, y)
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                self._refresh()
                self._area.tag_redraw()
                return result
            if self._popup is not None:
                result = self._popup_event(event, x, y)
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                self._refresh()
                self._area.tag_redraw()
                return result
            if self._picker is not None:
                result = self._picker_event(event, x, y)
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                self._refresh()
                self._area.tag_redraw()
                return result
            if self._command is not None:
                result = self._command_event(event, x, y)
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                self._refresh()
                self._area.tag_redraw()
                return result
            if event.value == "PRESS" and self._palettes and not self._edit and not self._capture:
                left, bottom, right, top = self._bounds()
                palette = next((item for item in self._palettes if self._hotkey_matches(item.hotkey, event)), None)
                if palette is not None and left <= x < right and bottom <= y < top:
                    self._command = {"key": palette.key, "query": "", "index": 0, "offset": 0}
                    self._popup = None
                    self._area.tag_redraw()
                    return {"RUNNING_MODAL"}
            kind, widget = self._hit(x, y)

            if event.type == "ESC" and event.value == "PRESS" and self._reorder is not None:
                self._reorder = None
                self._capture = False
                self._pressed = None
                self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "ESC" and event.value == "PRESS" and self._slide:
                key, initial = self._slide
                self._slide = None
                self._capture = False
                self._pressed = None
                self._write(self._buttons[key], initial, event, commit=False, clamp=False)
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                self._refresh()
                return {"RUNNING_MODAL"}
            if self._edit and event.type == "LEFTMOUSE" and event.value == "PRESS":
                edited = self._buttons.get(self._edit["key"])
                if edited is not None and edited.kind == "textarea" and widget is edited:
                    self._edit.update(cursor=self._textarea_cursor(edited, x, y), select_all=False)
                    self._swallow_release = True
                    self._area.tag_redraw()
                    return {"RUNNING_MODAL"}
                self._edit_commit(event)
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                if self._edit:
                    return {"RUNNING_MODAL"}
            keyboard_event = event.type not in (
                "MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "LEFTMOUSE", "RIGHTMOUSE", "MIDDLEMOUSE",
                "WHEELUPMOUSE", "WHEELDOWNMOUSE",
            )
            if keyboard_event and (self._focus or self._edit or kind):
                if self._keyboard(event):
                    if self._closed:
                        return {"CANCELLED"}
                    if not self._alive():
                        self._finish()
                        return {"CANCELLED"}
                    self._refresh()
                    self._area.tag_redraw()
                    return {"RUNNING_MODAL"}
            if event.type == "ESC" and event.value == "PRESS" and (kind or self._capture):
                self._finish()
                return {"CANCELLED"}
            if event.type in ("WHEELUPMOUSE", "WHEELDOWNMOUSE") and kind == "button" and widget.kind == "console":
                bottom = max(0, len(widget.value) - widget.rows)
                if bottom:
                    self._scroll_console(widget, widget.scroll + (1 if event.type == "WHEELDOWNMOUSE" else -1))
                    return {"RUNNING_MODAL"}
            if event.type in ("WHEELUPMOUSE", "WHEELDOWNMOUSE") and kind == "button" and \
                    widget.kind in _GPU_LIST_KINDS + ("textarea",):
                total = len(widget.items) if widget.kind != "textarea" else (
                    self._edit["text"] if self._edit and self._edit["key"] == widget.key else widget.value).count("\n") + 1
                if total > widget.rows:
                    widget.scroll = max(0, min(widget.scroll + (1 if event.type == "WHEELDOWNMOUSE" else -1),
                                               total - widget.rows))
                    self._scroll[widget.key] = widget.scroll
                    self._area.tag_redraw()
                    return {"RUNNING_MODAL"}
            if event.type in ("WHEELUPMOUSE", "WHEELDOWNMOUSE") and kind:
                panel = next(panel for panel in reversed(self._panels) if self._contains(panel.rect, x, y))
                panel_keys = set()
                def descendants(item):
                    panel_keys.add(item.key)
                    for child in item.visible_children():
                        descendants(child)
                descendants(panel)
                scrolls = [item for item in self._widgets.values()
                           if item.kind == "scroll" and item.key in panel_keys and self._contains(item.clip, x, y)]
                if scrolls:
                    scroll = scrolls[-1]
                    scroll.scroll += (1 if event.type == "WHEELDOWNMOUSE" else -1) * 32 * self._scale
                    self._scroll[scroll.key] = scroll.scroll
                    self._arrange()
                    self._area.tag_redraw()
                    return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE" and self._reorder is not None:
                self._reorder_drag(y)
                self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE" and self._slide:
                self._slider_value(x, event, y)
                if not self._alive():
                    self._finish()
                return {"CANCELLED"} if self._closed else {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE" and self._resize is not None:
                key, start_x, start_y, start_width, start_height, top = self._resize
                minimum = max(28, self._theme["font_size"] + 12) * self._scale + 16 * self._scale
                width = max(120 * self._scale, start_width + x - start_x)
                height = max(minimum, start_height + start_y - y)
                self._sizes[key] = (width, height)
                self._positions[key] = (self._positions[key][0], top - height)
                self._arrange()
                self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE" and self._edge_drag is not None:
                key, start_x, start_y, start_width, start_height = self._edge_drag
                panel = next((item for item in self._panels if item.key == key), None)
                if panel is not None:
                    left, bottom, right, top = self._bounds()
                    sign = 1 if panel.side in ("LEFT", "TOP") else -1
                    if panel.mode == "drawer":
                        width = start_width + (x - start_x) * (1 if panel.side == "LEFT" else -1)
                        width = max(120 * self._scale, min(width, (right - left) * 0.9))
                        self._sizes[key] = (width, self._sizes.get(key, (0, panel.rect[3]))[1])
                    else:
                        height = start_height + (start_y - y) * sign
                        height = max(self._line() + 16 * self._scale, min(height, (top - bottom) * 0.8))
                        self._sizes[key] = (self._sizes.get(key, (panel.rect[2], 0))[0], height)
                    self._arrange()
                    self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE" and self._split_drag is not None:
                widget = self._widgets.get(self._split_drag)
                if widget is not None and widget.rect[2] > 0:
                    gap = max(widget.gap * self._scale, 6 * self._scale)
                    available = max(1, widget.rect[2] - gap)
                    minimum = min(0.45, 40 * self._scale / available)
                    widget.ratio = max(minimum, min(1 - minimum, (x - widget.rect[0] - gap / 2) / available))
                    self._ratios[widget.key] = widget.ratio
                    self._arrange()
                    self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE" and self._drag is not None:
                key, offset_x, offset_y = self._drag
                self._positions[key] = (x - offset_x, y - offset_y)
                self._arrange()
                self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "PRESS" and kind:
                self._capture = True
                if kind == "close":
                    self._finish()
                    return {"CANCELLED"}
                if kind in ("minimize", "maximize"):
                    self._toggle_window(widget, kind)
                elif kind == "collapse":
                    self._focus = None
                    self._toggle_collapse(widget)
                elif kind == "edge":
                    self._focus = None
                    self._edge_drag = (widget.key, x, y, widget.rect[2], widget.rect[3])
                elif kind == "splitter":
                    self._focus = None
                    self._split_drag = widget.key
                elif kind == "drag":
                    if widget.key not in self._maximized:
                        self._drag = (widget.key, x - widget.rect[0], y - widget.rect[1])
                elif kind == "resize":
                    rect = widget.rect
                    self._resize = (widget.key, x, y, rect[2], rect[3], rect[1] + rect[3])
                elif kind == "button" and widget.enabled and widget.kind in self._controls:
                    self._focus = widget.key
                    self._pressed = widget.key
                    self._pressed_item = self._item_at(widget, x, y)
                    if widget.kind == "reorder" and self._pressed_item is not None:
                        self._reorder = {"key": widget.key, "from": self._pressed_item, "to": self._pressed_item,
                                         "y": y, "moved": False}
                    if widget.kind in _GPU_DRAG_KINDS:
                        if widget.kind == "range":
                            inner = self._control_rect(widget)
                            span = widget.maximum - widget.minimum
                            low, high = (inner[0] + inner[2] * max(0, min(1, (value - widget.minimum) / span))
                                         for value in widget.value)
                            nearest_high = abs(x - high) < abs(x - low) or (low == high and x > high)
                            self._handles[widget.key] = 1 if nearest_high else 0
                        self._slide = (widget.key, widget.value)
                        self._slide_control = widget
                        self._slide_origin = y
                        if widget.kind != "knob":
                            self._slider_value(x, event, y)
                else:
                    self._focus = None
                if not self._alive():
                    self._finish()
                if self._closed:
                    return {"CANCELLED"}
                self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self._capture:
                pressed = self._pressed
                self._capture = False
                self._pressed = None
                self._drag = None
                self._resize = None
                self._split_drag = None
                self._edge_drag = None
                if self._reorder is not None:
                    state, self._reorder = self._reorder, None
                    control = self._buttons.get(state["key"])
                    if control is not None and control.enabled:
                        if not state["moved"]:
                            self._select_row(control, state["from"])
                        elif state["to"] != state["from"]:
                            self._move_row(control, state["from"], state["to"], event)
                    if not self._closed and self._alive():
                        self._refresh()
                elif self._slide:
                    key, initial = self._slide
                    self._slide = None
                    control = self._buttons.get(key)
                    if control and control.value != initial:
                        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                            self._commit(control, event)
                elif kind == "button" and widget.enabled and widget.key == pressed:
                    if widget.kind not in _GPU_ITEM_KINDS or self._item_at(widget, x, y) == self._pressed_item:
                        self._activate(widget, event, self._pressed_item)
                    if self._closed:
                        return {"CANCELLED"}
                    if not self._alive():
                        self._finish()
                        return {"CANCELLED"}
                    self._refresh()
                if self._closed:
                    return {"CANCELLED"}
                if not self._alive():
                    self._finish()
                    return {"CANCELLED"}
                self._area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                self._focus = None
            return {"PASS_THROUGH"}
        except Exception as error:
            traceback.print_exc()
            self.report({"ERROR"}, "GPU UI interaction failed: " + str(error))
            self._finish()
            return {"CANCELLED"}
