"""FuncShader domain model — animated material/UV parameters.

A ``CFuncShader`` describes how a material slot is animated over time
in the engine. The two flavours we handle:

* :class:`FuncShader` (``CFuncShaderLayerUV``, class ``0x05015000``) —
  a single animation: UV scroll, atlas frame cycling, or a repeated
  parametric transform.
* :class:`FuncShaderArray` (``CFuncShaderArray``, class ``0x05014000``) —
  a composite of nested :class:`FuncShader` objects, used when several
  animations share the same trigger (e.g. SpecialFXTurboRoulette).

These are produced by :mod:`GBXBPY.load.funcshader` and attached to
:class:`~GBXBPY.model.material.Material` instances by
:mod:`GBXBPY.normalize.materials` when a same-stem ``.FuncShader.Gbx``
exists alongside the material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .asset import Asset, AssetKind


class FuncShaderKind(str, Enum):
    """What kind of animation the FuncShader describes."""

    UNKNOWN = "Unknown"
    UV_SCROLL = "UvScroll"        # 0x05015009 / 0x0501500D / 0x05015013
    ATLAS = "Atlas"               # 0x05015015 — sprite-sheet
    COMPOSITE = "Composite"       # CFuncShaderArray


@dataclass
class FuncShader(Asset):
    """A single CFuncShader (parsed from CFuncShaderLayerUV).

    Only the fields actually used downstream are surfaced here. ``raw``
    on the parent :class:`Asset` keeps the full container around so a
    later pass can mine extra params without re-parsing.
    """

    kind: AssetKind = AssetKind.MATERIAL  # FuncShader rides as a material-related asset
    shader_kind: FuncShaderKind = FuncShaderKind.UNKNOWN

    # Driver / timing (chunk 0x0500B005)
    period: float = 1.0           # seconds per cycle
    phase: float = 0.0
    randomize_phase: bool = False
    auto_create_motion: bool = True
    input_val_id: str = ""        # '', 'RaceTime', 'TurboRoulette', ...

    # Slot binding (chunk 0x05015005)
    layer_name: str = ""          # 'SelfIllum', 'BaseColorA', 'MulInside', 'Clouds'
    layer_mode: int = 0

    # UV scroll (chunk 0x05015009 / 0x0501500D / 0x05015013)
    uv_scale: Tuple[float, float] = (1.0, 1.0)
    uv_speed: Tuple[float, float] = (0.0, 0.0)
    uv_offset: Tuple[float, float] = (0.0, 0.0)

    # Atlas (chunk 0x05015015)
    atlas_frames: int = 0
    atlas_cols: int = 0
    atlas_rows: int = 0
    atlas_flag: int = 0

    # Composite children (CFuncShaderArray)
    children: List["FuncShader"] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.refresh_kind()

    def refresh_kind(self) -> None:
        """Recompute :attr:`shader_kind` from the current field values.

        Called by loaders after they finish populating chunk-derived
        fields. Cheap and idempotent — call again whenever fields
        change.
        """
        if self.shader_kind is not FuncShaderKind.UNKNOWN:
            return
        if self.children:
            self.shader_kind = FuncShaderKind.COMPOSITE
        elif self.atlas_frames > 0:
            self.shader_kind = FuncShaderKind.ATLAS
        elif self.uv_speed != (0.0, 0.0) or self.uv_offset != (0.0, 0.0):
            self.shader_kind = FuncShaderKind.UV_SCROLL


__all__ = ["FuncShader", "FuncShaderKind"]
