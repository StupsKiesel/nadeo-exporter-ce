"""Layout tree and measurement, independent of Serpens nodes."""

from ._support import (
    _GPU_CONTAINERS, _GPU_ICONS, _GPU_LIST_KINDS, _GPU_LOG_COLORS, _GPU_NUMERIC_KINDS, _GPU_PANEL_MODES, _GPU_SPACE_KINDS, _GPU_STACKED, _GPU_VALUE_KINDS, _gpu_finite, _gpu_parse_hotkey, math,
)
from .styling import theme_spec as _gpu_ui_theme

class Element:
    def __init__(self, kind="root", key="", text="", width=280, x=24, y=80,
                 gap=6, padding=10, enabled=True, callback=None, theme=None,
                 getter=None, setter=None, minimum=0, maximum=1, step=0.1,
                 items=(), tooltip="", height=160, index=0, on_change=None, on_commit=None, undo=True,
                 value=None, image=None, rows=5, horizontal=False, expanded=True, line=True, placeholder="",
                 columns=2, ratio=0.5, resizable=False, color=None, active=True, auto=True,
                 message="", cancel=None, headers=(), table=(), tree=(), minimizable=False, maximizable=False,
                 item_width=60, url="", pairs=(), level="INFO", closable=True, palette=(), labels=(),
                 mode="window", side="LEFT", corner="TOP_RIGHT", target=None, opacity=1.0, hotkey="CTRL+SHIFT+P",
                 icon="", folder=False, pattern="", relative=False, attribute="name", selected_get=None, collapsible=True,
                 selected_set=None):
        self.theme = _gpu_ui_theme(theme) if kind == "root" else None
        self.kind = kind
        self.key = key
        self.text = str(text)
        self.width = max(120, float(width))
        self.x = float(x)
        self.y = float(y)
        self.gap = max(0, float(gap))
        self.padding = max(0, float(padding))
        self.enabled = bool(enabled)
        self.callback = callback
        self.cancel = cancel
        self.message = str(message)
        if kind == "confirm" and not callable(callback):
            raise ValueError("GPU Confirm Button requires an On Confirm action.")
        if kind == "command" and not callable(callback):
            raise ValueError("GPU Command requires an On Run action.")
        self.getter, self.setter = getter, setter
        self.minimum, self.maximum, self.step = float(minimum), float(maximum), float(step)
        self.auto = bool(auto)
        if kind in _GPU_NUMERIC_KINDS or (kind == "graph" and not self.auto):
            if not all(math.isfinite(value) for value in (self.minimum, self.maximum, self.step)):
                raise ValueError("GPU numeric bounds and step must be finite.")
            if self.minimum >= self.maximum or self.step <= 0:
                raise ValueError("GPU numeric controls require Minimum < Maximum and Step > 0.")
        if kind == "dial" and (not math.isfinite(self.step) or self.step <= 0):
            raise ValueError("GPU Angle Dial Step (degrees) must be a positive number.")
        self.icon = str(icon or "").upper()
        if self.icon == "NONE":
            self.icon = ""
        if self.icon and self.icon not in _GPU_ICONS:
            raise ValueError("Unknown GPU icon " + repr(self.icon) + "; use one of: " + ", ".join(_GPU_ICONS) + ".")
        self.folder = bool(folder)
        self.pattern = str(pattern or "")
        self.relative = bool(relative)
        self.attribute = str(attribute or "")
        self.selected_get, self.selected_set = selected_get, selected_set
        if (selected_get is None) != (selected_set is None):
            raise ValueError("GPU Reorderable List Active Index needs both a getter and a setter.")
        if not isinstance(items, (list, tuple)):
            raise ValueError("GPU options must be a list of strings or (identifier, label) pairs.")
        self.items = []
        for item in items:
            if isinstance(item, str):
                self.items.append((item, item))
            elif isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(part, str) for part in item):
                self.items.append(tuple(item))
            else:
                raise ValueError("Invalid GPU option; use strings or (identifier, label) pairs.")
        if len({item[0] for item in self.items}) != len(self.items):
            raise ValueError("GPU option identifiers must be unique.")
        if kind in ("dropdown", "tabs", "radio", "search", "segmented", "flags") and not self.items:
            raise ValueError("GPU Dropdown/Tabs/Radio/Search/Segmented/Multi-Toggle requires at least one option.")
        if kind in _GPU_VALUE_KINDS and (not callable(getter) or not callable(setter)):
            raise ValueError("GPU value controls require a readable, writable Property.")
        self.tooltip = str(tooltip)
        if not math.isfinite(float(height)):
            raise ValueError("GPU widget Height must be finite.")
        self.view_height = max({"scroll": 28, "image": 16, "graph": 16, "imagebutton": 16, "barchart": 16,
                                "xypad": 60}.get(kind, 0), float(height))
        self.stretch = 0.0
        self.index = int(index)
        if kind in ("tab", "stack") and (isinstance(index, bool) or self.index != index or self.index < 0):
            raise ValueError("GPU Tab Index and Page Stack Page must be nonnegative integers.")
        if kind in _GPU_LIST_KINDS + ("textarea", "console") and (
                isinstance(rows, bool) or not isinstance(rows, (int, float)) or rows != int(rows) or rows < 1):
            raise ValueError("GPU List View/Table/Tree/Reorderable/Multi-select List/Text Area/Console Rows must be a "
                             "positive integer.")
        self.rows = int(rows)
        self.headers = ()
        self.cells = []
        if kind == "table":
            try:
                self.headers = tuple(str(header) for header in headers)
                self.cells = [tuple(str(cell) for cell in row) for row in table]
            except TypeError:
                raise ValueError("GPU Table Rows must be a list of rows, each a list of cells.") from None
            if isinstance(headers, str) or isinstance(table, str) or any(isinstance(row, str) for row in table):
                raise ValueError("GPU Table Rows must be a list of rows, each a list of cells.")
            self.items = [(str(index), " | ".join(row)) for index, row in enumerate(self.cells)]
        self.columns_count = max([len(self.headers)] + [len(row) for row in self.cells] + [1])
        self.tree = []
        if kind == "tree":
            seen = set()

            def validate(nodes, depth):
                if isinstance(nodes, str) or not isinstance(nodes, (list, tuple)) or depth > 32:
                    raise ValueError("GPU Tree Items must be nested (identifier, label[, children]) entries.")
                result = []
                for node in nodes:
                    if not isinstance(node, (list, tuple)) or len(node) not in (2, 3) or \
                            not all(isinstance(part, str) for part in node[:2]):
                        raise ValueError("GPU Tree Items must be nested (identifier, label[, children]) entries.")
                    if node[0] in seen:
                        raise ValueError("GPU Tree item identifiers must be unique.")
                    seen.add(node[0])
                    result.append((node[0], node[1], validate(node[2], depth + 1) if len(node) == 3 else []))
                return result
            self.tree = validate(tree, 0)
        self.meta = []
        self.on_change = on_change
        self.on_commit = on_commit
        self.undo = bool(undo)
        self.value = None
        if kind == "progress":
            if not _gpu_finite(value):
                raise ValueError("GPU Progress Bar Value must be a finite number.")
            self.value = float(value)
        self.image = image
        self.horizontal = bool(horizontal)
        self.expanded = bool(expanded)
        if kind == "collapsible":
            self.value = self.expanded
        self.line = bool(line)
        self.placeholder = str(placeholder)
        if kind in ("grid", "palette") and (isinstance(columns, bool) or not isinstance(columns, (int, float))
                                            or columns != int(columns) or columns < 1):
            raise ValueError("GPU Grid/Color Palette Columns must be a positive integer.")
        self.columns = int(columns)
        if not _gpu_finite(ratio) or not 0 < ratio < 1:
            raise ValueError("GPU Split Ratio must be between 0 and 1.")
        self.ratio = float(ratio)
        self.resizable = bool(resizable)
        self.minimizable = bool(minimizable)
        self.maximizable = bool(maximizable)
        self.active = bool(active)
        if kind == "graph":
            try:
                values = tuple(value) if value is not None and not isinstance(value, str) else None
            except TypeError:
                values = None
            if values is None or not all(_gpu_finite(item) for item in values):
                raise ValueError("GPU Graph Values must be a list of finite numbers.")
            self.value = tuple(float(item) for item in values)
        self.color = None
        if color is not None:
            try:
                rgba = tuple(color)
            except TypeError:
                rgba = ()
            if len(rgba) == 3:
                rgba += (1.0,)
            if len(rgba) != 4 or not all(_gpu_finite(item) and 0 <= item <= 1 for item in rgba):
                raise ValueError("GPU Badge Color must be an RGB or RGBA color with values from 0 to 1.")
            self.color = tuple(float(item) for item in rgba)
        if kind == "console":
            if isinstance(value, str):
                value = value.splitlines()
            if not isinstance(value, (list, tuple)):
                raise ValueError("GPU Console Lines must be a list of strings or (level, text) pairs.")
            lines = []
            for line in value:
                if isinstance(line, (list, tuple)) and len(line) == 2:
                    lines.append((str(line[0]).upper(), str(line[1])))
                else:
                    text_line = str(line)
                    level = next((name for name in _GPU_LOG_COLORS
                                  if text_line.upper().lstrip("[").startswith(name)), "INFO")
                    lines.append((level, text_line))
            self.value = lines
        self.url = str(url)
        if kind == "link" and not self.tooltip:
            self.tooltip = self.url
        self.level = str(level).upper()
        self.closable = bool(closable)
        self.collapsible = bool(collapsible)
        self.hidden = False
        self.wrapped = []
        self.pairs = []
        if kind == "keyvalue":
            if isinstance(pairs, str) or not isinstance(pairs, (list, tuple)) or not all(
                    isinstance(pair, (list, tuple)) and len(pair) == 2 for pair in pairs):
                raise ValueError("GPU Key-Value Rows must be a list of (key, value) pairs.")
            self.pairs = [(str(name), str(item)) for name, item in pairs]
        if kind == "rating" and (self.minimum != 0 or self.maximum != int(self.maximum) or not 1 <= self.maximum <= 20):
            raise ValueError("GPU Rating Stars requires Minimum 0 and a whole-number Maximum from 1 to 20.")
        self.palette = []
        if kind == "palette":
            if isinstance(palette, str) or not isinstance(palette, (list, tuple)) or not palette:
                raise ValueError("GPU Color Palette requires a list of RGB or RGBA colors.")
            for swatch in palette:
                try:
                    rgba = tuple(swatch)
                except TypeError:
                    rgba = ()
                if len(rgba) == 3:
                    rgba += (1.0,)
                if len(rgba) != 4 or not all(_gpu_finite(item) and 0 <= item <= 1 for item in rgba):
                    raise ValueError("GPU Color Palette requires a list of RGB or RGBA colors.")
                self.palette.append(tuple(float(item) for item in rgba))
            self.items = [(str(index), "") for index in range(len(self.palette))]
        if kind == "barchart":
            try:
                values = tuple(value) if value is not None and not isinstance(value, str) else None
            except TypeError:
                values = None
            if values is None or not all(_gpu_finite(item) for item in values):
                raise ValueError("GPU Bar Chart Values must be a list of finite numbers.")
            self.value = tuple(float(item) for item in values)
        self.labels = tuple(str(label) for label in labels) if not isinstance(labels, str) else (labels,)
        self.mode = str(mode).lower()
        if kind == "panel" and self.mode not in _GPU_PANEL_MODES:
            raise ValueError("GPU Panel mode must be one of: " + ", ".join(_GPU_PANEL_MODES) + ".")
        self.side = str(side).upper()
        if kind == "panel" and self.mode == "drawer" and self.side not in ("LEFT", "RIGHT"):
            raise ValueError("GPU Edge Drawer Side must be LEFT or RIGHT.")
        if kind == "panel" and self.mode == "dock" and self.side not in ("BOTTOM", "TOP"):
            raise ValueError("GPU Dock Bar Side must be BOTTOM or TOP.")
        self.corner = str(corner).upper()
        if kind == "panel" and self.mode == "hud" and self.corner not in (
                "TOP_LEFT", "TOP_RIGHT", "BOTTOM_LEFT", "BOTTOM_RIGHT"):
            raise ValueError("GPU HUD Overlay Corner must be TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT or BOTTOM_RIGHT.")
        self.target = target
        if kind == "panel" and self.mode == "attached" and target is not None \
                and not hasattr(target, "matrix_world"):
            try:
                point = tuple(target)
            except TypeError:
                point = ()
            if len(point) != 3 or not all(_gpu_finite(item) for item in point):
                raise ValueError("GPU Attached Panel Target must be an Object, a 3D location or None.")
            self.target = tuple(float(item) for item in point)
        if not _gpu_finite(opacity) or not 0 <= opacity <= 1:
            raise ValueError("GPU panel Opacity must be between 0 and 1.")
        self.opacity = float(opacity)
        self.titled = kind != "panel" or self.mode not in ("hud", "attached") or bool(self.text)
        self.hotkey = _gpu_parse_hotkey(hotkey) if kind == "commands" else None
        self.anchor = None
        self.screen = None
        if not _gpu_finite(item_width) or item_width < 0:
            raise ValueError("GPU Flow/Toolbar Item Width must be a nonnegative number.")
        self.item_width = float(item_width)
        self.natural = 0.0
        self.flow_width = None
        self.arranged_width = None
        self.parent = None
        self.overflow = None
        if kind == "toolbar":
            self.overflow = Element("overflow", key=str(key) + "__more", text="...",
                                             tooltip="More tools")
            self.overflow.parent = self
        self.scroll = 0
        self.clip = (0, 0, 0, 0)
        self.children = []
        self.rect = (0, 0, 0, 0)
        self.height = 0

    def _validate_child_kind(self, kind):
        if self.kind not in _GPU_CONTAINERS:
            raise ValueError("GPU " + self.kind + " cannot contain other widgets.")
        if self.kind == "root" and kind not in ("panel", "commands"):
            raise ValueError("GPU UI Root must connect to GPU Panel or Command Palette nodes.")
        if kind in ("panel", "commands") and self.kind != "root":
            raise ValueError("GPU Panels and Command Palettes must connect directly to GPU UI Root.")
        if (kind == "command") != (self.kind == "commands"):
            raise ValueError("GPU Command Palette accepts only GPU Command entries; commands must connect to one.")
        if (kind == "tab") != (self.kind in ("tabs", "stack")):
            raise ValueError("GPU Tabs and Page Stack accept only GPU Tab pages; pages must connect to one of them.")
        if self.kind == "accordion" and kind != "collapsible":
            raise ValueError("GPU Accordion accepts only GPU Collapsible sections.")
        if kind == "statusbar" and self.kind != "panel":
            raise ValueError("GPU Status Bar must connect directly to a GPU Panel.")
    def add(self, kind, **settings):
        self._validate_child_kind(kind)
        child = Element(kind, **settings)
        child.parent = self
        self.children.append(child)
        return child

    def visible_children(self):
        children = [child for child in self.children if not child.hidden]
        if self.kind == "tabs":
            return [child for child in children if child.index == self.value]
        if self.kind == "stack":
            return [child for child in children if child.index == self.index]
        if self.kind == "collapsible" and not self.value:
            return []
        if self.kind == "toolbar":
            return children + [self.overflow]
        return children

    def wrap(self, scale, font_size, text):
        """Split rich text into styled display lines, word-wrapped to flow_width when it is known."""
        compact = max(24, font_size + 10) * scale
        result = []
        for source in self.text.split("\n"):
            style, indent, prefix, body = "body", 0.0, "", source
            if source.startswith("# "):
                style, body = "heading", source[2:]
            elif source[:2] in ("- ", "* "):
                style, indent, prefix, body = "bullet", 14 * scale, "\u2022 ", source[2:]
            elif source.startswith("> "):
                style, indent, body = "quote", 10 * scale, source[2:]
            if not body.strip():
                result.append(("blank", "", 0.0, compact / 2))
                continue
            factor = 1.25 if style == "heading" else 1.0
            height = compact * (1.3 if style == "heading" else 1.0)
            available = None if self.flow_width is None or text is None else max(1.0, self.flow_width - indent - 12 * scale)
            line = prefix
            for word in body.split(" "):
                candidate = line + word if line in ("", prefix) else line + " " + word
                if available is not None and line not in ("", prefix) and text(candidate) * factor > available:
                    result.append((style, line, indent, height))
                    line = ("  " if prefix else "") + word
                else:
                    line = candidate
            result.append((style, line, indent, height))
        return result

    def stretch_targets(self):
        """Scroll areas stacked vertically below this widget that can absorb a resized panel's height."""
        targets = []
        for child in self.visible_children():
            if child.kind == "scroll":
                targets.append(child)
            elif child.kind in _GPU_STACKED:
                targets.extend(child.stretch_targets())
        return targets

    def natural_width(self, scale, text):
        """Preferred width inside a Flow or Toolbar: the label plus padding, at least Item Width."""
        width = text(self.text) + 24 * scale if self.text else 0
        if self.kind == "checkbox":
            width += text("[x] ")
        elif self.kind == "toggle":
            width += text(": OFF")
        elif self.kind == "overflow":
            width = max(28 * scale, width)
        if self.icon:
            width += 20 * scale if self.text else 28 * scale
        parent_minimum = self.parent.item_width * scale if self.parent is not None else 0
        return max(parent_minimum if self.kind != "overflow" else 0, width, 16 * scale)

    def flow_lines(self, children, width, gap):
        lines, line, used = [], [], 0
        for child in children:
            child_width = min(child.natural, width) if width is not None else child.natural
            if line and width is not None and used + gap + child_width > width:
                lines.append(line)
                line, used = [], 0
            used += (gap if line else 0) + child_width
            line.append((child, child_width))
        if line:
            lines.append(line)
        return lines

    def measure(self, scale, font_size=13, text=None):
        if text is None:
            def text(value):
                return len(value) * font_size * 0.55 * scale
        children = self.visible_children()
        for child in children:
            child.measure(scale, font_size, text)
        if self.kind in ("flow", "toolbar"):
            for child in children:
                child.natural = child.natural_width(scale, text)
        line = max(28, font_size + 12) * scale
        compact = max(24, font_size + 10) * scale
        stacked = [child for child in children if child.kind != "statusbar"]
        gap = self.gap * scale * max(0, len(stacked) - 1)
        if self.kind in ("row", "split", "splitter", "toolbar"):
            self.height = max((child.height for child in children), default=0)
        elif self.kind == "statusbar":
            self.height = max([compact] + [child.height for child in children])
        elif self.kind == "flow":
            lines = self.flow_lines(children, self.flow_width, self.gap * scale)
            self.height = sum(max(child.height for child, _width in chunk) for chunk in lines)
            self.height += self.gap * scale * max(0, len(lines) - 1)
        elif self.kind == "grid":
            chunks = [children[index:index + self.columns] for index in range(0, len(children), self.columns)]
            self.height = sum(max(child.height for child in chunk) for chunk in chunks)
            self.height += self.gap * scale * max(0, len(chunks) - 1)
        elif self.kind in ("column", "panel", "scroll", "tabs", "tab", "collapsible", "box", "stack", "accordion"):
            self.content_height = sum(child.height for child in stacked) + gap
            self.height = self.content_height
            if self.kind == "scroll":
                self.height = max(28 * scale, self.view_height * scale + self.stretch)
            if self.kind == "tabs":
                self.height += line
            if self.kind == "collapsible":
                self.height = line + (self.gap * scale + self.content_height if children else 0)
            if self.kind == "box":
                self.height += 2 * self.padding * scale + (compact if self.text else 0)
            if self.kind == "panel":
                self.height += (line if self.titled else 0) + 2 * self.padding * scale
                self.height += sum(child.height for child in children if child.kind == "statusbar")
        elif self.kind in _GPU_SPACE_KINDS:
            self.height = self.view_height * scale
        elif self.kind == "radio":
            self.height = ((1 if self.horizontal else len(self.items)) + (1 if self.text else 0)) * line
        elif self.kind in _GPU_LIST_KINDS:
            self.height = (self.rows + (1 if self.kind == "table" else 0)) * max(24, font_size + 10) * scale
        elif self.kind == "textarea":
            self.height = self.rows * max(24, font_size + 10) * scale + 8 * scale + (line if self.text else 0)
        elif self.kind == "console":
            self.height = self.rows * compact + 8 * scale
        elif self.kind == "keyvalue":
            self.height = max(1, len(self.pairs)) * compact
        elif self.kind == "richtext":
            self.wrapped = self.wrap(scale, font_size, text)
            self.height = sum(entry[3] for entry in self.wrapped)
        elif self.kind == "palette":
            rows = -(-len(self.palette) // self.columns)
            self.height = rows * compact + (line if self.text else 0)
        elif self.kind in ("knob", "dial"):
            self.height = 2 * line
        elif self.kind == "timeline":
            self.height = line + compact
        else:
            compact_kind = self.kind in ("label", "badge", "spinner", "link")
            self.height = max(24 if compact_kind else 28, font_size + (10 if compact_kind else 12)) * scale

    def pin_status(self, x, bottom, width, scale, font_size=13):
        """Lay out a panel's status bars flush with its bottom edge, below the padded content."""
        for child in reversed([child for child in self.children if child.kind == "statusbar"]):
            child.arrange(x, bottom + child.height, width, scale, font_size)
            bottom += child.height

    def arrange(self, x, top, width, scale, font_size=13):
        self.rect = (x, top - self.height, width, self.height)
        self.arranged_width = width
        children = self.visible_children()
        if self.kind == "panel":
            self.pin_status(x, top - self.height, width, scale, font_size)
            children = [child for child in children if child.kind != "statusbar"]
        if not children:
            return
        gap = self.gap * scale
        if self.kind in ("split", "splitter"):
            if self.kind == "splitter":
                gap = max(gap, 6 * scale)
            available = max(0, width - gap * (len(children) - 1))
            first = available * self.ratio if len(children) > 1 else available
            widths = [first] + [(available - first) / (len(children) - 1)] * (len(children) - 1)
            for child, child_width in zip(children, widths):
                child.arrange(x, top, child_width, scale, font_size)
                x += child_width + gap
        elif self.kind == "flow":
            for chunk in self.flow_lines(children, width, gap):
                left = x
                for child, child_width in chunk:
                    child.arrange(left, top, child_width, scale, font_size)
                    left += child_width + gap
                top -= max(child.height for child, _width in chunk) + gap
        elif self.kind == "toolbar":
            items, more = children[:-1], children[-1]
            total = sum(child.natural for child in items) + gap * max(0, len(items) - 1)
            limit = width if total <= width else width - more.natural - gap
            left, hidden = x, []
            for child in items:
                if not hidden and left + child.natural <= x + limit + 0.001:
                    child.arrange(left, top, child.natural, scale, font_size)
                    left += child.natural + gap
                else:
                    hidden.append(child)
                    child.arrange(left, top, 0, scale, font_size)
            more.items = [(child.key, child.text or child.key) for child in hidden
                          if child.kind in ("button", "imagebutton", "checkbox", "toggle") and child.enabled]
            more.arrange(x + width - more.natural, top, more.natural if hidden else 0, scale, font_size)
        elif self.kind == "statusbar":
            inset = 6 * scale
            child_width = max(0, (width - 2 * inset - gap * (len(children) - 1)) / len(children))
            left = x + inset
            for child in children:
                child.arrange(left, top - (self.height - child.height) / 2, child_width, scale, font_size)
                left += child_width + gap
        elif self.kind == "grid":
            cell = max(0, (width - gap * (self.columns - 1)) / self.columns)
            for start in range(0, len(children), self.columns):
                chunk = children[start:start + self.columns]
                for column, child in enumerate(chunk):
                    child.arrange(x + column * (cell + gap), top, cell, scale, font_size)
                top -= max(child.height for child in chunk) + gap
        elif self.kind == "row":
            child_width = max(0, (width - gap * (len(children) - 1)) / len(children))
            for child in children:
                child.arrange(x, top, child_width, scale, font_size)
                x += child_width + gap
        else:
            if self.kind == "panel":
                padding = self.padding * scale
                x += padding
                top -= (max(28, font_size + 12) * scale if self.titled else 0) + padding
                width = max(0, width - 2 * padding)
            if self.kind == "box":
                padding = self.padding * scale
                x += padding
                top -= padding + (max(24, font_size + 10) * scale if self.text else 0)
                width = max(0, width - 2 * padding)
            if self.kind == "tabs":
                top -= max(28, font_size + 12) * scale
            if self.kind == "collapsible":
                top -= max(28, font_size + 12) * scale + gap
            if self.kind == "scroll":
                self.scroll = max(0, min(self.scroll, max(0, self.content_height - self.height)))
                top += self.scroll
            for child in children:
                child.arrange(x, top, width, scale, font_size)
                top -= child.height + gap
