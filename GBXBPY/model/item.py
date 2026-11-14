"""Item domain model.

Covers ``CGameItemModel`` (0x2E002000) in both ingame (Nadeo) and custom
(user-made) flavours. The two flavours are distinguished by which of
``entity_variants`` / ``parts`` is populated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .asset import Asset, AssetKind
from .common import IsoLoc, Vec3
from .prefab import Prefab
from .geometry import CrystalLayer, MeshPart, Matrix4


@dataclass(slots=True)
class ItemPlacement:
    """Default placement parameters offered by an item to the editor."""

    ghost_mode: bool = False
    cube_size: float = 0.0
    grid_snap_h_step: float = 32.0
    grid_snap_v_step: float = 2.0
    grid_snap_h_offset: float = 0.0
    fly_step: float = 2.0
    pivot_snap_distance: float = -1.0
    pivot_positions: List[Vec3] = field(default_factory=list)


@dataclass(slots=True)
class EntityVariant:
    """One ingame-item variant (skin / form switchable in the editor)."""

    tags: List[tuple] = field(default_factory=list)   # list of (key, value) string pairs
    entity_model_path: str = ""                       # external StaticObject / VegetTreeModel path
    hidden_in_manual_cycle: bool = False


AIR_VARIANT_KEY_OFFSET = 1_000_000


@dataclass(slots=True)
class CustomBlock:
    """``CGameBlockItem`` (0x2E025000): a custom block built on a stock archetype.

    The archetype block info supplies units, footprint and ground behaviour;
    ``variants`` replaces its geometry. Keys follow ``CustomizedVariants``:
    the archetype variant index, plus :data:`AIR_VARIANT_KEY_OFFSET` for air
    variants. An empty part list is intentionally empty geometry (the
    ``VOID`` block trick that removes the stadium grass).
    """

    archetype_id: str = ""
    archetype_collection: str = ""
    variants: Dict[int, List[MeshPart]] = field(default_factory=dict)

    @staticmethod
    def key(kind: str, index: int) -> int:
        return index + (AIR_VARIANT_KEY_OFFSET if kind == "air" else 0)

    def variant_key(self, kind: str, index: int) -> Optional[int]:
        """Customized variant for a placement: exact, then same kind, then any."""
        if not self.variants:
            return None
        wanted = self.key(kind, index)
        if wanted in self.variants:
            return wanted
        same_kind = sorted(k for k in self.variants if (k >= AIR_VARIANT_KEY_OFFSET) == (kind == "air"))
        return same_kind[0] if same_kind else min(self.variants)


@dataclass
class Item(Asset):
    """A Trackmania item definition asset."""

    item_type: str = ""              # "Ornament", "PickUp", "Vehicle", ...
    collection: str = ""
    page_path: str = ""
    skin_directory: str = ""
    is_internal: bool = False
    is_advanced: bool = False
    catalog_position: int = 0
    ground_point: Vec3 = field(default_factory=Vec3)
    orbital_center_height: float = 0.0
    orbital_radius_base: float = 0.0
    icon: bytes = field(default=b"", repr=False)

    # Ingame items: variant list pointing at external entity models.
    entity_variants: List[EntityVariant] = field(default_factory=list)

    parts: List[MeshPart] = field(default_factory=list)
    crystal_layers: List[CrystalLayer] = field(default_factory=list)
    waypoint_type: str = "No"
    spawn_transform: Optional[Matrix4] = None

    # Custom-animated items (dyna items): EntityModel is a CPlugPrefab
    # whose entities are CPlugDynaObjectModel meshes plus
    # NPlugDyna_DynaPoint procedural-animation drivers. When set, it
    # carries the animated entity hierarchy.
    embedded_prefab: Optional[Prefab] = None

    # Custom blocks (``*.Block.Gbx``, item type "Block").
    custom_block: Optional[CustomBlock] = None

    # GameData-relative ``MaterialModifier`` (``CPlugGameSkinAndFolder``)
    # of the item's EntityModel chunk; per-colour obstacle items redirect
    # their materials through it (``ItemObstacle`` -> ``ItemObstacleLevel2``).
    material_modifier_path: str = ""

    default_placement: ItemPlacement = field(default_factory=ItemPlacement)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.ITEM

    @property
    def is_custom(self) -> bool:
        """True when this item carries an embedded mesh (user-made item)."""
        return bool(self.parts) or self.embedded_prefab is not None or self.custom_block is not None

    @property
    def is_ingame(self) -> bool:
        """True when this item is a Nadeo item with external entity variants."""
        return bool(self.entity_variants)

    @property
    def is_animated(self) -> bool:
        """True when this is a custom-animated (dyna) item."""
        return self.embedded_prefab is not None


__all__ = ["AIR_VARIANT_KEY_OFFSET", "CustomBlock", "Item", "ItemPlacement", "EntityVariant"]
