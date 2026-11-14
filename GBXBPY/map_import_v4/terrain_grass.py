"""Scatter vista terrain grass (3D decals) over world-space terrain triangles.

The game repeats each layer's decal pattern (:mod:`GBXBPY.textures.terrain_decals`)
in world XY with the layer's top tile size, exactly like the layer texture,
and grows a decal where the layer covers up-facing ground. This module only
does the geometry: the Blender bridge collects terrain triangles and layer
weights, and turns the result into instanced billboards.

Needs numpy (bundled with Blender).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..textures.terrain_decals import LayerDecals, Sprite

# Bucket size (metres) for the point-in-triangle search.
_CELL = 2.0
# Upper bound on candidate pairs per batch, to bound memory.
_PAIR_BATCH = 4_000_000


@dataclass(frozen=True)
class SpriteKey:
    atlas: str
    sprite: str
    record: Sprite = field(compare=False, hash=False, default=Sprite("", 0.0, 0.0, 1.0, 1.0))


@dataclass
class GrassPattern:
    """Flat arrays over one layer's decal entries."""

    tile: tuple[float, float]
    u: np.ndarray
    v: np.ndarray
    height: np.ndarray
    min_up: np.ndarray
    keep: np.ndarray
    sprite: np.ndarray  # index into :attr:`sprites`
    sprites: list[SpriteKey]

    def __len__(self) -> int:
        return int(self.u.shape[0])


def grass_pattern(
    decals: LayerDecals, tile: Sequence[float], sprites: list[SpriteKey] | None = None,
) -> GrassPattern | None:
    """Pattern arrays for ``decals``; sprite keys are appended to ``sprites``.

    Entries whose decal, atlas image or sprite is missing are dropped.
    Billboard height is the entry's vertical size times the tile size.
    """
    sprites = [] if sprites is None else sprites
    index = {key: i for i, key in enumerate(sprites)}
    rng = random.Random(f"{decals.layer.casefold()}#grass")
    rows = []
    tile_u, tile_v = float(tile[0]), float(tile[1])
    for entry in decals.texture_decals.entries:
        keep = rng.random()
        decal = decals.decals[entry.decal] if entry.decal < len(decals.decals) else None
        atlas = decal.atlas if decal else None
        sprite = atlas.sprites.get(entry.sprite) if atlas and atlas.image else None
        if sprite is None:
            continue
        key = SpriteKey(atlas.image, entry.sprite, sprite)
        slot = index.get(key)
        if slot is None:
            slot = index[key] = len(sprites)
            sprites.append(key)
        rows.append((
            entry.u % 1.0, entry.v % 1.0, entry.size * tile_v * sprite.scale,
            decal.min_up, keep, slot,
        ))
    if not rows:
        return None
    data = np.array(rows, dtype=np.float64)
    return GrassPattern(
        (tile_u, tile_v), data[:, 0], data[:, 1], data[:, 2], data[:, 3], data[:, 4],
        data[:, 5].astype(np.int32), sprites,
    )


def _pattern_points(pattern: GrassPattern, lo: np.ndarray, hi: np.ndarray):
    """Pattern points (x, y, entry, tile key) tiled over the XY box ``lo``..``hi``."""
    tu, tv = pattern.tile
    i0, i1 = int(np.floor(lo[0] / tu)), int(np.floor(hi[0] / tu))
    j0, j1 = int(np.floor(lo[1] / tv)), int(np.floor(hi[1] / tv))
    ti, tj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1), indexing="ij")
    ti, tj = ti.ravel(), tj.ravel()
    n = len(pattern)
    x = ((ti[:, None] + pattern.u[None, :]) * tu).ravel()
    y = ((tj[:, None] + pattern.v[None, :]) * tv).ravel()
    entry = np.tile(np.arange(n), ti.shape[0])
    tile_id = (ti.astype(np.int64)[:, None] * 1_000_003 + tj[:, None]).repeat(n, axis=1).ravel()
    inside = (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1])
    return x[inside], y[inside], entry[inside], tile_id[inside] * n + entry[inside]


