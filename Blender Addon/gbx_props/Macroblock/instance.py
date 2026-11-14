"""Macroblock instance PropertyGroup (lives on ``bpy.types.Object``).

A macroblock paste in a map: same grid placement record as a stock
block, but ``data_ref`` points at a Macroblock data Collection (which
expands into many blocks/items at import time).
"""

from __future__ import annotations

import bpy
from bpy.props import PointerProperty

from .._common import GbxBlockPlacement


class GbxMacroblockInstance(GbxBlockPlacement):
    """A placement of a Macroblock on the editor grid."""

    pass


CLASSES = (GbxMacroblockInstance,)
_ATTR = "gbx_macroblock_instance"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Object, _ATTR, PointerProperty(type=GbxMacroblockInstance))


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
