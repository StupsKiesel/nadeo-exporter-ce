"""Material modifiers that baked fillers inherit from the blocks they finish.

Generated clip and pillar records carry no material modifier of their own.
The game draws them with the modifier of the authored block they complete, so
the ``DecoWallBaseVFC`` panels around a ``DecoWallBaseIce`` cliff wear the
``PlatformIce`` folder's ``TrackWall`` (DecoCliffIcePxz concrete) instead of
the stock wooden TrackWall.

The rule follows vjeux/trackmania-tas ``tools/mapgeom/src/tiny_library.rs``
(``inherited_mod``, documented in ``tools/tmmaps/TINY.md``):

1. Every unit cell of an authored grid block records the block's terrain
   modifiers (``X.TerrainModifier.Gbx`` or ``TrackWallToDecoCliff.Gbx``); the
   first block that has one wins the cell.
2. A pillar cell without one takes the modifier of the block it supports:
   the first non-pillar unit above the pillar stack, preferring a block whose
   variant places this pillar kind, then the block's origin cell.
3. A baked record takes the modifier of the cell across its side. If that cell
   is a pillar, its answer is final, even when empty. Otherwise it falls back to
   its own cell, then to the first dressed cell up the across column, then up
   its own column.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from ..model.block import Block
from ..model.map import Map, MapBlock
from .assets import pick_variant
from .dependencies import Dependencies

# (dx, dz) per raw direction: North +z, East -x, South -z, West +x.
_SIDE_VEC = {"North": (0, 1), "East": (-1, 0), "South": (0, -1), "West": (1, 0)}
_DIR_INDEX = {"North": 0, "East": 1, "South": 2, "West": 3}

Cell = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _Unit:
    y: int
    pillar: bool
    mods: tuple[str, ...]
    name: str
    places: frozenset[str]
    origin: bool


def is_inheritable_modifier(path: str) -> bool:
    """Whether ``path`` is a modifier that dresses generated fillers."""
    name = path.replace(" ", "").replace("/", "\\").rsplit("\\", 1)[-1].casefold()
    return name.endswith(".terrainmodifier.gbx") or name == "trackwalltodecocliff.gbx"


def _stem(path: str) -> str:
    return os.path.basename(path.replace("\\", "/")).split(".", 1)[0].casefold()


def unit_cells(block: MapBlock, offsets: list[tuple[int, int, int]]) -> list[Cell]:
    """Grid cells covered by ``block`` for its variant's unit offsets."""
    offsets = offsets or [(0, 0, 0)]
    xs = [u[0] for u in offsets]
    zs = [u[2] for u in offsets]
    minx, minz = min(xs), min(zs)
    w, d = max(xs) - minx + 1, max(zs) - minz + 1
    dir_index = _DIR_INDEX.get(block.direction, 0)
    cx, cy, cz = block.coords
    cells = []
    for ux, uy, uz in offsets:
        x, z = ux - minx, uz - minz
        rx, rz = ((x, z), (d - 1 - z, x), (w - 1 - x, d - 1 - z), (z, w - 1 - x))[dir_index]
        cells.append((cx + rx, cy + uy, cz + rz))
    return cells


def _across(cell: Cell, direction: str) -> Cell | None:
    vec = _SIDE_VEC.get(direction)
    if vec is None:
        return None
    return (cell[0] + vec[0], cell[1], cell[2] + vec[1])


def baked_modifiers(
    map_asset: Map, dependencies: Dependencies,
    progress: Callable[[float, str], None] | None = None,
) -> dict[int, tuple[str, ...]]:
    """Inherited modifier paths per ``map_asset.baked_blocks`` index.

    Records that inherit nothing are omitted. ``progress(fraction, message)``
    is called with ``fraction`` in 0..1; reading the block definitions is the
    slow part and takes the first 90%.
    """
    if not map_asset.baked_blocks:
        return {}
    progress = progress or (lambda _fraction, _message: None)
    cell_mod: dict[Cell, tuple[str, ...]] = {}
    pillar_cells: set[Cell] = set()
    columns: dict[tuple[int, int], list[_Unit]] = defaultdict(list)

    total = max(1, len(map_asset.blocks))
    for done, block in enumerate(map_asset.blocks, 1):
        progress(0.9 * done / total, f"Resolving baked modifiers: blocks {done}/{total}")
        if block.is_free:
            continue
        mods: tuple[str, ...] = ()
        offsets: list[tuple[int, int, int]] = []
        places: frozenset[str] = frozenset()
        _path, asset = dependencies.stock_block(
            block.name, block.is_clip, "ground" if block.is_ground else "air",
            block.block_variant_index, block.mobil_index, block.mobil_variant_index,
        )
        if isinstance(asset, Block):
            mods = tuple(
                p for p in (asset.material_modifier_path, asset.material_modifier2_path)
                if p and is_inheritable_modifier(p)
            )
            variant = pick_variant(asset, "ground" if block.is_ground else "air", block.block_variant_index)
            if variant is not None:
                offsets = [tuple(u.relative_offset) for u in variant.units]
                places = frozenset(_stem(p) for p in variant.placed_pillar_paths if p)
        if block.is_pillar:
            pillar_cells.add(tuple(block.coords))
        for cell in unit_cells(block, offsets):
            if not cell_mod.get(cell):
                cell_mod[cell] = mods
            columns[(cell[0], cell[2])].append(_Unit(
                cell[1], block.is_pillar, mods, block.name.casefold(), places, cell == tuple(block.coords),
            ))

    progress(0.9, "Resolving baked modifiers: pillars")
    for (x, z), column in columns.items():
        for unit in column:
            if not unit.pillar or unit.mods:
                continue
            top = unit.y
            while True:
                top += 1
                if top > 255:
                    break
                here = [u for u in column if u.y == top]
                if not here or any(u.pillar for u in here):
                    if not here and top > unit.y + 2:
                        break
                    continue
                break
            above = [u for u in column if u.y == top and not u.pillar]
            if not above:
                continue
            placers = [u for u in above if unit.name in u.places]
            pick = (
                next((u for u in placers if u.origin), None) or (placers[0] if placers else None)
                or next((u for u in above if u.origin), None) or above[0]
            )
            cell = (x, unit.y, z)
            if pick.mods and not cell_mod.get(cell):
                cell_mod[cell] = pick.mods

    def first_up(x: int, y: int, z: int) -> tuple[str, ...]:
        for dy in range(1, 32):
            if y + dy > 255:
                break
            mods = cell_mod.get((x, y + dy, z))
            if mods:
                return mods
        return ()

    progress(0.95, "Resolving baked modifiers: baked records")
    result: dict[int, tuple[str, ...]] = {}
    for index, record in enumerate(map_asset.baked_blocks):
        if record.is_free:
            continue
        cell = tuple(record.coords)
        across = _across(cell, record.direction)
        mods: tuple[str, ...] = ()
        if across is not None and across in pillar_cells:
            mods = cell_mod.get(across, ())
        elif across is not None and cell_mod.get(across):
            mods = cell_mod[across]
        elif cell_mod.get(cell):
            mods = cell_mod[cell]
        else:
            if across is not None:
                mods = first_up(*across)
            mods = mods or first_up(*cell)
        if mods:
            result[index] = mods
    progress(1.0, "Resolving baked modifiers")
    return result


__all__ = ["baked_modifiers", "is_inheritable_modifier", "unit_cells"]
