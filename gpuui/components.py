"""Python composition API over the extracted layout engine."""

from __future__ import annotations

from itertools import count

from .element import Element
from .styling import Style


_identifiers = count()


class Component(Element):
    kind_name = "column"

    def __init__(self, *children: Element, key: str | None = None, style: Style | None = None, **settings):
        super().__init__(self.kind_name, key=key or f"widget-{next(_identifiers)}", **settings)
        if style is not None and {"font", "font_size"} & style.values.keys():
            raise ValueError("Font overrides belong to the Root's BlenderTheme")
        self.style = style
        for child in children:
            self.add(child)

    def add(self, child, **settings):
        if isinstance(child, str):
            return super().add(child, **settings)
        if not isinstance(child, Element):
            raise TypeError("Children must be GPUUI elements")
        if child is self or child.parent is not None:
            raise ValueError("An element may belong to exactly one parent")
        ancestor = self
        while ancestor is not None:
            if ancestor is child:
                raise ValueError("A UI tree cannot contain cycles")
            ancestor = ancestor.parent
        self._validate_child_kind(child.kind)
        child.parent = self
        self.children.append(child)
        return child

    def remove(self, child: Element):
        if child.parent is not self:
            raise ValueError("The element is not a child of this container")
        self.children.remove(child)
        child.parent = None
        return child


class Root(Component):
    kind_name = "root"

    def __init__(self, *children, theme=None, **settings):
        super().__init__(*children, theme=theme, **settings)


class Panel(Component):
    kind_name = "panel"

    def __init__(self, *children, title="", **settings):
        super().__init__(*children, text=title, **settings)


class Dock(Panel):
    def __init__(self, *children, edge="BOTTOM", **settings):
        super().__init__(*children, mode="dock", side=edge, **settings)


class Row(Component):
    kind_name = "row"


class Column(Component):
    kind_name = "column"


class Grid(Component):
    kind_name = "grid"


class Flow(Component):
    kind_name = "flow"


class Split(Component):
    kind_name = "splitter"


class ScrollArea(Component):
    kind_name = "scroll"


class Toolbar(Component):
    kind_name = "toolbar"


class Section(Component):
    kind_name = "collapsible"


class Label(Component):
    kind_name = "label"

    def __init__(self, text="", **settings):
        super().__init__(text=text, **settings)


class Button(Component):
    kind_name = "button"

    def __init__(self, text="", *, command=None, **settings):
        super().__init__(text=text, callback=command, **settings)


class IconButton(Button):
    """Compact vector-icon action; use inside Flow for square sizing."""

    def __init__(self, icon, *, tooltip, command, **settings):
        if not icon or icon == "NONE":
            raise ValueError("IconButton requires an icon")
        if not tooltip.strip():
            raise ValueError("IconButton requires an accessible tooltip")
        super().__init__(icon=icon, tooltip=tooltip, command=command, **settings)

    def natural_width(self, scale, text):
        return self.height


class Field(Component):
    def __init__(self, text="", *, binding, **settings):
        super().__init__(text=text, getter=binding.get, setter=binding.set, **settings)


class Checkbox(Field):
    kind_name = "checkbox"


class NumberField(Field):
    kind_name = "number"


class Slider(Field):
    kind_name = "slider"


class TextField(Field):
    kind_name = "text"


class Dropdown(Field):
    kind_name = "dropdown"


class ListView(Field):
    kind_name = "list"


class TreeView(Field):
    kind_name = "tree"
