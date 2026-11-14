"""Map element colours: ``CPlugMaterialColorTargetTable`` palettes.

TM2020 recolours hue-masked materials per placed block/item. Three
inputs decide the colour:

* the element's ``MapElemColor`` (``Default``/``White``/``Green``/
  ``Blue``/``Red``/``Black``, map body chunk ``0x03043062``);
* the map palette (``EMapElemColorPalette``, chunk ``0x0304306C``);
* the material's colour table, ``<Coll>/Media/ColorTargetTables/
  <Name>.ColorTable.gbx.json`` referenced by the material, falling back to
  ``Default``.

Each table row lists five ``#rrggbb[aa]`` colours in ``MapElemColor - 1``
order. ``Classic`` and ``Stunt`` use the material's own table; every
other palette uses the ramp of that name in ``Default`` (the only table
that has one). ``Default`` colour means "no recolour".

Py/Pxz hue materials (``TrackWall``) follow
:func:`projected_palette_targets` instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Optional

log = logging.getLogger("gbxbpy.color_tables")

ELEMENT_COLORS = ("Default", "White", "Green", "Blue", "Red", "Black")
DEFAULT_TABLE = "Stadium/Media/ColorTargetTables/Default.ColorTable.gbx.json"
_OWN_TABLE_PALETTES = frozenset({"classic", "stunt"})
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")

RGB = tuple[float, float, float]


def element_color_index(color: str) -> int:
    """Return the ``MapElemColor`` index (0 = Default = no recolour)."""
    try:
        return ELEMENT_COLORS.index(str(color or "Default"))
    except ValueError:
        return 0


def srgb_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def parse_color_table(text: str) -> dict[str, tuple[RGB, ...]]:
    """Parse a colour table into ``row -> sRGB colours`` (0..1).

    Nadeo's files contain trailing commas, and some hex values carry an
    alpha byte, which is ignored.
    """
    data = json.loads(_TRAILING_COMMA.sub(r"\1", text))
    rows: dict[str, tuple[RGB, ...]] = {}
    for name, values in data.items():
        if not isinstance(values, list):
            continue
        colors = []
        for value in values:
            digits = str(value).lstrip("#")
            if len(digits) < 6:
                break
            colors.append(tuple(int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4)))
        else:
            rows[name] = tuple(colors)  # type: ignore[arg-type]
    return rows


@lru_cache(maxsize=64)
def _load_table(path: str) -> dict[str, tuple[RGB, ...]]:
    with open(path, encoding="utf-8-sig") as fh:
        return parse_color_table(fh.read())


def palette_targets(
    resolver, table_path: str = "", palette: str = "Classic", *, source_path: str = "",
) -> Optional[tuple[RGB, ...]]:
    """Return the five linear target colours for one material, or ``None``.

    ``table_path`` is the material's referenced table (may be empty) and
    ``source_path`` the material file it was referenced from.
    """
    palette = palette or "Classic"
    own = palette.lower() in _OWN_TABLE_PALETTES
    candidates = [(table_path, source_path)] if own and table_path else []
    candidates.append((DEFAULT_TABLE, ""))
    for ref, src in candidates:
        resolved = resolver.resolve(ref, source_path=src) if resolver is not None else None
        if not resolved:
            continue
        try:
            rows = _load_table(os.path.normpath(resolved))
        except (OSError, ValueError) as exc:
            log.warning("colour table %s unreadable: %s", resolved, exc)
            continue
        row = rows.get(palette) or next(
            (v for k, v in rows.items() if k.lower() == palette.lower()), None,
        )
        if row is None and own:
            row = rows.get("Classic")
        if row:
            # Ramps shorter than five entries clamp to their last shade.
            row = tuple(row[min(i, len(row) - 1)] for i in range(5))
            return tuple(tuple(srgb_to_linear(c) for c in rgb) for rgb in row)  # type: ignore[return-value]
    log.warning("no colour targets for palette %r (table %r)", palette, table_path)
    return None


def named_table_path(material_path: str) -> str:
    """Return the colour table named after a material (``<Coll>/Media/
    ColorTargetTables/<Stem>.ColorTable.gbx.json``).

    The unpacked ``Work/GameData`` materials no longer reference their
    table, but the stock tables carry the material's name (``TrackWall``,
    ``Canopy``, ``CanopyStructure``).
    """
    norm = str(material_path or "").replace("\\", "/")
    stem = norm.rsplit("/", 1)[-1]
    stem = re.sub(r"\.material\.gbx$", "", stem, flags=re.IGNORECASE)
    if not stem:
        return ""
    collection = "Stadium"
    parts = norm.split("/")
    lowered = [p.lower() for p in parts]
    if "media" in lowered and lowered.index("media") > 0:
        collection = parts[lowered.index("media") - 1]
    return f"{collection}/Media/ColorTargetTables/{stem}.ColorTable.gbx.json"


def projected_palette_targets(
    resolver, material_path: str, palette: str = "Classic", target_id: int = 0,
) -> Optional[tuple[RGB, ...]]:
    """Return six linear targets (index 0..5) for a Py/Pxz hue material.

    ``Tech3 Block PyPxz*`` materials such as ``TrackWall`` carry
    ``BaseColorTarget_PyPxz``/``BaseColorTargetId`` instead of a plain
    ``BaseColorTarget``. Index 0 is the table's ``BaseColorTargetId`` entry;
    the adapter does not apply it because ``Default`` placements keep their
    original texture. Palettes the material's own table has no row for
    (the ``Red``..``Black`` ramps live only in ``Default``) use that same
    neutral entry for colours 1..5: the game draws Red walls of a
    Red-palette map neutral grey. ``Classic`` and ``Stunt`` index their row
    by the element colour. ``None`` without a material-named table.
    """
    ref = named_table_path(material_path)
    resolved = resolver.resolve(ref) if (resolver is not None and ref) else None
    if not resolved:
        return None
    try:
        rows = _load_table(os.path.normpath(resolved))
    except (OSError, ValueError) as exc:
        log.warning("colour table %s unreadable: %s", resolved, exc)
        return None
    base_row = rows.get("Colors") or rows.get("Classic")
    if not base_row:
        return None
    neutral = base_row[min(max(int(target_id), 0), len(base_row) - 1)]
    row = next(
        (v for k, v in rows.items() if k.lower() == (palette or "Classic").lower()), None,
    ) if (palette or "Classic").lower() in _OWN_TABLE_PALETTES else None
    colors = [neutral] + [
        row[min(i, len(row) - 1)] if row else neutral for i in range(5)
    ]
    return tuple(tuple(srgb_to_linear(c) for c in rgb) for rgb in colors)  # type: ignore[return-value]


__all__ = [
    "ELEMENT_COLORS", "DEFAULT_TABLE", "element_color_index", "srgb_to_linear",
    "parse_color_table", "palette_targets", "named_table_path",
    "projected_palette_targets",
]
