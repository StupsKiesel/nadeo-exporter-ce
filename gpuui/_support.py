"""Shared validation, icons and resources extracted from Serpens GPU UI."""

_shared = {}

import blf


import bpy


import colorsys


import gpu


import math


import os


import time


import traceback


from gpu_extras.batch import batch_for_shader


_GPU_VALUE_KINDS = frozenset((
    "checkbox", "toggle", "slider", "number", "dropdown", "tabs",
    "text", "radio", "color", "list", "range", "vector", "stepper", "knob", "search", "hotkey",
    "textarea", "table", "tree", "rating", "segmented", "flags", "palette", "xypad", "dial", "filepath",
    "timeline", "reorder", "multilist",
))


_GPU_NUMERIC_KINDS = ("slider", "number", "progress", "range", "vector", "stepper", "knob", "rating", "xypad",
                          "timeline")


_GPU_SCALAR_KINDS = ("slider", "number", "stepper", "knob", "rating", "timeline")


_GPU_DRAG_KINDS = ("slider", "range", "knob", "xypad", "dial", "timeline")


_GPU_ITEM_KINDS = ("tabs", "radio", "list", "vector", "stepper", "table", "tree", "rating", "segmented", "flags",
                       "palette", "reorder", "multilist")


_GPU_REGION_TOGGLES = {"UI": "show_region_ui", "TOOLS": "show_region_toolbar", "HEADER": "show_region_header",
                           "TOOL_HEADER": "show_region_tool_header", "ASSET_SHELF": "show_region_asset_shelf"}


_GPU_LIST_KINDS = ("list", "table", "tree", "reorder", "multilist")


_GPU_TEXT_KINDS = ("text", "textarea", "filepath")


_GPU_SEQUENCE_SIZES = {"color": (3, 4), "range": (2,), "vector": (2, 3, 4), "palette": (3, 4), "xypad": (2,)}


_GPU_CONTAINERS = ("root", "panel", "row", "column", "scroll", "tabs", "tab", "collapsible", "grid", "split",
                       "box", "splitter", "stack", "accordion", "flow", "toolbar", "statusbar", "commands")


_GPU_PANEL_MODES = ("window", "drawer", "dock", "hud", "attached")


_GPU_STACKED = ("column", "tabs", "tab", "collapsible", "box", "stack", "accordion")


_GPU_SPACE_KINDS = ("separator", "image", "spacer", "graph", "imagebutton", "barchart", "xypad")


_GPU_ICON_KINDS = ("button", "confirm", "label", "command")


