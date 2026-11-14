"""Prefab – CPlugPrefab / CGameCommonItemEntityModel.

Two PropertyGroups (data-only at the map level; never directly
instanced into a map):

* ``GbxPrefabData``           on Collection — the prefab definition.
* ``GbxPrefabEntityInstance`` on Object     — one sub-entity slot inside
                                              the prefab data Collection.
"""

from __future__ import annotations

from . import data, entity_instance


def register() -> None:
    data.register()
    entity_instance.register()


def unregister() -> None:
    entity_instance.unregister()
    data.unregister()
