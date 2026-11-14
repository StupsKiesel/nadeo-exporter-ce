"""Extracted drawing implementation for the Blender host."""

from ._support import (
    _GPU_DRAG_KINDS, _GPU_ICONS, _GPU_ITEM_KINDS, _GPU_LIST_KINDS, _GPU_LOG_COLORS, _shared, batch_for_shader, blf, bpy, colorsys, gpu, math, os, time,
)
from .drawing import Painter

class Drawing:
    def _quad(self, rect, color, clip, radius=None):
        visible = self._intersection(rect, clip)
        if visible[2] <= 0 or visible[3] <= 0:
            return
        x, y, width, height = rect
        radius = min(self._theme["roundness"] * self._scale if radius is None else radius, width / 2, height / 2)
        if radius <= 0:
            vertices = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
        else:
            vertices = []
            for center_x, center_y, start in (
                (x + width - radius, y + radius, -90),
                (x + width - radius, y + height - radius, 0),
                (x + radius, y + height - radius, 90),
                (x + radius, y + radius, 180),
            ):
                for step in range(9):
                    angle = math.radians(start + step * 90 / 8)
                    vertices.append((center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)))
        self._poly(vertices, color, clip)


    def _poly(self, vertices, color, clip):
        # Clip the convex polygon itself; rounding the clipped bounds distorts corners.
        for axis, boundary, keep_greater in (
            (0, clip[0], True), (0, clip[0] + clip[2], False),
            (1, clip[1], True), (1, clip[1] + clip[3], False),
        ):
            result = []
            if not vertices:
                return
            previous = vertices[-1]
            previous_inside = previous[axis] >= boundary if keep_greater else previous[axis] <= boundary
            for current in vertices:
                inside = current[axis] >= boundary if keep_greater else current[axis] <= boundary
                if inside != previous_inside:
                    fraction = (boundary - previous[axis]) / (current[axis] - previous[axis])
                    result.append(tuple(previous[i] + fraction * (current[i] - previous[i]) for i in (0, 1)))
                if inside:
                    result.append(current)
                previous, previous_inside = current, inside
            vertices = result
        if len(vertices) < 3:
            return
        key = tuple(vertices)
        batch = self._batches.get(key)
        if batch is None:
            batch = batch_for_shader(
                self._shader, "TRIS", {"pos": vertices},
                indices=tuple((0, index, index + 1) for index in range(1, len(vertices) - 1)),
            )
            if len(self._batches) >= 512:
                self._batches.pop(next(iter(self._batches)))
            self._batches[key] = batch
        self._shader.bind()
        self._shader.uniform_float("color", color)
        batch.draw(self._shader)


    def _segment(self, start, end, thickness, color, clip):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            return
        nx, ny = -dy / length * thickness / 2, dx / length * thickness / 2
        self._poly([(start[0] + nx, start[1] + ny), (start[0] - nx, start[1] - ny),
                    (end[0] - nx, end[1] - ny), (end[0] + nx, end[1] + ny)], color, clip)


    def _text(self, text, rect, color, clip, centered=False, right=False, size=1.0):
        clipped = self._intersection(rect, clip)
        if clipped[2] <= 0 or clipped[3] <= 0:
            return
        font = self._font_id
        size = max(1, round(self._theme["font_size"] * self._scale * size))
        blf.size(font, size)
        text_width, _height = blf.dimensions(font, text)
        x, y, width, height = rect
        if right:
            x += max(4 * self._scale, width - text_width - 6 * self._scale)
        else:
            x += max(4 * self._scale, (width - text_width) / 2) if centered else 6 * self._scale
        blf.position(font, x, y + (height - size) / 2 + 2 * self._scale, 0)
        blf.color(font, *color)
        blf.enable(font, blf.CLIPPING)
        try:
            left, bottom, clip_width, clip_height = clipped
            blf.clipping(font, left, bottom, left + clip_width, bottom + clip_height)
            blf.draw(font, text)
        finally:
            blf.disable(font, blf.CLIPPING)


    def _render(self):
        root_theme = self._theme
        theme = root_theme
        viewport = (0, 0, self._region.width, self._region.height)
        for panel in self._panels:
            if panel.rect[2] <= 0 or panel.rect[3] <= 0:
                continue
            theme = self._widget_themes[("panel", id(getattr(panel, "style", None)))]
            self._theme = theme
            clip = self._intersection(panel.rect, viewport)
            fade = panel.opacity
            if panel.mode == "attached" and panel.screen is not None:
                self._draw_leader(panel, viewport)
            self._quad(panel.rect, theme["panel_outline"][:3] + (theme["panel_outline"][3] * fade,), clip)
            self._quad(self._shrink(panel.rect, max(1, round(self._scale))),
                       theme["panel"][:3] + (theme["panel"][3] * fade,), clip)
            if panel.mode == "drawer" and self._folded(panel):
                self._draw_window_button("collapse", panel.rect, panel, clip)
                continue
            self._quad(self._header(panel), theme["header"][:3] + (theme["header"][3] * fade,), clip)
            header = self._header(panel)
            self._quad((header[0], header[1], header[2], max(1, round(self._scale))),
                       theme["panel_outline"], clip, radius=0)
            edge = self._edge_rect(panel)
            if self._window_hover == ("edge", panel.key) or self._edge_drag and self._edge_drag[0] == panel.key:
                self._quad(edge, theme["focus"], clip, radius=0)
            header = self._header(panel)
            buttons = self._window_buttons(panel)
            self._text(panel.text, (header[0], header[1], max(0, header[2] - len(buttons) * header[3]), header[3]),
                       theme["text"], clip)
            for button, rect in buttons:
                self._draw_window_button(button, rect, panel, clip)
            content_clip = self._intersection(
                clip, (panel.rect[0], panel.rect[1], panel.rect[2], max(0, panel.rect[3] - header[3]))
            )

            def draw_widget(widget):
                widget_clip = self._intersection(content_clip, widget.clip)
                if widget.kind in self._controls:
                    self._draw_control(widget, widget_clip)
                elif widget.kind == "label":
                    self._icon_text(widget.icon, widget.text, widget.rect, theme["label_text"], widget_clip)
                elif widget.kind == "progress":
                    x, y, width, height = widget.rect
                    fraction = max(0, min(1, (widget.value - widget.minimum) / (widget.maximum - widget.minimum)))
                    self._quad(widget.rect, theme["button"], widget_clip)
                    self._quad((x, y, width * fraction, height), theme["button_pressed"], widget_clip)
                    label = (widget.text + ": " if widget.text else "") + str(round(fraction * 100)) + "%"
                    self._text(label, widget.rect, theme["text"], widget_clip, centered=True)
                elif widget.kind == "separator" and widget.line:
                    x, y, width, height = widget.rect
                    thickness = max(1, round(self._scale))
                    self._quad((x, y + (height - thickness) / 2, width, thickness),
                               theme["disabled_text"], widget_clip, radius=0)
                elif widget.kind == "image":
                    self._draw_image(widget, widget_clip)
                elif widget.kind == "badge":
                    x, y, width, height = widget.rect
                    pill = (x, y + 2 * self._scale, min(width, self._measure(widget.text) + 16 * self._scale),
                            max(0, height - 4 * self._scale))
                    self._quad(pill, widget.color or theme["focus"], widget_clip, radius=pill[3] / 2)
                    self._text(widget.text, pill, theme["text"], widget_clip, centered=True)
                elif widget.kind == "spinner":
                    self._draw_spinner(widget, widget_clip)
                elif widget.kind == "graph":
                    self._draw_graph(widget, widget_clip)
                elif widget.kind == "barchart":
                    self._draw_barchart(widget, widget_clip)
                elif widget.kind == "keyvalue":
                    row = self._line(compact=True)
                    x, y, width, height = widget.rect
                    if not widget.pairs:
                        self._text("(empty)", (x, y + height - row, width, row), theme["disabled_text"], widget_clip)
                    for index, (name, item) in enumerate(widget.pairs):
                        cell = (x, y + height - (index + 1) * row, width, row)
                        if index % 2:
                            self._quad(cell, theme["button"][:3] + (theme["button"][3] * 0.35,), widget_clip, radius=0)
                        self._text(name, (x, cell[1], width * 0.45, row), theme["label_text"],
                                   self._intersection(widget_clip, (x, cell[1], width * 0.45, row)))
                        self._text(item, cell, theme["text"], widget_clip, right=True)
                elif widget.kind == "richtext":
                    x, y, width, height = widget.rect
                    top = y + height
                    for style, line, indent, line_height in widget.wrapped:
                        top -= line_height
                        cell = (x + indent, top, max(0, width - indent), line_height)
                        if style == "quote":
                            self._quad((x + 2 * self._scale, top, 3 * self._scale, line_height),
                                       theme["disabled_text"], widget_clip, radius=0)
                        color = {"heading": theme["text"], "quote": theme["disabled_text"]}.get(style, theme["label_text"])
                        self._text(line, cell, color, widget_clip, size=1.25 if style == "heading" else 1.0)
                elif widget.kind == "box":
                    colors = self._widget_themes[("box", id(getattr(widget, "style", None)))]
                    self._quad(widget.rect, colors["panel_outline"], widget_clip)
                    self._quad(self._shrink(widget.rect, max(1, round(self._scale))), colors["surface"], widget_clip)
                    if widget.text:
                        caption = self._line(compact=True)
                        x, y, width, height = widget.rect
                        self._text(widget.text, (x + widget.padding * self._scale,
                                                 y + height - widget.padding * self._scale - caption, width, caption),
                                   theme["label_text"], widget_clip)
                elif widget.kind == "statusbar":
                    x, y, width, height = widget.rect
                    self._quad(widget.rect, theme["header"], widget_clip, radius=0)
                    self._quad((x, y + height - max(1, round(self._scale)), width, max(1, round(self._scale))),
                               theme["disabled_text"][:3] + (0.5,), widget_clip, radius=0)
                elif widget.kind == "toolbar":
                    self._quad(widget.rect, theme["header"], widget_clip)
                elif widget.kind == "splitter":
                    divider = self._divider(widget)
                    active = self._split_drag == widget.key or self._window_hover == ("splitter", widget.key)
                    thickness = max(1, round(2 * self._scale))
                    middle = divider[0] + divider[2] / 2
                    self._quad((middle - thickness / 2, divider[1], thickness, divider[3]),
                               theme["focus"] if active else theme["disabled_text"][:3] + (0.5,), widget_clip, radius=0)
                for child in widget.visible_children():
                    draw_widget(child)

            for child in panel.children:
                draw_widget(child)
            if self._grip_active(panel):
                grip = self._grip_rect(panel)
                dot = max(1, 2 * self._scale)
                for column, row in ((0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (0, 2)):
                    self._quad((grip[0] + grip[2] - (column + 1) * 2 * dot, grip[1] + dot + row * 2 * dot, dot, dot),
                               theme["disabled_text"], clip, radius=0)
        theme = self._theme = root_theme
        if self._popup is not None:
            widget = self._buttons[self._popup]
            rect, height, count = self._popup_geometry(widget)
            items = self._popup_items(widget)
            self._quad(rect, theme["popup"], viewport)
            if widget.kind == "search":
                header = (rect[0], rect[1] + rect[3], rect[2], height)
                self._quad(header, theme["button"], viewport)
                self._text("Search: " + self._query + "|", header, theme["text"], viewport)
                if not items:
                    self._text("(no matches)", (rect[0], rect[1] + rect[3] - height, rect[2], height),
                               theme["disabled_text"], viewport)
            for row in range(count):
                index = row + self._popup_offset
                if index >= len(items):
                    break
                cell = (rect[0], rect[1] + rect[3] - (row + 1) * height, rect[2], height)
                if self._mouse is not None and self._contains(cell, *self._mouse):
                    self._quad(cell, theme["button_hover"], viewport)
                elif index == self._popup_index and self._popup_keyboard:
                    self._quad(cell, theme["focus"], viewport)
                elif items[index][0] == widget.value:
                    self._quad(cell, theme["button_pressed"], viewport)
                self._text(items[index][1], cell, theme["text"], viewport)
        elif self._confirm is not None:
            self._draw_confirm(viewport)
        elif self._picker is not None:
            self._draw_picker(viewport)
        elif self._hover in self._widgets and time.monotonic() - self._hover_since >= 0.6:
            widget = self._widgets[self._hover]
            if widget.tooltip and not self._capture:
                text = widget.tooltip.replace("\n", " ")
                blf.size(self._font_id, max(1, round(theme["font_size"] * self._scale)))
                width = min(self._region.width, blf.dimensions(self._font_id, text)[0] + 16 * self._scale)
                height = max(28, theme["font_size"] + 12) * self._scale
                x, y = self._mouse
                rect = (min(max(0, x), self._region.width - width),
                        max(0, min(y - height - 8, self._region.height - height)), width, height)
                self._quad(rect, theme["tooltip"], viewport)
                self._text(text, rect, theme["text"], viewport)
        if self._command is not None:
            self._draw_command(viewport)
        self._draw_toasts(viewport)


    @staticmethod
    def _shrink(rect, amount):
        return (rect[0] + amount, rect[1] + amount, max(0, rect[2] - 2 * amount), max(0, rect[3] - 2 * amount))


    def _draw_leader(self, panel, viewport):
        """Line from the attached object's screen position to the nearest point of its panel."""
        x, y, width, height = panel.rect
        anchor = panel.screen
        near = (max(x, min(anchor[0], x + width)), max(y, min(anchor[1], y + height)))
        color = self._theme["focus"]
        if math.hypot(near[0] - anchor[0], near[1] - anchor[1]) > 1:
            self._segment(anchor, near, max(1, 1.5 * self._scale), color[:3] + (0.8,), viewport)
        dot = 3 * self._scale
        self._quad((anchor[0] - dot, anchor[1] - dot, 2 * dot, 2 * dot), color, viewport, radius=dot)


    @staticmethod
    def _mix(color, other, factor):
        return tuple(a + (b - a) * factor for a, b in zip(color, other))


    def _banner_close(self, widget):
        x, y, width, height = widget.rect
        return (x + width - height, y, height, height) if widget.closable else (0, 0, 0, 0)


    def _draw_banner(self, widget, clip, focused=False):
        theme = self._theme
        accent = _GPU_LOG_COLORS.get(widget.level, theme["focus"])
        rect = widget.rect
        if focused:
            self._quad(rect, theme["focus"], clip)
            rect = self._shrink(rect, 2)
        self._quad(rect, self._mix(theme["panel"], accent[:3] + (1.0,), 0.25), clip)
        self._quad((rect[0], rect[1], 4 * self._scale, rect[3]), accent, clip, radius=0)
        close = self._banner_close(widget)
        self._text(widget.text, (rect[0] + 4 * self._scale, rect[1], max(0, rect[2] - close[2] - 4 * self._scale), rect[3]),
                   theme["text"], self._intersection(clip, (rect[0], rect[1], max(0, rect[2] - close[2]), rect[3])))
        if widget.closable:
            hovered = self._mouse is not None and self._contains(close, *self._mouse)
            size = close[3] * 0.22
            center = (close[0] + close[2] / 2, close[1] + close[3] / 2)
            color = theme["text"] if hovered else theme["disabled_text"]
            for sign in (1, -1):
                self._segment((center[0] - size, center[1] - sign * size), (center[0] + size, center[1] + sign * size),
                              max(1, 1.5 * self._scale), color, clip)


    def _star(self, cell, color, clip):
        cx, cy = cell[0] + cell[2] / 2, cell[1] + cell[3] / 2
        outer, inner = min(cell[2], cell[3]) * 0.42, min(cell[2], cell[3]) * 0.17
        points = []
        for index in range(10):
            angle = math.radians(90 + index * 36)
            radius = outer if index % 2 == 0 else inner
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        self._poly([points[index] for index in range(1, 10, 2)], color, clip)
        for index in range(0, 10, 2):
            self._poly([points[index - 1], points[index], points[(index + 1) % 10]], color, clip)


    def _draw_rating(self, widget, clip, hovered, focused, text_color):
        theme = self._theme
        x, y, width, height = widget.rect
        if widget.text:
            self._text(widget.text, (x, y, width * 0.3, height), theme["label_text"], clip)
        rect = self._control_rect(widget)
        if focused:
            self._quad(rect, theme["focus"][:3] + (0.35,), clip)
        preview = self._item_at(widget, *self._mouse) if hovered and self._mouse is not None else None
        filled = preview + 1 if preview is not None else widget.value
        for index, cell in self._item_cells(widget):
            if index < filled:
                color = theme["focus"] if preview is None else self._mix(theme["focus"], (1.0, 1.0, 1.0, 1.0), 0.3)
            else:
                color = theme["button"] if widget.enabled else theme["button_disabled"]
            self._star(cell, color, clip)


    def _palette_index(self, widget):
        value = self._rgba(widget.value)
        for index, swatch in enumerate(widget.palette):
            if all(abs(a - b) < 1e-3 for a, b in zip(value[:len(widget.value)], swatch[:len(widget.value)])):
                return index
        return None


    def _draw_palette(self, widget, clip, hovered, focused):
        theme = self._theme
        x, y, width, height = widget.rect
        if widget.text:
            self._text(widget.text, (x, y + height - self._line(), width, self._line()), theme["label_text"], clip)
        active = self._palette_index(widget)
        hovered_item = self._item_at(widget, *self._mouse) if hovered and self._mouse is not None else None
        for index, cell in self._item_cells(widget):
            cell = self._shrink(cell, 2 * self._scale)
            if index == active or index == hovered_item:
                ring = theme["focus"] if focused or index == hovered_item else theme["text"]
                self._quad(cell, ring, clip)
                cell = self._shrink(cell, 2 * self._scale)
            self._quad(cell, widget.palette[index] if widget.enabled else self._mix(
                widget.palette[index], theme["button_disabled"], 0.6), clip)


    def _draw_barchart(self, widget, clip):
        theme = self._theme
        rect = widget.rect
        self._quad(rect, theme["popup"], clip)
        line = self._line(compact=True)
        inner = self._shrink(rect, 4 * self._scale)
        if widget.text:
            self._text(widget.text, (rect[0], rect[1] + rect[3] - line, rect[2], line), theme["label_text"], clip)
            inner = (inner[0], inner[1], inner[2], max(0, inner[3] - line))
        values = widget.value
        if not values:
            self._text("(no data)", rect, theme["disabled_text"], clip, centered=True)
            return
        if widget.labels:
            labels = (inner[0], inner[1], inner[2], line)
            inner = (inner[0], inner[1] + line, inner[2], max(0, inner[3] - line))
        low = min(0.0, min(values)) if widget.auto else widget.minimum
        high = max(0.0, max(values)) if widget.auto else widget.maximum
        if high - low <= 0:
            high = low + 1
        slot = inner[2] / len(values)
        gap = min(4 * self._scale, slot * 0.2)
        base = inner[1] + inner[3] * (0 - low) / (high - low) if low < 0 else inner[1]
        for index, value in enumerate(values):
            top = inner[1] + inner[3] * max(0, min(1, (value - low) / (high - low)))
            bar = (inner[0] + index * slot + gap / 2, min(base, top), max(1, slot - gap), abs(top - base))
            self._quad(bar, widget.color or theme["focus"], clip, radius=min(2 * self._scale, bar[2] / 2))
            if widget.labels and index < len(widget.labels):
                cell = (inner[0] + index * slot, labels[1], slot, labels[3])
                self._text(widget.labels[index], cell, theme["label_text"], self._intersection(clip, cell),
                           centered=True)


    def _draw_console(self, widget, clip, focused):
        theme = self._theme
        rect = widget.rect
        if focused:
            self._quad(rect, theme["focus"], clip)
            rect = self._shrink(rect, 2)
        self._quad(rect, theme["tooltip"], clip)
        row = self._line(compact=True)
        inner = self._shrink(rect, 4 * self._scale)
        lines = widget.value[widget.scroll:widget.scroll + widget.rows]
        if not widget.value:
            self._text(widget.placeholder or "(empty)", (inner[0], inner[1] + inner[3] - row, inner[2], row),
                       theme["disabled_text"], clip)
        for index, (level, text) in enumerate(lines):
            cell = (inner[0], inner[1] + inner[3] - (index + 1) * row, inner[2] - 6 * self._scale, row)
            self._text(text, cell, _GPU_LOG_COLORS.get(level, theme["text"]), self._intersection(clip, inner))
        if len(widget.value) > widget.rows:
            track = (inner[0] + inner[2] - 3 * self._scale, inner[1], 3 * self._scale, inner[3])
            size = max(8 * self._scale, track[3] * widget.rows / len(widget.value))
            offset = (track[3] - size) * widget.scroll / (len(widget.value) - widget.rows)
            self._quad((track[0], track[1] + track[3] - offset - size, track[2], size),
                       theme["disabled_text"], clip, radius=0)


    def _toasts(self, now):
        """Active toasts as (toast, rect, alpha), newest at the bottom right of the free area."""
        store = _shared.setdefault("_gpu_toast_store", [])
        store[:] = [toast for toast in store if now - toast["start"] < toast["duration"]]
        left, bottom, right, top = self._bounds()
        height = self._line()
        margin = 12 * self._scale
        y, result = bottom + margin, []
        for toast in reversed(store[-5:]):
            width = min(right - left - 2 * margin, max(160 * self._scale, self._measure(toast["text"]) + 28 * self._scale))
            age, remaining = now - toast["start"], toast["duration"] - (now - toast["start"])
            alpha = max(0.0, min(1.0, age / 0.2, remaining / 0.5))
            result.append((toast, (right - margin - width, y, width, height), alpha))
            y += height + 6 * self._scale
        return result


    def _draw_toasts(self, viewport):
        theme = self._theme
        for toast, rect, alpha in self._toasts(time.monotonic()):
            accent = _GPU_LOG_COLORS.get(toast["level"], theme["focus"])
            self._quad(rect, theme["tooltip"][:3] + (theme["tooltip"][3] * alpha,), viewport)
            self._quad((rect[0], rect[1], 4 * self._scale, rect[3]), accent[:3] + (alpha,), viewport, radius=0)
            self._text(toast["text"], (rect[0] + 6 * self._scale, rect[1], rect[2] - 6 * self._scale, rect[3]),
                       theme["text"][:3] + (theme["text"][3] * alpha,), viewport)


    @staticmethod
    def _rgba(value):
        color = tuple(max(0.0, min(1.0, component)) for component in value)
        return color + ((1.0,) if len(color) == 3 else ())


    def _measure(self, text):
        blf.size(self._font_id, max(1, round(self._theme["font_size"] * self._scale)))
        return blf.dimensions(self._font_id, text)[0]


    def _draw_edit(self, prefix, edit, rect, clip, color):
        before, after = edit["text"][:edit["cursor"]], edit["text"][edit["cursor"]:]
        available = rect[2] - 12 * self._scale - self._measure(prefix)
        # Keep the caret visible by hiding leading characters of long values.
        while before and self._measure(before + "|") > available:
            before = before[1:]
        if edit["select_all"] and edit["text"]:
            start = rect[0] + 6 * self._scale + self._measure(prefix)
            width = min(self._measure(before + after), max(0, rect[0] + rect[2] - start))
            self._quad((start, rect[1] + 3 * self._scale, width, max(0, rect[3] - 6 * self._scale)),
                       self._theme["focus"], clip, radius=0)
        self._text(prefix + before + "|" + after, rect, color, clip)


    def _draw_control(self, widget, clip):
        previous = self._theme
        kind = getattr(widget, "theme_kind", widget.kind)
        self._theme = self._widget_themes[(kind, id(getattr(widget, "style", None)))]
        rect = widget.rect
        try:
            if widget.kind == "custom":
                widget.draw(Painter(self, widget), clip)
                return
            selected = (
                widget.kind in ("toggle", "checkbox") and widget.value
                or widget.key == self._pressed
            )
            if selected:
                self._theme = dict(self._theme, text=self._theme["text_selected"])
                if widget.kind == "toggle":
                    self._theme["button"] = self._theme["button_pressed"]
            if widget.kind in ("button", "number", "slider", "text", "dropdown", "toggle", "imagebutton"):
                outline = self._theme["outline_selected" if selected else "outline"]
                self._quad(rect, outline, clip)
                widget.rect = self._shrink(rect, max(1, round(self._scale)))
            self._draw_builtin_control(widget, clip)
        finally:
            widget.rect = rect
            self._theme = previous


    def _draw_builtin_control(self, widget, clip):
        theme = self._theme
        enabled = widget.enabled
        text_color = theme["text"] if enabled else theme["disabled_text"]
        hovered = enabled and widget.key == self._hover
        focused = enabled and widget.key == self._focus
        editing = self._edit if self._edit and self._edit["key"] == widget.key else None
        if widget.kind == "banner":
            self._draw_banner(widget, clip, focused)
            return
        if widget.kind == "link":
            rect = widget.rect
            if focused:
                self._quad(rect, theme["focus"][:3] + (0.35,), clip)
            color = theme["focus"] if enabled else theme["disabled_text"]
            if hovered:
                color = self._mix(color, (1.0, 1.0, 1.0, 1.0), 0.35)
            self._text(widget.text or widget.url, rect, color, clip)
            underline = (rect[0] + 6 * self._scale, rect[1] + rect[3] / 2 - self._theme["font_size"] * self._scale / 2,
                         min(self._measure(widget.text or widget.url), max(0, rect[2] - 12 * self._scale)),
                         max(1, round(self._scale)))
            self._quad(underline, color, clip, radius=0)
            return
        if widget.kind == "rating":
            self._draw_rating(widget, clip, hovered, focused, text_color)
            return
        if widget.kind == "palette":
            self._draw_palette(widget, clip, hovered, focused)
            return
        if widget.kind in _GPU_ITEM_KINDS:
            self._draw_items(widget, clip, hovered, focused, editing, text_color)
            return
        color = theme["button"]
        if not enabled:
            color = theme["button_disabled"]
        elif widget.key == self._pressed and (hovered or widget.kind in _GPU_DRAG_KINDS):
            color = theme["button_pressed"]
        elif widget.key == self._hotkey:
            color = theme["button_pressed"]
        elif hovered:
            color = theme["button_hover"]
        if widget.kind == "knob":
            self._draw_knob(widget, clip, color, focused, text_color)
            return
        if widget.kind == "dial":
            self._draw_dial(widget, clip, color, focused, text_color)
            return
        if widget.kind == "xypad":
            self._draw_xypad(widget, clip, focused, text_color)
            return
        if widget.kind == "timeline":
            self._draw_timeline(widget, clip, color, focused, text_color)
            return
        if widget.kind == "textarea":
            self._draw_textarea(widget, clip, color, focused, editing, text_color)
            return
        if widget.kind == "console":
            self._draw_console(widget, clip, focused)
            return
        rect = self._control_rect(widget)
        inner = rect
        if focused:
            self._quad(rect, theme["focus"], clip)
            inner = self._shrink(rect, 2)
        self._quad(inner, color, clip)
        if widget.kind == "imagebutton":
            area = self._shrink(inner, 4 * self._scale)
            if widget.text:
                label = (area[0], area[1], area[2], min(self._line(compact=True), area[3]))
                area = (area[0], area[1] + label[3], area[2], max(0, area[3] - label[3]))
                self._text(widget.text, label, text_color, clip, centered=True)
            self._draw_image(widget, self._intersection(clip, area), area, placeholder=not widget.text)
            return
        fill = theme["button_pressed"] if enabled else theme["disabled_text"]
        text = widget.text
        prefix = text + ": " if text else ""
        if widget.kind == "filepath":
            self._draw_filepath(widget, clip, inner, prefix, hovered, editing, text_color)
            return
        if widget.kind == "slider":
            fraction = max(0, min(1, (widget.value - widget.minimum) / (widget.maximum - widget.minimum)))
            self._quad((inner[0], inner[1], inner[2] * fraction, inner[3]), fill, clip)
        elif widget.kind == "range":
            span = widget.maximum - widget.minimum
            positions = [inner[0] + inner[2] * max(0, min(1, (value - widget.minimum) / span)) for value in widget.value]
            self._quad((positions[0], inner[1], max(0, positions[1] - positions[0]), inner[3]), fill, clip, radius=0)
            handle = 4 * self._scale
            active = self._handles.get(widget.key, 0)
            for index, position in enumerate(positions):
                handle_color = theme["focus"] if focused and index == active else text_color
                self._quad((position - handle / 2, inner[1], handle, inner[3]), handle_color, clip, radius=0)
            low, high = widget.value
            self._text(prefix + format(low, ".6g") + " - " + format(high, ".6g"), rect, text_color, clip, centered=True)
            return
        if editing:
            self._draw_edit(prefix, editing, inner, clip, text_color)
            return
        if widget.kind == "text":
            if widget.value:
                self._text(prefix + widget.value, inner, text_color, clip)
            else:
                self._text(prefix + widget.placeholder, inner, theme["disabled_text"], clip)
            return
        if widget.kind == "color":
            margin = 4 * self._scale
            swatch = self._shrink(inner, margin)
            if text:
                split = inner[0] + inner[2] * 0.45
                self._text(text, (inner[0], inner[1], inner[2] * 0.45, inner[3]), text_color, clip)
                swatch = (split, swatch[1], max(0, inner[0] + inner[2] - margin - split), swatch[3])
            self._quad(swatch, self._rgba(widget.value), clip)
            return
        if widget.kind == "collapsible":
            self._text(("v " if widget.value else "> ") + text, inner, text_color, clip)
            return
        if widget.kind == "checkbox":
            text = ("[x] " if widget.value else "[ ] ") + text
        elif widget.kind == "toggle":
            text += ": " + ("ON" if widget.value else "OFF")
        elif widget.kind in ("number", "slider"):
            text = prefix + format(widget.value, ".6g")
        elif widget.kind in ("dropdown", "search"):
            text = prefix + dict(widget.items)[widget.value] + " v"
        elif widget.kind == "hotkey":
            text = prefix + ("Press a key..." if self._hotkey == widget.key else widget.value or "None")
        self._icon_text(widget.icon, text, rect, text_color, clip, centered=True)


    def _draw_items(self, widget, clip, hovered, focused, editing, text_color):
        theme = self._theme
        enabled = widget.enabled
        rect = self._control_rect(widget)
        x, y, width, height = widget.rect
        if widget.kind == "radio" and widget.text:
            self._text(widget.text, (x, y + height - self._line(), width, self._line()), theme["label_text"], clip)
        if widget.kind in ("vector", "segmented", "flags") and widget.text:
            self._text(widget.text, (x, y, width * 0.3, height), theme["label_text"], clip)
        if widget.kind in _GPU_LIST_KINDS:
            self._quad(rect, theme["popup"], clip)
        if widget.kind == "table":
            row = self._line(compact=True)
            header = (rect[0], rect[1] + rect[3] - row, rect[2], row)
            self._quad(header, theme["button"], clip, radius=0)
            for column, label in enumerate(widget.headers):
                self._text(label, self._table_cell(widget, header, column), theme["label_text"],
                           self._intersection(clip, self._table_cell(widget, header, column)))
        hovered_item = self._item_at(widget, *self._mouse) if hovered and self._mouse is not None else None
        if widget.kind in ("vector", "flags", "multilist", "reorder"):
            active = self._components.get(widget.key, 0)
        elif widget.kind == "stepper":
            active = 1
        elif widget.kind in ("tabs", "table"):
            active = widget.value
        else:
            identifiers = [item[0] for item in widget.items]
            active = identifiers.index(widget.value) if widget.value in identifiers else None
        for index, cell in self._item_cells(widget):
            selected = index == active and widget.kind not in ("vector", "stepper", "flags", "multilist")
            if widget.kind in ("flags", "multilist"):
                selected = widget.items[index][0] in widget.value
            if not enabled:
                color = theme["button"] if selected and widget.kind in _GPU_LIST_KINDS else theme["button_disabled"]
            elif index == hovered_item:
                color = theme["button_pressed"] if (
                    self._pressed == widget.key and self._pressed_item == index
                ) else theme["button_hover"]
            elif selected:
                color = theme["button_pressed"]
            else:
                color = None if widget.kind in _GPU_LIST_KINDS else theme["button"]
            inner = cell
            if focused and index == active:
                self._quad(cell, theme["focus"], clip)
                inner = self._shrink(cell, 2)
            if color is not None:
                self._quad(inner, color, clip)
            if widget.kind == "stepper":
                if index != 1:
                    self._text("-" if index == 0 else "+", cell, text_color, clip, centered=True)
                elif editing:
                    self._draw_edit(widget.text + ": " if widget.text else "", editing, inner, clip, text_color)
                else:
                    label = (widget.text + ": " if widget.text else "") + format(widget.value, ".6g")
                    self._text(label, cell, text_color, clip, centered=True)
            elif widget.kind == "vector":
                label = "XYZW"[index] + ": "
                if editing and editing["index"] == index:
                    self._draw_edit(label, editing, inner, clip, text_color)
                else:
                    self._text(label + format(widget.value[index], ".6g"), cell, text_color, clip, centered=True)
            elif widget.kind == "radio":
                self._text(("(o) " if selected else "( ) ") + widget.items[index][1], cell, text_color, clip,
                           centered=widget.horizontal)
            elif widget.kind == "list":
                self._text(widget.items[index][1], cell, text_color, clip)
            elif widget.kind == "reorder":
                dragged = self._reorder is not None and self._reorder["key"] == widget.key and \
                    self._reorder["moved"] and self._reorder["from"] == index
                self._icon_text("MENU", widget.items[index][1], cell,
                                theme["disabled_text"] if dragged else text_color, clip)
            elif widget.kind == "multilist":
                side = min(14 * self._scale, max(0, cell[3] - 8 * self._scale))
                box = (cell[0] + 6 * self._scale, cell[1] + (cell[3] - side) / 2, side, side)
                self._quad(box, theme["button"], clip)
                if selected:
                    self._draw_icon("CHECKMARK", self._shrink(box, 2 * self._scale), text_color, clip)
                start = cell[0] + self._line(compact=True) - 6 * self._scale
                self._text(widget.items[index][1], (start, cell[1], max(0, cell[0] + cell[2] - start), cell[3]),
                           text_color, clip)
            elif widget.kind == "table":
                for column, label in enumerate(widget.cells[index]):
                    target = self._table_cell(widget, cell, column)
                    self._text(label, target, text_color, self._intersection(clip, target))
            elif widget.kind == "tree":
                depth, children, expanded = widget.meta[index]
                indent = depth * 14 * self._scale
                arrow = ("v " if expanded else "> ") if children else "   "
                self._text(arrow + widget.items[index][1], (cell[0] + indent, cell[1], max(0, cell[2] - indent), cell[3]),
                           text_color, clip)
            else:
                self._text(widget.items[index][1], cell, text_color, clip, centered=True)
        state = self._reorder
        if widget.kind == "reorder" and state is not None and state["key"] == widget.key and state["moved"]:
            target = dict(self._item_cells(widget)).get(state["to"])
            if target is not None:
                edge = target[1] + target[3] if state["to"] < state["from"] else target[1]
                self._quad((target[0], edge - self._scale, target[2], 2 * self._scale), theme["focus"], clip, radius=0)
        if widget.kind in _GPU_LIST_KINDS:
            row = self._line(compact=True)
            if not widget.items:
                top = rect[1] + rect[3] - row * (2 if widget.kind == "table" else 1)
                self._text("(empty)", (rect[0], top, rect[2], row), theme["disabled_text"], clip)
            elif len(widget.items) > widget.rows:
                bar = 4 * self._scale
                area = rect[3] - (row if widget.kind == "table" else 0)
                thumb = area * widget.rows / len(widget.items)
                offset = area * widget.scroll / len(widget.items)
                self._quad((rect[0] + rect[2] - bar, rect[1] + area - offset - thumb, bar, thumb),
                           theme["disabled_text"], clip, radius=0)


    @staticmethod
    def _entry_label(widget, entry):
        if isinstance(entry, str):
            return entry
        label = getattr(entry, widget.attribute, None) if widget.attribute else None
        return str(entry if label is None else label)


    def _select_row(self, widget, index):
        self._components[widget.key] = index
        if widget.selected_set is not None:
            with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
                if widget.selected_get(bpy.context) != index:
                    widget.selected_set(bpy.context, index)
        self._area.tag_redraw()


    def _move_row(self, widget, source, target, event):
        order = list(widget.value)
        order.insert(target, order.pop(source))
        self._write(widget, order, event)
        if not self._closed and self._alive():
            self._select_row(widget, target)


    def _reveal_row(self, widget, index):
        start = self._scroll.get(widget.key, 0)
        self._scroll[widget.key] = index if index < start else (
            index - widget.rows + 1 if index >= start + widget.rows else start)
        widget.scroll = self._scroll[widget.key]


    def _reorder_drag(self, y):
        state = self._reorder
        widget = self._buttons.get(state["key"])
        if widget is None or not widget.items:
            self._reorder = None
            return
        if abs(y - state["y"]) > 4 * self._scale:
            state["moved"] = True
        if not state["moved"]:
            return
        bottom, height = self._control_rect(widget)[1], self._control_rect(widget)[3]
        top = bottom + height
        # Dragging past either end scrolls the list one row per mouse move.
        if y > top and widget.scroll > 0:
            self._reveal_row(widget, widget.scroll - 1)
        elif y < bottom and widget.scroll + widget.rows < len(widget.items):
            self._reveal_row(widget, widget.scroll + widget.rows)
        row = self._line(compact=True)
        index = widget.scroll + int(max(0, min(height - 1, top - y)) // row)
        state["to"] = max(0, min(len(widget.items) - 1, index))


    def _table_cell(self, widget, row, column):
        width = row[2] / widget.columns_count
        return (row[0] + column * width, row[1], width, row[3])


    def _draw_confirm(self, viewport):
        theme = self._theme
        widget = self._buttons[self._confirm]
        geometry = self._confirm_geometry()
        self._quad(viewport, (0, 0, 0, 0.35), viewport, radius=0)
        self._quad(geometry["rect"], theme["popup"], viewport)
        self._text(widget.message or widget.text, geometry["message"], theme["text"], viewport, centered=True)
        for name, label in (("ok", "OK"), ("cancel", "Cancel")):
            rect = geometry[name]
            hovered = self._mouse is not None and self._contains(rect, *self._mouse)
            self._quad(rect, theme["button_hover"] if hovered else theme["button"], viewport)
            self._text(label, rect, theme["text"], viewport, centered=True)


    def _draw_textarea(self, widget, clip, color, focused, editing, text_color):
        theme = self._theme
        x, y, width, height = widget.rect
        if widget.text:
            label = self._line()
            self._text(widget.text, (x, y + height - label, width, label), theme["label_text"], clip)
            height -= label
        box = (x, y, width, height)
        if focused:
            self._quad(box, theme["focus"], clip)
            box = self._shrink(box, 2)
        self._quad(box, theme["popup"] if editing else color, clip)
        inner = self._intersection(clip, box)
        row = self._line(compact=True)
        top = y + height - 4 * self._scale
        text = editing["text"] if editing else widget.value
        lines = text.split("\n")
        if editing:
            if editing["select_all"] and text:
                self._quad(self._shrink(box, 2 * self._scale), theme["focus"], inner, radius=0)
            caret_line = text.count("\n", 0, editing["cursor"])
            caret_column = editing["cursor"] - (text.rfind("\n", 0, editing["cursor"]) + 1)
        elif not text and widget.placeholder:
            self._text(widget.placeholder, (x, top - row, width, row), theme["disabled_text"], inner)
            return
        for offset in range(widget.rows):
            index = widget.scroll + offset
            if index >= len(lines):
                break
            line_rect = (x, top - (offset + 1) * row, width, row)
            if editing and index == caret_line:
                self._draw_edit("", {"text": lines[index], "cursor": caret_column, "select_all": False},
                                line_rect, inner, text_color)
            else:
                self._text(lines[index], line_rect, text_color, inner)
        if len(lines) > widget.rows:
            bar = 4 * self._scale
            area = height - 8 * self._scale
            thumb = area * widget.rows / len(lines)
            self._quad((x + width - bar - 2 * self._scale, top - area * widget.scroll / len(lines) - thumb, bar, thumb),
                       theme["disabled_text"], inner, radius=0)


    def _draw_knob(self, widget, clip, color, focused, text_color):
        theme = self._theme
        scale = self._scale
        x, y, width, height = widget.rect
        size = max(0, min(height, width) - 4 * scale)
        dial = (x + 2 * scale, y + (height - size) / 2, size, size)
        if focused:
            self._quad(self._shrink(dial, -2 * scale), theme["focus"], clip, radius=size)
        self._quad(dial, color, clip, radius=size / 2)
        center = (dial[0] + size / 2, dial[1] + size / 2)
        radius = max(0, size / 2 - 4 * scale)
        fraction = max(0, min(1, (widget.value - widget.minimum) / (widget.maximum - widget.minimum)))
        fill = theme["button_pressed"] if widget.enabled else theme["disabled_text"]

        def point(amount, distance):
            angle = math.radians(225 - 270 * amount)
            return (center[0] + distance * math.cos(angle), center[1] + distance * math.sin(angle))

        segments = 24
        for step in range(segments):
            start, end = step / segments, (step + 1) / segments
            if start < fraction < end:
                self._segment(point(start, radius), point(fraction, radius), 3 * scale, fill, clip)
                self._segment(point(fraction, radius), point(end, radius), 3 * scale, theme["button_disabled"], clip)
            else:
                track = fill if end <= fraction else theme["button_disabled"]
                self._segment(point(start, radius), point(end, radius), 3 * scale, track, clip)
        self._segment(point(fraction, radius * 0.3), point(fraction, radius), 2 * scale, text_color, clip)
        label = (widget.text + ": " if widget.text else "") + format(widget.value, ".6g")
        self._text(label, (dial[0] + size + 6 * scale, y, max(0, width - size - 8 * scale), height), text_color, clip)


    def _draw_icon(self, name, rect, color, clip):
        shapes = _GPU_ICONS.get(name)
        if not shapes:
            return
        size = max(0, min(rect[2], rect[3]))
        left, bottom = rect[0] + (rect[2] - size) / 2, rect[1] + (rect[3] - size) / 2
        thickness = max(1.0, size * 0.1)

        def point(px, py):
            return (left + px * size, bottom + py * size)

        for shape in shapes:
            tag = shape[0]
            if tag == "l":
                self._segment(point(*shape[1:3]), point(*shape[3:5]), thickness, color, clip)
            elif tag in ("s", "o"):
                points = [point(*item) for item in shape[1]]
                if tag == "o":
                    points.append(points[0])
                for start, end in zip(points, points[1:]):
                    self._segment(start, end, thickness, color, clip)
            elif tag == "b":
                box_x, box_y, box_width, box_height = shape[1:]
                self._quad((left + box_x * size, bottom + box_y * size, box_width * size, box_height * size),
                           color, clip, radius=0)
            elif tag == "p":
                self._poly([point(*item) for item in shape[1]], color, clip)
            elif tag == "c":
                center_x, center_y, radius = shape[1:]
                self._quad((left + (center_x - radius) * size, bottom + (center_y - radius) * size,
                            2 * radius * size, 2 * radius * size), color, clip, radius=radius * size)


    def _icon_text(self, icon, text, rect, color, clip, centered=False):
        """Draw an optional vector icon followed by text; without an icon this is plain _text."""
        if not icon:
            self._text(text, rect, color, clip, centered=centered)
            return
        scale = self._scale
        size = max(0, min(16 * scale, rect[3] - 8 * scale))
        gap = 4 * scale if text else 0
        width = size + gap + (self._measure(text) if text else 0)
        left = rect[0] + (max(4 * scale, (rect[2] - width) / 2) if centered else 6 * scale)
        self._draw_icon(icon, (left, rect[1] + (rect[3] - size) / 2, size, size), color,
                        self._intersection(clip, rect))
        if text:
            # _text indents left-aligned text by 6 px, so start that much before the text position.
            start = left + size + gap - 6 * scale
            self._text(text, (start, rect[1], max(0, rect[0] + rect[2] - start), rect[3]), color, clip)


    def _dial_geometry(self, widget):
        x, y, width, height = widget.rect
        size = max(0, min(height, width) - 4 * self._scale)
        dial = (x + 2 * self._scale, y + (height - size) / 2, size, size)
        return (dial[0] + size / 2, dial[1] + size / 2), size, dial


    def _draw_dial(self, widget, clip, color, focused, text_color):
        theme, scale = self._theme, self._scale
        x, y, width, height = widget.rect
        center, size, dial = self._dial_geometry(widget)
        if focused:
            self._quad(self._shrink(dial, -2 * scale), theme["focus"], clip, radius=size)
        self._quad(dial, color, clip, radius=size / 2)
        radius = max(0, size / 2 - 4 * scale)
        for index in range(12):
            angle = index * math.pi / 6
            inner = radius * (0.62 if index % 3 == 0 else 0.8)
            self._segment((center[0] + inner * math.cos(angle), center[1] + inner * math.sin(angle)),
                          (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)),
                          max(1, scale), theme["disabled_text"], clip)
        needle = theme["focus"] if widget.enabled else theme["disabled_text"]
        tip = (center[0] + radius * math.cos(widget.value), center[1] + radius * math.sin(widget.value))
        self._segment(center, tip, 2 * scale, needle, clip)
        dot = 3 * scale
        self._quad((center[0] - dot, center[1] - dot, 2 * dot, 2 * dot), needle, clip, radius=dot)
        label = (widget.text + ": " if widget.text else "") + format(math.degrees(widget.value), ".1f") + "\u00b0"
        self._text(label, (dial[0] + size + 6 * scale, y, max(0, width - size - 8 * scale), height), text_color, clip)


    def _pad_rect(self, widget):
        x, y, width, height = widget.rect
        label = self._line(compact=True) if widget.text else 0
        return self._shrink((x, y, width, max(0, height - label)), 2 * self._scale)


    def _draw_xypad(self, widget, clip, focused, text_color):
        theme, scale = self._theme, self._scale
        rect = widget.rect
        pad = self._pad_rect(widget)
        x_value, y_value = widget.value
        if widget.text:
            line = self._line(compact=True)
            self._text(widget.text, (rect[0], rect[1] + rect[3] - line, rect[2], line), text_color, clip)
        if focused:
            self._quad(self._shrink(pad, -2 * scale), theme["focus"], clip)
        self._quad(pad, theme["popup"] if widget.enabled else theme["button_disabled"], clip)
        thickness = max(1, round(scale))
        grid = theme["disabled_text"][:3] + (0.35,)
        for index in (1, 2, 3):
            self._quad((pad[0] + pad[2] * index / 4, pad[1], thickness, pad[3]), grid, clip, radius=0)
            self._quad((pad[0], pad[1] + pad[3] * index / 4, pad[2], thickness), grid, clip, radius=0)
        span = widget.maximum - widget.minimum
        px = pad[0] + pad[2] * max(0, min(1, (x_value - widget.minimum) / span))
        py = pad[1] + pad[3] * max(0, min(1, (y_value - widget.minimum) / span))
        accent = theme["focus"] if widget.enabled else theme["disabled_text"]
        self._quad((pad[0], py - thickness / 2, pad[2], thickness), accent[:3] + (0.6,), clip, radius=0)
        self._quad((px - thickness / 2, pad[1], thickness, pad[3]), accent[:3] + (0.6,), clip, radius=0)
        dot = 5 * scale
        self._quad((px - dot, py - dot, 2 * dot, 2 * dot), accent, clip, radius=dot)
        line = self._line(compact=True)
        self._text(format(x_value, ".3g") + ", " + format(y_value, ".3g"), (pad[0], pad[1], pad[2], line),
                   theme["disabled_text"], self._intersection(clip, pad), right=True, size=0.85)


    def _timeline_track(self, widget):
        x, y, width, height = widget.rect
        margin = 10 * self._scale
        return (x + margin, y, max(1, width - 2 * margin), height)


    def _draw_timeline(self, widget, clip, color, focused, text_color):
        theme, scale = self._theme, self._scale
        rect = widget.rect
        inner = rect
        if focused:
            self._quad(rect, theme["focus"], clip)
            inner = self._shrink(rect, 2)
        self._quad(inner, color, clip)
        track = self._timeline_track(widget)
        line = self._line(compact=True)
        low, high = widget.minimum, widget.maximum
        span = high - low
        # Label ticks at the smallest 1/2/5 interval whose labels do not overlap.
        label_width = max(self._measure(format(low, ".6g")), self._measure(format(high, ".6g"))) + 16 * scale
        candidates = [factor * 10 ** exponent * widget.step for exponent in range(9) for factor in (1, 2, 5)]
        interval = next((candidate for candidate in candidates if track[2] * candidate / span >= label_width), span)
        thickness = max(1, round(scale))
        tick = text_color[:3] + (0.45,)
        value = math.ceil(low / interval) * interval
        for _index in range(1000):
            if value > high + 1e-9:
                break
            px = track[0] + track[2] * (value - low) / span
            self._quad((px - thickness / 2, inner[1], thickness, inner[3] - line * 0.6), tick, clip, radius=0)
            self._text(format(value, ".6g"), (px - label_width / 2, inner[1] + inner[3] - line, label_width, line),
                       text_color, clip, centered=True, size=0.85)
            value += interval
        if widget.text:
            self._text(widget.text, (inner[0], inner[1], inner[2], inner[3] - line), theme["disabled_text"], clip)
        head = theme["focus"] if widget.enabled else theme["disabled_text"]
        px = track[0] + track[2] * max(0, min(1, (widget.value - low) / span))
        self._quad((px - thickness, inner[1], 2 * thickness, inner[3]), head, clip, radius=0)
        number = format(widget.value, ".6g")
        box_width = self._measure(number) + 12 * scale
        box_x = max(inner[0], min(px - box_width / 2, inner[0] + inner[2] - box_width))
        box = (box_x, inner[1] + 3 * scale, box_width, max(0, inner[3] - line - 6 * scale))
        self._quad(box, head, clip)
        self._text(number, box, theme["text"], clip, centered=True)


    def _browse_rect(self, widget):
        x, y, width, height = widget.rect
        side = min(height, width / 3)
        return (x + width - side, y, side, height)


    @staticmethod
    def _path_exists(value):
        try:
            path = bpy.path.abspath(value)
        except (AttributeError, TypeError, ValueError):
            path = value
        return os.path.exists(path)


    def _draw_filepath(self, widget, clip, inner, prefix, hovered, editing, text_color):
        theme, scale = self._theme, self._scale
        browse = self._browse_rect(widget)
        field = (inner[0], inner[1], max(0, browse[0] - inner[0]), inner[3])
        over = hovered and self._mouse is not None and self._contains(browse, *self._mouse)
        button = self._shrink(browse, 2 * scale)
        self._quad(button, theme["button_hover"] if over else theme["popup"], clip)
        icon = min(button[2], button[3]) * 0.6
        self._draw_icon("FOLDER", (button[0] + (button[2] - icon) / 2, button[1] + (button[3] - icon) / 2, icon, icon),
                        text_color, clip)
        if editing:
            self._draw_edit(prefix, editing, field, clip, text_color)
            return
        if not widget.value:
            self._text(prefix + widget.placeholder, field, theme["disabled_text"], clip)
            return
        shown = widget.value
        available = field[2] - 12 * scale - self._measure(prefix)
        if self._measure(shown) > available:
            # Long paths keep their file name visible by eliding the start.
            while len(shown) > 1 and self._measure("..." + shown) > available:
                shown = shown[1:]
            shown = "..." + shown
        missing = widget.enabled and not self._path_exists(widget.value)
        self._text(prefix + shown, field, _GPU_LOG_COLORS["WARNING"] if missing else text_color, clip)


    def _browse(self, widget):
        operator = getattr(type(self), "browse_operator", "")
        if not operator:
            self.report({"WARNING"}, "GPU File Path browsing requires a GPU UI Root overlay.")
            return
        _shared["_gpu_browse_pending"] = {
            "overlay": self, "key": widget.key, "folder": widget.folder, "pattern": widget.pattern,
            "relative": widget.relative, "value": widget.value,
        }
        category, name = operator.split(".", 1)
        with bpy.context.temp_override(window=self._window, area=self._area, region=self._region):
            getattr(getattr(bpy.ops, category), name)("INVOKE_DEFAULT")


    def _browse_done(self, key, path):
        if self._closed or not self._alive():
            return
        widget = self._buttons.get(key)
        if widget is None or widget.kind != "filepath" or not widget.enabled:
            return
        self._write(widget, path, None)


    def _draw_spinner(self, widget, clip):
        theme = self._theme
        x, y, width, height = widget.rect
        size = min(height, width)
        center = (x + size / 2, y + height / 2)
        radius = max(0, size / 2 - 5 * self._scale)
        dot = max(2, 3 * self._scale)
        phase = int(time.monotonic() * 10) % 8
        red, green, blue, alpha = theme["focus"]
        for index in range(8):
            angle = math.pi / 2 - index * math.pi / 4
            strength = 1 - ((phase - index) % 8) / 8 if widget.active else 0.3
            self._quad((center[0] + radius * math.cos(angle) - dot / 2, center[1] + radius * math.sin(angle) - dot / 2,
                        dot, dot), (red, green, blue, alpha * strength), clip, radius=dot / 2)
        self._text(widget.text, (x + size, y, max(0, width - size), height), theme["label_text"], clip)


    def _draw_graph(self, widget, clip):
        theme = self._theme
        rect = widget.rect
        self._quad(rect, theme["popup"], clip)
        if widget.text:
            line = self._line(compact=True)
            self._text(widget.text, (rect[0], rect[1] + rect[3] - line, rect[2], line), theme["label_text"], clip)
        values = widget.value
        if not values:
            self._text("(no data)", rect, theme["disabled_text"], clip, centered=True)
            return
        if widget.auto:
            low, high = min(values), max(values)
            if high - low <= 0:
                low, high = low - 0.5, high + 0.5
        else:
            low, high = widget.minimum, widget.maximum
        inner = self._shrink(rect, 4 * self._scale)
        last = max(1, len(values) - 1)
        points = [(inner[0] + inner[2] * (index / last if len(values) > 1 else 0.5),
                   inner[1] + inner[3] * max(0, min(1, (value - low) / (high - low))))
                  for index, value in enumerate(values)]
        thickness = 2 * self._scale
        for start, end in zip(points, points[1:]):
            self._segment(start, end, thickness, theme["focus"], clip)
        if len(points) == 1:
            px, py = points[0]
            self._quad((px - thickness, py - thickness, 2 * thickness, 2 * thickness), theme["focus"], clip,
                       radius=thickness)


    def _draw_image(self, widget, clip, rect=None, placeholder=True):
        theme = self._theme
        rect = widget.rect if rect is None else rect
        texture = None
        if widget.image is not None:
            try:
                texture = gpu.texture.from_image(widget.image)
            except (RuntimeError, ValueError, TypeError, ReferenceError):
                texture = None
        if texture is None or texture.width <= 0 or texture.height <= 0 or rect[2] <= 0 or rect[3] <= 0:
            if not placeholder:
                return
            self._quad(rect, theme["button_disabled"], clip)
            message = "No image" if widget.image is None else "Image unavailable"
            self._text(message, rect, theme["disabled_text"], clip, centered=True)
            return
        fit = min(rect[2] / texture.width, rect[3] / texture.height)
        width, height = texture.width * fit, texture.height * fit
        target = (rect[0] + (rect[2] - width) / 2, rect[1] + (rect[3] - height) / 2, width, height)
        left, bottom, visible_width, visible_height = self._intersection(target, clip)
        if visible_width <= 0 or visible_height <= 0:
            return
        # Crop texture coordinates with the quad so images respect scroll and panel clipping.
        u0 = (left - target[0]) / width
        v0 = (bottom - target[1]) / height
        u1 = u0 + visible_width / width
        v1 = v0 + visible_height / height
        if self._image_shader is None:
            try:
                self._image_shader = gpu.shader.from_builtin("IMAGE_SCENE_LINEAR_TO_REC709_SRGB")
            except ValueError:
                self._image_shader = gpu.shader.from_builtin("IMAGE")
        shader = self._image_shader
        right, top = left + visible_width, bottom + visible_height
        batch = batch_for_shader(
            shader, "TRIS",
            {"pos": ((left, bottom), (right, bottom), (right, top), (left, top)),
             "texCoord": ((u0, v0), (u1, v0), (u1, v1), (u0, v1))},
            indices=((0, 1, 2), (0, 2, 3)),
        )
        shader.bind()
        shader.uniform_sampler("image", texture)
        batch.draw(shader)


    def _picker_geometry(self, widget):
        line = self._line()
        pad = 6 * self._scale
        channels = len(widget.value)
        width = min(max(widget.rect[2], 220 * self._scale), self._region.width)
        height = min((channels + 1) * line + 2 * pad, self._region.height)
        x = max(0, min(widget.rect[0], self._region.width - width))
        y = widget.rect[1] - height
        if y < 0:
            y = widget.rect[1] + widget.rect[3]
        y = max(0, min(y, self._region.height - height))
        top = y + height - pad
        preview = (x + pad, top - line, width - 2 * pad, line)
        label = 24 * self._scale
        rows = []
        for index in range(channels):
            row = (x + pad, top - (index + 2) * line, width - 2 * pad, line)
            bar = (row[0] + label, row[1] + 4 * self._scale, max(1, row[2] - label), max(1, row[3] - 8 * self._scale))
            rows.append((row, bar))
        return (x, y, width, height), preview, rows


    @staticmethod
    def _picker_channels(picker):
        return list(picker["hsv"]) + ([picker["alpha"]] if picker["alpha"] is not None else [])


    @staticmethod
    def _picker_color(picker):
        rgb = tuple(colorsys.hsv_to_rgb(*picker["hsv"]))
        return rgb + ((picker["alpha"],) if picker["alpha"] is not None else ())


    def _draw_picker(self, viewport):
        theme = self._theme
        picker = self._picker
        rect, preview, rows = self._picker_geometry(self._buttons[picker["key"]])
        self._quad(rect, theme["popup"], viewport)
        half = preview[2] / 2
        inset = 2 * self._scale
        self._quad((preview[0], preview[1] + inset, half, preview[3] - 2 * inset),
                   self._rgba(picker["initial"]), viewport)
        self._quad((preview[0] + half, preview[1] + inset, half, preview[3] - 2 * inset),
                   self._rgba(self._picker_color(picker)), viewport)
        hue, saturation, value = picker["hsv"]
        channels = self._picker_channels(picker)
        segments = 24
        for index, (row, bar) in enumerate(rows):
            label_color = theme["focus"] if index == picker["channel"] else theme["text"]
            self._text("HSVA"[index], (row[0], row[1], bar[0] - row[0], row[3]), label_color, viewport)
            for step in range(segments):
                fraction = (step + 0.5) / segments
                if index == 0:
                    color = colorsys.hsv_to_rgb(fraction, 1, 1)
                elif index == 1:
                    color = colorsys.hsv_to_rgb(hue, fraction, value)
                elif index == 2:
                    color = colorsys.hsv_to_rgb(hue, saturation, fraction)
                else:
                    color = colorsys.hsv_to_rgb(hue, saturation, value)
                segment = (bar[0] + bar[2] * step / segments, bar[1], bar[2] / segments, bar[3])
                self._quad(segment, tuple(color) + (fraction if index == 3 else 1.0,), viewport, radius=0)
            marker = bar[0] + bar[2] * channels[index]
            self._quad((marker - 1.5 * self._scale, bar[1] - inset, 3 * self._scale, bar[3] + 2 * inset),
                       theme["text"], viewport, radius=0)


    def _popup_items(self, widget):
        if widget.kind != "search" or not self._query:
            return widget.items
        query = self._query.lower()
        return [item for item in widget.items if query in item[1].lower() or query in item[0].lower()]


    def _popup_geometry(self, widget):
        """Return the item list rectangle; search popups add a query row directly above it."""
        height = max(28, self._theme["font_size"] + 12) * self._scale
        items = self._popup_items(widget)
        header = 1 if widget.kind == "search" else 0
        count = min(max(len(items), header), max(1, min(8, int(self._region.height / height) - header)))
        width = min(max(widget.rect[2], 180 * self._scale), self._region.width)
        total_height = min((count + header) * height, self._region.height)
        x = max(0, min(widget.rect[0], self._region.width - width))
        y = max(0, min(widget.rect[1] - total_height, self._region.height - total_height))
        self._popup_offset = max(0, min(self._popup_offset, len(items) - count))
        return (x, y, width, max(0, total_height - header * height)), height, count
