"""Prefab data PropertyGroup (lives on ``bpy.types.Collection``).

A Prefab definition (CPlugPrefab / CGameCommonItemEntityModel). Stores
only the prefab-level metadata; the entity *list* is materialised as
real child Empties in this Collection, each carrying a
``gbx_prefab_entity_instance`` group.
"""

from __future__ import annotations

import bpy
from bpy.props import IntProperty, PointerProperty, StringProperty

from .._common import GbxCommonData


class GbxPrefabData(GbxCommonData):
    """Definition of a Prefab."""

    prefab_version: IntProperty(
        name="Prefab Version",
        description="CPlugPrefab body version",
        default=0,
        min=0,
    )
    url: StringProperty(
        name="URL",
        description="External prefab reference URL (empty for local prefabs)",
        default="",
    )


CLASSES = (GbxPrefabData,)
_ATTR = "gbx_prefab_data"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Collection, _ATTR, PointerProperty(type=GbxPrefabData))


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
