"""Vista terrain material slots from the collision shape.

Vista terrain meshes (``Tech3 Block PyPxz_Ids``) select a per-prefab
*material slot* per vertex through ``BlendIndices`` (``layer_idx_a/b``).
The slot is not a texture-array layer index and the slot -> material table
is not stored in the mesh. The static object's collision shape
(``CPlugSurface`` chunk ``0x0900C003``) names a material per triangle, so
each slot is resolved by voting: sampled vertices that use only one slot
are matched to the closest collision triangle within :data:`MAX_DISTANCE`.

The votes are stored on :attr:`Mesh.terrain_slot_votes` as material names
(``CPlugMaterialUserInst.link``), which the addon maps to texture-array
layers through the environment's ``Media/Material/<name>.Material.Gbx``.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..coords import gbx_to_blender_vec3

log = logging.getLogger("gbxbpy.load.terrain_slots")

SURFACE_MESH_CHUNK = 0x0900C003
MATERIAL_USER_INST_CHUNK = 0x090FD000
# Max vertex-to-triangle distance (metres) for a vote.
MAX_DISTANCE = 0.5
SAMPLES_PER_SLOT = 64
_CELL = 4.0

Point = Tuple[float, float, float]
Triangle = Tuple[Point, Point, Point, str]


def _material_name(entry: Any) -> str:
    if not getattr(entry, "hasMaterial", False):
        return ""
    body = getattr(getattr(entry, "material", None), "body", None)
    inst = body.get(MATERIAL_USER_INST_CHUNK) if hasattr(body, "get") else None
    if inst is None:
        return ""
    return str(getattr(inst, "link", "") or getattr(inst, "materialName", "") or "")


def collision_triangles(shape: Any) -> List[Triangle]:
    """Blender-space triangles of a ``CPlugSurface`` with material names."""
    body = getattr(shape, "body", None)
    chunk = body.get(SURFACE_MESH_CHUNK) if hasattr(body, "get") else None
    data = getattr(getattr(chunk, "surf", None), "data", None)
    if chunk is None or data is None:
        return []
    names = [_material_name(m) for m in getattr(chunk, "materials", None) or []]
    try:
        verts = []
        for v in data.vertices:
            p = gbx_to_blender_vec3(v.x, v.y, v.z)
            verts.append((p.x, p.y, p.z))
        result = []
        for tri in data.triangles:
            index = int(getattr(tri, "materialIndex", -1))
            name = names[index] if 0 <= index < len(names) else ""
            if name:
                f = tri.face
                result.append((verts[f.x], verts[f.y], verts[f.z], name))
        return result
    except (AttributeError, IndexError, TypeError) as exc:
        log.debug("collision shape unreadable: %s", exc)
        return []


def _closest_dist2(p: Point, a: Point, b: Point, c: Point) -> float:
    """Squared distance from ``p`` to triangle ``abc`` (Ericson, RTCD 5.1.5)."""
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    ap = (p[0] - a[0], p[1] - a[1], p[2] - a[2])
    dot = lambda u, v: u[0] * v[0] + u[1] * v[1] + u[2] * v[2]  # noqa: E731
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        q = a
    else:
        bp = (p[0] - b[0], p[1] - b[1], p[2] - b[2])
        d3, d4 = dot(ab, bp), dot(ac, bp)
        cp = (p[0] - c[0], p[1] - c[1], p[2] - c[2])
        d5, d6 = dot(ab, cp), dot(ac, cp)
        vc = d1 * d4 - d3 * d2
        vb = d5 * d2 - d1 * d6
        va = d3 * d6 - d5 * d4
        if d3 >= 0 and d4 <= d3:
            q = b
        elif d6 >= 0 and d5 <= d6:
            q = c
        elif vc <= 0 and d1 >= 0 and d3 <= 0:
            t = d1 / (d1 - d3)
            q = (a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2])
        elif vb <= 0 and d2 >= 0 and d6 <= 0:
            t = d2 / (d2 - d6)
            q = (a[0] + t * ac[0], a[1] + t * ac[1], a[2] + t * ac[2])
        elif va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
            t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
            q = (b[0] + t * (c[0] - b[0]), b[1] + t * (c[1] - b[1]), b[2] + t * (c[2] - b[2]))
        else:
            denom = va + vb + vc
            if denom == 0.0:
                return min(dot(ap, ap), dot(bp, bp), dot(cp, cp))
            v, w = vb / denom, vc / denom
            q = (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w,
                 a[2] + ab[2] * v + ac[2] * w)
    d = (p[0] - q[0], p[1] - q[1], p[2] - q[2])
    return dot(d, d)


def _cell(value: float) -> int:
    return math.floor(value / _CELL)


def _grid(triangles: Sequence[Triangle]) -> Dict[Tuple[int, int, int], List[int]]:
    grid: Dict[Tuple[int, int, int], List[int]] = {}
    for index, (a, b, c, _name) in enumerate(triangles):
        lo = [_cell(min(a[i], b[i], c[i]) - MAX_DISTANCE) for i in range(3)]
        hi = [_cell(max(a[i], b[i], c[i]) + MAX_DISTANCE) for i in range(3)]
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    grid.setdefault((x, y, z), []).append(index)
    return grid


def _single_slot(vertex: Any) -> Optional[int]:
    """Slot of a vertex that is not blended between two slots, else ``None``."""
    a, b = int(vertex.layer_idx_a), int(vertex.layer_idx_b)
    if a < 0:
        return None
    t = float(vertex.color.g) if vertex.color is not None else 0.0
    if a == b or t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return None


def vote_slots(
    vertices: Sequence[Any],
    indices: Iterable[int],
    triangles: Sequence[Triangle],
) -> Dict[int, Dict[str, int]]:
    """Vote ``slot -> {material name: count}`` for the given vertex indices."""
    if not triangles:
        return {}
    by_slot: Dict[int, List[int]] = {}
    for index in sorted(set(indices)):
        slot = _single_slot(vertices[index])
        if slot is not None:
            by_slot.setdefault(slot, []).append(index)
    grid = _grid(triangles)
    limit = MAX_DISTANCE * MAX_DISTANCE
    votes: Dict[int, Dict[str, int]] = {}
    for slot, members in by_slot.items():
        step = max(1, len(members) // SAMPLES_PER_SLOT)
        for index in members[::step][:SAMPLES_PER_SLOT]:
            pos = vertices[index].position
            p = (float(pos.x), float(pos.y), float(pos.z))
            best, best_d = "", limit
            for tri in grid.get((_cell(p[0]), _cell(p[1]), _cell(p[2])), ()):
                a, b, c, name = triangles[tri]
                d = _closest_dist2(p, a, b, c)
                if d <= best_d:
                    best, best_d = name, d
            if best:
                counts = votes.setdefault(slot, {})
                counts[best] = counts.get(best, 0) + 1
    return votes


def terrain_slot_votes(mesh: Any, shape: Any) -> Dict[int, Dict[str, int]]:
    """Votes for every vista-terrain submesh of ``mesh`` (LOD0) against ``shape``."""
    from ..textures.terrain_layers import is_texture_array

    if mesh is None or shape is None or not getattr(mesh, "lods", None):
        return {}
    terrain = {
        i for i, binding in enumerate(mesh.materials)
        if is_texture_array((binding.inline_textures or {}).get("BaseColor") or "")
    }
    if not terrain:
        return {}
    lod = min(mesh.lods, key=lambda l: l.lod)
    indices = [
        i for sm in lod.submeshes if sm.material_index in terrain for i in sm.indices
    ]
    if not indices:
        return {}
    return vote_slots(lod.vertices, indices, collision_triangles(shape))


__all__ = ["collision_triangles", "terrain_slot_votes", "vote_slots"]