def _gpu_icon_shapes():
    """Vector icons in a unit square: l=line, s=polyline, o=closed polyline, b=box, p=convex polygon, c=dot."""
    def arc(cx, cy, radius, start, end, steps=16):
        return [(cx + radius * math.cos(math.radians(start + (end - start) * index / steps)),
                 cy + radius * math.sin(math.radians(start + (end - start) * index / steps)))
                for index in range(steps + 1)]

    star = []
    for index in range(5):
        tip = math.radians(90 + index * 72)
        left, right = math.radians(90 + index * 72 + 36), math.radians(90 + index * 72 - 36)
        star.append(("p", [(0.5 + 0.19 * math.cos(right), 0.47 + 0.19 * math.sin(right)),
                           (0.5 + 0.44 * math.cos(tip), 0.47 + 0.44 * math.sin(tip)),
                           (0.5 + 0.19 * math.cos(left), 0.47 + 0.19 * math.sin(left))]))
    star.append(("p", [(0.5 + 0.19 * math.cos(math.radians(54 + index * 72)),
                        0.47 + 0.19 * math.sin(math.radians(54 + index * 72))) for index in range(5)]))
    gear = [("o", arc(0.5, 0.5, 0.24, 0, 360, 20)), ("c", 0.5, 0.5, 0.08)]
    for index in range(8):
        angle = math.radians(index * 45)
        gear.append(("l", 0.5 + 0.26 * math.cos(angle), 0.5 + 0.26 * math.sin(angle),
                     0.5 + 0.42 * math.cos(angle), 0.5 + 0.42 * math.sin(angle)))
    circle = ("o", arc(0.5, 0.5, 0.4, 0, 360, 24))
    return {
        "RENAME": [("l", 0.15, 0.2, 0.85, 0.2), ("l", 0.5, 0.2, 0.5, 0.8),
                   ("l", 0.25, 0.8, 0.75, 0.8)],
        "IMPORT": [("o", [(0.15, 0.65), (0.15, 0.15), (0.85, 0.15), (0.85, 0.65)]),
                   ("l", 0.5, 0.9, 0.5, 0.35), ("s", [(0.3, 0.55), (0.5, 0.35), (0.7, 0.55)])],
        "EXPORT": [("o", [(0.15, 0.65), (0.15, 0.15), (0.85, 0.15), (0.85, 0.65)]),
                   ("l", 0.5, 0.35, 0.5, 0.9), ("s", [(0.3, 0.7), (0.5, 0.9), (0.7, 0.7)])],
        "FIRST": [("l", 0.2, 0.2, 0.2, 0.8), ("p", [(0.75, 0.2), (0.3, 0.5), (0.75, 0.8)])],
        "FIT": [("s", [(0.1, 0.35), (0.1, 0.1), (0.35, 0.1)]),
                ("s", [(0.65, 0.1), (0.9, 0.1), (0.9, 0.35)]),
                ("s", [(0.1, 0.65), (0.1, 0.9), (0.35, 0.9)]),
                ("s", [(0.65, 0.9), (0.9, 0.9), (0.9, 0.65)])],
        "ADD": [("l", 0.2, 0.5, 0.8, 0.5), ("l", 0.5, 0.2, 0.5, 0.8)],
        "REMOVE": [("l", 0.2, 0.5, 0.8, 0.5)],
        "CUT": [("b", 0.1, 0.3, 0.3, 0.4), ("b", 0.6, 0.3, 0.3, 0.4),
                ("l", 0.5, 0.1, 0.5, 0.9)],
        "CLOSE": [("l", 0.25, 0.25, 0.75, 0.75), ("l", 0.25, 0.75, 0.75, 0.25)],
        "CHECKMARK": [("s", [(0.2, 0.5), (0.42, 0.27), (0.82, 0.75)])],
        "PLAY": [("p", [(0.3, 0.2), (0.8, 0.5), (0.3, 0.8)])],
        "PAUSE": [("b", 0.28, 0.2, 0.14, 0.6), ("b", 0.58, 0.2, 0.14, 0.6)],
        "STOP": [("b", 0.25, 0.25, 0.5, 0.5)],
        "ARROW_LEFT": [("l", 0.8, 0.5, 0.2, 0.5), ("s", [(0.45, 0.75), (0.2, 0.5), (0.45, 0.25)])],
        "ARROW_RIGHT": [("l", 0.2, 0.5, 0.8, 0.5), ("s", [(0.55, 0.75), (0.8, 0.5), (0.55, 0.25)])],
        "ARROW_UP": [("l", 0.5, 0.2, 0.5, 0.8), ("s", [(0.25, 0.55), (0.5, 0.8), (0.75, 0.55)])],
        "ARROW_DOWN": [("l", 0.5, 0.8, 0.5, 0.2), ("s", [(0.25, 0.45), (0.5, 0.2), (0.75, 0.45)])],
        "SEARCH": [("o", arc(0.42, 0.58, 0.24, 0, 360, 20)), ("l", 0.6, 0.4, 0.84, 0.16)],
        "FOLDER": [("p", [(0.1, 0.2), (0.9, 0.2), (0.9, 0.66), (0.1, 0.66)]),
                   ("p", [(0.1, 0.66), (0.42, 0.66), (0.36, 0.78), (0.1, 0.78)])],
        "FILE": [("o", [(0.25, 0.1), (0.75, 0.1), (0.75, 0.66), (0.55, 0.9), (0.25, 0.9)]),
                 ("s", [(0.55, 0.9), (0.55, 0.66), (0.75, 0.66)])],
        "TRASH": [("p", [(0.28, 0.12), (0.72, 0.12), (0.76, 0.68), (0.24, 0.68)]), ("b", 0.18, 0.74, 0.64, 0.08),
                  ("b", 0.4, 0.82, 0.2, 0.08)],
        "EYE": [("o", [(0.08, 0.5), (0.3, 0.7), (0.5, 0.76), (0.7, 0.7), (0.92, 0.5), (0.7, 0.3), (0.5, 0.24),
                       (0.3, 0.3)]), ("c", 0.5, 0.5, 0.13)],
        "LOCK": [("b", 0.24, 0.12, 0.52, 0.42), ("s", arc(0.5, 0.6, 0.17, 0, 180, 10))],
        "INFO": [circle, ("c", 0.5, 0.71, 0.05), ("l", 0.5, 0.27, 0.5, 0.57)],
        "WARNING": [("o", [(0.5, 0.88), (0.92, 0.14), (0.08, 0.14)]), ("l", 0.5, 0.64, 0.5, 0.4),
                    ("c", 0.5, 0.27, 0.045)],
        "ERROR": [circle, ("l", 0.35, 0.35, 0.65, 0.65), ("l", 0.35, 0.65, 0.65, 0.35)],
        "REFRESH": [("s", arc(0.5, 0.5, 0.32, 60, 340, 20)), ("p", [(0.56, 0.86), (0.8, 0.86), (0.7, 0.66)])],
        "HOME": [("p", [(0.1, 0.5), (0.5, 0.88), (0.9, 0.5)]), ("b", 0.24, 0.12, 0.52, 0.4)],
        "STAR": star,
        "GEAR": gear,
        "MENU": [("l", 0.2, 0.28, 0.8, 0.28), ("l", 0.2, 0.5, 0.8, 0.5), ("l", 0.2, 0.72, 0.8, 0.72)],
    }


