"""``gbx_props`` – PropertyGroup models for every supported gbx type.

Layout
------
::

    gbx_props/
        _common.py              shared bases + KIND_ITEMS enum
        EDClassic/              one folder per gbx type
            data.py             Collection-attached "definition"
            instance.py         Object-attached "placement"
        EDClip/
            data.py
            instance.py
        ...

Each type folder is a normal Python package, so future per-type logic scripts
(serializers, validators, panels, operators) can sit alongside ``data.py`` and
``instance.py`` without touching anything else.

Conventions
-----------
* Data PropertyGroups live on ``bpy.types.Collection`` as
  ``Collection.gbx_<type_snake>_data``.
* Instance PropertyGroups live on ``bpy.types.Object`` as
  ``Object.gbx_<type_snake>_instance``.
* ``Object.gbx_kind`` / ``Collection.gbx_kind`` (defined in ``_common``) select
  which group is the *active* one. UI / serializers should always read the
  active group via :func:`active_data` / :func:`active_instance`.
"""

from __future__ import annotations

from . import _common
from . import EDClassic
from . import Block
from . import EDClip
from . import EDHorizontalClip
from . import EDVerticalClip
from . import EDFrontier
from . import EDFlat
from . import EDTransition
from . import Item
from . import Prefab
from . import Macroblock
from . import Decoration
from . import DecorationMood
from . import DecorationSize
from . import DecorationAudio
from . import StaticObject
from . import DynaObject
from . import Decal
from . import VegetTreeModel


# Order matters for register: _common first (bases + kind enums), then types.
_TYPE_MODULES = (
    EDClassic,
    Block,
    EDClip,
    EDHorizontalClip,
    EDVerticalClip,
    EDFrontier,
    EDFlat,
    EDTransition,
    Item,
    Prefab,
    Macroblock,
    Decoration,
    DecorationMood,
    DecorationSize,
    DecorationAudio,
    StaticObject,
    DynaObject,
    Decal,
    VegetTreeModel,
)


# kind enum value -> attribute name on Collection / Object
KIND_TO_DATA_ATTR = {
    'ED_CLASSIC':         'gbx_ed_classic_data',
    'BLOCK':              'gbx_block_data',
    'ED_CLIP':            'gbx_ed_clip_data',
    'ED_HORIZONTAL_CLIP': 'gbx_ed_horizontal_clip_data',
    'ED_VERTICAL_CLIP':   'gbx_ed_vertical_clip_data',
    'ED_FRONTIER':        'gbx_ed_frontier_data',
    'ED_FLAT':            'gbx_ed_flat_data',
    'ED_TRANSITION':      'gbx_ed_transition_data',
    'ITEM':               'gbx_item_data',
    'PREFAB':             'gbx_prefab_data',
    'MACROBLOCK':         'gbx_macroblock_data',
    'DECORATION':         'gbx_decoration_data',
    'DECORATION_MOOD':    'gbx_decoration_mood_data',
    'DECORATION_SIZE':    'gbx_decoration_size_data',
    'DECORATION_AUDIO':   'gbx_decoration_audio_data',
    'STATIC_OBJECT':      'gbx_static_object_data',
    'DYNA_OBJECT':        'gbx_dyna_object_data',
    'DECAL':              'gbx_decal_data',
    'VEGET_TREE_MODEL':   'gbx_veget_tree_model_data',
}

KIND_TO_INSTANCE_ATTR = {
    'BLOCK':      'gbx_block_instance',
    'ITEM':       'gbx_item_instance',
    'MACROBLOCK': 'gbx_macroblock_instance',
}


def active_data(collection):
    """Return the active gbx data PropertyGroup on ``collection``, or ``None``."""
    if collection is None:
        return None
    attr = KIND_TO_DATA_ATTR.get(getattr(collection, 'gbx_kind', 'NONE'))
    return getattr(collection, attr, None) if attr else None


def active_instance(obj):
    """Return the active gbx instance PropertyGroup on ``obj``, or ``None``."""
    if obj is None:
        return None
    attr = KIND_TO_INSTANCE_ATTR.get(getattr(obj, 'gbx_kind', 'NONE'))
    return getattr(obj, attr, None) if attr else None


def register() -> None:
    _common.register()
    for mod in _TYPE_MODULES:
        mod.register()


def unregister() -> None:
    for mod in reversed(_TYPE_MODULES):
        try:
            mod.unregister()
        except Exception:
            pass
    _common.unregister()
