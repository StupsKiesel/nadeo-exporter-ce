"""Shared primitive value types used across the domain model.

All types here are plain ``dataclass`` records. They are intentionally
independent of any third-party math library (no ``mathutils``, no ``numpy``)
so the model stays usable from any Python context.

Coordinate convention: **Blender-oriented (right-handed, Z-up)**, distances
in meters, quaternions in ``(x, y, z, w)`` order. The GBX → Blender axis
swizzle is performed by loaders via :mod:`GBXBPY.coords`; downstream code
treats these values as already addon-ready.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Tuple


@dataclass(frozen=True, slots=True)
class Vec2:
    x: float = 0.0
    y: float = 0.0

    def as_tuple(self) -> Tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class Vec3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def as_tuple(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)


@dataclass(frozen=True, slots=True)
class Quat:
    """Quaternion in ``(x, y, z, w)`` order (matches GBX on-disk layout)."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 1.0

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.z, self.w)

    def is_normalized(self, tol: float = 1e-3) -> bool:
        n = self.x * self.x + self.y * self.y + self.z * self.z + self.w * self.w
        return abs(n - 1.0) <= tol

    @classmethod
    def identity(cls) -> "Quat":
        return cls(0.0, 0.0, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class Color:
    """Linear RGBA color in 0..1 range."""

    r: float = 0.0
    g: float = 0.0
    b: float = 0.0
    a: float = 1.0

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.r, self.g, self.b, self.a)


@dataclass(frozen=True, slots=True)
class Box:
    """Axis-aligned bounding box."""

    min: Vec3 = field(default_factory=Vec3)
    max: Vec3 = field(default_factory=Vec3)

    @property
    def size(self) -> Vec3:
        return self.max - self.min

    @property
    def center(self) -> Vec3:
        return Vec3(
            (self.min.x + self.max.x) * 0.5,
            (self.min.y + self.max.y) * 0.5,
            (self.min.z + self.max.z) * 0.5,
        )


@dataclass(frozen=True, slots=True)
class IsoLoc:
    """A position + rotation pair as it appears in GBX placement data."""

    position: Vec3 = field(default_factory=Vec3)
    rotation: Quat = field(default_factory=Quat.identity)


def vec3_from_iter(values: Iterable[float]) -> Vec3:
    x, y, z = values
    return Vec3(float(x), float(y), float(z))


def quat_from_iter(values: Iterable[float]) -> Quat:
    x, y, z, w = values
    return Quat(float(x), float(y), float(z), float(w))


__all__ = [
    "Vec2",
    "Vec3",
    "Quat",
    "Color",
    "Box",
    "IsoLoc",
    "vec3_from_iter",
    "quat_from_iter",
]
