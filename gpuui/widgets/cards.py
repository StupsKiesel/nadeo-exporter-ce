"""Folder and file presentations; no filesystem access is implied."""

from .custom import CustomWidget


class Card(CustomWidget):
    theme_kind = "filecard"

    def __init__(
        self, text="", *, image=None, badge="", shortcut="", selected=False,
        on_select=None, on_activate=None, on_context_menu=None, **settings,
    ):
        super().__init__(text=text, image=image, height=settings.pop("height", 100), **settings)
        self.badge = str(badge)
        self.shortcut = str(shortcut)
        self.selected = bool(selected)
        self.on_select = on_select
        self.on_activate = on_activate
        self.on_context_menu = on_context_menu
        self._pressed = False

    def natural_width(self, scale, text):
        return self.width * scale

    def draw(self, painter, clip):
        theme = painter.theme
        hover = self.enabled and painter.hovered
        selected = self.selected or self._pressed
        color = theme["button_pressed"] if selected else theme["button_hover"] if hover else theme["button"]
        if not self.enabled:
            color = theme["button_disabled"]
        painter.rect(self.rect, theme["outline_selected" if selected else "outline"], clip)
        inner = painter.inset(self.rect, max(1, painter.scale))
        painter.rect(inner, color, clip)
        x, y, width, height = inner
        line = min(height, painter.line_height(compact=True))
        text_color = theme["text_selected"] if selected else theme["text"]
        if not self.enabled:
            text_color = theme["disabled_text"]
        label = (x, y, width, line)
        preview = (x + 5, y + line, max(0, width - 10), max(0, height - line - 5))
        if self.image is not None:
            painter.image(self.image, preview, clip)
        else:
            painter.icon("FOLDER" if isinstance(self, FolderCard) else "FILE", preview, text_color, clip)
        painter.text(self.text, label, text_color, clip, centered=True)
        if self.badge:
            painter.text(self.badge, (x, y + height - line, width, line), text_color, clip, right=True)
        if self.shortcut:
            painter.text(self.shortcut, (x, y + height - line, width, line), text_color, clip)
        if painter.focused:
            painter.rect((x, y, width, max(1, painter.scale)), theme["focus"], clip, radius=0)

    def handle_event(self, host, event, x, y):
        inside = host._contains(host._intersection(self.rect, self.clip), x, y)
        if event.type == "RIGHTMOUSE" and event.value == "PRESS" and inside:
            if self.on_context_menu is not None:
                self.on_context_menu(host._context(), event)
            return True
        if event.type == "LEFTMOUSE" and event.value in {"PRESS", "DOUBLE_CLICK"} and inside:
            host._focus = self.key
            if event.value == "DOUBLE_CLICK":
                if self.on_activate is not None:
                    self.on_activate(host._context(), event)
            else:
                self._pressed = True
                host._custom_capture = self
            return True
        if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self._pressed:
            self.cancel_interaction()
            host._custom_capture = None
            if inside:
                self.selected = True
                if self.on_select is not None:
                    self.on_select(host._context(), event)
            return True
        if event.type in {"RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS" and host._focus == self.key:
            if self.on_activate is not None:
                self.on_activate(host._context(), event)
            return True
        if event.type == "ESC" and self._pressed:
            self.cancel_interaction()
            host._custom_capture = None
            return True
        return False

    def cancel_interaction(self, context=None, event=None):
        self._pressed = False


class FolderCard(Card):
    theme_kind = "foldercard"


class FileCard(Card):
    theme_kind = "filecard"
