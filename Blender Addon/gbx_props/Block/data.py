"""Block data PropertyGroup (lives on ``bpy.types.Collection``).

Custom block authored as ``Foo.Block.Gbx`` and embedded into the map.
Also the home of the block-footprint metadata (``block_size``) used by
every block-style placement (EDClassic, Block, clip family) to compute
their Object transform.
"""

from __future__ import annotations

import bpy
from bpy.props import IntVectorProperty, PointerProperty

from .._common import GbxCommonData


class GbxBlockData(GbxCommonData):
    """Definition of a custom Block."""

    block_size: IntVectorProperty(
        name="Block Size",
        description="Footprint of the block in editor units (X, Y, Z) – TM Y-up convention",
        size=3,
        default=(1, 1, 1),
        min=1,
    )


CLASSES = (GbxBlockData,)
_ATTR = "gbx_block_data"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Collection, _ATTR, PointerProperty(type=GbxBlockData))


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
