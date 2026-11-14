"""Extracted arrangement implementation for the Blender host."""

from ._support import (
    _GPU_ITEM_KINDS, _GPU_LIST_KINDS, _GPU_REGION_TOGGLES, time,
)

class Arrangement:
    def _layout_clips(self, widget, clip):
        widget.clip = self._intersection(widget.rect, clip)
        if widget.kind == "panel":
            clip = self._intersection(clip, (
                widget.rect[0], widget.rect[1], widget.rect[2],
                max(0, widget.rect[3] - self._header(widget)[3]),
            ))
            status = sum(child.height for child in widget.children if child.kind == "statusbar")
            content = self._intersection(clip, (widget.rect[0], widget.rect[1] + status, widget.rect[2],
                                                max(0, widget.rect[3] - status)))
            for child in widget.visible_children():
                self._layout_clips(child, clip if child.kind == "statusbar" else content)
            return
        if widget.kind == "scroll":
            clip = widget.clip
            self._scroll[widget.key] = widget.scroll
        for child in widget.visible_children():
            self._layout_clips(child, clip)


    def _bounds(self):
        """Free part of the region as (left, bottom, right, top).

        With Region Overlap the sidebar, toolbar, headers and asset shelf are drawn on top of the
        WINDOW region, so panels are kept out of them and their mouse events are left to Blender.
        """
        region = self._region
        width, height = region.width, region.height
        left, bottom, right, top = 0, 0, width, height
        space = getattr(self._area.spaces, "active", None) if hasattr(self._area, "spaces") else None
        for other in self._area.regions[:]:
            kind = getattr(other, "type", "")
            if other == region or kind in ("WINDOW", "HUD") or other.width <= 1 or other.height <= 1:
                continue
            shown = _GPU_REGION_TOGGLES.get(kind)
            if shown and space is not None and not getattr(space, shown, True):
                continue
            x0 = max(other.x, region.x) - region.x
            x1 = min(other.x + other.width, region.x + width) - region.x
            y0 = max(other.y, region.y) - region.y
            y1 = min(other.y + other.height, region.y + height) - region.y
            if x1 <= x0 or y1 <= y0:
                continue
            if x0 <= 0 and x1 >= width:
                if (y0 + y1) / 2 > height / 2:
                    top = min(top, y0)
                else:
                    bottom = max(bottom, y1)
            elif (x0 + x1) / 2 > width / 2:
                right = min(right, x0)
            else:
                left = max(left, x1)
        if right - left < 1 or top - bottom < 1:
            return (0, 0, width, height)
        return (left, bottom, right, top)


    def _arrange(self):
        left, bottom, right, top = self._bounds()
        # Dock bars reserve their edge first, so windows, drawers and HUDs fit in the space that remains.
        ordered = sorted(self._panels, key=lambda panel: panel.mode != "dock")
        for panel in ordered:
            free_width, free_height = right - left, top - bottom
            if panel.mode == "attached":
                panel.screen = self._project(panel.anchor)
            # Flows wrap by the width they get, which is only known after arranging; re-measure until stable.
            for _attempt in range(3):
                self._arrange_panel(panel, left, bottom, right, top, free_width, free_height)
                stale = [widget for widget in self._walk(panel)
                         if widget.kind in ("flow", "richtext") and widget.flow_width != widget.arranged_width]
                if not stale:
                    break
                for widget in stale:
                    widget.flow_width = self._flow_widths[widget.key] = widget.arranged_width
            if not self._folded(panel) and panel.rect[2] > 0:
                panel.pin_status(panel.rect[0], panel.rect[1], panel.rect[2], self._scale, self._theme["font_size"])
            self._layout_clips(panel, panel.rect)
            if panel.mode == "dock":
                if panel.side == "BOTTOM":
                    bottom = min(top - 1, panel.rect[1] + panel.rect[3])
                else:
                    top = max(bottom + 1, panel.rect[1])
        self._update_hover()


    def _folded(self, panel):
        """Minimized windows and collapsed drawers/docks only show their title bar or tab."""
        if panel.mode in ("drawer", "dock"):
            return panel.collapsible and panel.key in self._collapsed
        return panel.key in self._minimized


    @staticmethod
    def _anchor_world(target):
        if target is None:
            return None
        matrix = getattr(target, "matrix_world", None)
        if matrix is None:
            return tuple(target)
        visible = getattr(target, "visible_get", None)
        if callable(visible):
            try:
                if not visible():
                    return None
            except (RuntimeError, ReferenceError):
                return None
        location = matrix.translation
        return (location[0], location[1], location[2])


    def _project(self, point):
        """World location to region pixels, like bpy_extras.view3d_utils.location_3d_to_region_2d."""
        matrix = getattr(getattr(self._region, "data", None), "perspective_matrix", None)
        if point is None or matrix is None:
            return None
        x, y, z = point
        clip = [matrix[row][0] * x + matrix[row][1] * y + matrix[row][2] * z + matrix[row][3] for row in range(4)]
        if clip[3] <= 1e-6:
            return None
        return ((clip[0] / clip[3] + 1) / 2 * self._region.width, (clip[1] / clip[3] + 1) / 2 * self._region.height)


    def _walk(self, widget):
        yield widget
        for child in widget.visible_children():
            yield from self._walk(child)


    def _arrange_panel(self, panel, left, bottom, right, top, free_width, free_height):
        if panel.mode != "window":
            self._arrange_anchored(panel, left, bottom, right, top, free_width, free_height)
            return
        targets = panel.stretch_targets()
        for target in targets:
            target.stretch = 0.0
        panel.measure(self._scale, self._theme["font_size"], self._measure)
        maximized = panel.key in self._maximized
        size = self._sizes.get(panel.key) if panel.resizable else None
        if maximized:
            size = (free_width, free_height)
        if size and targets and panel.key not in self._minimized:
            extra = (min(size[1], free_height) - panel.height) / len(targets)
            for target in targets:
                target.stretch = extra
            panel.measure(self._scale, self._theme["font_size"], self._measure)
        width = min(size[0] if size else panel.width * self._scale, free_width)
        height = min(size[1] if size else panel.height, free_height)
        if panel.key in self._minimized:
            height = min(max(28, self._theme["font_size"] + 12) * self._scale, free_height)
        if maximized:
            # The stored position is kept so restoring returns the panel to where it was.
            x, y = left, top - height
        else:
            x, y = self._positions.get(panel.key, (panel.x * self._scale, panel.y * self._scale))
            x = max(left, min(x, right - width))
            y = max(bottom, min(y, top - height))
            self._positions[panel.key] = (x, y)
        panel.arrange(x, y + height, width, self._scale, self._theme["font_size"])
        # Keep oversized content clipped while its header remains reachable.
        panel.rect = (x, y, width, height)


    def _arrange_anchored(self, panel, left, bottom, right, top, free_width, free_height):
        scale, font_size = self._scale, self._theme["font_size"]
        mode, folded = panel.mode, self._folded(panel)
        targets = panel.stretch_targets()
        for target in targets:
            target.stretch = 0.0
        panel.measure(scale, font_size, self._measure)
        stored = self._sizes.get(panel.key) if panel.resizable else None
        if mode == "drawer" and folded:
            width, height = min(22 * scale, free_width), min(self._line() * 3, free_height)
            x = left if panel.side == "LEFT" else right - width
            y = max(bottom, top - height - panel.y * scale)
            panel.arrange(x, y + height, width, scale, font_size)
            panel.rect = (x, y, width, height)
            return
        if mode == "drawer":
            width = min(stored[0] if stored else panel.width * scale, free_width)
            height = free_height
            x, y = (left if panel.side == "LEFT" else right - width), bottom
        elif mode == "dock":
            width = free_width
            height = min(self._line(), free_height) if folded else min(
                stored[1] if stored else max(self._line(), panel.view_height * scale), free_height * 0.8)
            x, y = left, (bottom if panel.side == "BOTTOM" else top - height)
        else:
            width, height = min(panel.width * scale, free_width), min(panel.height, free_height)
            offset_x, offset_y = panel.x * scale, panel.y * scale
            if mode == "hud":
                x = left + offset_x if panel.corner.endswith("LEFT") else right - width - offset_x
                y = top - height - offset_y if panel.corner.startswith("TOP") else bottom + offset_y
            else:
                if panel.screen is None:
                    panel.arrange(left, bottom, 0, scale, font_size)
                    panel.rect = (left, bottom, 0, 0)
                    return
                x, y = panel.screen[0] + offset_x, panel.screen[1] + offset_y
            x = max(left, min(x, right - width))
            y = max(bottom, min(y, top - height))
        if mode in ("drawer", "dock") and targets and not folded:
            extra = (height - panel.height) / len(targets)
            for target in targets:
                target.stretch = extra
            panel.measure(scale, font_size, self._measure)
        panel.arrange(x, y + height, width, scale, font_size)
        panel.rect = (x, y, width, height)


    def _edge_rect(self, panel):
        """Inner edge that resizes an open drawer (width) or dock bar (height)."""
        if panel.mode not in ("drawer", "dock") or not panel.resizable or self._folded(panel):
            return (0, 0, 0, 0)
        x, y, width, height = panel.rect
        size = min(5 * self._scale, width, height)
        return {
            "LEFT": (x + width - size, y, size, height), "RIGHT": (x, y, size, height),
            "BOTTOM": (x, y + height - size, width, size), "TOP": (x, y, width, size),
        }[panel.side]


    def _update_hover(self):
        idle = self._mouse is not None and self._popup is None and self._picker is None and self._confirm is None
        kind, widget = self._hit(*self._mouse) if idle else (None, None)
        hover = widget.key if kind == "button" else None
        window_hover = (kind, widget.key) if kind in ("close", "maximize", "minimize", "splitter", "collapse",
                                                       "edge") else None
        if window_hover != self._window_hover:
            self._window_hover = window_hover
            self._area.tag_redraw()
        if hover != self._hover:
            self._hover = hover
            self._hover_since = time.monotonic()
            self._area.tag_redraw()


    def _line(self, compact=False):
        size = self._theme["font_size"]
        return (max(24, size + 10) if compact else max(28, size + 12)) * self._scale


    def _control_rect(self, widget):
        x, y, width, height = widget.rect
        if widget.kind in ("tabs", "collapsible"):
            header_height = min(self._line(), height)
            return (x, y + height - header_height, width, header_height)
        if widget.kind in ("radio", "palette") and widget.text:
            return (x, y, width, max(0, height - self._line()))
        if widget.kind in ("vector", "rating", "segmented", "flags") and widget.text:
            return (x + width * 0.3, y, width * 0.7, height)
        return widget.rect


    def _control_clip(self, widget):
        return self._intersection(self._control_rect(widget), widget.clip)


    def _item_cells(self, widget):
        x, y, width, height = self._control_rect(widget)
        if widget.kind == "stepper":
            side = min(height, width / 3)
            return [(0, (x, y, side, height)), (1, (x + side, y, width - 2 * side, height)),
                    (2, (x + width - side, y, side, height))]
        if widget.kind in _GPU_LIST_KINDS:
            row = self._line(compact=True)
            start = widget.scroll
            header = 1 if widget.kind == "table" else 0
            return [(index, (x, y + height - (index - start + 1 + header) * row, width, row))
                    for index in range(start, min(len(widget.items), start + widget.rows))]
        if widget.kind == "rating":
            count = int(widget.maximum)
            side = min(height, width / count)
            return [(index, (x + index * side, y + (height - side) / 2, side, side)) for index in range(count)]
        if widget.kind == "palette":
            row, cell = self._line(compact=True), width / widget.columns
            return [(index, (x + index % widget.columns * cell, y + height - (index // widget.columns + 1) * row,
                             cell, row)) for index in range(len(widget.palette))]
        count = len(widget.value) if widget.kind == "vector" else len(widget.items)
        if widget.kind == "radio" and not widget.horizontal:
            row = height / count
            return [(index, (x, y + height - (index + 1) * row, width, row)) for index in range(count)]
        cell = width / count
        return [(index, (x + index * cell, y, cell, height)) for index in range(count)]


    def _item_at(self, widget, x, y):
        if widget.kind == "filepath":
            if not self._contains(self._control_clip(widget), x, y):
                return None
            return 1 if self._contains(self._browse_rect(widget), x, y) else 0
        if widget.kind not in _GPU_ITEM_KINDS or not self._contains(self._control_clip(widget), x, y):
            return None
        for index, cell in self._item_cells(widget):
            if self._contains(cell, x, y):
                return index
        return None


    def _header(self, panel):
        x, y, width, height = panel.rect
        if panel.mode == "drawer" and self._folded(panel):
            return panel.rect
        if not panel.titled:
            return (x, y + height, width, 0)
        header_height = min(max(28, self._theme["font_size"] + 12) * self._scale, height)
        return (x, y + height - header_height, width, header_height)


    def _close_rect(self, panel):
        return self._window_buttons(panel)[0][1]


    def _window_buttons(self, panel):
        """Title bar buttons from right to left, like a desktop window: close, maximize/restore, minimize."""
        x, y, width, height = self._header(panel)
        if panel.mode in ("hud", "attached") or panel.mode == "drawer" and self._folded(panel):
            return []
        kinds = (["collapse"] if panel.collapsible else []) if panel.mode in ("drawer", "dock") else \
            (["close"] if panel.closable else []) + (["maximize"] if panel.maximizable else []) + (["minimize"] if panel.minimizable else [])
        buttons, right = [], x + width
        for kind in kinds:
            size = min(height, max(0, right - x))
            buttons.append((kind, (right - size, y, size, height)))
            right -= size
        return buttons


    def _toggle_window(self, panel, kind):
        key = panel.key
        maximized = key in self._maximized
        top = panel.rect[1] + panel.rect[3]
        if kind == "minimize":
            self._maximized.discard(key)
            self._minimized.symmetric_difference_update({key})
        else:
            self._minimized.discard(key)
            self._maximized.symmetric_difference_update({key})
        self._arrange()
        if key not in self._maximized and not (maximized and kind == "maximize"):
            # Minimizing and restoring keep the title bar where it is.
            panel = next(item for item in self._panels if item.key == key)
            self._positions[key] = (self._positions[key][0], top - panel.rect[3])
            self._arrange()
        widget = self._buttons.get(self._focus)
        if widget is not None and (self._control_clip(widget)[2] <= 0 or self._control_clip(widget)[3] <= 0):
            self._focus = None


    def _toggle_collapse(self, panel):
        self._collapsed.symmetric_difference_update({panel.key})
        self._arrange()
        widget = self._buttons.get(self._focus)
        if widget is not None and (self._control_clip(widget)[2] <= 0 or self._control_clip(widget)[3] <= 0):
            self._focus = None


    def _draw_window_button(self, kind, rect, panel, clip):
        theme = self._theme
        if self._window_hover == (kind, panel.key):
            self._quad(rect, (0.77, 0.17, 0.17, 1) if kind == "close" else theme["button_hover"], clip, radius=0)
        x, y, width, height = rect
        cx, cy = x + width / 2, y + height / 2
        half = min(5 * self._scale, width / 3, height / 3)
        thick = max(1, round(self._scale))
        color = theme["text"]
        if kind == "collapse":
            # The chevron points where the panel will move: toward its edge to close, away from it to open.
            direction = {"LEFT": (-1, 0), "RIGHT": (1, 0), "BOTTOM": (0, -1), "TOP": (0, 1)}[panel.side]
            if self._folded(panel):
                direction = (-direction[0], -direction[1])
            dx, dy = direction
            tip = (cx + dx * half * 0.5, cy + dy * half * 0.5)
            for sign in (1, -1):
                tail = (tip[0] - dx * half + dy * half * sign, tip[1] - dy * half + dx * half * sign)
                self._segment(tail, tip, thick * 1.4, color, clip)
            return
        if kind == "close":
            self._segment((cx - half, cy - half), (cx + half, cy + half), thick * 1.4, color, clip)
            self._segment((cx - half, cy + half), (cx + half, cy - half), thick * 1.4, color, clip)
        elif kind == "minimize":
            self._quad((cx - half, cy - thick / 2, 2 * half, thick), color, clip, radius=0)
        else:
            def outline(left, bottom, size):
                for edge in ((left, bottom, size, thick), (left, bottom + size - thick, size, thick),
                             (left, bottom, thick, size), (left + size - thick, bottom, thick, size)):
                    self._quad(edge, color, clip, radius=0)
            if panel.key in self._maximized:
                # Restore icon: a back window peeking out above and to the right of the front one.
                small, offset = 2 * half * 0.8, 2 * half * 0.2
                back = (cx - half + offset, cy - half + offset)
                self._quad((back[0] + offset, back[1] + small - thick, small - offset, thick), color, clip, radius=0)
                self._quad((back[0] + small - thick, back[1] + offset, thick, small - offset), color, clip, radius=0)
                outline(cx - half, cy - half, small)
            else:
                outline(cx - half, cy - half, 2 * half)


    def _grip_rect(self, panel):
        size = min(14 * self._scale, panel.rect[2], panel.rect[3])
        return (panel.rect[0] + panel.rect[2] - size, panel.rect[1], size, size)


    def _grip_active(self, panel):
        return panel.mode == "window" and panel.resizable and panel.key not in self._minimized \
            and panel.key not in self._maximized


    def _hit(self, x, y):
        left, bottom, right, top = self._bounds()
        if not (left <= x < right and bottom <= y < top):
            return (None, None)
        for panel in reversed(self._panels):
            if panel.mode == "hud" or not self._contains(panel.rect, x, y):
                continue
            if panel.mode == "drawer" and self._folded(panel):
                return ("collapse", panel)
            if self._contains(self._edge_rect(panel), x, y):
                return ("edge", panel)
            for button, rect in self._window_buttons(panel):
                if self._contains(rect, x, y):
                    return (button, panel)
            if self._contains(self._header(panel), x, y):
                return ("drag", panel) if panel.mode == "window" else ("panel", panel)
            if self._grip_active(panel) and self._contains(self._grip_rect(panel), x, y):
                return ("resize", panel)

            def button_at(widget):
                if not self._contains(widget.clip, x, y):
                    return None
                if widget.kind == "splitter" and self._contains(self._intersection(self._divider(widget), widget.clip), x, y):
                    return widget
                for child in reversed(widget.visible_children()):
                    result = button_at(child)
                    if result is not None:
                        return result
                if (widget.kind in self._controls or widget.tooltip) and self._contains(self._control_clip(widget), x, y):
                    return widget
                return None

            button = button_at(panel)
            if button is not None and button.kind == "splitter":
                return ("splitter", button)
            return ("button", button) if button else ("panel", panel)
        return (None, None)


    def _divider(self, widget):
        children = widget.visible_children()
        if len(children) < 2:
            return (0, 0, 0, 0)
        left = children[0].rect
        return (left[0] + left[2], widget.rect[1], max(0, children[1].rect[0] - left[0] - left[2]), widget.rect[3])
