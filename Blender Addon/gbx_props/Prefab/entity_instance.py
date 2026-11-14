"""Prefab *entity* instance PropertyGroup.

Lives on ``bpy.types.Object`` Empties that act as one slot inside a
Prefab data Collection. Carries the per-slot metadata (name, model
class id, model path) and the dyna-item animation fields used by
``NPlugDyna_DynaPoint`` / ``CPlugDynaObjectModel``.

Easing enum mirrors NICE ``GbxEAnimEase`` (35 values, 0..34) so values
round-trip exactly to and from the parser.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from .._common import GbxCommonInstance


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

# Order MUST match GbxEAnimEase in NICE/src/gbx_enums.py.
EASE_ITEMS = [
    ('CONSTANT',       "Constant",       ""),
    ('LINEAR',         "Linear",         ""),
    ('QUAD_IN',        "QuadIn",         ""),
    ('QUAD_OUT',       "QuadOut",        ""),
    ('QUAD_IN_OUT',    "QuadInOut",      ""),
    ('CUBIC_IN',       "CubicIn",        ""),
    ('CUBIC_OUT',      "CubicOut",       ""),
    ('CUBIC_IN_OUT',   "CubicInOut",     ""),
    ('QUART_IN',       "QuartIn",        ""),
    ('QUART_OUT',      "QuartOut",       ""),
    ('QUART_IN_OUT',   "QuartInOut",     ""),
    ('QUINT_IN',       "QuintIn",        ""),
    ('QUINT_OUT',      "QuintOut",       ""),
    ('QUINT_IN_OUT',   "QuintInOut",     ""),
    ('SINE_IN',        "SineIn",         ""),
    ('SINE_OUT',       "SineOut",        ""),
    ('SINE_IN_OUT',    "SineInOut",      ""),
    ('EXP_IN',         "ExpIn",          ""),
    ('EXP_OUT',        "ExpOut",         ""),
    ('EXP_IN_OUT',     "ExpInOut",       ""),
    ('CIRC_IN',        "CircIn",         ""),
    ('CIRC_OUT',       "CircOut",        ""),
    ('CIRC_IN_OUT',    "CircInOut",      ""),
    ('BACK_IN',        "BackIn",         ""),
    ('BACK_OUT',       "BackOut",        ""),
    ('BACK_IN_OUT',    "BackInOut",      ""),
    ('ELASTIC_IN',     "ElasticIn",      ""),
    ('ELASTIC_OUT',    "ElasticOut",     ""),
    ('ELASTIC_IN_OUT', "ElasticInOut",   ""),
    ('ELASTIC_IN2',    "ElasticIn2",     ""),
    ('ELASTIC_OUT2',   "ElasticOut2",    ""),
    ('ELASTIC_IN_OUT2',"ElasticInOut2",  ""),
    ('BOUNCE_IN',      "BounceIn",       ""),
    ('BOUNCE_OUT',     "BounceOut",      ""),
    ('BOUNCE_IN_OUT',  "BounceInOut",    ""),
]

# GbxEAxis: X=0, Y=1, Z=2; NONE = "channel inactive" (added for UI).
AXIS_ITEMS = [
    ('NONE', "None", "Channel inactive"),
    ('X',    "X",    ""),
    ('Y',    "Y",    ""),
    ('Z',    "Z",    ""),
]


# ---------------------------------------------------------------------------
# PropertyGroup
# ---------------------------------------------------------------------------

class GbxPrefabEntityInstance(GbxCommonInstance):
    """One sub-entity slot inside a Prefab data Collection.

    ``data_ref`` (inherited) points at the entity's referenced asset
    Collection (mesh, sub-prefab, item) when one is loaded; empty for
    parametric / external-only entities.
    """

    # --- Slot identity ------------------------------------------------------
    slot_name: StringProperty(
        name="Slot Name",
        description="PrefabEntity.name",
        default="",
    )
    model_path: StringProperty(
        name="Model Path",
        description="GameData-relative path to the referenced asset (informational)",
        default="",
    )
    model_class_id: IntProperty(
        name="Model Class Id",
        description="GBX class id of the referenced model; 0 = unknown / parametric",
        default=0,
        min=0,
    )

    # --- Dyna flags ---------------------------------------------------------
    is_dynamic: BoolProperty(
        name="Is Dynamic",
        description="Entity is a CPlugDynaObjectModel (animated mesh)",
        default=False,
    )
    is_kinematic: BoolProperty(
        name="Is Kinematic",
        description="Dyna object behaves kinematically",
        default=False,
    )
    params_chunk_id: IntProperty(
        name="Params Chunk Id",
        description="Raw chunk id of the params payload (round-trip); 0 = none",
        default=0,
        min=0,
    )

    # --- Anim driver bookkeeping -------------------------------------------
    anim_time_is_duration: BoolProperty(
        name="Time Is Duration",
        description="TransAnimFunc.TimeIsDuration",
        default=True,
    )

    # --- Translation channel -----------------------------------------------
    anim_trans_axis: EnumProperty(
        name="Trans Axis",
        items=AXIS_ITEMS,
        default='NONE',
    )
    anim_trans_min: FloatProperty(name="Trans Min", default=0.0)
    anim_trans_max: FloatProperty(name="Trans Max", default=0.0)
    anim_trans_duration: IntProperty(
        name="Trans Duration",
        description="Duration in milliseconds; 0 = inactive",
        default=0,
        min=0,
    )
    anim_trans_easing: EnumProperty(
        name="Trans Easing",
        items=EASE_ITEMS,
        default='CONSTANT',
    )
    anim_trans_reverse: BoolProperty(name="Trans Reverse", default=False)

    # --- Rotation channel --------------------------------------------------
    anim_rot_axis: EnumProperty(
        name="Rot Axis",
        items=AXIS_ITEMS,
        default='NONE',
    )
    anim_rot_min_deg: FloatProperty(name="Rot Min (deg)", default=0.0)
    anim_rot_max_deg: FloatProperty(name="Rot Max (deg)", default=0.0)
    anim_rot_duration: IntProperty(
        name="Rot Duration",
        description="Duration in milliseconds; 0 = inactive",
        default=0,
        min=0,
    )
    anim_rot_easing: EnumProperty(
        name="Rot Easing",
        items=EASE_ITEMS,
        default='CONSTANT',
    )
    anim_rot_reverse: BoolProperty(name="Rot Reverse", default=False)

    # --- Entity link (animator -> animated) --------------------------------
    link_ent1: IntProperty(
        name="Link Ent 1",
        description="First linked entity index (-1 = world / root)",
        default=-1,
    )
    link_ent2: IntProperty(
        name="Link Ent 2",
        description="Second linked entity index (-1 = world / root)",
        default=-1,
    )
    link_pos1: FloatVectorProperty(
        name="Link Pos 1",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='XYZ',
    )
    link_pos2: FloatVectorProperty(
        name="Link Pos 2",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='XYZ',
    )


CLASSES = (GbxPrefabEntityInstance,)
_ATTR = "gbx_prefab_entity_instance"


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Object, _ATTR, PointerProperty(type=GbxPrefabEntityInstance))


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
