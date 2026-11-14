"""Clip domain model.

A clip is a small visual piece attached to one face / edge / corner of a
block unit (rails, borders, deco walls, fences, ...). Clips share most of
their structure with blocks (a ground variant, optionally an air variant,
each carrying mobils + units), so this module reuses
:class:`~GBXBPY.model.block.BlockVariant` to describe them.

Three GBX class ids are surfaced as :class:`Clip`:

* ``0x03053000`` — ``CGameCtnBlockInfoClip`` (regular face / edge clip)
* ``0x03340000`` — ``CGameCtnBlockInfoClipVertical`` (height-stackable clip)
* ``0x0335B000`` — ``CGameCtnBlockInfoClipHorizontal`` (neighbour-aware clip)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .asset import Asset, AssetKind
from .block import BlockVariant


@dataclass
class Clip(Asset):
    """A Trackmania clip definition asset."""

    # Clip-specific flags / labels (from CGameCtnBlockInfoClip chunks)
    clip_type: str = ""                   # e.g. "FreeClipSide", "FreeClipBottom"
    is_full_free_clip: bool = False
    is_exclusive_free_clip: bool = False
    can_be_deleted_by_full_free: bool = False
    top_bottom_multi_dir: str = ""        # GbxEMultiDir value, e.g. "SameDir"

    # 0x03053007 — passing point (clip mouth position/orientation)
    has_passing_point: bool = False
    passing_point_pos: tuple = field(default_factory=tuple)  # (x, y)
    passing_point_roll: float = 0.0
    passing_point_pitch: float = 0.0

    symmetrical_clip_id: str = ""
    clip_group_id: str = ""               # e.g. "RoadEdge" — pairs with neighbours
    symmetrical_clip_group_id: str = ""
    # subtype-specific group IDs (horizontal / vertical clip subtypes)
    horizontal_clip_group_id: str = ""
    vertical_clip_group_id: str = ""

    # Ingame catalog metadata (CGameCtnObjectInfo header / chunks)
    page_path: str = ""
    skin_directory: str = ""
    # CPlugGameSkinAndFolder refs (chunk 0x0304E031), same as Block.
    material_modifier_path: str = ""
    material_modifier2_path: str = ""
    is_internal: bool = False
    is_advanced: bool = False
    catalog_position: int = 0
    icon: bytes = field(default=b"", repr=False)

    # Variants — clips usually have a single ground variant; some define an
    # air variant too. Use the same BlockVariant container as Block does so
    # downstream code can treat them uniformly.
    variant_ground: Optional[BlockVariant] = None
    variant_air: Optional[BlockVariant] = None

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.CLIP

    # ----- variant selection -------------------------------------------------

    def all_variants(self) -> list:
        """Return non-``None`` variants in canonical order: ground, then air."""
        out = []
        if self.variant_ground is not None:
            out.append(self.variant_ground)
        if self.variant_air is not None:
            out.append(self.variant_air)
        return out

    def get_variant(self, kind: str, index: int = 0):
        """Return a single variant by ``(kind, index)``.

        Clips only have a single ground and a single air variant, so
        ``index`` must be ``0``.
        """
        if index != 0:
            return None
        if kind == "ground":
            return self.variant_ground
        if kind == "air":
            return self.variant_air
        return None


__all__ = ["Clip"]
