"""FxSystem domain model.

``CPlugFxSystem`` (``0x0915C000``, ``*.FxSys.Gbx``) is the effect graph a
prefab entity references: a tree of Parallel/Condition/UpdateVar nodes whose
leaves are particle (or sound) emitters. Stock show items use it for foggers,
sparklers and torch smoke.

The tree is flattened: every emitter and var update keeps the conjunction of
the ``Condition`` nodes above it. Expressions (``cos(Time/500)``,
``DecorationColorHSL.x``) are game-side scripts and kept as text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .asset import Asset, AssetKind
from .particle import ParticleEmitterModel

_FLOAT = r"\s*(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*"
_FLOAT3 = re.compile(rf"^\s*float3\({_FLOAT}(?:,{_FLOAT},{_FLOAT})?\)\s*$")
_SCALAR = re.compile(rf"^{_FLOAT}$")


def constant_float3(expr: str) -> Optional[Tuple[float, float, float]]:
    """Value of a literal ``float3(x)``/``float3(x,y,z)`` expression, else ``None``."""
    match = _FLOAT3.match(expr or "")
    if not match:
        return None
    x, y, z = match.groups()
    if y is None:
        return (float(x),) * 3
    return float(x), float(y), float(z)


def constant_float(expr: str) -> Optional[float]:
    """Value of a literal scalar expression (``"1"``, ``"-1.0"``), else ``None``."""
    match = _SCALAR.match(expr or "")
    return float(match.group(1)) if match else None


@dataclass
class FxEmitter:
    """A ``CPlugFxSystemNode_ParticleEmitter`` leaf (TM-local expressions)."""

    name: str = ""
    model_path: str = ""
    model: Optional[ParticleEmitterModel] = None
    condition: str = ""             # "" = always active
    local_offset: str = ""
    world_offset: str = ""
    spawn_freq: str = ""
    scale: str = ""
    opacity: str = ""
    up: str = ""
    dov: str = ""
    dov_and_up_local: bool = False
    linear_hue: str = ""
    hue_lightness: str = ""


@dataclass
class FxVarUpdate:
    """A ``CPlugFxSystemNode_UpdateVar``: ``var = expr`` while ``condition`` holds."""

    var: str = ""
    expr: str = ""
    condition: str = ""
    reset_if_inactive: bool = False


@dataclass
class FxVar:
    """An ``SPlugGraphVar`` declaration with its default value."""

    name: str = ""
    type: str = ""
    value: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


@dataclass
class FxSystem(Asset):
    """A ``CPlugFxSystem`` asset (``*.FxSys.Gbx``)."""

    emitters: List[FxEmitter] = field(default_factory=list)
    var_updates: List[FxVarUpdate] = field(default_factory=list)
    vars: List[FxVar] = field(default_factory=list)
    context_class_id: int = -1
    sound_emitter_count: int = 0

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.FX


__all__ = [
    "FxEmitter", "FxSystem", "FxVar", "FxVarUpdate",
    "constant_float", "constant_float3",
]
