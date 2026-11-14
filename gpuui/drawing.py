"""Drawing interface for custom components, backed by the host's GPU cache."""

from types import SimpleNamespace


class Painter:
    def __init__(self, runtime, widget):
        self._runtime = runtime
        self._widget = widget

    @property
    def scale(self):
        return self._runtime._scale

    @property
    def theme(self):
        return self._runtime._theme

    @property
    def hovered(self):
        return self._runtime._hover == self._widget.key

    @property
    def focused(self):
        return self._runtime._focus == self._widget.key

    def style(self, widget):
        kind = getattr(widget, "theme_kind", widget.kind)
        return self._runtime._widget_themes[(kind, id(getattr(widget, "style", None)))]

    def rect(self, bounds, color, clip, radius=None):
        self._runtime._quad(bounds, color, clip, radius)

    def polygon(self, vertices, color, clip):
        self._runtime._poly(vertices, color, clip)

    def line(self, start, end, thickness, color, clip):
        self._runtime._segment(start, end, thickness, color, clip)

    def text(self, text, bounds, color, clip, *, centered=False, right=False, size=1.0):
        self._runtime._text(text, bounds, color, clip, centered, right, size)

    def icon(self, name, bounds, color, clip):
        self._runtime._draw_icon(name, bounds, color, clip)

    def image(self, image, bounds, clip):
        self._runtime._draw_image(SimpleNamespace(image=image), clip, bounds)

    def line_height(self, compact=False):
        return self._runtime._line(compact)

    def measure_text(self, text):
        return self._runtime._measure(text)

    def intersection(self, first, second):
        return self._runtime._intersection(first, second)

    def inset(self, bounds, amount):
        return self._runtime._shrink(bounds, amount)
