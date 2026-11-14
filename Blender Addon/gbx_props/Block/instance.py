"""Block instance PropertyGroup (lives on ``bpy.types.Object`` Empties).

A grid-placed block in a map. Shares :class:`GbxBlockPlacement` with the
other block-style placements so the map serializer can treat them
uniformly.
"""

from __future__ import annotations

import bpy
from bpy.props import PointerProperty

from .._common import GbxBlockPlacement


class GbxBlockInstance(GbxBlockPlacement):
    """A placement of a Block in a map."""

    pass


CLASSES = (GbxBlockInstance,)
_ATTR = "gbx_block_instance"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Object, _ATTR, PointerProperty(type=GbxBlockInstance))


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
