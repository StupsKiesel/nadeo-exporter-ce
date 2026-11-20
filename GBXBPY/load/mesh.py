"""Loader for ``CPlugSolid2Model`` (class id ``0x090BB000``).

Reads visuals + materials + skeleton + lights out of the parsed NICE
container and produces a :class:`~GBXBPY.model.mesh.Mesh` with vertices
already in Blender axes (Z-up). Skeleton bone transforms, when present,
are baked into vertex positions so the addon does not need to apply them.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..color_tables import srgb_to_linear
from ..coords import gbx_to_blender_vec3, multiply_matrices
from ..errors import ParseError
from ..model import (
    Box,
    Color,
    LightAttachment,
    MaterialBinding,
    Mesh,
    MeshLOD,
    Submesh,
    Vec2,
    Vec3,
    Vertex,
)
from ..registry import register
from ..model.geometry import Matrix4
from ._light import apply_light_source, light_from_user_model

log = logging.getLogger("gbxbpy.load.mesh")

CLASS_ID = 0x090BB000

# Body chunks
_CHUNK_MAIN = 0x090BB000
_CHUNK_ICON = 0x090BB002

# Visual chunks
_CHUNK_VISUAL_F = 0x0900600F
_CHUNK_VISUAL_E = 0x0900600E
_CHUNK_VISUAL_D = 0x0900600D
_CHUNK_VISUAL_3D_ARRAYS = 0x0902C004
_CHUNK_INDEX_BUFFER = 0x0906A001
_CHUNK_VERTEX_STREAM = 0x09056000
_CHUNK_INDICES_ABS = 0x09057000
_CHUNK_INDICES_REL = 0x09057001
_CHUNK_SUB_VISUALS = 0x09006005

# CPlugMaterialUserInst chunks
_CHUNK_MUI_LINK = 0x090FD000
_CHUNK_MUI_DETAIL = 0x090FD001
_CHUNK_MUI_NEW_PATH = 0x09079012
_CHUNK_MUI_NEW_LINK = 0x09079015
_CHUNK_MUI_CUSTOM = 0x09079007
_CHUNK_MUI_TEXTURES = 0x0903A013

# Skeleton
_CHUNK_SKEL = 0x090BA000


# ---------------------------------------------------------------------------
#  Internal mutable scratch records used while parsing visuals.
# ---------------------------------------------------------------------------

class _ScratchVertex:
    """Mutable vertex while parsing — converted to immutable Vertex later."""

    __slots__ = (
        "px", "py", "pz", "nx", "ny", "nz",
        "u0", "v0", "u1", "v1", "u2", "v2",
        "color", "bone_index", "layer_idx_a", "layer_idx_b", "bone_weights",
    )

    def __init__(self) -> None:
        self.px = self.py = self.pz = 0.0
        self.nx = 0.0
        self.ny = 1.0
        self.nz = 0.0
        self.u0 = self.v0 = 0.0
        self.u1 = self.v1 = 0.0
        self.u2 = self.v2 = 0.0
        self.color: Optional[Tuple[float, float, float, float]] = None
        self.bone_index = -1
        self.layer_idx_a = -1
        self.layer_idx_b = -1
        self.bone_weights: Tuple[Tuple[int, float], ...] = ()


class _ScratchVisual:
    __slots__ = ("vertices", "indices", "bbox_min", "bbox_max")

    def __init__(self) -> None:
        self.vertices: List[_ScratchVertex] = []
        self.indices: List[int] = []
        self.bbox_min: Optional[Tuple[float, float, float]] = None
        self.bbox_max: Optional[Tuple[float, float, float]] = None


# ---------------------------------------------------------------------------
#  Generic helpers
# ---------------------------------------------------------------------------

def _body_dict(parsed: Any) -> dict:
    body = getattr(parsed, "body", None)
    if isinstance(body, dict):
        return body
    try:
        return dict(body) if body is not None else {}
    except (TypeError, ValueError):
        return {}


def _file_stem(path: str) -> str:
    if not path:
        return ""
    base = os.path.basename(path)
    return base.split(".", 1)[0] if base else ""


def _chunk(body: dict, *keys: int) -> Any:
    for key in keys:
        chunk = body.get(key)
        if chunk is not None:
            return chunk
    return None


# ---------------------------------------------------------------------------
#  Material parsing (CPlugMaterialUserInst)
# ---------------------------------------------------------------------------

def _texture_path_from_node(node: Any) -> str:
    if node is None:
        return ""
    for attr in ("gamedata_path", "filePath", "filepath"):
        value = getattr(node, attr, "")
        if value:
            return str(value)
    rel = getattr(node, "_relativeFilePath", "") or ""
    if rel:
        parts = rel.replace("\\", "/").split("/")
        while parts and parts[0] in ("..", ""):
            parts.pop(0)
        return "/".join(parts)
    return ""


def target_color_from_user_inst(data: Any) -> tuple:
    """Linear RGB ``TargetColor`` of a 0x090FD000 user-instance chunk.

    ``csts`` declares the shader constants the instance overrides and
    ``color`` may hold linear floats or sRGB byte values in 32-bit words.
    NICE exposes both as floats; recover the byte form before linearising.
    """
    declared = any(
        str(getattr(cst, "u01", "") or "") == "TargetColor"
        for cst in (getattr(data, "csts", None) or ())
    )
    if not declared:
        return ()
    try:
        floats = tuple(float(c) for c in (getattr(data, "color", None) or ()))
    except (TypeError, ValueError):
        return ()
    if len(floats) < 3:
        return ()
    rgb = floats[:3]
    if all(0.0 <= c <= 1.0 for c in rgb):
        words = struct.unpack("<3I", struct.pack("<3f", *rgb))
        if all(word <= 255 for word in words):
            return tuple(srgb_to_linear(word / 255.0) for word in words)
    return rgb


def _parse_material_binding(raw: Any) -> MaterialBinding:
    binding = MaterialBinding()
    data = raw
    data2: Any = None  # second body chunk carrying tiling / textureSize
    body_dict: Optional[dict] = None
    if hasattr(raw, "body"):
        body = raw.body
        if isinstance(body, dict):
            body_dict = body
            non_none = [v for v in body.values() if v is not None]
            if len(non_none) >= 2:
                data2 = non_none[1]
            if non_none:
                data = non_none[0]

    binding.material_path = (
        getattr(data, "link", "")
        or getattr(raw, "link", "")
        or ""
    )

    if not binding.material_path and body_dict is not None:
        new_path_chunk = body_dict.get(_CHUNK_MUI_NEW_PATH)
        if new_path_chunk is not None:
            src_path = getattr(new_path_chunk, "u01", "") or ""
            if src_path:
                rel = src_path
                prefix = ":data:\\Projects\\"
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                binding.material_path = rel
        if not binding.material_path:
            new_link_chunk = body_dict.get(_CHUNK_MUI_NEW_LINK)
            if new_link_chunk is not None:
                base_mat = getattr(new_link_chunk, "baseMaterial", None)
                if base_mat is not None:
                    bm_body = getattr(base_mat, "body", None)
                    if isinstance(bm_body, dict):
                        bm_chunk = bm_body.get(_CHUNK_MUI_LINK)
                        if bm_chunk is not None:
                            binding.material_path = (
                                getattr(bm_chunk, "link", "") or ""
                            )

    if body_dict is not None:
        custom_chunk = body_dict.get(_CHUNK_MUI_CUSTOM)
        if custom_chunk is not None:
            cm = getattr(custom_chunk, "custom_material", None)
            if cm is not None:
                cm_body = getattr(cm, "body", None)
                if isinstance(cm_body, dict):
                    tex_chunk = cm_body.get(_CHUNK_MUI_TEXTURES)
                    if tex_chunk is not None:
                        for tex in getattr(tex_chunk, "textures", None) or []:
                            name = str(getattr(tex, "name", "") or "")
                            path = _texture_path_from_node(
                                getattr(tex, "textureNod", None)
                            )
                            if name and path:
                                binding.inline_textures[name] = path

    from ._gbx_helpers import surface_id
    binding.surface_physic_id = surface_id(getattr(data, "surfacePhysicId", None))
    binding.surface_gameplay_id = surface_id(getattr(data, "surfaceGameplayId", None), gameplay=True)

    # CPlugMaterialUserInst extras: shader model name, material name, tint.
    # ``data`` here is the inner 0x090FD000 chunk struct (per the
    # ``non_none = [v for v in body.values() if v is not None]`` pick above),
    # which exposes ``model``, ``materialName``, and ``color`` directly.
    model = getattr(data, "model", "") or ""
    if model:
        binding.shader_model = str(model)
    mat_name = getattr(data, "materialName", "") or ""
    if mat_name:
        binding.material_name = str(mat_name)
    color = getattr(data, "color", None)
    binding.target_color = target_color_from_user_inst(data)
    if color and not binding.target_color:
        try:
            floats = tuple(float(c) for c in color)
        except (TypeError, ValueError):
            floats = ()
        # Only keep the tint when it's actually meaningful (3+ components,
        # not all zero — game uses (0,0,0) as "unset" for some chunks).
        if len(floats) >= 3 and any(c != 0.0 for c in floats):
            binding.color_tint = floats[:4]

    # Per-binding tiling / texture_size (chunk 0x090FD001 picked up as
    # ``data2`` above). Falls back to ``data`` for older variants that
    # carry the fields on the primary chunk.
    tiling_src = data2 if data2 is not None else data
    binding.texture_size = float(
        getattr(tiling_src, "textureSize", 1.0) or 1.0
    )
    binding.tiling_u = str(
        getattr(tiling_src, "tilingU", "Wrap") or "Wrap"
    )
    binding.tiling_v = str(
        getattr(tiling_src, "tilingV", "Wrap") or "Wrap"
    )

    return binding


# ---------------------------------------------------------------------------
#  Visual parsing
# ---------------------------------------------------------------------------

def _parse_visual(raw: Any) -> _ScratchVisual:
    vis = _ScratchVisual()
    body = raw.body if hasattr(raw, "body") else raw
    if not isinstance(body, dict):
        return vis

    chunk_f = _chunk(body, _CHUNK_VISUAL_F, _CHUNK_VISUAL_E, _CHUNK_VISUAL_D)

    if chunk_f is not None:
        streams = getattr(chunk_f, "vertexStreams", None) or []
        if streams:
            _parse_vertex_stream_noderefs(streams, vis)

    chunk_b = body.get(_CHUNK_VISUAL_3D_ARRAYS)
    if chunk_b is not None and not vis.vertices:
        _parse_vertex_arrays(chunk_b, chunk_f, vis)

    chunk_idx = body.get(_CHUNK_INDEX_BUFFER)
    if chunk_idx is not None:
        _parse_index_buffer(chunk_idx, vis)

    if chunk_f is not None:
        bb = getattr(chunk_f, "BoundingBox", None)
        if bb is not None:
            min_p = getattr(bb, "min", None)
            max_p = getattr(bb, "max", None)
            if min_p is not None and max_p is not None:
                vis.bbox_min = (
                    float(getattr(min_p, "x", 0.0)),
                    float(getattr(min_p, "y", 0.0)),
                    float(getattr(min_p, "z", 0.0)),
                )
                vis.bbox_max = (
                    float(getattr(max_p, "x", 0.0)),
                    float(getattr(max_p, "y", 0.0)),
                    float(getattr(max_p, "z", 0.0)),
                )
    return vis


def _parse_vertex_stream_noderefs(streams: Iterable[Any], vis: _ScratchVisual) -> None:
    for stream_ref in streams:
        if not hasattr(stream_ref, "body") or stream_ref.body is None:
            continue
        stream_body = stream_ref.body
        chunk_s = (
            stream_body.get(_CHUNK_VERTEX_STREAM)
            if isinstance(stream_body, dict)
            else stream_body
        )
        if chunk_s is None:
            continue

        data_decl = getattr(chunk_s, "DataDecl", None) or []
        data_arrays = getattr(chunk_s, "Data", None) or []
        num_verts = int(getattr(chunk_s, "numVertices", 0) or 0)
        if not data_decl or not data_arrays or num_verts == 0:
            continue

        channels: Dict[str, Any] = {}
        for i, decl in enumerate(data_decl):
            if i >= len(data_arrays):
                continue
            name = ""
            if hasattr(decl, "header"):
                name = str(getattr(decl.header, "Name", "") or "")
            channels[name] = data_arrays[i]

        positions = channels.get("Position", [])
        normals = channels.get("Normal", [])
        uv0s = channels.get("TexCoord0", [])
        uv1s = channels.get("TexCoord1", [])
        uv2s = channels.get("TexCoord2", [])
        blend_indices = channels.get("BlendIndices", [])
        blend_weights = channels.get("BlendWeight", [])
        colors = (
            channels.get("Color", [])
            or channels.get("Color0", [])
            or channels.get("Diffuse", [])
        )

        for i in range(num_verts):
            vert = _ScratchVertex()
            if i < len(positions):
                p = positions[i]
                if hasattr(p, "x"):
                    vert.px, vert.py, vert.pz = p.x, p.y, p.z
            if i < len(normals):
                n = normals[i]
                if hasattr(n, "x"):
                    vert.nx, vert.ny, vert.nz = n.x, n.y, n.z
            if i < len(uv0s):
                uv = uv0s[i]
                if hasattr(uv, "x"):
                    vert.u0, vert.v0 = uv.x, uv.y
            if i < len(uv1s):
                uv = uv1s[i]
                if hasattr(uv, "x"):
                    vert.u1, vert.v1 = uv.x, uv.y
            if i < len(uv2s):
                uv = uv2s[i]
                if hasattr(uv, "x"):
                    vert.u2, vert.v2 = uv.x, uv.y
            if i < len(blend_indices):
                bi = blend_indices[i]
                if hasattr(bi, "__getitem__"):
                    try:
                        b0 = int(bi[0])
                    except (IndexError, TypeError, ValueError):
                        b0 = -1
                    try:
                        b1 = int(bi[1])
                    except (IndexError, TypeError, ValueError):
                        b1 = -1
                    vert.bone_index = b0
                    vert.layer_idx_a = b0
                    vert.layer_idx_b = b1
                    if i < len(blend_weights):
                        weights = blend_weights[i]
                        if not all(hasattr(weights, component) for component in ("x", "y", "z")):
                            raise ValueError("Unsupported mesh blend-weight packing; expected Float3")
                        components = [float(weights.x), float(weights.y), float(weights.z)]
                        components.append(1.0 - sum(components))
                        if any(not math.isfinite(w) or w < -1e-5 or w > 1.00001 for w in components):
                            raise ValueError("Mesh contains invalid blend weights")
                        influences = {}
                        for bone, weight in zip(bi, components):
                            if weight > 1e-6:
                                influences[int(bone)] = influences.get(int(bone), 0.0) + weight
                        vert.bone_weights = tuple(influences.items())
                elif isinstance(bi, (int, float)):
                    vert.bone_index = int(bi)
                    vert.layer_idx_a = int(bi)
            if i < len(colors):
                vert.color = _normalize_color(colors[i])
            vis.vertices.append(vert)


def _parse_vertex_arrays(chunk: Any, chunk_f: Any, vis: _ScratchVisual) -> None:
    verts = getattr(chunk, "vertices", None) or []
    tex_coords_list: List[List[Any]] = []
    if chunk_f is not None:
        for tc_set in getattr(chunk_f, "texCoords", None) or []:
            tex_coords_list.append(list(getattr(tc_set, "tex_coords", None) or []))

    for i, v in enumerate(verts):
        vert = _ScratchVertex()
        pos = getattr(v, "position", None) or v
        if hasattr(pos, "x"):
            vert.px, vert.py, vert.pz = pos.x, pos.y, pos.z
        norm = getattr(v, "normal", None)
        if norm is not None and hasattr(norm, "x"):
            vert.nx, vert.ny, vert.nz = norm.x, norm.y, norm.z
        color = getattr(v, "color", None)
        if color is not None:
            vert.color = _normalize_color(color)

        for slot, tc_list in enumerate(tex_coords_list[:3]):
            if i >= len(tc_list):
                continue
            uv = getattr(tc_list[i], "uv", None)
            if uv is None or not hasattr(uv, "x"):
                continue
            if slot == 0:
                vert.u0, vert.v0 = uv.x, uv.y
            elif slot == 1:
                vert.u1, vert.v1 = uv.x, uv.y
            else:
                vert.u2, vert.v2 = uv.x, uv.y

        vis.vertices.append(vert)


def _parse_index_buffer(chunk: Any, vis: _ScratchVisual) -> None:
    ib = getattr(chunk, "indexBuffer", None)
    if ib is None:
        return

    flags = 0
    raw_indices: List[int] = []
    is_relative_chunk = False
    is_absolute_chunk = False

    if isinstance(ib, dict):
        ib_chunk = ib.get(_CHUNK_INDICES_ABS)
        if ib_chunk is not None:
            is_absolute_chunk = True
            flags = int(getattr(ib_chunk, "flags", 0) or 0)
            raw_indices = [int(i) for i in (getattr(ib_chunk, "indices", None) or [])]
        else:
            ib_chunk = ib.get(_CHUNK_INDICES_REL)
            if ib_chunk is not None:
                is_relative_chunk = True
                flags = int(getattr(ib_chunk, "flags", 0) or 0)
                raw_indices = [int(i) for i in (getattr(ib_chunk, "indices", None) or [])]
    elif hasattr(ib, "__iter__"):
        raw_indices = [int(i) for i in ib]

    if not raw_indices:
        return

    is_delta = is_relative_chunk or flags == 2
    if not is_delta and not is_absolute_chunk and len(raw_indices) > 3:
        if max(abs(v) for v in raw_indices) <= 1:
            is_delta = True

    if is_delta:
        absolute = []
        acc = 0
        # Signed deltas wrap at 16 bits for visuals in the uint16 index domain.
        # Larger vertex buffers can legitimately accumulate beyond 65535.
        wrap_16bit = is_relative_chunk and 0 < len(vis.vertices) <= 65536
        for delta in raw_indices:
            acc += delta
            if wrap_16bit:
                acc &= 0xFFFF
            absolute.append(acc)
        vis.indices = absolute
    else:
        vis.indices = raw_indices


def _normalize_color(value: Any) -> Optional[Tuple[float, float, float, float]]:
    if isinstance(value, int):
        a = ((value >> 0) & 0xFF) / 255.0
        b = ((value >> 8) & 0xFF) / 255.0
        g = ((value >> 16) & 0xFF) / 255.0
        r = ((value >> 24) & 0xFF) / 255.0
        return (r, g, b, a)

    rgba: Optional[List[float]] = None
    if all(hasattr(value, attr) for attr in ("x", "y", "z", "w")):
        rgba = [value.x, value.y, value.z, value.w]
    elif all(hasattr(value, attr) for attr in ("r", "g", "b", "a")):
        rgba = [value.r, value.g, value.b, value.a]
    elif hasattr(value, "__getitem__"):
        try:
            if len(value) >= 4:
                rgba = [value[0], value[1], value[2], value[3]]
        except (TypeError, ValueError):
            return None

    if rgba is None:
        return None

    try:
        floats = [float(v) for v in rgba]
    except (TypeError, ValueError):
        return None

    if max(floats) > 1.5:
        floats = [v / 255.0 for v in floats]
    return (floats[0], floats[1], floats[2], floats[3])


# ---------------------------------------------------------------------------
#  Skeleton baking
# ---------------------------------------------------------------------------

class _BakedJoint:
    __slots__ = ("rotation", "translation", "name")

    def __init__(self) -> None:
        self.rotation = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        self.translation = (0.0, 0.0, 0.0)
        self.name = ""


def _parse_skeleton(skel_node: Any) -> List[_BakedJoint]:
    if skel_node is None:
        return []
    body = getattr(skel_node, "body", None)
    if not isinstance(body, dict):
        return []
    chunk = body.get(_CHUNK_SKEL)
    if chunk is None:
        return []
    out: List[_BakedJoint] = []
    for j in getattr(chunk, "joints", None) or []:
        bone = _BakedJoint()
        bone.name = str(getattr(j, "name", "") or "")
        gl = getattr(j, "GlobalLoc", None)
        if gl is not None:
            bone.rotation = (
                (
                    float(getattr(gl, "XX", 1.0)),
                    float(getattr(gl, "XY", 0.0)),
                    float(getattr(gl, "XZ", 0.0)),
                ),
                (
                    float(getattr(gl, "YX", 0.0)),
                    float(getattr(gl, "YY", 1.0)),
                    float(getattr(gl, "YZ", 0.0)),
                ),
                (
                    float(getattr(gl, "ZX", 0.0)),
                    float(getattr(gl, "ZY", 0.0)),
                    float(getattr(gl, "ZZ", 1.0)),
                ),
            )
            bone.translation = (
                float(getattr(gl, "TX", 0.0)),
                float(getattr(gl, "TY", 0.0)),
                float(getattr(gl, "TZ", 0.0)),
            )
        out.append(bone)
    return out


def _apply_skeleton(visuals: List[_ScratchVisual], joints: List[_BakedJoint]) -> None:
    if not joints:
        return
    for vis in visuals:
        if not any(v.bone_index >= 0 for v in vis.vertices):
            continue
        for v in vis.vertices:
            bi = v.bone_index
            if bi < 0 or bi >= len(joints):
                continue
            r = joints[bi].rotation
            t = joints[bi].translation
            px, py, pz = v.px, v.py, v.pz
            v.px = r[0][0] * px + r[0][1] * py + r[0][2] * pz + t[0]
            v.py = r[1][0] * px + r[1][1] * py + r[1][2] * pz + t[1]
            v.pz = r[2][0] * px + r[2][1] * py + r[2][2] * pz + t[2]
            nx, ny, nz = v.nx, v.ny, v.nz
            v.nx = r[0][0] * nx + r[0][1] * ny + r[0][2] * nz
            v.ny = r[1][0] * nx + r[1][1] * ny + r[1][2] * nz
            v.nz = r[2][0] * nx + r[2][1] * ny + r[2][2] * nz


# ---------------------------------------------------------------------------
#  Assembly
# ---------------------------------------------------------------------------

def _to_vertex(s: _ScratchVertex) -> Vertex:
    color = Color(*s.color) if s.color is not None else None
    return Vertex(
        position=gbx_to_blender_vec3(s.px, s.py, s.pz),
        normal=gbx_to_blender_vec3(s.nx, s.ny, s.nz),
        uv0=Vec2(s.u0, s.v0),
        uv1=Vec2(s.u1, s.v1),
        uv2=Vec2(s.u2, s.v2),
        color=color,
        bone_index=s.bone_index,
        layer_idx_a=s.layer_idx_a,
        layer_idx_b=s.layer_idx_b,
        bone_weights=s.bone_weights,
    )


def _bbox_for_visuals(visuals: Iterable[_ScratchVisual]) -> Box:
    mins = [v.bbox_min for v in visuals if v.bbox_min is not None]
    maxs = [v.bbox_max for v in visuals if v.bbox_max is not None]
    if not mins or not maxs:
        return Box()
    mn = (min(p[0] for p in mins), min(p[1] for p in mins), min(p[2] for p in mins))
    mx = (max(p[0] for p in maxs), max(p[1] for p in maxs), max(p[2] for p in maxs))
    return Box(
        min=gbx_to_blender_vec3(*mn),
        max=gbx_to_blender_vec3(*mx),
    )


def _build_lod(
    lod: int,
    distance: float,
    geoms: List[Tuple[int, int]],  # (visual_index, material_index)
    visuals: List[_ScratchVisual],
) -> MeshLOD:
    # Unique visuals in order of first appearance — a single visual can
    # be referenced by multiple submeshes with different material indices.
    unique_visuals: List[int] = []
    seen: set[int] = set()
    for vi, _mi in geoms:
        if 0 <= vi < len(visuals) and vi not in seen:
            seen.add(vi)
            unique_visuals.append(vi)

    # Detect packed VertexTween pose blocks per visual.
    packed: Dict[int, Tuple[int, int]] = {}  # vi -> (base_count, pose_count)
    for vi in unique_visuals:
        sv = visuals[vi]
        n = len(sv.vertices)
        min_ref = min(sv.indices, default=0)
        max_ref = max(sv.indices, default=-1)
        if sv.indices and (min_ref < 0 or max_ref >= n):
            raise ParseError(
                f"LOD {lod} visual {vi}: index range "
                f"{min_ref}..{max_ref} outside {n} vertices"
            )
        if not sv.vertices or not sv.indices:
            packed[vi] = (n, 1)
            continue
        base_count = max_ref + 1
        if base_count <= 0 or base_count > n:
            packed[vi] = (n, 1)
            continue
        if n % base_count == 0 and n >= 2 * base_count:
            packed[vi] = (base_count, n // base_count)
        else:
            packed[vi] = (base_count, 1)

    common_pose_count = min((pc for _, pc in packed.values()), default=1)
    has_packed_poses = common_pose_count >= 2

    flat: List[Vertex] = []
    visual_offsets: Dict[int, int] = {}
    visual_keep: Dict[int, int] = {}
    used_visuals: List[_ScratchVisual] = []

    for vi in unique_visuals:
        sv = visuals[vi]
        base_count, _pose_count = packed[vi]
        keep = base_count if has_packed_poses else len(sv.vertices)
        visual_offsets[vi] = len(flat)
        visual_keep[vi] = keep
        flat.extend(_to_vertex(v) for v in sv.vertices[:keep])
        used_visuals.append(sv)

    submeshes: List[Submesh] = []
    for vi, mi in geoms:
        if vi not in visual_offsets:
            continue
        offset = visual_offsets[vi]
        submeshes.append(
            Submesh(
                material_index=mi,
                indices=[idx + offset for idx in visuals[vi].indices],
            )
        )

    pose_blocks: List[List[Vec3]] = []
    if has_packed_poses:
        for k in range(1, common_pose_count):
            block: List[Vec3] = []
            for vi in unique_visuals:
                sv = visuals[vi]
                base_count, pose_count = packed[vi]
                keep = visual_keep[vi]
                if pose_count >= 2:
                    start = k * base_count
                    src = sv.vertices[start:start + base_count]
                    block.extend(
                        gbx_to_blender_vec3(s.px, s.py, s.pz) for s in src
                    )
                else:
                    block.extend(
                        gbx_to_blender_vec3(s.px, s.py, s.pz)
                        for s in sv.vertices[:keep]
                    )
            pose_blocks.append(block)

    return MeshLOD(
        lod=lod,
        distance=distance,
        vertices=flat,
        submeshes=submeshes,
        bounding_box=_bbox_for_visuals(used_visuals),
        pose_blocks=pose_blocks,
    )


# ---------------------------------------------------------------------------
#  GrassFence UV expansion
# ---------------------------------------------------------------------------
#
# Vegetation fence/grass materials store the visible card texture in the
# ``FenceA`` slot of a ``VDepLight`` shader.  In the source mesh every
# billboard quad's four vertices share a single ``UV.x`` value (it acts
# as a column selector into the FenceA atlas) with ``UV.y`` in {0, 1}.
# That produces zero-width vertical lines in the UV editor and a single
# pixel column sampled per quad — visually wrong.
#
# The expansion below mirrors the legacy addon (see
# ``Legacy/gbxbpy/blender/mesh_builder.py`` ``_expand_fence_uvs``).  Each
# quad's UV.x is widened to a real range whose width is proportional to
# the quad's world-space horizontal extent (``meters_per_uv`` metres per
# UV unit).  The original UV.x is preserved as the centre offset so each
# blade samples a different slice of the atlas.

# Default tile rate copied from the legacy addon (32 m of grass / 16 UV
# atlas columns ⇒ 2 m per UV unit).
_FENCE_METERS_PER_UV = 32.0 / 16.0


def _is_fence_material(binding: MaterialBinding) -> bool:
    """Return True when ``binding`` looks like a GrassFence billboard."""
    path = (binding.material_path or "")
    name = os.path.basename(path.replace("\\", "/")).lower()
    if "grassfence" in name:
        return True
    inline = getattr(binding, "inline_material", None)
    if inline is not None:
        inline_name = (
            getattr(inline, "name", "")
            or getattr(inline, "material_name", "")
            or ""
        )
        if "grassfence" in str(inline_name).lower():
            return True
    return False


def strip_grass_fins(mesh: Mesh) -> bool:
    """Remove GrassFence fin submeshes from every LOD of ``mesh`` in place.

    Unused vertices (and their pose-block entries) are dropped so the fins
    leave no loose points. Returns True when anything was removed.
    """
    fins = {i for i, mb in enumerate(mesh.materials) if _is_fence_material(mb)}
    if not fins:
        return False
    removed = False
    for lod in mesh.lods:
        kept = [s for s in lod.submeshes if s.material_index not in fins]
        if len(kept) == len(lod.submeshes):
            continue
        removed = True
        used = sorted({i for s in kept for i in s.indices})
        remap = {old: new for new, old in enumerate(used)}
        lod.vertices = [lod.vertices[i] for i in used]
        lod.pose_blocks = [[pose[i] for i in used] for pose in lod.pose_blocks]
        for sub in kept:
            sub.indices = [remap[i] for i in sub.indices]
        lod.submeshes = kept
    return removed


def _expand_fence_uvs_in_lod(
    lod: MeshLOD,
    materials: List[MaterialBinding],
) -> None:
    """Widen GrassFence quad UVs in-place for every fence submesh in *lod*."""
    if not lod.vertices or not lod.submeshes:
        return
    fence_mats = {
        mi for mi, mb in enumerate(materials) if _is_fence_material(mb)
    }
    if not fence_mats:
        return

    verts = lod.vertices
    n_verts = len(verts)
    # Collect every distinct vertex index touched by a fence submesh so
    # we don't double-rewrite a vertex shared with a non-fence submesh
    # (rare, but cheap to guard).
    fence_vert_mask: List[bool] = [False] * n_verts

    import math

    for sub in lod.submeshes:
        if sub.material_index not in fence_mats:
            continue
        indices = sub.indices
        # Quads are encoded as two triangles ⇒ 6 indices each.
        for qi in range(0, len(indices) - 5, 6):
            tri0 = indices[qi:qi + 3]
            tri1 = indices[qi + 3:qi + 6]
            quad: List[int] = []
            for idx in tri0 + tri1:
                if idx not in quad:
                    quad.append(idx)
            if len(quad) != 4:
                continue
            if any(i < 0 or i >= n_verts for i in quad):
                continue

            bottom = [i for i in quad if verts[i].uv0.y < 0.5]
            top = [i for i in quad if verts[i].uv0.y >= 0.5]
            if len(bottom) != 2 or len(top) != 2:
                continue

            b0, b1 = bottom
            p0 = verts[b0].position
            p1 = verts[b1].position
            # Positions are already in Blender axes (Z-up).  The billboard
            # bottom edge therefore lies in the X/Y plane; pick whichever
            # axis has the larger span to determine left/right ordering.
            dx = p1.x - p0.x
            dy = p1.y - p0.y
            if abs(dx) >= abs(dy):
                left_is_b0 = dx > 0
            else:
                left_is_b0 = dy > 0
            left_bottom, right_bottom = (b0, b1) if left_is_b0 else (b1, b0)

            t0, t1 = top
            plb = verts[left_bottom].position
            pt0 = verts[t0].position
            pt1 = verts[t1].position
            d_t0_lb = (
                (pt0.x - plb.x) ** 2
                + (pt0.y - plb.y) ** 2
                + (pt0.z - plb.z) ** 2
            )
            d_t1_lb = (
                (pt1.x - plb.x) ** 2
                + (pt1.y - plb.y) ** 2
                + (pt1.z - plb.z) ** 2
            )
            left_top, right_top = (t0, t1) if d_t0_lb <= d_t1_lb else (t1, t0)

            face_width = math.sqrt(dx * dx + dy * dy)
            half_u = (face_width / _FENCE_METERS_PER_UV) / 2.0
            cx = verts[quad[0]].uv0.x

            for vi in (left_bottom, left_top):
                if fence_vert_mask[vi]:
                    continue
                v = verts[vi]
                v.uv0 = Vec2(cx - half_u, v.uv0.y)
                fence_vert_mask[vi] = True
            for vi in (right_bottom, right_top):
                if fence_vert_mask[vi]:
                    continue
                v = verts[vi]
                v.uv0 = Vec2(cx + half_u, v.uv0.y)
                fence_vert_mask[vi] = True


def _light_transform_values(iso: Any) -> List[float]:
    fields = ("XX", "XY", "XZ", "YX", "YY", "YZ", "ZX", "ZY", "ZZ", "TX", "TY", "TZ")
    return [float(getattr(iso, name, 1.0 if i in (0, 4, 8) else 0.0) or 0.0)
            for i, name in enumerate(fields)]


def _user_light_transform(skel: Any, socket_index: int, source_path: str) -> List[float] | None:
    sockets = getattr(skel, "sockets", None) or []
    if not 0 <= socket_index < len(sockets):
        log.warning("Invalid light socket index %s in %s", socket_index, source_path)
        return None
    socket = sockets[socket_index]
    loc = getattr(socket, "loc", None)
    if loc is None:
        log.warning("Missing light socket transform %s in %s", socket_index, source_path)
        return None

    def matrix(iso: Any) -> Matrix4:
        xx, xy, xz, yx, yy, yz, zx, zy, zz, tx, ty, tz = _light_transform_values(iso)
        # Skeleton Iso4 entries are serialized basis vectors, not matrix rows.
        return ((xx, yx, zx, tx), (xy, yy, zy, ty), (xz, yz, zz, tz), (0, 0, 0, 1))

    transform = matrix(loc)
    joint_index = socket.linkedJoint
    if joint_index != -1:
        joints = getattr(skel, "joints", None) or []
        if not 0 <= joint_index < len(joints):
            log.warning("Invalid light socket joint index %s in %s", joint_index, source_path)
            return None
        parent = getattr(joints[joint_index], "GlobalLoc", None)
        if parent is None:
            log.warning("Missing light socket joint transform %s in %s", joint_index, source_path)
            return None
        transform = multiply_matrices(matrix(parent), transform)
    return [transform[r][c] for r in range(3) for c in range(3)] + [
        transform[r][3] for r in range(3)
    ]


@register(CLASS_ID)
def load_mesh(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> Mesh:
    """Build a :class:`Mesh` from a parsed CPlugSolid2Model container."""
    body = _body_dict(parsed)
    main = body.get(_CHUNK_MAIN)

    materials: List[MaterialBinding] = []
    visuals: List[_ScratchVisual] = []
    shaded: List[Tuple[int, int, int]] = []  # (visual_index, material_index, lod)
    lod_distances: List[float] = []

    if main is not None:
        mat_list = list(getattr(main, "materials", None) or [])
        if not mat_list:
            for cm in getattr(main, "customMaterials", None) or []:
                mui = getattr(cm, "materialUserInst", None)
                if mui is not None:
                    mat_list.append(mui)
        materials = [_parse_material_binding(m) for m in mat_list]

        for vis in getattr(main, "visuals", None) or []:
            visuals.append(_parse_visual(vis))

        for sg in getattr(main, "shadedGeoms", None) or []:
            shaded.append((
                int(getattr(sg, "visualIndex", 0) or 0),
                int(getattr(sg, "materialIndex", 0) or 0),
                int(getattr(sg, "lod", 1) or 1),
            ))

        lod_distances = [float(d) for d in getattr(main, "lodDistances", None) or []]

        skel_node = getattr(main, "skel", None)
        if skel_node is not None:
            _apply_skeleton(visuals, _parse_skeleton(skel_node))

    # ``lod`` is a bitmask of LOD levels (1, 2, 4, ...): geometry with
    # mask 7 is shared by levels 1, 2 and 4, not a separate level 7.
    # A negative mask (-1, e.g. the Stadium256 Flag cloth) means every level.
    all_levels = (1 << max(1, len(lod_distances))) - 1
    by_lod: Dict[int, List[Tuple[int, int]]] = {}
    for vi, mi, mask in shaded:
        if mask < 0:
            mask = all_levels
        bit = 1
        while bit <= mask:
            if mask & bit:
                by_lod.setdefault(bit, []).append((vi, mi))
            bit <<= 1

    lods: List[MeshLOD] = []
    for level in sorted(by_lod.keys()):
        index = level.bit_length() - 1
        distance = lod_distances[index] if index < len(lod_distances) else 0.0
        lod_obj = _build_lod(level, distance, by_lod[level], visuals)
        _expand_fence_uvs_in_lod(lod_obj, materials)
        lods.append(lod_obj)

    icon = b""
    icon_chunk = body.get(_CHUNK_ICON)
    if icon_chunk is not None:
        icon = bytes(getattr(icon_chunk, "data", b"") or b"")

    # Lights — each entry is a noderef whose ``name`` typically ends in
    # ``.Light.Gbx`` plus an Iso4 transform (3×3 rotation + translation).
    # We store the raw transform; the addon converts it to a Blender
    # matrix when placing the light.
    lights: List[LightAttachment] = []
    if main is not None:
        light_cache: Dict[str, Any] = {}
        user_models = getattr(main, "lightUserModels", None) or []
        for raw_light in getattr(main, "lights", None) or []:
            la = LightAttachment()
            la.name = str(getattr(raw_light, "name", "") or "")
            if la.name.lower().endswith(".light.gbx"):
                la.light_path = la.name
            embedded = getattr(raw_light, "u03", None)
            apply_light_source(la, embedded, source_path=source_path, resolver=resolver, cache=light_cache)
            tm = getattr(raw_light, "u05", None)
            if tm is not None:
                la.transform = _light_transform_values(tm)
            lights.append(la)
        skel = _body_dict(getattr(main, "skel", None)).get(_CHUNK_SKEL)
        for inst in getattr(main, "lightInsts", None) or []:
            if not 0 <= inst.modelIndex < len(user_models):
                log.warning("Invalid light model index %s in %s", inst.modelIndex, source_path)
                continue
            model = _body_dict(user_models[inst.modelIndex]).get(0x090F9000)
            la = light_from_user_model(model, source_path=source_path)
            if la is None:
                continue
            transform = _user_light_transform(skel, inst.socketIndex, source_path)
            if transform is None:
                continue
            la.transform = transform
            lights.append(la)
        for index, la in enumerate(lights):
            if la.light_type:
                # Several embedded meshes can share one prefab source path.
                identity = ((la.metadata.get("source_path"), la.light_path) if la.light_path
                            else (source_path, index, la.transform))
                key = repr((identity, la.light_type, la.color, la.intensity, la.distance,
                            la.spot_inner_angle, la.spot_outer_angle, la.night_only,
                            la.emitting_radius, la.use_shadow, la.metadata))
                light_key = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
                la.name = f"Light_{light_key}_{_file_stem(la.light_path or source_path)}"

    mesh = Mesh(
        source_path=source_path or None,
        class_id=CLASS_ID,
        name=_file_stem(source_path),
        raw=parsed,
        lods=lods,
        materials=materials,
        skeleton=[],   # baked into vertex positions above
        lights=lights,
        icon=icon,
    )

    log.debug(
        "Loaded mesh %s lods=%s materials=%d total_verts=%d total_tris=%d pose_blocks=%s",
        mesh.name,
        [lod.lod for lod in lods],
        len(materials),
        sum(len(lod.vertices) for lod in lods),
        sum(sum(len(s.indices) for s in lod.submeshes) // 3 for lod in lods),
        [len(lod.pose_blocks) for lod in lods],
    )
    return mesh


__all__ = ["load_mesh", "strip_grass_fins", "CLASS_ID"]
