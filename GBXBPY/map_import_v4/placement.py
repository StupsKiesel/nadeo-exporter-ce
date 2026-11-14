"""Explicit transform math, matching the normal stock-block importer."""

from __future__ import annotations

import math

from ..model.block import BlockMobil, BlockVariant
from ..model.common import Quat, Vec3
from ..coords import blender_transform_matrix, multiply_matrices as multiply
from .scene import Cell, IDENTITY, Matrix4

YAW = {"North": 0.0, "East": -math.pi / 2, "South": -math.pi, "West": -3 * math.pi / 2}


def translation(x: float, y: float, z: float) -> Matrix4:
    return ((1.0, 0.0, 0.0, x), (0.0, 1.0, 0.0, y),
            (0.0, 0.0, 1.0, z), (0.0, 0.0, 0.0, 1.0))


def rotation(axis: str, angle: float) -> Matrix4:
    c, s = math.cos(angle), math.sin(angle)
    if axis == "X":
        return ((1.0, 0.0, 0.0, 0.0), (0.0, c, -s, 0.0),
                (0.0, s, c, 0.0), IDENTITY[3])
    if axis == "Y":
        return ((c, 0.0, s, 0.0), (0.0, 1.0, 0.0, 0.0),
                (-s, 0.0, c, 0.0), IDENTITY[3])
    if axis == "Z":
        return ((c, -s, 0.0, 0.0), (s, c, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0), IDENTITY[3])
    raise ValueError(f"Unknown rotation axis: {axis}")


def footprint(variant: BlockVariant) -> Cell:
    extents = [max((u.relative_offset[i] for u in variant.units), default=0) + 1 for i in range(3)]
    return (extents[0], extents[1], extents[2])


def grid_matrix(
    coords: Cell, direction: str, size: Cell, grid: tuple[float, float] = (32.0, 8.0),
) -> Matrix4:
    """Block pose; ``grid`` is the collection's (square size, square height)."""
    if direction not in YAW:
        raise ValueError(f"Unknown block direction: {direction}")
    square, height = grid
    cx, cy, cz = coords
    sx, _sy, sz = size
    ox, oy = {
        "North": (0.0, 0.0), "East": (sz * square, 0.0),
        "South": (sx * square, -sz * square), "West": (0.0, -sx * square),
    }[direction]
    return multiply(translation(cx * square + ox, -cz * square + oy, cy * height),
                    rotation("Z", YAW[direction]))


def mobil_matrix(mobil: BlockMobil, *, clip: bool = False) -> Matrix4:
    """Pose of a mobil inside its block/clip from ``GeomTransformation``.

    The three angles are degrees, not radians (stock values are -90/0/90/180).
    Despite the ``yaw, pitch, roll`` field names, the first angle tilts about
    X, the second turns about Y and the third spins about the up axis:
    ``Rx(a1) @ Ry(a2) @ Rz(-a3)`` in Blender space. This was derived by fitting
    every ``PlatformTechWallCheckpoint*`` and ``PlatformWall*Racing`` clip
    prefab inside its own block bounds; it is the only order that fits them all
    and stands wall checkpoints upright. Blocks and clips use the same rule.
    """
    if mobil.geom_transform is None:
        return IDENTITY
    x, y, z, a1, a2, a3 = mobil.geom_transform
    orientation = multiply(
        multiply(rotation("X", math.radians(a1)), rotation("Y", math.radians(a2))),
        rotation("Z", -math.radians(a3)),
    )
    return multiply(translation(x, -z, y), orientation)


def world_cell(coords: Cell, direction: str, offset: Cell, size: Cell) -> Cell:
    cx, cy, cz = coords
    ux, uy, uz = offset
    sx, _sy, sz = size
    if direction == "East":
        return (cx + sz - uz - 1, cy + uy, cz + ux)
    if direction == "South":
        return (cx + sx - ux - 1, cy + uy, cz + sz - uz - 1)
    if direction == "West":
        return (cx + uz, cy + uy, cz + sx - ux - 1)
    return (cx + ux, cy + uy, cz + uz)


def absolute_matrix(position: Vec3, quaternion: Quat, scale: float, height: float) -> Matrix4:
    return blender_transform_matrix(Vec3(position.x, position.y, position.z + height), quaternion, scale)
