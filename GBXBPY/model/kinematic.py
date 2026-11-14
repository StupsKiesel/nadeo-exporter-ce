"""KinematicConstraint domain model.

``CPlugKinematicConstraint`` (``0x2F0CA000``) describes an animated
translation/rotation constraint used by the game engine for dynamic obstacles
such as pushers, rotors, and turnstiles.  Files are named
``*.KinematicConstraint.Gbx``.

The key geometry data is:
- ``trans_axis`` / ``trans_min`` / ``trans_max`` — which axis the object
  slides along and how far in each direction.
- ``rot_axis`` / ``angle_min_deg`` / ``angle_max_deg`` — rotation axis and
  angular range.
- ``trans_sub_funcs`` / ``rot_sub_funcs`` — per-sub-step animation curves
  (ease enum, reverse flag, duration in ms).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from .asset import Asset, AssetKind


@dataclass(slots=True)
class AnimSubFunc:
    """One animation sub-step: easing, direction, duration."""

    ease: int = 0        # GbxEAnimEase int value
    reverse: bool = False
    duration_ms: int = 0  # milliseconds


@dataclass
class KinematicConstraint(Asset):
    """A ``CPlugKinematicConstraint`` asset (``*.KinematicConstraint.Gbx``)."""

    # Translation constraint
    trans_axis: int = 0       # GbxEAxis int (0=X,1=Y,2=Z)
    trans_min: float = 0.0    # metres
    trans_max: float = 0.0    # metres
    trans_sub_funcs: List[AnimSubFunc] = field(default_factory=list)

    # Rotation constraint
    rot_axis: int = 0         # GbxEAxis int
    angle_min_deg: float = 0.0
    angle_max_deg: float = 0.0
    rot_sub_funcs: List[AnimSubFunc] = field(default_factory=list)

    # Shader texture-coord animation type (0 = none)
    shader_tc_type: int = 0

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.KINEMATIC


__all__ = ["KinematicConstraint", "AnimSubFunc"]
