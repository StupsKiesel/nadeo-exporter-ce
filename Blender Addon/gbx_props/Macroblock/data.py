"""Macroblock data PropertyGroup (lives on ``bpy.types.Collection``)."""

from __future__ import annotations

import bpy
from bpy.props import PointerProperty

from .._common import GbxCommonData


class GbxMacroblockData(GbxCommonData):
    """Definition of a Macroblock (only ``source_file_path`` for now)."""

    pass


CLASSES = (GbxMacroblockData,)
_ATTR = "gbx_macroblock_data"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Collection, _ATTR, PointerProperty(type=GbxMacroblockData))


def unregister() -> None:
    if hasattr(bpy.types.Collection, _ATTR):
        try:
            delattr(bpy.types.Collection, _ATTR)
        except Exception:
            pass
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