def _locate(x: np.ndarray, y: np.ndarray, tris: np.ndarray):
    """Yield ``(point idx, triangle idx, wa, wb, wc)`` batches of XY containment.

    Points are bucketed on a :data:`_CELL` grid; each triangle tests the
    points of the cells its XY box overlaps. Barycentric weights are for
    the triangle's corners 0, 1, 2.
    """
    if x.shape[0] == 0 or tris.shape[0] == 0:
        return
    lo = tris[:, :, :2].min(axis=1)
    hi = tris[:, :, :2].max(axis=1)
    origin = np.minimum(lo.min(axis=0), (x.min(), y.min()))
    cx = np.floor((x - origin[0]) / _CELL).astype(np.int64)
    cy = np.floor((y - origin[1]) / _CELL).astype(np.int64)
    t_lo = np.floor((lo - origin) / _CELL).astype(np.int64)
    t_hi = np.floor((hi - origin) / _CELL).astype(np.int64)
    height = int(max(cy.max(), t_hi[:, 1].max())) + 2
    cell = cx * height + cy
    order = np.argsort(cell, kind="stable")
    cell_sorted = cell[order]

    span_x = t_hi[:, 0] - t_lo[:, 0] + 1
    span_y = t_hi[:, 1] - t_lo[:, 1] + 1
    counts = span_x * span_y
    tri_of = np.repeat(np.arange(tris.shape[0]), counts)
    offset = np.arange(tri_of.shape[0]) - np.repeat(np.cumsum(counts) - counts, counts)
    pair_cells = (
        (t_lo[tri_of, 0] + offset // span_y[tri_of]) * height
        + t_lo[tri_of, 1] + offset % span_y[tri_of]
    )
    start = np.searchsorted(cell_sorted, pair_cells, side="left")
    sizes = np.searchsorted(cell_sorted, pair_cells, side="right") - start
    cumulative = np.cumsum(sizes)
    begin = 0
    while begin < sizes.shape[0]:
        base = cumulative[begin - 1] if begin else 0
        end = max(int(np.searchsorted(cumulative, base + _PAIR_BATCH, side="right")), begin + 1)
        s, n = start[begin:end], sizes[begin:end]
        tri = np.repeat(tri_of[begin:end], n)
        first = np.repeat(s - (np.cumsum(n) - n), n)
        pts = order[first + np.arange(tri.shape[0])]
        begin = end
        if tri.shape[0] == 0:
            continue
        a, b, c = tris[tri, 0], tris[tri, 1], tris[tri, 2]
        v0x, v0y = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
        v1x, v1y = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]
        v2x, v2y = x[pts] - a[:, 0], y[pts] - a[:, 1]
        den = v0x * v1y - v1x * v0y
        ok = np.abs(den) > 1e-12
        safe = np.where(ok, den, 1.0)
        wb = (v2x * v1y - v1x * v2y) / safe
        wc = (v0x * v2y - v2x * v0y) / safe
        wa = 1.0 - wb - wc
        inside = ok & (wa >= -1e-9) & (wb >= -1e-9) & (wc >= -1e-9)
        if inside.any():
            yield pts[inside], tri[inside], wa[inside], wb[inside], wc[inside]


def below_surfaces(points: np.ndarray, triangles: np.ndarray, tolerance: float = 0.05) -> np.ndarray:
    """Mask of ``points`` lying at or under any of ``triangles`` (e.g. water)."""
    points = np.asarray(points, dtype=np.float64)
    mask = np.zeros(points.shape[0], dtype=bool)
    tris = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    if points.shape[0] == 0 or tris.shape[0] == 0:
        return mask
    for pts, tri, wa, wb, wc in _locate(points[:, 0], points[:, 1], tris):
        z = wa * tris[tri, 0, 2] + wb * tris[tri, 1, 2] + wc * tris[tri, 2, 2]
        mask[pts[points[pts, 2] <= z + tolerance]] = True
    return mask


def scatter(
    triangles: np.ndarray,
    weights: np.ndarray,
    pattern: GrassPattern,
    density: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Grass points on ``triangles`` (``(n, 3, 3)`` world XYZ, Z up).

    ``weights`` (``(n, 3)``) is the layer's corner weight; a point is kept
    when the interpolated weight times ``density`` exceeds the entry's fixed
    random threshold and the face is no steeper than the decal allows.
    Points covered by several triangles keep the highest one.
    Returns ``(positions (m, 3), entry indices (m,))``.
    """
    empty = (np.zeros((0, 3)), np.zeros(0, dtype=np.int64))
    if len(pattern) == 0 or triangles.shape[0] == 0 or density <= 0.0:
        return empty
    tris = np.asarray(triangles, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(normal, axis=1)
    up = np.divide(normal[:, 2], length, out=np.zeros_like(length), where=length > 1e-12)
    usable = (up >= float(pattern.min_up.min())) & (weights.max(axis=1) > 0.0)
    tris, weights, up = tris[usable], weights[usable], up[usable]
    if tris.shape[0] == 0:
        return empty

    lo = tris[:, :, :2].min(axis=1)
    hi = tris[:, :, :2].max(axis=1)
    x, y, entry, key = _pattern_points(pattern, lo.min(axis=0), hi.max(axis=0))
    if x.shape[0] == 0:
        return empty
    out_pos, out_entry, out_key = [], [], []
    for pts, tri, wa, wb, wc in _locate(x, y, tris):
        e = entry[pts]
        w = wa * weights[tri, 0] + wb * weights[tri, 1] + wc * weights[tri, 2]
        keep = (w * density > pattern.keep[e]) & (up[tri] >= pattern.min_up[e])
        if not keep.any():
            continue
        z = wa * tris[tri, 0, 2] + wb * tris[tri, 1, 2] + wc * tris[tri, 2, 2]
        out_pos.append(np.stack([x[pts][keep], y[pts][keep], z[keep]], axis=1))
        out_entry.append(e[keep])
        out_key.append(key[pts][keep])
    if not out_pos:
        return empty
    pos = np.concatenate(out_pos)
    ent = np.concatenate(out_entry)
    keys = np.concatenate(out_key)
    # Highest surface wins for points covered more than once.
    order = np.lexsort((-pos[:, 2], keys))
    first = np.ones(order.shape[0], dtype=bool)
    first[1:] = keys[order][1:] != keys[order][:-1]
    chosen = order[first]
    return pos[chosen], ent[chosen].astype(np.int64)


__all__ = ["GrassPattern", "SpriteKey", "below_surfaces", "grass_pattern", "scatter"]
