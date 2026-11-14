"""Extracted interaction implementation for the Blender host."""

from ._support import (
    _GPU_LIST_KINDS, _GPU_SCALAR_KINDS, _GPU_SEQUENCE_SIZES, _GPU_TEXT_KINDS, bpy, colorsys, math,
)

class Interaction:
    def _clamp(self, widget, value, previous):
        value = max(widget.minimum, min(widget.maximum, value))
        if isinstance(previous, int) and not isinstance(previous, bool):
            lower, upper = math.ceil(widget.minimum), math.floor(widget.maximum)
            if lower > upper:
                raise ValueError("GPU integer control bounds contain no integer value.")
            value = max(lower, min(upper, round(value)))
        return value


    def _write(self, widget, value, event, commit=True, clamp=True):
        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
            context = bpy.context
            previous = widget.getter(context)
            if widget.kind in _GPU_SEQUENCE_SIZES:
                previous, value = tuple(previous), tuple(value)
            if widget.kind == "reorder":
                previous, value = list(previous), list(value)
            if clamp and widget.kind in _GPU_SCALAR_KINDS:
                value = self._clamp(widget, value, previous)
            if clamp and widget.kind in ("range", "vector", "xypad"):
                value = tuple(self._clamp(widget, item, old) for item, old in zip(value, previous))
                if widget.kind == "range" and value[0] > value[1]:
                    value = (value[1], value[1]) if value[0] != previous[0] else (value[0], value[0])
            if value != previous:
                widget.setter(context, value)
                widget.value = self._read(widget, context)
                if widget.on_change:
                    widget.on_change(context, event)
                if commit and not self._closed and self._alive():
                    self._commit(widget, event)
        if not self._closed and self._alive():
            self._area.tag_redraw()


    def _commit(self, widget, event):
        if widget.on_commit:
            widget.on_commit(bpy.context, event)
        if widget.undo and not self._closed and self._alive():
            self._undo(widget.text)


    def _undo(self, message):
        if bpy.context.preferences.edit.use_global_undo:
            result = bpy.ops.ed.undo_push(message="GPU UI: " + message)
            if "FINISHED" not in result:
                raise RuntimeError("Blender could not create an undo step for GPU UI.")


    def _start_edit(self, widget, text, index=None):
        self._focus = widget.key
        self._edit = {"key": widget.key, "text": text, "select_all": True, "cursor": len(text), "index": index}


    def _activate(self, widget, event, item=None):
        if not widget.enabled:
            return
        kind = widget.kind
        if kind in ("button", "imagebutton"):
            with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                widget.callback(bpy.context, event)
        elif kind in ("checkbox", "toggle"):
            self._write(widget, not widget.value, event)
        elif kind in ("dropdown", "search"):
            self._popup = widget.key
            self._query = ""
            self._popup_keyboard = kind == "search"
            self._popup_index = [entry[0] for entry in widget.items].index(widget.value)
            self._popup_offset = max(0, self._popup_index - 3)
        elif kind == "hotkey":
            self._focus = widget.key
            self._hotkey = widget.key
        elif kind == "confirm":
            self._focus = widget.key
            self._confirm = widget.key
        elif kind == "tabs":
            self._write(widget, (widget.value + 1) % len(widget.items) if item is None else item, event)
        elif kind == "radio":
            identifiers = [entry[0] for entry in widget.items]
            index = (identifiers.index(widget.value) + 1) % len(identifiers) if item is None else item
            self._write(widget, identifiers[index], event)
        elif kind == "list":
            if item is not None:
                self._write(widget, widget.items[item][0], event)
        elif kind == "reorder":
            if item is not None:
                self._select_row(widget, item)
        elif kind == "multilist":
            identifiers = [entry[0] for entry in widget.items]
            if not identifiers or item is None and getattr(event, "type", "") == "LEFTMOUSE":
                return
            if item is None:
                item = self._components.get(widget.key, 0)
                value = set(widget.value) ^ {identifiers[item]}
            elif getattr(event, "shift", False):
                anchor = self._components.get(widget.key, item)
                low, high = sorted((anchor, item))
                value = set(identifiers[low:high + 1]) | (set(widget.value) if getattr(event, "ctrl", False) else set())
                self._write(widget, value, event)
                return
            else:
                cell = dict(self._item_cells(widget))[item]
                on_box = self._mouse is not None and self._mouse[0] < cell[0] + self._line(compact=True)
                toggle = on_box or getattr(event, "ctrl", False)
                value = set(widget.value) ^ {identifiers[item]} if toggle else {identifiers[item]}
            self._components[widget.key] = item
            self._write(widget, value, event)
        elif kind == "table":
            if item is not None:
                self._write(widget, item, event)
        elif kind == "tree":
            identifiers = [entry[0] for entry in widget.items]
            if item is None:
                if widget.value in identifiers:
                    self._toggle_tree(widget, identifiers.index(widget.value))
                return
            cells = dict(self._item_cells(widget))
            arrow = cells[item][0] + (widget.meta[item][0] * 14 + 16) * self._scale
            if widget.meta[item][1] and self._mouse is not None and self._mouse[0] < arrow:
                self._toggle_tree(widget, item)
            else:
                self._write(widget, identifiers[item], event)
        elif kind == "textarea":
            self._start_edit(widget, widget.value)
            self._edit["select_all"] = False
        elif kind == "number" or kind == "stepper" and item not in (0, 2):
            self._start_edit(widget, str(widget.value))
        elif kind == "stepper":
            self._write(widget, widget.value + (widget.step if item == 2 else -widget.step), event)
        elif kind == "text":
            self._start_edit(widget, widget.value)
        elif kind == "filepath":
            if item == 1:
                self._browse(widget)
            else:
                self._start_edit(widget, widget.value)
        elif kind == "vector":
            index = self._components.get(widget.key, 0) if item is None else item
            self._components[widget.key] = index
            self._start_edit(widget, str(widget.value[index]), index)
        elif kind == "color":
            value = widget.value
            self._picker = {
                "key": widget.key, "widget": widget, "initial": value,
                "hsv": list(colorsys.rgb_to_hsv(*self._rgba(value)[:3])),
                "alpha": self._rgba(value)[3] if len(value) == 4 else None,
                "channel": 0, "drag": None,
            }
        elif kind == "collapsible":
            self._set_expanded(widget, not widget.value)
        elif kind == "overflow":
            if widget.items:
                self._popup = widget.key
                self._query = ""
                self._popup_keyboard = False
                self._popup_index = 0
                self._popup_offset = 0
        elif kind == "range":
            self._handles[widget.key] = 1 - self._handles.get(widget.key, 0)
        elif kind == "rating":
            if item is None and getattr(event, "type", "") == "LEFTMOUSE":
                return
            index = (int(widget.value) % int(widget.maximum)) if item is None else item
            self._write(widget, 0 if item is not None and widget.value == index + 1 else index + 1, event)
        elif kind == "segmented":
            identifiers = [entry[0] for entry in widget.items]
            index = (identifiers.index(widget.value) + 1) % len(identifiers) if item is None else item
            self._write(widget, identifiers[index], event)
        elif kind == "flags":
            if item is None and getattr(event, "type", "") == "LEFTMOUSE":
                return
            index = self._components.get(widget.key, 0) if item is None else item
            self._components[widget.key] = index
            self._write(widget, set(widget.value) ^ {widget.items[index][0]}, event)
        elif kind == "palette":
            if item is None:
                if getattr(event, "type", "") == "LEFTMOUSE":
                    return
                active = self._palette_index(widget)
                item = 0 if active is None else (active + 1) % len(widget.palette)
            self._write(widget, widget.palette[item][:len(widget.value)], event)
        elif kind == "banner":
            mouse = getattr(event, "type", "") == "LEFTMOUSE"
            if not mouse or self._mouse is not None and self._contains(self._banner_close(widget), *self._mouse):
                self._dismissed.add((widget.key, widget.text))
                if self._focus == widget.key:
                    self._focus = None
                self._area.tag_redraw()
        elif kind == "link":
            with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                if widget.url:
                    bpy.ops.wm.url_open(url=widget.url)
                if callable(widget.callback):
                    widget.callback(bpy.context, event)


    def _set_expanded(self, widget, value):
        self._expanded[widget.key] = value
        if value and widget.parent is not None and widget.parent.kind == "accordion":
            for sibling in widget.parent.children:
                if sibling is not widget:
                    self._expanded[sibling.key] = False


    def _choose(self, widget, identifier, event):
        if widget.kind == "overflow":
            target = self._buttons.get(identifier)
            if target is not None:
                self._activate(target, event)
        else:
            self._write(widget, identifier, event)


    def _scroll_console(self, widget, value):
        bottom = max(0, len(widget.value) - widget.rows)
        widget.scroll = max(0, min(int(value), bottom))
        self._scroll[widget.key] = widget.scroll
        self._tail[widget.key] = widget.scroll >= bottom
        self._area.tag_redraw()


    def _toggle_tree(self, widget, index):
        depth, children, expanded = widget.meta[index]
        if children:
            self._open[(widget.key, widget.items[index][0])] = not expanded
            self._area.tag_redraw()


    def _edit_commit(self, event):
        widget = self._buttons.get(self._edit["key"])
        if widget is None:
            self._edit = None
            return
        value = self._edit["text"]
        if widget.kind not in _GPU_TEXT_KINDS:
            try:
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError()
            except ValueError:
                self.report({"WARNING"}, "Enter a finite number.")
                return
            if widget.kind == "vector":
                index = self._edit["index"]
                value = tuple(value if position == index else component
                              for position, component in enumerate(widget.value))
        self._write(widget, value, event)
        self._edit = None


    @staticmethod
    def _insert(edit, typed, limit):
        if edit["select_all"]:
            edit["text"], edit["cursor"] = "", 0
        typed = typed[:max(0, limit - len(edit["text"]))]
        edit["text"] = edit["text"][:edit["cursor"]] + typed + edit["text"][edit["cursor"]:]
        edit["cursor"] += len(typed)
        edit["select_all"] = False


    def _edit_key(self, event):
        edit = self._edit
        widget = self._buttons.get(edit["key"])
        numeric = widget is None or widget.kind not in _GPU_TEXT_KINDS
        multiline = widget is not None and widget.kind == "textarea"
        limit = 128 if numeric else 8192 if multiline else 1024
        ctrl = getattr(event, "ctrl", False)
        text, cursor = edit["text"], edit["cursor"]
        line_start = text.rfind("\n", 0, cursor) + 1
        line_end = text.find("\n", cursor)
        line_end = len(text) if line_end < 0 else line_end
        if event.type == "ESC":
            self._edit = None
        elif multiline and event.type in ("RET", "NUMPAD_ENTER") and not ctrl:
            self._insert(edit, "\n", limit)
        elif multiline and event.type in ("UP_ARROW", "DOWN_ARROW"):
            column = cursor - line_start
            if event.type == "UP_ARROW" and line_start > 0:
                start = text.rfind("\n", 0, line_start - 1) + 1
                cursor = min(start + column, line_start - 1)
            elif event.type == "DOWN_ARROW" and line_end < len(text):
                end = text.find("\n", line_end + 1)
                cursor = min(line_end + 1 + column, len(text) if end < 0 else end)
            edit.update(cursor=cursor, select_all=False)
        elif multiline and event.type in ("HOME", "END") and not ctrl:
            edit.update(cursor=line_start if event.type == "HOME" else line_end, select_all=False)
        elif event.type in ("RET", "NUMPAD_ENTER"):
            self._edit_commit(event)
        elif event.type == "TAB":
            self._edit_commit(event)
            if self._edit is None and not self._closed and self._alive():
                self._keyboard(event)
        elif ctrl and event.type == "A":
            edit["select_all"] = True
            edit["cursor"] = len(text)
        elif ctrl and event.type in ("C", "X"):
            self._manager.clipboard = text
            if event.type == "X" and edit["select_all"]:
                edit.update(text="", cursor=0, select_all=False)
        elif ctrl and event.type == "V":
            pasted = str(self._manager.clipboard).replace("\r\n", "\n") if multiline else \
                " ".join(str(self._manager.clipboard).splitlines())
            if numeric:
                pasted = pasted.strip()[:limit]
                edit.update(text=pasted, cursor=len(pasted), select_all=False)
            else:
                self._insert(edit, pasted, limit)
        elif event.type in ("LEFT_ARROW", "RIGHT_ARROW", "HOME", "END"):
            if event.type in ("HOME", "END"):
                cursor = 0 if event.type == "HOME" else len(text)
            elif edit["select_all"]:
                cursor = 0 if event.type == "LEFT_ARROW" else len(text)
            else:
                cursor = max(0, min(len(text), cursor + (1 if event.type == "RIGHT_ARROW" else -1)))
            edit.update(cursor=cursor, select_all=False)
        elif event.type in ("BACK_SPACE", "DEL"):
            if edit["select_all"]:
                edit.update(text="", cursor=0)
            elif event.type == "BACK_SPACE" and cursor > 0:
                edit.update(text=text[:cursor - 1] + text[cursor:], cursor=cursor - 1)
            elif event.type == "DEL":
                edit["text"] = text[:cursor] + text[cursor + 1:]
            edit["select_all"] = False
        elif not ctrl:
            typed = getattr(event, "unicode", "") or getattr(event, "ascii", "")
            if typed and typed.isprintable() and (
                    not numeric or all(character in "0123456789.eE+-" for character in typed)):
                self._insert(edit, typed, limit)
        if multiline and self._edit is edit:
            line = edit["text"].count("\n", 0, edit["cursor"])
            start = self._scroll.get(widget.key, 0)
            self._scroll[widget.key] = line if line < start else line - widget.rows + 1 if line >= start + widget.rows else start
            widget.scroll = self._scroll[widget.key]


    def _textarea_cursor(self, widget, x, y):
        """Map a click inside an edited text area to a caret index."""
        text = self._edit["text"]
        lines = text.split("\n")
        row = self._line(compact=True)
        top = widget.rect[1] + widget.rect[3] - (self._line() if widget.text else 0) - 4 * self._scale
        line = max(0, min(len(lines) - 1, widget.scroll + int((top - y) // row)))
        offset = x - widget.rect[0] - 6 * self._scale
        column = 0
        while column < len(lines[line]) and self._measure(lines[line][:column + 1]) <= offset:
            column += 1
        return sum(len(previous) + 1 for previous in lines[:line]) + column


    def _keyboard(self, event):
        if self._edit:
            if event.value == "PRESS":
                self._edit_key(event)
            return True
        if event.type == "TAB" and event.value == "PRESS":
            keys = [key for key, widget in self._buttons.items()
                    if widget.enabled and self._control_clip(widget)[2] > 0 and self._control_clip(widget)[3] > 0]
            if keys:
                index = keys.index(self._focus) if self._focus in keys else (-1 if not getattr(event, "shift", False) else 0)
                self._focus = keys[(index + (-1 if getattr(event, "shift", False) else 1)) % len(keys)]
                return True
        widget = self._buttons.get(self._focus)
        if widget and widget.enabled and event.value == "PRESS":
            kind = widget.kind
            if event.type in ("RET", "NUMPAD_ENTER", "SPACE"):
                self._activate(widget, event)
                return True
            horizontal = event.type in ("LEFT_ARROW", "RIGHT_ARROW")
            vertical = event.type in ("UP_ARROW", "DOWN_ARROW")
            direction = 1 if event.type in ("RIGHT_ARROW", "DOWN_ARROW") else -1
            if kind in ("slider", "number", "tabs", "stepper", "knob") and horizontal or kind == "knob" and vertical:
                if kind == "knob" and vertical:
                    direction = -direction
                value = (widget.value + direction) % len(widget.items) if kind == "tabs" else widget.value + direction * widget.step
                self._write(widget, value, event)
                return True
            if kind == "range" and horizontal:
                low, high = widget.value
                if self._handles.get(widget.key, 0) == 0:
                    value = (min(low + direction * widget.step, high), high)
                else:
                    value = (low, max(high + direction * widget.step, low))
                self._write(widget, value, event)
                return True
            if kind == "rating" and horizontal:
                self._write(widget, widget.value + direction, event)
                return True
            if kind == "timeline" and event.type in ("HOME", "END"):
                self._write(widget, widget.minimum if event.type == "HOME" else widget.maximum, event)
                return True
            if kind == "timeline" and horizontal:
                self._write(widget, widget.value + direction * widget.step, event)
                return True
            if kind == "dial" and (horizontal or vertical):
                # Right/Up turn counter-clockwise like Blender's rotation fields increasing.
                turn = direction if horizontal else -direction
                self._write(widget, widget.value + turn * math.radians(widget.step), event)
                return True
            if kind == "xypad" and (horizontal or vertical):
                x_value, y_value = widget.value
                if horizontal:
                    x_value += direction * widget.step
                else:
                    y_value -= direction * widget.step
                self._write(widget, (x_value, y_value), event)
                return True
            if kind in ("radio", "segmented") and (horizontal or vertical):
                identifiers = [entry[0] for entry in widget.items]
                index = (identifiers.index(widget.value) + direction) % len(identifiers)
                self._write(widget, identifiers[index], event)
                return True
            if kind == "multilist" and event.type == "A" and getattr(event, "ctrl", False):
                everything = {entry[0] for entry in widget.items}
                self._write(widget, set() if set(widget.value) == everything else everything, event)
                return True
            if kind == "multilist" and vertical and widget.items:
                index = max(0, min(len(widget.items) - 1, self._components.get(widget.key, 0) + direction))
                self._components[widget.key] = index
                self._reveal_row(widget, index)
                if getattr(event, "shift", False):
                    self._write(widget, set(widget.value) | {widget.items[index][0]}, event)
                return True
            if kind == "reorder" and vertical and widget.items:
                current = self._components.get(widget.key, -1)
                if getattr(event, "ctrl", False) or getattr(event, "alt", False):
                    if 0 <= current and 0 <= current + direction < len(widget.items):
                        self._move_row(widget, current, current + direction, event)
                        self._reveal_row(widget, current + direction)
                    return True
                index = max(0, min(len(widget.items) - 1, current + direction if current >= 0 else
                                   (0 if direction > 0 else len(widget.items) - 1)))
                self._select_row(widget, index)
                self._reveal_row(widget, index)
                return True
            if kind in _GPU_LIST_KINDS and vertical and widget.items:
                identifiers = [entry[0] for entry in widget.items]
                value = identifiers[widget.value] if kind == "table" and widget.value >= 0 else widget.value
                current = identifiers.index(value) if value in identifiers else (-1 if direction > 0 else len(identifiers))
                index = max(0, min(len(identifiers) - 1, current + direction))
                start = self._scroll.get(widget.key, 0)
                self._scroll[widget.key] = index if index < start else (
                    index - widget.rows + 1 if index >= start + widget.rows else start)
                self._write(widget, index if kind == "table" else identifiers[index], event)
                return True
            if kind == "tree" and horizontal:
                identifiers = [entry[0] for entry in widget.items]
                if widget.value in identifiers:
                    index = identifiers.index(widget.value)
                    if widget.meta[index][2] != (direction > 0):
                        self._toggle_tree(widget, index)
                return True
            if kind == "flags" and horizontal:
                self._components[widget.key] = (self._components.get(widget.key, 0) + direction) % len(widget.items)
                return True
            if kind == "palette" and (horizontal or vertical):
                active = self._palette_index(widget)
                step = direction * (widget.columns if vertical else 1)
                index = 0 if active is None else max(0, min(len(widget.palette) - 1, active + step))
                self._write(widget, widget.palette[index][:len(widget.value)], event)
                return True
            if kind == "vector" and horizontal:
                self._components[widget.key] = (self._components.get(widget.key, 0) + direction) % len(widget.value)
                return True
            if kind == "vector" and vertical:
                index = self._components.get(widget.key, 0)
                value = tuple(component - direction * widget.step if position == index else component
                              for position, component in enumerate(widget.value))
                self._write(widget, value, event)
                return True
            if kind == "collapsible" and horizontal:
                self._set_expanded(widget, direction > 0)
                return True
            if kind == "console" and (vertical or event.type in ("HOME", "END", "PAGE_UP", "PAGE_DOWN")):
                step = {"HOME": -len(widget.value), "END": len(widget.value),
                        "PAGE_UP": -widget.rows, "PAGE_DOWN": widget.rows}.get(event.type, direction)
                self._scroll_console(widget, widget.scroll + step)
                return True
            if event.type == "ESC":
                self._focus = None
                return True
        return False


    def _popup_event(self, event, x, y):
        widget = self._buttons[self._popup]
        rect, height, count = self._popup_geometry(widget)
        items = self._popup_items(widget)
        search = widget.kind == "search"
        header = (rect[0], rect[1] + rect[3], rect[2], height)
        if event.type in ("WHEELUPMOUSE", "WHEELDOWNMOUSE"):
            self._popup_offset += -1 if event.type == "WHEELUPMOUSE" else 1
            self._popup_geometry(widget)
        elif event.type in ("UP_ARROW", "DOWN_ARROW") and event.value == "PRESS":
            if items:
                self._popup_keyboard = True
                self._popup_index = (self._popup_index + (1 if event.type == "DOWN_ARROW" else -1)) % len(items)
                self._popup_offset = max(0, self._popup_index - count + 1) if self._popup_index >= self._popup_offset + count else min(self._popup_offset, self._popup_index)
        elif event.type == "ESC" and event.value == "PRESS":
            self._popup = None
        elif event.type == "TAB" and event.value == "PRESS":
            self._popup = None
            self._keyboard(event)
        elif event.type in ("RET", "NUMPAD_ENTER") and event.value == "PRESS":
            self._popup = None
            if items:
                self._choose(widget, items[min(self._popup_index, len(items) - 1)][0], event)
        elif event.type == "MOUSEMOVE":
            self._popup_keyboard = False
            if self._contains(rect, x, y) and items:
                self._popup_index = min(len(items) - 1, self._popup_offset + int((rect[1] + rect[3] - y) / height))
        elif event.type == "LEFTMOUSE":
            if event.value == "PRESS":
                if search and self._contains(header, x, y):
                    return {"RUNNING_MODAL"}
                if self._contains(rect, x, y) and items:
                    index = min(len(items) - 1, self._popup_offset + int((rect[1] + rect[3] - y) / height))
                    self._popup = None
                    self._choose(widget, items[index][0], event)
                self._popup = None
                self._swallow_release = True
        elif search and event.value == "PRESS":
            query = self._query
            if event.type == "BACK_SPACE":
                query = query[:-1]
            elif event.type == "DEL":
                query = ""
            elif getattr(event, "ctrl", False) and event.type == "V":
                query += " ".join(str(self._manager.clipboard).splitlines())
            elif not getattr(event, "ctrl", False):
                typed = getattr(event, "unicode", "") or getattr(event, "ascii", "")
                if typed and typed.isprintable():
                    query += typed
            if query != self._query:
                self._query = query[:64]
                self._popup_index = 0
                self._popup_offset = 0
                self._popup_keyboard = True
        return {"RUNNING_MODAL"}


    @staticmethod
    def _hotkey_matches(hotkey, event):
        key, modifiers = hotkey
        held = frozenset(name for name in ("ctrl", "shift", "alt", "oskey") if getattr(event, name, False))
        return event.type == key and held == modifiers


    def _command_items(self, palette):
        """Commands whose text contains every typed word; enabled and prefix matches come first."""
        words = self._command["query"].lower().split()
        found = [command for command in palette.children if all(word in command.text.lower() for word in words)]
        first = words[0] if words else ""
        return sorted(found, key=lambda command: (not command.enabled, not command.text.lower().startswith(first)))


    def _command_geometry(self, palette, count):
        left, bottom, right, top = self._bounds()
        scale, line = self._scale, self._line()
        rows = max(1, min(palette.rows, count))
        width = max(0, min(palette.width * scale, right - left - 16 * scale))
        height = min(line * (rows + 1) + 8 * scale, top - bottom)
        x = left + (right - left - width) / 2
        y = max(bottom, top - min(60 * scale, (top - bottom) * 0.1) - height)
        return (x, y, width, height), line, rows


    def _command_row(self, rect, line, row):
        """Row 0 is the search field, rows 1.. list the commands."""
        pad = 4 * self._scale
        return (rect[0] + pad, rect[1] + rect[3] - pad - line * (row + 1), rect[2] - 2 * pad, line)


    def _command_event(self, event, x, y):
        state = self._command
        palette = self._widgets[state["key"]]
        items = self._command_items(palette)
        rect, line, rows = self._command_geometry(palette, len(items))
        state["index"] = max(0, min(state["index"], len(items) - 1))

        def row_at():
            for row in range(rows):
                if state["offset"] + row < len(items) and self._contains(self._command_row(rect, line, row + 1), x, y):
                    return state["offset"] + row
            return None

        def follow():
            index = state["index"]
            if index < state["offset"]:
                state["offset"] = index
            elif index >= state["offset"] + rows:
                state["offset"] = index - rows + 1

        if event.type in ("WHEELUPMOUSE", "WHEELDOWNMOUSE"):
            step = -1 if event.type == "WHEELUPMOUSE" else 1
            state["offset"] = max(0, min(state["offset"] + step, len(items) - rows))
        elif event.type in ("UP_ARROW", "DOWN_ARROW") and event.value == "PRESS":
            if items:
                state["index"] = (state["index"] + (1 if event.type == "DOWN_ARROW" else -1)) % len(items)
                follow()
        elif event.type == "ESC" and event.value == "PRESS":
            self._command = None
        elif event.type in ("RET", "NUMPAD_ENTER") and event.value == "PRESS":
            if items and items[state["index"]].enabled:
                self._run_command(items[state["index"]], event)
        elif event.type == "MOUSEMOVE":
            index = row_at()
            if index is not None:
                state["index"] = index
        elif event.type in ("LEFTMOUSE", "RIGHTMOUSE") and event.value == "PRESS":
            self._swallow_release = event.type == "LEFTMOUSE"
            index = row_at()
            if index is not None and event.type == "LEFTMOUSE":
                if items[index].enabled:
                    self._run_command(items[index], event)
            elif not self._contains(rect, x, y):
                self._command = None
        elif event.value == "PRESS":
            query = state["query"]
            if event.type == "BACK_SPACE":
                query = query[:-1]
            elif event.type == "DEL":
                query = ""
            elif getattr(event, "ctrl", False) and event.type == "V":
                query += " ".join(str(self._manager.clipboard).splitlines())
            elif not getattr(event, "ctrl", False) and not getattr(event, "alt", False):
                typed = getattr(event, "unicode", "") or getattr(event, "ascii", "")
                if typed and typed.isprintable():
                    query += typed
            if query != state["query"]:
                state.update(query=query[:64], index=0, offset=0)
        return {"RUNNING_MODAL"}


    def _run_command(self, command, event):
        self._command = None
        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
            command.callback(bpy.context, event)


    def _draw_command(self, viewport):
        theme, scale = self._theme, self._scale
        state = self._command
        palette = self._widgets[state["key"]]
        items = self._command_items(palette)
        rect, line, rows = self._command_geometry(palette, len(items))
        self._quad(rect, theme["popup"], viewport)
        search = self._command_row(rect, line, 0)
        self._quad(search, theme["button"], viewport)
        text_rect = (search[0] + 6 * scale, search[1], search[2] - 12 * scale, search[3])
        if state["query"]:
            self._text(state["query"] + "|", text_rect, theme["text"], viewport)
        else:
            self._text(palette.placeholder or "Search commands...", text_rect, theme["disabled_text"], viewport)
        if not items:
            self._text("No matching commands", self._shrink(self._command_row(rect, line, 1), 6 * scale),
                       theme["disabled_text"], viewport)
            return
        for row in range(rows):
            index = state["offset"] + row
            if index >= len(items):
                break
            command = items[index]
            cell = self._command_row(rect, line, row + 1)
            if index == state["index"]:
                self._quad(cell, theme["button_pressed"] if command.enabled else theme["button_disabled"], viewport)
            inner = (cell[0] + 6 * scale, cell[1], cell[2] - 12 * scale, cell[3])
            color = theme["text"] if command.enabled else theme["disabled_text"]
            self._icon_text(command.icon, command.text, inner, color, viewport)
            if command.tooltip:
                dim = theme["disabled_text"]
                self._text(command.tooltip.replace("\n", " "), inner, dim, viewport, right=True, size=0.85)
        if len(items) > rows:
            track = (rect[0] + rect[2] - 4 * scale, rect[1] + 4 * scale, 2 * scale, line * rows)
            thumb_height = track[3] * rows / len(items)
            thumb_y = track[1] + track[3] - thumb_height - (track[3] - thumb_height) * state["offset"] / (len(items) - rows)
            self._quad((track[0], thumb_y, track[2], thumb_height), theme["focus"], viewport, radius=0)


    def _hotkey_event(self, event, _x=None, _y=None):
        widget = self._buttons.get(self._hotkey)
        modifiers = ("LEFT_CTRL", "RIGHT_CTRL", "LEFT_SHIFT", "RIGHT_SHIFT", "LEFT_ALT", "RIGHT_ALT", "OSKEY")
        if widget is None:
            self._hotkey = None
        elif event.type in ("LEFTMOUSE", "RIGHTMOUSE") and event.value == "PRESS":
            self._hotkey = None
            self._swallow_release = True
        elif event.value != "PRESS" or event.type in modifiers or event.type.endswith("MOUSEMOVE") \
                or event.type.startswith(("WHEEL", "TRACKPAD", "NDOF", "TEXTINPUT")) or event.type == "NONE":
            pass
        elif event.type == "ESC":
            self._hotkey = None
        elif event.type in ("BACK_SPACE", "DEL"):
            self._hotkey = None
            self._write(widget, "", event)
        else:
            parts = [name for name, attribute in (("CTRL", "ctrl"), ("SHIFT", "shift"), ("ALT", "alt"), ("OSKEY", "oskey"))
                     if getattr(event, attribute, False)]
            self._hotkey = None
            self._write(widget, "+".join(parts + [event.type]), event)
        return {"RUNNING_MODAL"}


    def _confirm_geometry(self):
        scale = self._scale
        widget = self._buttons[self._confirm]
        line = self._line()
        pad = 10 * scale
        width = min(self._region.width, max(260 * scale, self._measure(widget.message) + 2 * pad))
        height = min(self._region.height, 2 * line + 3 * pad)
        x = (self._region.width - width) / 2
        y = (self._region.height - height) / 2
        button = (width - 3 * pad) / 2
        return {
            "rect": (x, y, width, height),
            "message": (x + pad, y + 2 * pad + line, width - 2 * pad, line),
            "ok": (x + pad, y + pad, button, line),
            "cancel": (x + 2 * pad + button, y + pad, button, line),
        }


    def _confirm_event(self, event, x, y):
        widget = self._buttons[self._confirm]
        geometry = self._confirm_geometry()
        accept = None
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            self._swallow_release = True
            if self._contains(geometry["ok"], x, y):
                accept = True
            elif self._contains(geometry["cancel"], x, y) or not self._contains(geometry["rect"], x, y):
                accept = False
        elif event.type in ("RET", "NUMPAD_ENTER") and event.value == "PRESS":
            accept = True
        elif event.type == "ESC" and event.value == "PRESS":
            accept = False
        if accept is not None:
            self._confirm = None
            action = widget.callback if accept else widget.cancel
            if action:
                with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                    action(bpy.context, event)
        return {"RUNNING_MODAL"}


    def _close_picker(self, event):
        picker, self._picker = self._picker, None
        widget = self._buttons.get(picker["key"], picker["widget"])
        if widget.value != picker["initial"]:
            with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                self._commit(widget, event)


    def _picker_set(self, channel, fraction, event):
        picker = self._picker
        fraction = max(0.0, min(1.0, fraction))
        if channel < 3:
            picker["hsv"][channel] = fraction
        else:
            picker["alpha"] = fraction
        picker["channel"] = channel
        self._write(self._buttons[picker["key"]], self._picker_color(picker), event, commit=False, clamp=False)


    def _picker_event(self, event, x, y):
        picker = self._picker
        widget = self._buttons[picker["key"]]
        rect, _preview, rows = self._picker_geometry(widget)
        if event.type == "MOUSEMOVE" and picker["drag"] is not None:
            bar = rows[picker["drag"]][1]
            self._picker_set(picker["drag"], (x - bar[0]) / bar[2], event)
        elif event.type == "LEFTMOUSE" and event.value == "PRESS":
            for index, (row, bar) in enumerate(rows):
                if self._contains(row, x, y):
                    picker["drag"] = index
                    self._picker_set(index, (x - bar[0]) / bar[2], event)
                    break
            else:
                if not self._contains(rect, x, y):
                    self._close_picker(event)
                    self._swallow_release = True
        elif event.type == "LEFTMOUSE" and event.value == "RELEASE":
            picker["drag"] = None
        elif event.value == "PRESS":
            if event.type == "ESC":
                self._picker = None
                self._write(widget, picker["initial"], event, commit=False, clamp=False)
            elif event.type in ("RET", "NUMPAD_ENTER", "TAB"):
                self._close_picker(event)
            elif event.type in ("UP_ARROW", "DOWN_ARROW"):
                picker["channel"] = (picker["channel"] + (1 if event.type == "DOWN_ARROW" else -1)) % len(rows)
            elif event.type in ("LEFT_ARROW", "RIGHT_ARROW"):
                channel = picker["channel"]
                step = 0.01 if event.type == "RIGHT_ARROW" else -0.01
                self._picker_set(channel, self._picker_channels(picker)[channel] + step, event)
        return {"RUNNING_MODAL"}


    def _slider_value(self, x, event, y=None):
        key, initial = self._slide
        widget = self._buttons.get(key)
        if widget is None or not widget.enabled:
            return
        if widget.kind == "dial":
            if y is None:
                return
            center = self._dial_geometry(widget)[0]
            if math.hypot(x - center[0], y - center[1]) < 1:
                return
            angle = math.atan2(y - center[1], x - center[0])
            # Follow the pointer the short way round so whole turns of the property are kept.
            value = widget.value + math.remainder(angle - widget.value, 2 * math.pi)
            if getattr(event, "ctrl", False):
                step = math.radians(widget.step)
                value = round(value / step) * step
            self._write(widget, value, event, commit=False)
            return
        span = widget.maximum - widget.minimum
        if widget.kind == "xypad":
            if y is None:
                return
            pad = self._pad_rect(widget)
            value = tuple(
                widget.minimum + round(max(0, min(1, (position - origin) / max(1, size))) * span / widget.step)
                * widget.step
                for position, origin, size in ((x, pad[0], pad[2]), (y, pad[1], pad[3])))
            self._write(widget, value, event, commit=False)
            return
        if widget.kind == "knob":
            if y is None:
                return
            # Vertical drag: 200 UI-scaled pixels cover the whole range, like Blender's value dragging.
            value = initial + (y - self._slide_origin) / (200 * self._scale) * span
        else:
            track = self._timeline_track(widget) if widget.kind == "timeline" else widget.rect
            fraction = max(0, min(1, (x - track[0]) / max(1, track[2])))
            value = widget.minimum + fraction * span
        value = widget.minimum + round((value - widget.minimum) / widget.step) * widget.step
        if widget.kind == "range":
            low, high = widget.value
            value = (min(value, high), high) if self._handles.get(key, 0) == 0 else (low, max(value, low))
        self._write(widget, value, event, commit=False)

