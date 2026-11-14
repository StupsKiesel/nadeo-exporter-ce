"""Composable timed-object widgets with a shared, domain-independent time axis."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..components import Component
from .custom import CustomWidget


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


@dataclass
class TimeAxis:
    origin: float = 0.0
    pixels_per_second: float = 60.0
    snap: float = 0.0
    current_time: float = 0.0

    def __post_init__(self):
        self.origin = _finite(self.origin, "origin")
        self.current_time = _finite(self.current_time, "current_time")
        self.pixels_per_second = _finite(self.pixels_per_second, "pixels_per_second")
        self.snap = _finite(self.snap, "snap")
        if self.pixels_per_second <= 0 or self.snap < 0:
            raise ValueError("Time scale must be positive and snap must be nonnegative")

    def pixel(self, time: float) -> float:
        return (time - self.origin) * self.pixels_per_second

    def time(self, pixel: float) -> float:
        return self.origin + pixel / self.pixels_per_second

    def snapped(self, time: float) -> float:
        return round(time / self.snap) * self.snap if self.snap else time

    def zoom(self, factor: float, anchor_pixel: float):
        factor = _finite(factor, "zoom factor")
        if factor <= 0:
            raise ValueError("Zoom factor must be positive")
        anchor = self.time(anchor_pixel)
        self.pixels_per_second = max(0.01, min(100000.0, self.pixels_per_second * factor))
        self.origin = anchor - anchor_pixel / self.pixels_per_second


class TimeBlock(Component):
    kind_name = "timeblock"
    theme_kind = "timeblock"

    def __init__(self, start, end, text="", *, selected=False, keys=(), **settings):
        super().__init__(text=text, **settings)
        self.start, self.end = _finite(start, "start"), _finite(end, "end")
        if self.end <= self.start:
            raise ValueError("TimeBlock end must be greater than start")
        self.keys = [_finite(time, "key time") for time in keys]
        if any(not self.start <= time <= self.end for time in self.keys):
            raise ValueError("TimeBlock keys must lie between start and end")
        self.selected = bool(selected)
        self.selected_handle = None

    def handle_rects(self, scale):
        x, y, width, height = self.rect
        grip = min(6 * scale, width / 2)
        return (x, y, grip, height), (x + width - grip, y, grip, height)

    def key_rects(self, scale):
        x, y, width, height = self.rect
        size = min(height, 10 * scale)
        span = self.end - self.start
        return [
            (x + (time - self.start) / span * width - size / 2, y + (height - size) / 2, size, size)
            for time in self.keys
        ]

    def hit_handle(self, x, scale):
        start, end = self.handle_rects(scale)
        if x < start[0] + start[2]:
            return "start"
        if x >= end[0]:
            return "end"
        return next((index for index, rect in enumerate(self.key_rects(scale))
                     if rect[0] <= x < rect[0] + rect[2]), None)


class Keyframe(Component):
    kind_name = "keyframe"
    theme_kind = "timeblock"

    def __init__(self, time, text="", *, selected=False, **settings):
        super().__init__(text=text, **settings)
        self.time = _finite(time, "time")
        self.selected = bool(selected)


class TimeMarker(Keyframe):
    kind_name = "timemarker"


class TimeRange(TimeBlock):
    kind_name = "timerange"


class TimelineLane(Component):
    kind_name = "timelane"

    def __init__(self, *objects, text="", **settings):
        super().__init__(*objects, text=text, **settings)

    def _validate_child_kind(self, kind):
        if kind not in {"timeblock", "keyframe", "timemarker", "timerange"}:
            raise ValueError("TimelineLane accepts timed objects only")


class TimeRuler(CustomWidget):
    theme_kind = "timeline"

    def __init__(self, *, axis: TimeAxis, header_width=120, **settings):
        super().__init__(height=settings.pop("height", 30), **settings)
        self.axis = axis
        self.header_width = _finite(header_width, "header_width")
        if self.header_width < 0:
            raise ValueError("header_width must be nonnegative")
        self._initial = None
        self._signature = None

    def needs_redraw(self):
        signature = (self.axis.origin, self.axis.pixels_per_second, self.axis.current_time)
        changed = signature != self._signature
        self._signature = signature
        return changed

    def draw(self, painter, clip):
        x, y, width, height = self.rect
        left = x + self.header_width * painter.scale
        theme = painter.theme
        painter.rect(self.rect, theme["header"], clip, radius=0)
        clip = painter.intersection(clip, (left, y, max(0, x + width - left), height))
        interval = 10 ** math.floor(math.log10(80 / self.axis.pixels_per_second))
        interval *= next(factor for factor in (1, 2, 5, 10) if interval * factor * self.axis.pixels_per_second >= 60)
        start = math.ceil(self.axis.origin / interval) * interval
        count = min(2000, max(0, math.ceil((x + width - left) / (interval * self.axis.pixels_per_second)) + 1))
        for index in range(count):
            time = start + index * interval
            px = left + self.axis.pixel(time)
            painter.rect((px, y, 1, height * 0.25), theme["text"], clip, radius=0)
            painter.text(f"{time:g}s", (px, y + height * 0.25, 100, height * 0.75), theme["text"], clip)
        px = left + self.axis.pixel(self.axis.current_time)
        painter.rect((px - 1, y, 2, height), theme["focus"], clip, radius=0)

    def handle_event(self, host, event, x, y):
        left = self.rect[0] + self.header_width * host._scale
        if event.type == "LEFTMOUSE" and event.value == "PRESS" and x >= left:
            self._initial = self.axis.current_time
            host._custom_capture = self
            host._focus = self.key
        if self._initial is not None:
            if event.type == "ESC":
                self.cancel_interaction(host._context(), event)
                host._custom_capture = None
            elif event.type in {"MOUSEMOVE", "LEFTMOUSE"}:
                self.axis.current_time = self.axis.snapped(self.axis.time(x - left))
                if self.on_change is not None:
                    self.on_change(host._context(), event)
                if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                    self._initial = None
                    host._custom_capture = None
                    if self.on_commit is not None:
                        self.on_commit(host._context(), event)
            return True
        return False

    def cancel_interaction(self, context=None, event=None):
        if self._initial is not None:
            self.axis.current_time = self._initial
            self._initial = None
            if self.on_change is not None:
                self.on_change(context, event)


class Playhead(TimeRuler):
    def __init__(self, **settings):
        super().__init__(height=settings.pop("height", 12), **settings)

    def draw(self, painter, clip):
        x, y, width, height = self.rect
        px = x + self.header_width * painter.scale + self.axis.pixel(self.axis.current_time)
        painter.rect((px - 1, y, 2, height), painter.theme["focus"], clip, radius=0)


class TimelineSurface(CustomWidget):
    theme_kind = "timeline"

    def __init__(self, *lanes, axis: TimeAxis, header_width=120, lane_height=34, **settings):
        super().__init__(height=settings.pop("height", 220), undo=settings.pop("undo", False), **settings)
        self.axis = axis
        self.header_width = _finite(header_width, "header_width")
        self.lane_height = _finite(lane_height, "lane_height")
        if self.header_width < 0 or self.lane_height <= 8:
            raise ValueError("header_width must be nonnegative and lane_height must exceed 8")
        self.offset = 0.0
        self._gesture = None
        self._signature = None
        for lane in lanes:
            self.add(lane)

    def _validate_child_kind(self, kind):
        if kind != "timelane":
            raise ValueError("TimelineSurface accepts TimelineLane objects only")

    def max_scroll(self, scale):
        """Largest lane-aligned offset; the clamp itself must not cut the top row."""
        step = self.lane_height * scale
        limit = max(0.0, len(self.children) * step - self.height)
        return math.ceil(limit / step - 1e-9) * step if limit else 0.0

    def arrange(self, x, top, width, scale, font_size=13):
        self.rect = (x, top - self.height, width, self.height)
        self.arranged_width = width
        self.offset = max(0.0, min(self.max_scroll(scale), self.offset))
        lane_height = self.lane_height * scale
        left = x + self.header_width * scale
        for index, lane in enumerate(self.children):
            y = top - (index + 1) * lane_height + self.offset
            lane.rect = (x, y, width, lane_height)
            for item in lane.children:
                if isinstance(item, TimeBlock):
                    item.rect = (left + self.axis.pixel(item.start), y + 4 * scale,
                                 (item.end - item.start) * self.axis.pixels_per_second, lane_height - 8 * scale)
                else:
                    item.rect = (left + self.axis.pixel(item.time) - 5 * scale, y + lane_height / 2 - 5 * scale,
                                 10 * scale, 10 * scale)

    def needs_redraw(self):
        signature = (self.axis.origin, self.axis.pixels_per_second, self.axis.current_time, self.offset)
        changed = signature != self._signature
        self._signature = signature
        return changed

    @staticmethod
    def _diamond(painter, rect, color, clip):
        x, y, width, height = rect
        painter.polygon([(x, y + height / 2), (x + width / 2, y), (x + width, y + height / 2),
                         (x + width / 2, y + height)], color, clip)

    def _marker(self, painter, colors, rect, active, clip):
        # The focus color can match the selected fill, so back it with text_selected.
        self._diamond(painter, rect, colors["text_selected"] if active else colors["text"], clip)
        if active:
            self._diamond(painter, painter.inset(rect, 2 * max(1, painter.scale)), colors["focus"], clip)

    def draw(self, painter, clip):
        self.arrange(self.rect[0], self.rect[1] + self.height, self.rect[2], painter.scale)
        theme = painter.theme
        painter.rect(self.rect, theme["surface"], clip, radius=0)
        left = self.rect[0] + self.header_width * painter.scale
        content_clip = painter.intersection(clip, (left, self.rect[1], max(0, self.rect[0] + self.rect[2] - left), self.height))
        for index, lane in enumerate(self.children):
            if not painter.intersection(lane.rect, clip)[3]:
                continue
            x, y, width, height = lane.rect
            painter.rect(lane.rect, theme["surface_alt"] if index % 2 else theme["surface"], clip, radius=0)
            selected = getattr(lane, "selected", False)
            painter.rect((x, y, left - x, height), theme["button_pressed"] if selected else theme["header"], clip, radius=0)
            painter.rect((x, y, width, max(1, painter.scale)), theme["panel_outline"], clip, radius=0)
            indent = 8 * painter.scale
            painter.text(lane.text, (x + indent, y, max(0, left - x - indent), height), theme["text_selected"] if selected else theme["text"],
                         painter.intersection(clip, (x, y, left - x, height)))
            for item in lane.children:
                if not painter.intersection(item.rect, content_clip)[2]:
                    continue
                colors = painter.style(item)
                color = colors["button_pressed"] if item.selected else colors["button"]
                if isinstance(item, TimeBlock):
                    painter.rect(item.rect, colors["outline_selected" if item.selected else "outline"], content_clip)
                    painter.rect(painter.inset(item.rect, 1), color, content_clip)
                    handles = item.handle_rects(painter.scale)
                    for side, handle in zip(("start", "end"), handles):
                        grip = painter.inset(handle, 1)
                        if item.selected and item.selected_handle == side:
                            painter.rect(grip, colors["text_selected"], content_clip, radius=0)
                            painter.rect(painter.inset(grip, max(1, painter.scale)), colors["focus"],
                                         content_clip, radius=0)
                        else:
                            painter.rect(grip, colors["text"], content_clip, radius=0)
                    ix, iy, iw, ih = item.rect
                    grip = handles[0][2]
                    painter.text(item.text, (ix + grip, iy, max(0, iw - 2 * grip), ih),
                                 colors["text_selected"] if item.selected else colors["text"], content_clip)
                    for index, key_rect in enumerate(item.key_rects(painter.scale)):
                        self._marker(painter, colors, key_rect,
                                     item.selected and item.selected_handle == index, content_clip)
                else:
                    self._marker(painter, colors, item.rect, item.selected, content_clip)
        px = left + self.axis.pixel(self.axis.current_time)
        painter.rect((left - max(1, painter.scale), self.rect[1], max(1, painter.scale), self.height),
                     theme["panel_outline"], clip, radius=0)
        # Separate the lanes from whatever sits above, typically a TimeRuler.
        painter.rect((self.rect[0], self.rect[1] + self.height - max(1, painter.scale), self.rect[2],
                      max(1, painter.scale)), theme["panel_outline"], clip, radius=0)
        painter.rect((px, self.rect[1], 1, self.height), theme["focus"], content_clip, radius=0)

    def _hit_item(self, host, x, y):
        if x < self.rect[0] + self.header_width * host._scale:
            return None
        return next((item for lane in reversed(self.children) for item in reversed(lane.children)
                     if host._contains(item.rect, x, y)), None)

    def handle_event(self, host, event, x, y):
        left = self.rect[0] + self.header_width * host._scale
        if self._gesture is not None:
            gesture = self._gesture
            if event.type == "ESC":
                self.cancel_interaction(host._context(), event)
                host._custom_capture = None
                host._arrange()
                return True
            if event.type == "MOUSEMOVE":
                delta = (x - gesture["x"]) / self.axis.pixels_per_second
                item = gesture["item"]
                if item is None:
                    self.axis.origin = gesture["initial"][0] - delta
                elif isinstance(item, TimeBlock):
                    if gesture["mode"] == "key":
                        index = gesture["index"]
                        low = item.keys[index - 1] if index else item.start
                        high = item.keys[index + 1] if index + 1 < len(item.keys) else item.end
                        item.keys[index] = min(max(self.axis.snapped(gesture["initial"][0] + delta),
                                                   low + 1e-6), high - 1e-6)
                    else:
                        start, end = gesture["initial"]
                        if gesture["mode"] == "start":
                            item.start = min(self.axis.snapped(start + delta), end - 1e-6)
                        elif gesture["mode"] == "end":
                            item.end = max(self.axis.snapped(end + delta), start + 1e-6)
                        else:
                            item.start = self.axis.snapped(start + delta)
                            item.end = item.start + end - start
                        scale = (item.end - item.start) / (end - start)
                        item.keys = [item.start + (key - start) * scale for key in gesture["keys"]]
                else:
                    item.time = self.axis.snapped(gesture["initial"][0] + delta)
                if self.on_change is not None:
                    self.on_change(host._context(), event)
                host._arrange()
            elif event.type in {"LEFTMOUSE", "MIDDLEMOUSE"} and event.value == "RELEASE":
                self._gesture = None
                host._custom_capture = None
                item = gesture["item"]
                if item is None:
                    changed = False
                elif gesture["mode"] == "key":
                    changed = (item.keys[gesture["index"]],) != gesture["initial"]
                elif isinstance(item, TimeBlock):
                    changed = (item.start, item.end) != gesture["initial"]
                else:
                    changed = (item.time,) != gesture["initial"]
                if changed and self.on_commit is not None:
                    self.on_commit(host._context(), event)
                if changed and self.undo:
                    host._undo(self.text or "Timeline edit")
            return True
        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            direction = 1 if event.type == "WHEELUPMOUSE" else -1
            if event.ctrl:
                self.axis.zoom(1.2 ** direction, x - left)
            else:
                step = self.lane_height * host._scale
                self.offset = max(0, min(self.max_scroll(host._scale), self.offset - direction * step))
            host._arrange()
            return True
        if event.value == "PRESS" and event.type in {"LEFTMOUSE", "MIDDLEMOUSE"}:
            item = self._hit_item(host, x, y) if event.type == "LEFTMOUSE" else None
            if item is None and event.type == "LEFTMOUSE":
                return True
            if item is not None:
                for lane in self.children:
                    for child in lane.children:
                        if not event.shift:
                            child.selected = False
                item.selected = True
                index = None
                if isinstance(item, TimeBlock):
                    item.selected_handle = item.hit_handle(x, host._scale)
                    if isinstance(item.selected_handle, int):
                        index = item.selected_handle
                        initial, mode = (item.keys[index],), "key"
                    else:
                        initial = (item.start, item.end)
                        mode = item.selected_handle or "move"
                else:
                    initial, mode = (item.time,), "move"
            else:
                index, initial, mode = None, (self.axis.origin,), "pan"
            self._gesture = {
                "item": item, "x": x, "initial": initial, "mode": mode, "index": index,
                "keys": list(item.keys) if isinstance(item, TimeBlock) else None,
            }
            host._custom_capture = self
            host._focus = self.key
            return True
        return False

    def cancel_interaction(self, context=None, event=None):
        if self._gesture is None:
            return
        gesture = self._gesture
        item, initial = gesture["item"], gesture["initial"]
        if item is None:
            self.axis.origin = initial[0]
        elif gesture["mode"] == "key":
            item.keys[gesture["index"]] = initial[0]
        elif isinstance(item, TimeBlock):
            item.start, item.end = initial
            item.keys = list(gesture["keys"])
        else:
            item.time = initial[0]
        self._gesture = None
        if item is not None and self.on_change is not None:
            self.on_change(context, event)
