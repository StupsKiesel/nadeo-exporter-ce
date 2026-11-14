"""Base class shared by every loaded GBX asset (map, item, prefab, mesh, ...).

Concrete asset kinds live in sibling modules and inherit from :class:`Asset`.
They keep a reference to the parsed NICE container they came from
(``raw``) so loaders and normalization passes can re-read fields without
re-parsing the file. Downstream consumers — the Blender addon, tools, debug
scripts — should treat ``raw`` as opaque and only read the dataclass fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AssetKind(str, Enum):
    """High-level category of a loaded GBX asset.

    Mirrors the top-level GBX class families that GBXBPY exposes loaders for.
    Used for dispatch in the registry and for filtering in inspector tools.
    """

    MAP = "map"
    REPLAY = "replay"
    ITEM = "item"
    PREFAB = "prefab"
    BLOCK = "block"
    MESH = "mesh"
    MATERIAL = "material"
    CLIP = "clip"
    DECORATION = "decoration"
    TERRAIN = "terrain"
    VEGETATION = "vegetation"
    ZONE = "zone"
    SKIN = "skin"
    PARTICLE = "particle"
    FX = "fx"
    KINEMATIC = "kinematic"
    PLACEMENT = "placement"
    UNKNOWN = "unknown"


@dataclass
class Asset:
    """Base record for a loaded GBX file.

    Attributes:
        kind: High-level category (see :class:`AssetKind`).
        source_path: Absolute path the asset was loaded from, if known.
        class_id: GBX class id of the root node (e.g. ``0x03043000`` for a map).
        name: Human-readable identifier (file stem or in-file name when present).
        raw: The parsed NICE container the asset was built from. Opaque to
            consumers; kept so loaders/normalizers can read additional fields
            on demand without re-parsing.
    """

    kind: AssetKind = AssetKind.UNKNOWN
    source_path: Optional[str] = None
    class_id: Optional[int] = None
    name: Optional[str] = None
    raw: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        cls = type(self).__name__
        cid = f"0x{self.class_id:08X}" if self.class_id is not None else "None"
        return f"<{cls} kind={self.kind.value} class_id={cid} name={self.name!r}>"


__all__ = ["Asset", "AssetKind"]
