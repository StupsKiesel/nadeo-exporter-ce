"""Shared building blocks for every gbx property model.

* ``GbxCommonData``     – base for *data* PropertyGroups (lives on Collections).
                          Carries only ``source_file_path`` for now.
* ``GbxCommonInstance`` – base for *instance* PropertyGroups (lives on Objects).
                          Carries the ``data_ref`` pointer back to the source
                          Collection.
* ``GbxBlockPlacement`` – mixin used by every grid-placed block instance
                          (EDClassic, Block, EDClip, EDHorizontalClip,
                          EDVerticalClip, EDFlat). Carries the placement
                          fields (``coord``, ``direction``, ``is_*``,
                          ``color``, variant indices) and an ``update``
                          callback that mirrors them onto the Object's
                          transform.
* ``KIND_ITEMS``        – the master enum of every gbx kind we model.
"""

from __future__ import annotations

import math

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    IntVectorProperty,
    PointerProperty,
    StringProperty,
)


# ---------------------------------------------------------------------------
# Shared enums (used by several gbx types)
# ---------------------------------------------------------------------------

DIRECTION_ITEMS = [
    ('NORTH', "North", ""),
    ('EAST',  "East",  ""),
    ('SOUTH', "South", ""),
    ('WEST',  "West",  ""),
]

COLOR_ITEMS = [
    ('DEFAULT', "Default", ""),
    ('WHITE',   "White",   ""),
    ('GREEN',   "Green",   ""),
    ('BLUE',    "Blue",    ""),
    ('RED',     "Red",     ""),
    ('BLACK',   "Black",   ""),
]


# Order here is the order shown in the kind dropdown.
KIND_ITEMS = [
    ('NONE',                "None",             "Not a GBX object"),
    ('ED_CLASSIC',          "EDClassic",        "Classic editor block"),
    ('BLOCK',               "Block",            "Custom .Block.Gbx block"),
    ('ED_CLIP',             "EDClip",           "Generic clip"),
    ('ED_HORIZONTAL_CLIP',  "EDHorizontalClip", "Horizontal clip"),
    ('ED_VERTICAL_CLIP',    "EDVerticalClip",   "Vertical clip"),
    ('ED_FRONTIER',         "EDFrontier",       "Frontier clip"),
    ('ED_FLAT',             "EDFlat",           "Flat clip"),
    ('ED_TRANSITION',       "EDTransition",     "Transition clip"),
    ('ITEM',                "Item",             "Editor item"),
    ('PREFAB',              "Prefab",           "Prefab"),
    ('MACROBLOCK',          "Macroblock",       "Macroblock"),
    ('DECORATION',          "Decoration",       "Decoration"),
    ('DECORATION_MOOD',     "DecorationMood",   "Decoration mood"),
    ('DECORATION_SIZE',     "DecorationSize",   "Decoration size"),
    ('DECORATION_AUDIO',    "DecorationAudio",  "Decoration audio"),
    ('STATIC_OBJECT',       "StaticObject",     "Static object"),
    ('DYNA_OBJECT',         "DynaObject",       "Dynamic object"),
    ('DECAL',               "Decal",            "Decal"),
    ('VEGET_TREE_MODEL',    "VegetTreeModel",   "Vegetation tree model"),
]


class GbxCommonData(bpy.types.PropertyGroup):
    """Fields shared by every gbx *data* PropertyGroup."""

    source_file_path: StringProperty(
        name="Source File Path",
        description="Original .Gbx path this data was imported from",
        subtype='FILE_PATH',
        default="",
    )


class GbxCommonInstance(bpy.types.PropertyGroup):
    """Fields shared by every gbx *instance* PropertyGroup."""

    data_ref: PointerProperty(
        name="Data",
        description="Source data collection this instance references",
        type=bpy.types.Collection,
    )


# ---------------------------------------------------------------------------
# Block-placement transform sync
# ---------------------------------------------------------------------------

# Stadium grid in meters (TM convention: X width, Y up, Z depth).
_BLOCK_GRID_X = 32.0
_BLOCK_GRID_Y = 8.0
_BLOCK_GRID_Z = 32.0

# Direction -> Z rotation in Blender (CW from above).
_DIR_TO_ROT_Z = {
    'NORTH':  0.0,
    'EAST':  -math.pi / 2.0,
    'SOUTH': -math.pi,
    'WEST':  -3.0 * math.pi / 2.0,
}


