"""GBX → Blender coordinate conversion.

GBX uses a right-handed, **Y-up** coordinate system. Blender uses a
right-handed, **Z-up** system. GBXBPY exposes everything in Blender's
convention so the addon never has to swizzle.

The conversion is a 90° rotation about the +X axis::

    blender_x =  gbx_x
    blender_y = -gbx_z
    blender_z =  gbx_y

Loaders in :mod:`GBXBPY.load` must call these helpers exactly once when
building :mod:`GBXBPY.model` objects from raw NICE data. Downstream code
(normalize / serializer / inspector / addon) treats the data as already
Blender-oriented and must not swizzle again.

Distance units are unchanged — GBX already uses meters.
"""

from __future__ import annotations

import math
from typing import Iterable, Tuple

from .model.common import Quat, Vec3
from .model.geometry import Matrix4


def multiply_matrices(a: Matrix4, b: Matrix4) -> Matrix4:
    def row(i: int) -> tuple[float, float, float, float]:
        def dot(j: int) -> float:
            return sum(a[i][k] * b[k][j] for k in range(4))
        return (dot(0), dot(1), dot(2), dot(3))
    return (row(0), row(1), row(2), row(3))


def blender_transform_matrix(position: Vec3, quaternion: Quat, scale: float = 1.0) -> Matrix4:
    x, y, z, w = quaternion.as_tuple()
    norm = math.sqrt(x*x + y*y + z*z + w*w)
    if norm == 0:
        raise ValueError("Zero placement quaternion")
    x, y, z, w = x/norm, y/norm, z/norm, w/norm
    return (
        ((1-2*(y*y+z*z))*scale, 2*(x*y-z*w)*scale, 2*(x*z+y*w)*scale, position.x),
        (2*(x*y+z*w)*scale, (1-2*(x*x+z*z))*scale, 2*(y*z-x*w)*scale, position.y),
        (2*(x*z-y*w)*scale, 2*(y*z+x*w)*scale, (1-2*(x*x+y*y))*scale, position.z),
        (0.0, 0.0, 0.0, 1.0),
    )


def gbx_iso_to_blender_matrix(iso) -> Matrix4:
    """Convert NICE's serialized Iso4 basis and translation exactly once."""
    basis = gbx_to_blender_basis((
        (iso.XX, iso.XY, iso.XZ),
        (iso.YX, iso.YY, iso.YZ),
        (iso.ZX, iso.ZY, iso.ZZ),
    ))
    pos = gbx_to_blender_vec3(iso.TX, iso.TY, iso.TZ)
    # Serialized basis vectors are columns of the executable transform.
    return (
        (basis[0][0], basis[1][0], basis[2][0], pos.x),
        (basis[0][1], basis[1][1], basis[2][1], pos.y),
        (basis[0][2], basis[1][2], basis[2][2], pos.z),
        (0.0, 0.0, 0.0, 1.0),
    )


def gbx_to_blender_vec3(x: float, y: float, z: float) -> Vec3:
    """Convert a raw GBX position triple to a Blender-oriented :class:`Vec3`."""
    return Vec3(float(x), -float(z), float(y))


def gbx_to_blender_vec3_iter(values: Iterable[float]) -> Vec3:
    """Same as :func:`gbx_to_blender_vec3`, accepting an iterable of length 3."""
    x, y, z = values
    return gbx_to_blender_vec3(x, y, z)


def gbx_to_blender_basis(
    rows: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    """Convert serialized skeleton matrix rows using C R C^-1."""
    (xx, xy, xz), (yx, yy, yz), (zx, zy, zz) = rows
    return ((xx, -xz, xy), (-zx, zz, -zy), (yx, -yz, yy))


def gbx_to_blender_quat(x: float, y: float, z: float, w: float) -> Quat:
    """Convert a raw GBX quaternion ``(x, y, z, w)`` to Blender orientation.

    Applies the same axis swizzle as :func:`gbx_to_blender_vec3` to the
    vector part; the scalar part is unchanged.
    """
    return Quat(float(x), -float(z), float(y), float(w))


def gbx_to_blender_quat_iter(values: Iterable[float]) -> Quat:
    """Same as :func:`gbx_to_blender_quat`, accepting an iterable of length 4."""
    x, y, z, w = values
    return gbx_to_blender_quat(x, y, z, w)


def blender_to_gbx_vec3(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """Inverse of :func:`gbx_to_blender_vec3`.

    Useful for round-trip tests and for debug exports back to GBX-space.
    """
    return (float(x), float(z), -float(y))


def blender_to_gbx_quat(
    x: float, y: float, z: float, w: float
) -> Tuple[float, float, float, float]:
    """Inverse of :func:`gbx_to_blender_quat`."""
    return (float(x), float(z), -float(y), float(w))


__all__ = [
    "multiply_matrices",
    "blender_transform_matrix",
    "gbx_iso_to_blender_matrix",
    "gbx_to_blender_vec3",
    "gbx_to_blender_vec3_iter",
    "gbx_to_blender_basis",
    "gbx_to_blender_quat",
    "gbx_to_blender_quat_iter",
    "blender_to_gbx_vec3",
    "blender_to_gbx_quat",
]