_GPU_ICONS = _gpu_icon_shapes()


_GPU_LOG_COLORS = {"ERROR": (0.95, 0.4, 0.4, 1.0), "WARNING": (0.95, 0.75, 0.3, 1.0),
                       "SUCCESS": (0.45, 0.85, 0.45, 1.0)}


def _gpu_toast(text, level="INFO", duration=3.0):
    """Show a short message in the corner of every open GPU UI overlay."""
    import time
    store = _shared.setdefault("_gpu_toast_store", [])
    store.append({"text": str(text), "level": str(level).upper(), "start": time.monotonic(),
                  "duration": max(0.5, float(duration))})
    del store[:-20]


def _gpu_log(channel, text, level="INFO", clear=False):
    """Append lines to a GPU Console channel; Clear empties the channel first."""
    lines = _shared.setdefault("_gpu_log_store", {}).setdefault(str(channel), [])
    if clear:
        lines.clear()
    if str(text) or not clear:
        lines.extend((str(level).upper(), line) for line in (str(text).splitlines() or [""]))
    del lines[:-1000]


def _gpu_log_lines(channel):
    return list(_shared.get("_gpu_log_store", {}).get(str(channel), ()))


def _gpu_finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _gpu_parse_hotkey(text):
    """Turn "CTRL+SHIFT+P" into (key, {"ctrl", "shift"}) using Blender event type names."""
    parts = [part.strip().upper() for part in str(text).split("+") if part.strip()]
    names = {"CTRL": "ctrl", "SHIFT": "shift", "ALT": "alt", "OSKEY": "oskey", "CMD": "oskey"}
    if not parts or parts[-1] in names or any(part not in names for part in parts[:-1]):
        raise ValueError("GPU Command Palette Hotkey must look like CTRL+SHIFT+P (modifiers, then one key).")
    return (parts[-1], frozenset(names[part] for part in parts[:-1]))


def _gpu_reorder(collection, order):
    """Apply a reordered list to a Python list, or to a Blender collection, which can only move items."""
    order = list(order)
    if hasattr(collection, "move"):
        for target, entry in enumerate(order):
            current = target + list(collection)[target:].index(entry)
            if current != target:
                collection.move(current, target)
    else:
        collection[:] = order


