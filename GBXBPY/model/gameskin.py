"""GameSkin domain model.

``CPlugGameSkin`` (``0x090F4000``) maps a skin directory to a set of
customisable file references (textures, materials) and optional painter
scene metadata.  Files are named ``*.GameSkin.Gbx``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .asset import Asset, AssetKind


@dataclass(slots=True)
class SkinFid:
    """One customisable file reference inside a GameSkin."""

    class_id: int = 0
    name: str = ""
    directory: str = ""


@dataclass
class GameSkin(Asset):
    """A ``CPlugGameSkin`` asset (``*.GameSkin.Gbx``)."""

    # The skin's root directory (relative to GameData)
    dir_name: str = ""
    # Alternative / fallback directory (v4+)
    dir_name_alt: str = ""

    # Painter integration
    painter_texture_name: str = ""
    painter_scene_id: str = ""

    # Customisable file references (list of SkinFid)
    fids: List[SkinFid] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.SKIN


__all__ = ["GameSkin", "SkinFid"]
