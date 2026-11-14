"""Item instance PropertyGroup (lives on ``bpy.types.Object`` Empties).

A placement of an Item (anchored object) in a map. Source identification
goes through ``data_ref`` (Nadeo built-in -> GameData collection,
non-Nadeo -> embedded item collection); no model id / collection /
author strings are kept on the instance.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    IntVectorProperty,
    PointerProperty,
    StringProperty,
)

from .._common import COLOR_ITEMS, GbxCommonInstance


class GbxItemInstance(GbxCommonInstance):
    """A placement of an Item in a map."""

    block_unit_coord: IntVectorProperty(
        name="Block Unit Coord",
        description="Grid cell the item is anchored in (X, Y, Z)",
        size=3,
        default=(0, 0, 0),
    )
    pivot: FloatVectorProperty(
        name="Pivot",
        description="Offset from the item origin to its anchor pivot",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='XYZ',
    )
    scale: FloatProperty(
        name="Scale",
        default=1.0,
        min=0.0,
    )
    variant_index: IntProperty(
        name="Variant",
        default=0,
        min=0,
    )
    color: EnumProperty(
        name="Color",
        items=COLOR_ITEMS,
        default='DEFAULT',
    )
    anim_phase_offset: IntProperty(
        name="Anim Phase Offset",
        default=0,
    )
    anchor_tree_id: StringProperty(
        name="Anchor Tree Id",
        description="Waypoint linking id (start / cp / finish / multilap)",
        default="",
    )


CLASSES = (GbxItemInstance,)
_ATTR = "gbx_item_instance"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Object, _ATTR, PointerProperty(type=GbxItemInstance))


def unregister() -> None:
    if hasattr(bpy.types.Object, _ATTR):
        try:
            delattr(bpy.types.Object, _ATTR)
        except Exception:
            pass
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