def _gpu_grid_text(value):
    if isinstance(value, float):
        return format(value, ".4g")
    if isinstance(value, (set, frozenset)):
        return ", ".join(sorted(value)) or "-"
    if value is None:
        return "None"
    if hasattr(value, "bl_rna"):
        return str(getattr(value, "name", type(value).__name__))
    if not isinstance(value, str):
        try:
            return ", ".join(_gpu_grid_text(item) for item in value)
        except TypeError:
            pass
    return str(value)


def _gpu_grid_control(prop):
    """Pick a GPU widget and its settings for an RNA property, or None to show the value read-only."""
    kind, length = prop.type, getattr(prop, "array_length", 0)
    subtype = getattr(prop, "subtype", "NONE")
    if getattr(prop, "is_readonly", False):
        return None
    if kind == "BOOLEAN" and not length:
        return {"kind": "checkbox"}
    if kind in ("INT", "FLOAT"):
        low, high = getattr(prop, "soft_min", -1e9), getattr(prop, "soft_max", 1e9)
        if not (_gpu_finite(low) and _gpu_finite(high) and low < high):
            low, high = -1e9, 1e9
        # Blender stores float steps in hundredths.
        step = 1 if kind == "INT" else max(getattr(prop, "step", 1) / 100, 1e-6)
        numeric = {"minimum": low, "maximum": high, "step": step}
        if not length:
            return dict(numeric, kind="slider" if subtype in ("FACTOR", "PERCENTAGE") else "number")
        if kind == "FLOAT" and subtype in ("COLOR", "COLOR_GAMMA") and length in (3, 4):
            return {"kind": "color"}
        if 2 <= length <= 4:
            return dict(numeric, kind="vector")
        return None
    if kind == "ENUM":
        items = [(item.identifier, item.name) for item in prop.enum_items]
        if not items:
            return None
        return {"kind": "flags" if prop.is_enum_flag else "dropdown", "items": items}
    if kind == "STRING":
        if subtype in ("FILE_PATH", "DIR_PATH"):
            return {"kind": "filepath", "folder": subtype == "DIR_PATH"}
        return {"kind": "text"}
    return None


def _gpu_property_grid(parent, key, data, names=(), ratio=0.4, enabled=True, undo=True):
    """Add a label/control row for each property of a Blender struct, choosing a widget per property type."""
    if not _gpu_finite(ratio) or not 0.05 <= ratio <= 0.95:
        raise ValueError("GPU Property Grid Label Width must be between 0.05 and 0.95.")
    grid = parent.add("column", key=key, gap=4)
    if data is None:
        grid.add("label", key=key + "__empty", text="(no data)")
        return grid
    rna = getattr(data, "bl_rna", None)
    if rna is None:
        raise ValueError("GPU Property Grid Data must be a Blender data-block or struct, not " +
                         type(data).__name__ + ".")
    if isinstance(names, str):
        names = [name.strip() for name in names.split(",") if name.strip()]
    if names:
        chosen = []
        for name in names:
            prop = rna.properties.get(str(name))
            if prop is None:
                raise ValueError("GPU Property Grid: " + rna.identifier + " has no property " + repr(name) + ".")
            chosen.append(prop)
    else:
        chosen = [prop for prop in rna.properties if prop.identifier != "rna_type"
                  and not getattr(prop, "is_hidden", False) and prop.type not in ("POINTER", "COLLECTION")]
    for prop in chosen:
        identifier = prop.identifier
        row = grid.add("split", key=key + "__" + identifier, ratio=ratio, gap=6)
        row.add("label", key=row.key + "__label", text=prop.name or identifier, tooltip=prop.description)
        settings = _gpu_grid_control(prop)
        if settings is None:
            row.add("label", key=row.key + "__value", text=_gpu_grid_text(getattr(data, identifier)),
                    tooltip=prop.description)
            continue
        row.add(settings.pop("kind"), key=row.key + "__control", enabled=enabled, undo=undo,
                tooltip=prop.description,
                getter=lambda context, data=data, name=identifier: getattr(data, name),
                setter=lambda context, value, data=data, name=identifier: setattr(data, name, value),
                **settings)
    return grid
