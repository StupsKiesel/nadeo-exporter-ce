"""Styles sourced from the user's active Blender theme, not factory colors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from typing import Any


_FAMILIES = {
    "button": "wcol_regular", "imagebutton": "wcol_regular", "confirm": "wcol_regular",
    "checkbox": "wcol_option", "toggle": "wcol_toggle",
    "number": "wcol_num", "vector": "wcol_num", "stepper": "wcol_num",
    "slider": "wcol_numslider", "range": "wcol_numslider", "knob": "wcol_numslider",
    "text": "wcol_text", "textarea": "wcol_text", "filepath": "wcol_text",
    "dropdown": "wcol_menu", "search": "wcol_menu", "radio": "wcol_radio",
    "tabs": "wcol_tab", "list": "wcol_list_item", "tree": "wcol_list_item",
    "table": "wcol_list_item", "reorder": "wcol_list_item", "multilist": "wcol_list_item",
    "foldercard": "wcol_list_item", "filecard": "wcol_list_item",
    "timeblock": "wcol_regular", "timeline": "wcol_numslider",
}
_SPACES = {
    "VIEW_3D": "view_3d", "IMAGE_EDITOR": "image_editor", "NODE_EDITOR": "node_editor",
    "NLA_EDITOR": "nla_editor", "DOPESHEET_EDITOR": "dopesheet_editor",
    "GRAPH_EDITOR": "graph_editor", "SEQUENCE_EDITOR": "sequence_editor",
}
_COLORS = frozenset({
    "panel", "header", "button", "button_hover", "button_pressed", "button_disabled",
    "text", "text_selected", "label_text", "disabled_text", "focus", "popup", "tooltip",
    "outline", "outline_selected", "item", "surface", "surface_alt", "panel_outline",
})
_OPTIONS = _COLORS | {"font", "font_size", "roundness"}


def _rgba(value) -> tuple[float, float, float, float]:
    channels = tuple(float(component) for component in value)
    if len(channels) == 3:
        channels += (1.0,)
    if len(channels) != 4 or not all(math.isfinite(c) and 0 <= c <= 1 for c in channels):
        raise ValueError("Style colors must be finite RGB/RGBA values in 0..1")
    return channels


def _mix(a, b, amount):
    return tuple(x + (y - x) * amount for x, y in zip(a, b))


def _over(foreground, background):
    alpha = foreground[3] + background[3] * (1 - foreground[3])
    return tuple(
        (foreground[i] * foreground[3] + background[i] * background[3] * (1 - foreground[3])) / alpha
        if alpha else 0.0 for i in range(3)
    ) + (alpha,)


def _separate(color, background, text, amount):
    if max(abs(color[i] - background[i]) for i in range(3)) < amount:
        return _mix(background, text[:3] + (background[3],), amount)
    return color


def _validate(overrides: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(overrides) - _OPTIONS
    if unknown:
        raise ValueError(f"Unknown GPUUI style options: {sorted(unknown)}")
    result = dict(overrides)
    for key in result.keys() & _COLORS:
        result[key] = _rgba(result[key])
    for key, low, high in (("font_size", 1, 128), ("roundness", 0, 128)):
        if key in result:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be a finite number in {low}..{high}")
    if "font" in result and not isinstance(result["font"], str):
        raise ValueError("font must be a font file path or an empty string")
    return result


@dataclass(frozen=True)
class Style:
    """Explicit semantic overrides; omitted values follow the active theme."""

    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        from types import MappingProxyType

        object.__setattr__(self, "values", MappingProxyType(_validate(self.values)))


class BlenderTheme:
    """Read live RNA settings and map widget families onto semantic draw colors."""

    def __init__(self, overrides: Mapping[str, Any] | None = None):
        self.overrides = Style(overrides or {})

    @staticmethod
    def value(context, path: str):
        """Expose any active-theme RNA option without mirroring its schema."""
        value = context.preferences.themes[0]
        for component in path.split("."):
            if not component or component.startswith("_"):
                raise ValueError(f"Invalid theme path: {path!r}")
            value = getattr(value, component)
        if hasattr(value, "to_tuple"):
            return value.to_tuple()
        return value

    def resolve(self, context, kind: str = "button", style: Style | None = None) -> dict[str, Any]:
        theme = context.preferences.themes[0]
        ui = theme.user_interface
        group = getattr(ui, _FAMILIES.get(kind, "wcol_regular"))
        area_type = context.area.type if context.area else "VIEW_3D"
        editor = getattr(theme, _SPACES.get(area_type, "view_3d"))
        # Blender 4.0 stores panel colors on the editor; newer versions store them on UI.
        panels = ui if hasattr(ui, "panel_back") else editor.space.panelcolors
        panel = _rgba(getattr(panels, "panel_back", None) if panels == ui else panels.back)
        header = _rgba(getattr(panels, "panel_header", None) if panels == ui else panels.header)
        inner, selected = _rgba(group.inner), _rgba(group.inner_sel)
        text, text_selected = _rgba(group.text), _rgba(group.text_sel)
        label = _rgba(ui.panel_text) if hasattr(ui, "panel_text") else _rgba(editor.space.text)
        sub = _rgba(ui.panel_sub_back) if hasattr(ui, "panel_sub_back") else _rgba(panels.sub_back)
        surface = _separate(_over(sub, panel), panel, label, 0.06)
        surface_alt = _separate(_over(sub, surface), surface, label, 0.06)
        header = _separate(header, panel, label, 0.10)
        panel_outline = _rgba(ui.panel_outline) if hasattr(ui, "panel_outline") else _rgba(group.outline)
        result = {
            "panel": panel, "header": header,
            "surface": surface, "surface_alt": surface_alt,
            "panel_outline": _separate(_over(panel_outline, panel), panel, label, 0.22),
            "button": inner,
            "button_hover": _mix(inner, selected, 0.25),
            "button_pressed": selected,
            "button_disabled": _mix(inner, panel, 0.5),
            "text": text, "text_selected": text_selected, "label_text": label,
            "disabled_text": text[:3] + (text[3] * 0.5,),
            "focus": _rgba(ui.wcol_numslider.item),
            "popup": _rgba(ui.wcol_menu_back.inner),
            "tooltip": _rgba(ui.wcol_tooltip.inner),
            "outline": _rgba(group.outline),
            "outline_selected": _rgba(group.outline_sel if hasattr(group, "outline_sel") else group.outline),
            "item": _rgba(group.item),
            "font": "",
            "font_size": float(context.preferences.ui_styles[0].widget.points),
            "roundness": float(group.roundness) * 10,
        }
        result.update(self.overrides.values)
        if style is not None:
            result.update(style.values)
        return result


def theme_spec(value=None) -> BlenderTheme:
    if value is None:
        return BlenderTheme()
    if isinstance(value, BlenderTheme):
        return value
    if isinstance(value, Mapping):
        return BlenderTheme(value)
    raise TypeError("theme must be a BlenderTheme or a mapping of style overrides")