def _data_block_size(coll) -> tuple[int, int, int]:
    """Return ``(sx, sy, sz)`` block-unit footprint from the data Collection.

    Looks up ``block_size`` on whichever gbx data PropertyGroup the
    Collection carries. Lives on ``GbxBlockData``; other data types fall
    back to ``(1, 1, 1)``.
    """
    if coll is None:
        return (1, 1, 1)
    for attr in ("gbx_block_data",):
        data = getattr(coll, attr, None)
        if data is not None and hasattr(data, "block_size"):
            sx, sy, sz = data.block_size
            return (int(sx), int(sy), int(sz))
    return (1, 1, 1)


def _on_block_placement_change(self, context):
    """Mirror ``coord`` / ``direction`` onto the Object's transform.

    Skipped when ``is_free`` is on (free blocks own their own transform).
    Reads block footprint from ``data_ref`` so rotated multi-cell blocks
    still fill their grid cells.
    """
    obj = self.id_data
    if obj is None or self.is_free:
        return

    sx, _sy, sz = _data_block_size(self.data_ref)
    cx, cy, cz = self.coord

    # Block-coord world position (already in Blender orientation).
    bx = cx * _BLOCK_GRID_X
    by = -cz * _BLOCK_GRID_Z
    bz = cy * _BLOCK_GRID_Y

    # Footprint compensation so a rotated block keeps filling its cells.
    sxm = sx * _BLOCK_GRID_X
    szm = sz * _BLOCK_GRID_Z
    direction = self.direction
    if direction == 'EAST':
        bx += szm
    elif direction == 'SOUTH':
        bx += sxm
        by += -szm
    elif direction == 'WEST':
        by += -sxm

    obj.location = (bx, by, bz)
    obj.rotation_mode = 'XYZ'
    obj.rotation_euler = (0.0, 0.0, _DIR_TO_ROT_Z.get(direction, 0.0))


class GbxBlockPlacement(GbxCommonInstance):
    """Mixin: fields common to every grid-placed block in a map.

    Used by ``EDClassic``, ``Block`` (custom), and the clip family
    (``EDClip``, ``EDHorizontalClip``, ``EDVerticalClip``, ``EDFlat``)
    because the map serializer writes them all into the same block record
    structure.
    """

    coord: IntVectorProperty(
        name="Coord",
        description="Block coordinate in the editor grid (X, Y, Z)",
        size=3,
        default=(0, 0, 0),
        update=_on_block_placement_change,
    )
    direction: EnumProperty(
        name="Direction",
        items=DIRECTION_ITEMS,
        default='NORTH',
        update=_on_block_placement_change,
    )
    is_ground: BoolProperty(name="Is Ground", default=False)
    is_free: BoolProperty(
        name="Is Free",
        description="Free placement (use Object transform; ignore Coord)",
        default=False,
        update=_on_block_placement_change,
    )
    is_ghost: BoolProperty(name="Is Ghost", default=False)
    is_pillar: BoolProperty(name="Is Pillar", default=False)
    is_waypoint: BoolProperty(name="Is Waypoint", default=False)
    is_skinnable: BoolProperty(name="Is Skinnable", default=False)
    color: EnumProperty(name="Color", items=COLOR_ITEMS, default='DEFAULT')
    block_variant_index: IntProperty(name="Block Variant", default=0, min=0)
    mobil_variant_index: IntProperty(name="Mobil Variant", default=0, min=0)
    mobil_index: IntProperty(name="Mobil Index", default=0, min=0)


CLASSES = (GbxCommonData, GbxCommonInstance, GbxBlockPlacement)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Object.gbx_kind = EnumProperty(
        name="GBX Kind",
        description="Which gbx instance model is active on this object",
        items=KIND_ITEMS,
        default='NONE',
    )
    bpy.types.Collection.gbx_kind = EnumProperty(
        name="GBX Kind",
        description="Which gbx data model is active on this collection",
        items=KIND_ITEMS,
        default='NONE',
    )


def unregister() -> None:
    for attr in ("gbx_kind",):
        if hasattr(bpy.types.Object, attr):
            try:
                delattr(bpy.types.Object, attr)
            except Exception:
                pass
        if hasattr(bpy.types.Collection, attr):
            try:
                delattr(bpy.types.Collection, attr)
            except Exception:
                pass

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
