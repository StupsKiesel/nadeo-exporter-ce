"""Prefab domain model.

A prefab (``CPlugPrefab`` / ``CGameCommonItemEntityModel``) groups several
sub-entities (meshes, lights, sub-prefabs) under a single transform. It is
referenced by blocks, items, and other prefabs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from .asset import Asset, AssetKind
from .common import IsoLoc, Vec3
from .kinematic import AnimSubFunc

if TYPE_CHECKING:
    from .fx import FxSystem
    from .mesh import Mesh


@dataclass(slots=True)
class DynaAnimDriver:
    """Procedural translation/rotation animation parameters.

    Sourced from a ``NPlugDyna_DynaPoint`` (``0x2F0CA000``) entity inside
    a prefab. The driver specifies axis-aligned linear translation and/or
    rotation, with a separate easing curve and duration for each.

    A duration of 0 means that channel is inactive.
    """

    time_is_duration: bool = True
    rot_time_is_duration: bool = True

    # Translation channel
    trans_axis: str = ""           # "X" / "Y" / "Z" or "" if None
    trans_min: float = 0.0
    trans_max: float = 0.0
    trans_duration_ms: int = 0
    trans_easing: str = "Constant"
    trans_reverse: bool = False
    trans_sub_funcs: List[AnimSubFunc] = field(default_factory=list)

    # Rotation channel
    rot_axis: str = ""
    rot_angle_min_deg: float = 0.0
    rot_angle_max_deg: float = 0.0
    rot_duration_ms: int = 0
    rot_easing: str = "Constant"
    rot_reverse: bool = False
    rot_sub_funcs: List[AnimSubFunc] = field(default_factory=list)


@dataclass(slots=True)
class EntityLink:
    """Connects two prefab entities (e.g. animator → animated mesh).

    ``ent1`` / ``ent2`` are zero-based indices into the prefab's
    :attr:`Prefab.entities` list. ``-1`` means "world / root". ``pos1`` /
    ``pos2`` are pivot offsets attached to each end of the link.
    """

    ent1: int = -1
    ent2: int = -1
    pos1: Vec3 = field(default_factory=Vec3)
    pos2: Vec3 = field(default_factory=Vec3)


@dataclass(slots=True)
class PlacementGroup:
    """``NPlugItemPlacement_SPlacementGroup`` (``0x2F0D8000``) entity params.

    The game fills these slots at runtime (e.g. grandstand spectators).
    ``placements`` holds, per placement, the alternative ``RequiredTags``
    dicts. ``slot_placements[i]`` indexes ``placements`` for slot ``i`` and
    ``slots[i]`` is that slot's entity-local transform in Blender axes.
    """

    placements: List[List[Dict[str, str]]] = field(default_factory=list)
    slot_placements: List[int] = field(default_factory=list)
    slots: List[IsoLoc] = field(default_factory=list)

    def slot_tags(self, index: int) -> List[Dict[str, str]]:
        placement = self.slot_placements[index] if index < len(self.slot_placements) else -1
        return self.placements[placement] if 0 <= placement < len(self.placements) else []


@dataclass(slots=True)
class PrefabEntity:
    """One sub-entity of a prefab.

    Each entity is a transform-placed reference to a child model. The child
    may be one of:

    * **Embedded mesh** — ``mesh`` is populated with a fully-loaded
      :class:`~GBXBPY.model.mesh.Mesh` (typical for prefabs that inline
      their CPlugSolid2Model geometry).
    * **Nested prefab** — ``sub_prefab`` points at another :class:`Prefab`.
    * **External reference** — ``model_path`` is the GameData-relative path
      to the referenced asset (mesh, item, or prefab). The addon resolves
      and loads these on demand.
    * **Parametric** — all of the above are None and the entity carries
      only placement data (vegetation, pillars, etc.).

    Custom-animated items add two extra entity flavours:

    * ``is_dynamic=True`` — ``CPlugDynaObjectModel`` (``0x09144000``):
      a mesh that animates at runtime. Looks like a static object except
      it carries kinematic/physics flags and may be linked to an animator.
    * ``anim_driver`` set — ``NPlugDyna_DynaPoint`` (``0x2F0CA000``):
      a non-rendered entity that drives an animation channel. The
      :attr:`link` field tells which other entities it animates.
    """

    name: str = ""
    model_path: str = ""
    model_class_id: int = 0              # 0 when unknown / parametric
    transform: IsoLoc = field(default_factory=IsoLoc)
    mesh: Optional["Mesh"] = None
    sub_prefab: Optional["Prefab"] = None

    # Dyna-item extensions
    is_dynamic: bool = False
    is_kinematic: bool = False
    anim_driver: Optional[DynaAnimDriver] = None
    link: Optional[EntityLink] = None
    params_chunk_id: int = 0           # raw chunk id of params payload, 0 if none
    placement_group: Optional[PlacementGroup] = None
    # NPlugItemPlacement_SPlacement (0x2F0A9000): alternative RequiredTags
    # sets; the game fills the slot with a matching item variant at runtime.
    placement_options: List[Dict[str, str]] = field(default_factory=list)
    placement_layout: int = -1
    # Set by the map importer when geometry is unavailable: the entity is
    # represented by a named marker (e.g. vista vegetation with truncated
    # VegetTreeModel files).
    marker_name: str = ""
    marker_tags: Dict[str, str] = field(default_factory=dict)
    # Set by importers for CPlugFxSystem references (fogger/sparkler effects).
    fx: Optional["FxSystem"] = None


@dataclass
class Prefab(Asset):
    """A Trackmania prefab asset."""

    entities: List[PrefabEntity] = field(default_factory=list)
    prefab_version: int = 0
    url: str = ""

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.PREFAB


__all__ = ["Prefab", "PrefabEntity", "DynaAnimDriver", "EntityLink", "PlacementGroup"]
