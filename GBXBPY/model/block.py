"""Block domain model.

A :class:`Block` is the GBXBPY representation of a Trackmania block
definition (``CGameCtnBlockInfo`` / ``CGameCtnBlockInfoClassic``). It
exposes the per-variant unit grid, mobil-to-mesh references, and clip
information the addon needs to instantiate the block in a scene.

Per-instance placement (where a block sits in a map) lives on
:class:`~GBXBPY.model.map.MapBlock`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .asset import Asset, AssetKind
from .common import IsoLoc, Vec3


@dataclass(slots=True)
class BlockMobil:
    """A movable / static piece referenced by a block variant.

    Either ``solid_path`` (link to a mesh) or ``prefab_path`` (link to a
    prefab) is populated, depending on the mobil's source.
    """

    solid_path: str = ""
    prefab_path: str = ""
    kind: int = 0                    # 0 = primary, 1 = secondary/shadow
    transform: IsoLoc = field(default_factory=IsoLoc)
    # GeomTransformation (GbxPose3D) as ``(x, y, z, yaw, pitch, roll)``
    # in TM coordinates (Y-up, Y-axis is yaw). ``None`` when the chunk
    # had no GeomTransformation set. The loader leaves this raw so the
    # consumer can do the swizzle in Blender space.
    geom_transform: Optional[Tuple[float, float, float, float, float, float]] = None
    waypoint_paths: List[str] = field(default_factory=list)


@dataclass(slots=True)
class BlockUnit:
    """One grid cell of a block variant.

    Clip lists are GameData-relative .Clip.Gbx paths. Empty list means no
    clip on that face.
    """

    relative_offset: Tuple[int, int, int] = (0, 0, 0)
    clips_north: List[str] = field(default_factory=list)
    clips_east: List[str] = field(default_factory=list)
    clips_south: List[str] = field(default_factory=list)
    clips_west: List[str] = field(default_factory=list)
    clips_top: List[str] = field(default_factory=list)
    clips_bottom: List[str] = field(default_factory=list)


@dataclass(slots=True)
class BlockVariant:
    """A single visual variant of a block (ground, air, mobil-state, etc.).

    Variants are identified by the tuple ``(kind, index)`` where
    ``kind`` is one of ``"ground" | "air"`` and ``index`` is the position
    within the corresponding list on the parent Block/Clip (``0`` is the
    base variant; ``1..N`` are the additionals from the
    ``additionalVariantsGround/Air`` chunks).

    The fields below mirror the chunks of ``CGameCtnBlockInfoVariant``
    (0x0315B*). Anything not parseable from the file is left at its
    default; callers should treat empty strings / ``None`` references as
    "absent".
    """

    # --- geometry payload ---
    mobils: List[List["BlockMobil"]] = field(default_factory=list)  # outer = mobil group, inner = mobil sub-state
    units: List["BlockUnit"] = field(default_factory=list)

    # --- identification (0x0315B003 / 0x0315B008.name) ---
    name: str = ""                          # variant name from 0x0315B008
    kind: str = ""                          # "ground" | "air" (filled by loader)
    index: int = 0                          # 0 = base, 1..N = additional
    cardinal_dir: str = ""                  # "North" | "East" | "South" | "West"
    variant_base_type: str = ""             # "Inherit" | "No" | "Conductor" | "Generator"
    multi_dir: str = ""                     # GbxEMultiDir name, from 0x0315B002
    symmetrical_variant_index: int = -1
    no_pillar_below_index: int = -1

    # --- manual symmetry flags (0x0315B008) ---
    has_manual_symmetry_h: bool = False
    has_manual_symmetry_v: bool = False
    has_manual_symmetry_d1: bool = False
    has_manual_symmetry_d2: bool = False

    # --- spawn pose (0x0315B008) ---
    # Stored as ``(translation_xyz, rotation_xyz_or_quat)`` — opaque tuple,
    # exact shape depends on chunk version. Empty tuple when missing.
    spawn: Tuple = field(default_factory=tuple)

    # --- helper solids (0x0315B005 v2+) ---
    helper_solid_path: str = ""
    facultative_helper_solid_path: str = ""

    # --- waypoint / interaction (0x0315B006) ---
    screen_interaction_trigger_solid: str = ""
    screen_interaction_trigger_shape: str = ""
    waypoint_trigger_solid: str = ""
    waypoint_trigger_shape: str = ""

    # --- gameplay sub-models (0x0315B006) ---
    gate_path: str = ""
    teleporter_path: str = ""
    turbine_path: str = ""
    flock_model_path: str = ""
    spawn_model_path: str = ""
    entity_spawner_paths: List[str] = field(default_factory=list)

    # --- probe (0x0315B007) ---
    probe_path: str = ""

    # --- compound physics model (0x0315B00A) ---
    compound_model_path: str = ""

    # --- water volumes (0x0315B00B) ---
    # List of dicts {"chunks_size": (min, max), "water_type": str} — kept
    # opaque so we don't drag GbxBox into the model layer.
    water_volumes: List[dict] = field(default_factory=list)

    # --- pillar params (0x0315B009) — list of paths ---
    placed_pillar_paths: List[str] = field(default_factory=list)
    replaced_pillar_paths: List[str] = field(default_factory=list)

    # --- ground-only (0x0315C001) ---
    auto_terrain_zone_ids: List[str] = field(default_factory=list)
    auto_terrain_height_offset: int = 0
    auto_terrain_place_type: str = ""

    @property
    def is_air(self) -> bool:
        return self.kind == "air"

    @property
    def is_ground(self) -> bool:
        return self.kind == "ground"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        tag = f"{self.kind}[{self.index}]" if self.kind else "?"
        return (
            f"<BlockVariant {tag} name={self.name!r} "
            f"mobils={sum(len(g) for g in self.mobils)} units={len(self.units)}>"
        )


@dataclass
class Block(Asset):
    """A Trackmania block definition asset."""

    collection: str = ""             # e.g. "Stadium"
    is_terrain: bool = False
    ground_variants: List[BlockVariant] = field(default_factory=list)
    air_variants: List[BlockVariant] = field(default_factory=list)
    icon: bytes = field(default=b"", repr=False)

    # Catalog / metadata (mirrors what the legacy GbxBlockInfo exposed).
    page_path: str = ""
    skin_directory: str = ""
    is_internal: bool = False
    is_advanced: bool = False
    catalog_position: int = 0
    waypoint_type: str = "No"
    no_respawn: bool = False
    is_pillar: bool = False
    pillar_shape_multi_dir: int = 0
    symmetrical_block_id: str = ""
    base_block_path: str = ""
    material_modifier_path: str = ""
    material_modifier2_path: str = ""

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.BLOCK

    # ----- variant selection -------------------------------------------------

    def all_variants(self) -> List[BlockVariant]:
        """Return *every* variant in canonical order: ground first, then air."""
        return list(self.ground_variants) + list(self.air_variants)

    def get_variant(self, kind: str, index: int = 0) -> Optional[BlockVariant]:
        """Return a single variant by ``(kind, index)``, or ``None``.

        Args:
            kind: ``"ground"`` or ``"air"``.
            index: 0 = base variant, 1..N = additional variants.
        """
        bucket = (
            self.ground_variants if kind == "ground"
            else self.air_variants if kind == "air"
            else None
        )
        if bucket is None or not (0 <= index < len(bucket)):
            return None
        return bucket[index]

    def find_variants(
        self,
        *,
        kind: Optional[str] = None,
        cardinal_dir: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[BlockVariant]:
        """Filter variants by kind / cardinal_dir / name (any combination)."""
        out: List[BlockVariant] = []
        for v in self.all_variants():
            if kind is not None and v.kind != kind:
                continue
            if cardinal_dir is not None and v.cardinal_dir != cardinal_dir:
                continue
            if name is not None and v.name != name:
                continue
            out.append(v)
        return out


__all__ = ["Block", "BlockVariant", "BlockMobil", "BlockUnit"]
