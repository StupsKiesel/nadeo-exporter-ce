"""Extension point for GPU widgets without editing the built-in renderer."""

from ..components import Component


class CustomWidget(Component):
    kind_name = "custom"
    theme_kind = "button"

    def measure(self, scale, font_size=13, text=None):
        self.height = self.view_height * scale

    def needs_redraw(self):
        return False

    def draw(self, painter, clip):
        raise NotImplementedError

    def handle_event(self, host, event, x, y):
        return False

    def cancel_interaction(self, context=None, event=None):
        pass
