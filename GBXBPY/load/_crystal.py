"""CPlugCrystal → :class:`Mesh` extractor.

Custom (user-made) items embed their geometry as a ``CPlugCrystal``
(class ``0x09003000``) hanging off ``EntityModelEdition.meshCrystal``.
The crystal stores n-gon faces, a shared UV pool indexed per face-corner,
and a per-layer "Geometry" container with collision/visibility flags.

The low-level extractor builds single-LOD meshes. Item import uses
``build_parts_from_crystal`` to retain independent layers and their semantics.

Notes:

* Faces may have variable ``vertCount`` (3, 4, ...). They are triangulated
  with a fan (``v0, vi, vi+1``).
* Positions go through :func:`gbx_to_blender_vec3`. The Y/Z swap is a
  proper rotation (det ``+1``) so face winding is preserved.
* Normals are computed per face (flat shading) since the crystal does not
  store them. The addon will smooth on import if requested elsewhere.
* UVs are addressed by walking ``uvsIndicies`` in face-traversal order,
  one entry per face corner.
* Materials may be either a string ``materialName`` (Nadeo material lookup)
  or a ``materialUserInst`` :class:`CPlugMaterialUserInst` link. Both are
  surfaced as :class:`MaterialBinding.material_path`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..coords import gbx_to_blender_vec3
from ..model.geometry import CrystalLayer, MeshPart
from ..model import (
    MaterialBinding,
    Mesh,
    MeshLOD,
    Submesh,
    Vec2,
    Vec3,
    Vertex,
)
from ._deformation import (
    deform_vertices,
    find_deformation_layer_for_index,
    subdivide_axial_quads,
    tile_along_axis,
    world_to_source_local,
)
from .mesh import _parse_material_binding, target_color_from_user_inst

log = logging.getLogger("gbxbpy.load.crystal")

CRYSTAL_CLASS_ID = 0x09003000

_CHUNK_MATERIALS = 0x09003003
_CHUNK_LAYERS = 0x09003005

# CPlugMaterialUserInst sub-chunks (mirror material.py)
_MUI_BASE_PATH = 0x09079012
_MUI_BASE_LINK = 0x09079015
# Custom-item editor flavour: CPlugFileImg-style chunk holding the link directly.
_MUI_INFO = 0x090FD000


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _body_dict(node: Any) -> dict:
    body = getattr(node, "body", None)
    if isinstance(body, dict):
        return body
    try:
        return dict(body) if body is not None else {}
    except (TypeError, ValueError):
        return {}


def _strip_data_prefix(value: str) -> str:
    """``:data:\\Projects\\Foo.Material.Gbx`` → ``Foo.Material.Gbx``."""
    s = str(value or "").replace("\\", "/")
    marker = ":data:/"
    idx = s.lower().find(marker)
    if idx >= 0:
        s = s[idx + len(marker):]
    # Drop ``Projects/`` prefix often left by the editor.
    if s.lower().startswith("projects/"):
        s = s[len("projects/"):]
    return s.strip()


def _node_path(node: Any) -> str:
    """Best-effort GameData-relative path off a NodeRef-like object."""
    if node is None:
        return ""
    for attr in ("gamedata_path", "filePath", "filepath"):
        v = getattr(node, attr, "")
        if v:
            return str(v).replace("\\", "/")
    rel = getattr(node, "_relativeFilePath", "") or ""
    if rel:
        parts = rel.replace("\\", "/").split("/")
        while parts and parts[0] in ("..", ""):
            parts.pop(0)
        return "/".join(parts)
    return ""


def _material_user_inst_path(mui: Any) -> str:
    """Pull a base-material path out of a ``CPlugMaterialUserInst`` node."""
    if mui is None:
        return ""
    body = _body_dict(mui)
    # Custom-item editor format: 0x090FD000.link is the bare material path
    # (e.g. "Stadium\\Media\\Material\\PlatformDirt.Material.Gbx").
    chunk = body.get(_MUI_INFO)
    if chunk is not None:
        link = getattr(chunk, "link", None)
        if link:
            return _strip_data_prefix(link)
        name = getattr(chunk, "materialName", None)
        if name:
            return _strip_data_prefix(name)
    chunk = body.get(_MUI_BASE_PATH)
    if chunk is not None:
        raw = (
            getattr(chunk, "baseMaterialPath", None)
            or getattr(chunk, "u01", None)
            or ""
        )
        if raw:
            return _strip_data_prefix(raw)
    chunk = body.get(_MUI_BASE_LINK)
    if chunk is not None:
        bm = getattr(chunk, "baseMaterial", None)
        if bm is not None:
            path = _node_path(bm)
            if path:
                return path
            # baseMaterial may also be a parsed CPlugMaterial node — recurse
            # into its body to find an inner base-path chunk.
            inner = _body_dict(bm).get(_MUI_BASE_PATH)
            if inner is not None:
                raw = (
                    getattr(inner, "baseMaterialPath", None)
                    or getattr(inner, "u01", None)
                    or ""
                )
                if raw:
                    return _strip_data_prefix(raw)
    return ""


def _build_binding(mat_entry: Any) -> MaterialBinding:
    name = str(getattr(mat_entry, "materialName", "") or "")
    if name:
        # Nadeo-named materials use the bare name (e.g. "PlatformDirt"); the
        # addon resolver turns these into Stadium/Media/Material/<name>.Material.Gbx.
        return MaterialBinding(material_path=name)

    mui = getattr(mat_entry, "materialUserInst", None)
    path = _material_user_inst_path(mui)
    binding = _parse_material_binding(mui) if mui is not None else MaterialBinding()
    binding.material_path = path or binding.material_path
    if mui is not None:
        chunk = _body_dict(mui).get(_MUI_INFO)
        if chunk is not None:
            binding.target_color = target_color_from_user_inst(chunk)
    return binding


def _modifier_layer_targets(modifier_layer: Any) -> Optional[List[str]]:
    """Return the list of source-layer ids a modifier layer targets, or
    ``None`` if it applies to *every* layer below it (empty/missing mask).

    The mask is an array of ``(GroupIndex, LayerId)`` records; we treat it
    as a set of LayerIds. ``GroupIndex`` (per-group fine-grained selection)
    is currently ignored — modifier layers in the wild use empty masks or
    whole-layer masks.
    """
    content = getattr(modifier_layer, "content", None)
    if content is None:
        return None
    mask = getattr(content, "mask", None)
    if not mask:
        return None
    ids: List[str] = []
    for entry in mask:
        lid = getattr(entry, "LayerId", None)
        if lid:
            ids.append(str(lid))
    return ids if ids else None


def _modifier_targets_geometry(modifier_layer: Any, geom_layer_id: str) -> bool:
    """``True`` if this modifier layer should be applied to the Geometry
    layer with id ``geom_layer_id``.

    Empty mask => applies to all geometry layers below. Otherwise only the
    explicitly listed ``LayerId`` entries match.
    """
    targets = _modifier_layer_targets(modifier_layer)
    if targets is None:
        return True
    return geom_layer_id in targets


def _connected_components(
    positions: List[tuple],
    faces_simple: List[dict],
) -> List[List[int]]:
    """Group vertex indices into connected components linked by shared faces.

    Two vertices are in the same component if they appear in the same face
    (or are connected transitively through other faces). Used by the Mirror
    layer's "mirror each part independently" mode where every disjoint part
    is reflected about its own local center plane.
    """
    n = len(positions)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for face_d in faces_simple:
        inds = face_d["inds"]
        if len(inds) < 2:
            continue
        first = inds[0]
        for i in inds[1:]:
            union(first, i)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _apply_mirror_layer(
    raw_positions: List[tuple],
    faces_simple: List[dict],
    mirror_inner: Any,
) -> tuple:
    """Duplicate ``(positions, faces)`` with a Mirror modifier applied to
    the duplicate, and return the merged ``(positions, faces)``.

    In-game UI labels map to schema fields as follows:
      * "perpendicular to axis"               -> ``axis`` (X / Y / Z)
      * "distance from middle mirror plane"   -> ``distance`` (meters)
      * "mirror each part independently"      -> ``independently`` (bool)

    Plane placement:
      The mirror plane is anchored to the **inner edge** of the mesh
      (its bbox extreme along ``axis`` closest to the world origin) —
      NOT to the world axis itself. ``distance`` is an extra offset
      pushing the plane *away* from the source bulk (positive values
      open a gap between source and mirror; negative values let the
      mirror overlap the source). ``distance == 0`` therefore means
      "plane touches the mesh", producing a seamless symmetric pair.

      With ``independently == True``, the bbox is computed per
      connected component instead of globally — each "part" gets its
      own anchored plane.

    Face winding is reversed on every mirrored copy so outward normals
    stay consistent with the source.
    """
    axis_enum = getattr(mirror_inner, "axis", None)
    axis_str = str(axis_enum) if axis_enum is not None else "X"
    axis_idx = {"X": 0, "Y": 1, "Z": 2}.get(axis_str, 0)
    distance = float(getattr(mirror_inner, "distance", 0.0) or 0.0)
    independently = bool(getattr(mirror_inner, "independently", False))

    base_count = len(raw_positions)
    if base_count == 0:
        return raw_positions, faces_simple

    def _plane_for(indices: List[int]) -> float:
        """Inner-edge plane along ``axis`` for a vertex group, plus
        ``distance`` pushing the plane outward (away from the bulk).
        """
        vals = [raw_positions[i][axis_idx] for i in indices]
        mn, mx = min(vals), max(vals)
        # Inner edge = the bbox face closer to world origin. Mirror
        # extends in the opposite direction (away from origin).
        if abs(mn) <= abs(mx):
            # Bulk is on the +axis side; plane sits at mn, mirror goes -axis.
            return mn - distance
        # Bulk is on the -axis side; plane sits at mx, mirror goes +axis.
        return mx + distance

    # Decide a per-vertex mirror plane offset (one shared, or one per part).
    if independently:
        components = _connected_components(raw_positions, faces_simple)
        plane_offset = [0.0] * base_count
        for comp in components:
            if not comp:
                continue
            part_plane = _plane_for(comp)
            for i in comp:
                plane_offset[i] = part_plane
    else:
        shared_plane = _plane_for(list(range(base_count)))
        plane_offset = [shared_plane] * base_count

    # Mirrored vertex copies.
    mirrored_positions: List[tuple] = []
    for i, p in enumerate(raw_positions):
        coords = list(p)
        coords[axis_idx] = 2.0 * plane_offset[i] - coords[axis_idx]
        mirrored_positions.append(tuple(coords))

    new_positions = list(raw_positions) + mirrored_positions

    # Mirrored face copies — indices shift by base_count, winding reversed.
    new_faces = list(faces_simple)
    for face_d in faces_simple:
        old_inds = face_d["inds"]
        old_uvs = face_d["uvs"]
        rev_inds = [i + base_count for i in reversed(old_inds)]
        rev_uvs = list(reversed(old_uvs))
        new_faces.append({
            **face_d,
            "inds": rev_inds,
            "mat": face_d["mat"],
            "group": face_d["group"],
            "uvs": rev_uvs,
        })
    return new_positions, new_faces


def _apply_translation_layer(
    raw_positions: List[tuple],
    faces_simple: List[dict],
    trans_inner: Any,
) -> tuple:
    """Duplicate the geometry with a translation offset applied to the copy.

    The Translation modifier in the editor adds a translated *copy* of the
    source layer (the original is preserved).
    """
    t = getattr(trans_inner, "translation", None)
    tx = float(getattr(t, "x", 0.0) or 0.0) if t is not None else 0.0
    ty = float(getattr(t, "y", 0.0) or 0.0) if t is not None else 0.0
    tz = float(getattr(t, "z", 0.0) or 0.0) if t is not None else 0.0

    if tx == 0.0 and ty == 0.0 and tz == 0.0:
        return raw_positions, faces_simple

    base_count = len(raw_positions)
    translated = [(p[0] + tx, p[1] + ty, p[2] + tz) for p in raw_positions]
    new_positions = list(raw_positions) + translated

    new_faces = list(faces_simple)
    for face_d in faces_simple:
        new_faces.append({
            **face_d,
            "inds": [i + base_count for i in face_d["inds"]],
            "mat": face_d["mat"],
            "group": face_d["group"],
            "uvs": list(face_d["uvs"]),
        })
    return new_positions, new_faces


def _apply_scale_layer(
    raw_positions: List[tuple],
    faces_simple: List[dict],
    scale_inner: Any,
) -> tuple:
    """Scale the geometry per axis about the item origin.

    With ``independently`` each connected part is scaled about its own
    bounding-box centre instead. A mirroring (negative) scale reverses the
    face winding so normals keep pointing outward.
    """
    s = getattr(scale_inner, "scale", None)
    factors = tuple(
        float(getattr(s, axis, 1.0)) if s is not None else 1.0 for axis in "xyz"
    )
    if factors == (1.0, 1.0, 1.0) or not raw_positions:
        return raw_positions, faces_simple

    centres = [(0.0, 0.0, 0.0)] * len(raw_positions)
    if bool(getattr(scale_inner, "independently", False)):
        for comp in _connected_components(raw_positions, faces_simple):
            centre = tuple(
                (min(raw_positions[i][a] for i in comp) + max(raw_positions[i][a] for i in comp)) / 2.0
                for a in range(3)
            )
            for i in comp:
                centres[i] = centre

    new_positions = [
        tuple(c[a] + (p[a] - c[a]) * factors[a] for a in range(3))
        for p, c in zip(raw_positions, centres)
    ]
    if factors[0] * factors[1] * factors[2] < 0:
        faces_simple = [
            {**f, "inds": list(reversed(f["inds"])), "uvs": list(reversed(f["uvs"]))}
            for f in faces_simple
        ]
    return new_positions, faces_simple


def _apply_modifier_layers(
    raw_positions: List[tuple],
    faces_simple: List[dict],
    layers_chunk: Any,
    geom_layer_index: int,
    geom_layer_id: str,
) -> tuple:
    """Walk modifier layers above ``geom_layer_index`` in the layer stack
    and apply Mirror/Translation/Scale operations to the geometry buffer.

    Stops at the first Deformation layer (handled by the deformation
    pipeline downstream). Rotation/Smooth/Subdivide/Chaos are not
    yet implemented and are logged + skipped so callers can spot which
    items still need work.
    """
    layers = list(getattr(layers_chunk, "layers", None) or [])
    n = len(layers)

    for idx in range(geom_layer_index + 1, n):
        layer = layers[idx]
        ltype = str(getattr(layer, "type", "") or "")
        if ltype == "Deformation":
            break  # Deformation runs after all other modifiers.
        if ltype == "Geometry":
            continue  # Other geometry layers — independent meshes.
        if not getattr(layer, "is_enabled", True):
            continue
        if not _modifier_targets_geometry(layer, geom_layer_id):
            continue

        inner = getattr(getattr(layer, "content", None), "content", None)
        if inner is None:
            continue

        if ltype == "Mirror":
            raw_positions, faces_simple = _apply_mirror_layer(
                raw_positions, faces_simple, inner,
            )
            log.debug(
                "Crystal: applied Mirror layer %r to geometry %r "
                "(axis=%s, distance=%g, independently=%s)",
                getattr(layer, "name", ""), geom_layer_id,
                getattr(inner, "axis", "?"),
                float(getattr(inner, "distance", 0.0) or 0.0),
                bool(getattr(inner, "independently", False)),
            )
        elif ltype == "Translation":
            raw_positions, faces_simple = _apply_translation_layer(
                raw_positions, faces_simple, inner,
            )
            log.debug(
                "Crystal: applied Translation layer %r to geometry %r",
                getattr(layer, "name", ""), geom_layer_id,
            )
        elif ltype == "Scale":
            raw_positions, faces_simple = _apply_scale_layer(
                raw_positions, faces_simple, inner,
            )
            log.debug(
                "Crystal: applied Scale layer %r to geometry %r",
                getattr(layer, "name", ""), geom_layer_id,
            )
        else:
            log.debug(
                "Crystal: skipping unsupported modifier layer type %r (name=%r)",
                ltype, getattr(layer, "name", ""),
            )

    return raw_positions, faces_simple


def _face_normal(p0: Vec3, p1: Vec3, p2: Vec3) -> Vec3:
    ax, ay, az = p1.x - p0.x, p1.y - p0.y, p1.z - p0.z
    bx, by, bz = p2.x - p0.x, p2.y - p0.y, p2.z - p0.z
    nx = ay * bz - az * by
    ny = az * bx - ax * bz
    nz = ax * by - ay * bx
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length <= 1e-12:
        return Vec3(0.0, 0.0, 1.0)
    return Vec3(nx / length, ny / length, nz / length)


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

def build_mesh_from_crystal(
    crystal_node: Any,
    *,
    name: str = "",
    source_path: str = "",
    visible_only: bool = True,
    face_groups: Optional[List[int]] = None,
    material_indices: Optional[List[int]] = None,
    face_sources: Optional[List[int]] = None,
    layer_index: Optional[int] = None,
    include_disabled: bool = False,
) -> Optional[Mesh]:
    """Build a single-LOD :class:`Mesh` from a ``CPlugCrystal`` node.

    Returns ``None`` if no geometry layer could be extracted.
    """
    if crystal_node is None:
        return None

    body = _body_dict(crystal_node)
    materials_chunk = body.get(_CHUNK_MATERIALS)
    layers_chunk = body.get(_CHUNK_LAYERS)
    if layers_chunk is None:
        log.debug("Crystal has no layers chunk — skipping")
        return None

    raw_materials = list(getattr(materials_chunk, "materials", None) or [])

    out_vertices: List[Vertex] = []
    out_materials: List[MaterialBinding] = []
    submeshes_by_slot: Dict[int, List[int]] = {}
    crystal_mat_to_slot: Dict[int, int] = {}
    groups_by_slot: Dict[int, List[int]] = {}
    sources_by_slot: Dict[int, List[int]] = {}

    def _slot_for_crystal_mat(idx: int) -> int:
        if idx in crystal_mat_to_slot:
            return crystal_mat_to_slot[idx]
        if 0 <= idx < len(raw_materials):
            binding = _build_binding(raw_materials[idx])
        else:
            binding = MaterialBinding()
        slot = len(out_materials)
        out_materials.append(binding)
        crystal_mat_to_slot[idx] = slot
        if material_indices is not None:
            material_indices.append(idx)
        return slot

    for current_index, layer in enumerate(getattr(layers_chunk, "layers", None) or []):
        if layer_index is not None and current_index != layer_index:
            continue
        if getattr(layer, "type", "") not in ("Geometry", "Trigger"):
            continue
        if getattr(layer, "type", "") == "Trigger" and layer_index is None:
            continue
        if not include_disabled and getattr(layer, "is_enabled", True) is False:
            continue
        content = getattr(layer, "content", None)
        if content is None:
            continue
        if visible_only and not bool(getattr(content, "isVisible", True)):
            continue

        crystal_wrap = getattr(content, "crystal", None)
        if crystal_wrap is None or not getattr(crystal_wrap, "isEmbeddedCrystal", False):
            continue
        ec = getattr(crystal_wrap, "embeddedCrystal", None)
        if ec is None:
            continue

        verts = list(getattr(ec, "vertices", None) or [])
        faces = list(getattr(ec, "faces", None) or [])
        uvs_pool = list(getattr(ec, "uvsCoords", None) or [])
        uvs_idx = list(getattr(ec, "uvsIndicies", None) or [])

        if not verts or not faces:
            continue

        # Pre-extract raw GBX positions, then optionally apply a Deformation
        # layer found elsewhere in this same crystal. Deformation operates in
        # raw GBX coords; we swizzle to Blender axes only afterwards.
        raw_positions: List[tuple] = [
            (
                float(getattr(v, "x", 0.0)),
                float(getattr(v, "y", 0.0)),
                float(getattr(v, "z", 0.0)),
            )
            for v in verts
        ]

        # Normalize crystal faces into a simple per-face representation so
        # subdivide_axial_quads (which needs to add new verts and split
        # quads) can operate uniformly.
        faces_simple: List[dict] = []
        corner_cursor = 0
        for source_index, face in enumerate(faces):
            inds = list(getattr(face, "inds", None) or [])
            fn = len(inds)
            if fn < 3:
                corner_cursor += fn
                continue
            uvs_face: List[tuple] = []
            # Two UV layouts depending on crystal version:
            #   * version >= 37: shared UV pool on the embedded crystal
            #     (``uvsCoords``) indexed by face-corner via ``uvsIndicies``.
            #   * version <  37: each face carries its own ``uv`` array of
            #     length ``vertCount`` (no shared pool).
            face_uv_inline = getattr(face, "uv", None)
            if face_uv_inline is not None:
                for i in range(fn):
                    uv_pt = face_uv_inline[i] if i < len(face_uv_inline) else None
                    if uv_pt is None:
                        uvs_face.append((0.0, 0.0))
                    else:
                        uvs_face.append((
                            float(getattr(uv_pt, "x", 0.0)),
                            float(getattr(uv_pt, "y", 0.0)),
                        ))
            else:
                for i in range(fn):
                    uv = (0.0, 0.0)
                    if uvs_idx:
                        cursor = corner_cursor + i
                        if 0 <= cursor < len(uvs_idx):
                            uv_pt = uvs_pool[uvs_idx[cursor]]
                            uv = (
                                float(getattr(uv_pt, "x", 0.0)),
                                float(getattr(uv_pt, "y", 0.0)),
                            )
                    uvs_face.append(uv)
            faces_simple.append({
                "source": source_index,
                "inds": inds,
                "mat": int(getattr(face, "material_index", 0) or 0),
                "group": int(getattr(face, "group_index", 0) or 0),
                "uvs": uvs_face,
            })
            corner_cursor += fn

        # Apply modifier layers (Mirror, Translation, ...) sitting above
        # this Geometry layer in the stack — they duplicate / reflect /
        # translate the source geometry to produce the final shape that
        # then gets deformed by any Deformation layer.
        geom_layer_id = str(getattr(layer, "id", "") or "")
        raw_positions, faces_simple = _apply_modifier_layers(
            raw_positions, faces_simple, layers_chunk, current_index, geom_layer_id,
        )

        deform_layer = find_deformation_layer_for_index(layers_chunk, current_index)
        if deform_layer is not None:
            mask = getattr(deform_layer, "mask", None)
            selected_groups = {
                int(entry.GroupIndex) for entry in mask or []
                if str(entry.LayerId) == geom_layer_id
            }
            mask_bits = int(getattr(deform_layer, "mask_bits", 0) or 0)
            log.debug(
                "Crystal %r: applying Deformation layer (n_controls=%d, mask=0x%x)",
                name, int(getattr(deform_layer, "n_controls", 0)),
                mask_bits,
            )

            def _is_deformed(face_d: dict) -> bool:
                gi = face_d["group"]
                if mask is not None:
                    return not mask or gi in selected_groups
                # Compatibility with older raw containers using a skip bitmask.
                if gi < 0 or gi >= 32:
                    return True  # groups beyond the 32-bit mask: deform
                return ((mask_bits >> gi) & 1) == 0

            deform_face_idxs = [i for i, f in enumerate(faces_simple) if _is_deformed(f)]
            pass_face_idxs = [i for i, f in enumerate(faces_simple) if not _is_deformed(f)]

            if not deform_face_idxs:
                # Nothing to deform — leave raw_positions / faces_simple as-is.
                pass
            else:
                # Build an independent (positions, faces) sub-mesh containing
                # only the deformed faces, run the deformation pipeline on it,
                # then combine with the passthrough sub-mesh (kept in world
                # coords). Vertices shared between deformed and passthrough
                # faces get duplicated — one copy is deformed, the other
                # stays put — which matches the engine's behavior of only
                # moving verts owned by selected groups.
                def _extract_submesh(face_idxs):
                    remap: dict = {}
                    new_positions: List[tuple] = []
                    new_faces: List[dict] = []
                    for fi in face_idxs:
                        face_d = faces_simple[fi]
                        new_inds: List[int] = []
                        for old_i in face_d["inds"]:
                            ni = remap.get(old_i)
                            if ni is None:
                                ni = len(new_positions)
                                remap[old_i] = ni
                                new_positions.append(raw_positions[old_i])
                            new_inds.append(ni)
                        new_faces.append({
                            **face_d,
                            "inds": new_inds,
                            "mat": face_d["mat"],
                            "group": face_d["group"],
                            "uvs": face_d["uvs"],
                        })
                    return new_positions, new_faces

                pass_positions, pass_faces = _extract_submesh(pass_face_idxs)
                deform_positions, deform_faces = _extract_submesh(deform_face_idxs)

                # The engine's deformation axis is source-local +Z, but the
                # author can pick any world axis via start_iso's rotation
                # (e.g. half-banked-curve variants deform along world X/Y).
                # Bring verts into the source-local frame so downstream code
                # can keep its "Z = axial" assumption; after deform_vertices
                # the curve frames are world-space so outputs are world-space
                # too — directly mergeable with pass_positions.
                deform_positions = world_to_source_local(deform_positions, deform_layer)
                # Tile (replicate end-to-end) the source mesh along its
                # local +Z axis BEFORE deforming. The engine bakes
                # n_tiles = round(chord_length / source_z_extent) copies and
                # welds the boundary rings between adjacent copies, so
                # authored cross-section rings (and their UV islands) ride
                # the bend rather than being smeared across the curve.
                src_z_vals = [v[2] for v in deform_positions]
                src_z_extent = (max(src_z_vals) - min(src_z_vals)) if src_z_vals else 0.0
                deform_positions, deform_faces = tile_along_axis(
                    deform_positions, deform_faces, deform_layer,
                )
                deform_positions = deform_vertices(
                    deform_positions, deform_layer, src_z_extent=src_z_extent
                )

                # Merge passthrough + deformed back into a single vert/face
                # pool. Deformed vert indices get shifted by len(pass).
                offset = len(pass_positions)
                merged_positions = list(pass_positions)
                merged_positions.extend(deform_positions)
                for face_d in deform_faces:
                    face_d["inds"] = [i + offset for i in face_d["inds"]]
                merged_faces = list(pass_faces)
                merged_faces.extend(deform_faces)

                raw_positions = merged_positions
                faces_simple = merged_faces

        gbx_positions: List[Vec3] = [
            gbx_to_blender_vec3(p[0], p[1], p[2]) for p in raw_positions
        ]

        for face_d in faces_simple:
            inds = face_d["inds"]
            n = len(inds)
            if n < 3:
                continue

            corner_pos: List[Vec3] = [gbx_positions[i] for i in inds]
            corner_uvs: List[Vec2] = [Vec2(u, v) for (u, v) in face_d["uvs"]]

            normal = _face_normal(corner_pos[0], corner_pos[1], corner_pos[2])

            base = len(out_vertices)
            for i in range(n):
                out_vertices.append(Vertex(
                    position=corner_pos[i],
                    normal=normal,
                    uv0=corner_uvs[i],
                ))

            slot = _slot_for_crystal_mat(face_d["mat"])
            tri_list = submeshes_by_slot.setdefault(slot, [])
            for i in range(1, n - 1):
                tri_list.extend([base, base + i, base + i + 1])
                if face_groups is not None:
                    groups_by_slot.setdefault(slot, []).append(face_d["group"])
                if face_sources is not None:
                    sources_by_slot.setdefault(slot, []).append(face_d["source"])

    if not out_vertices:
        log.debug("Crystal yielded no vertices for %s", name or source_path)
        return None

    submeshes = [
        Submesh(material_index=slot, indices=indices)
        for slot, indices in submeshes_by_slot.items()
        if indices
    ]

    lod = MeshLOD(
        lod=1,
        distance=0.0,
        vertices=out_vertices,
        submeshes=submeshes,
    )
    if face_groups is not None:
        for submesh in submeshes:
            face_groups.extend(groups_by_slot[submesh.material_index])
    if face_sources is not None:
        for submesh in submeshes:
            face_sources.extend(sources_by_slot[submesh.material_index])

    log.debug(
        "Built crystal mesh %r: verts=%d submeshes=%d materials=%d",
        name, len(out_vertices), len(submeshes), len(out_materials),
    )

    return Mesh(
        source_path=source_path or None,
        name=name,
        lods=[lod],
        materials=out_materials,
    )


def crystal_layers(node: Any) -> List[CrystalLayer]:
    chunk = _body_dict(node).get(_CHUNK_LAYERS)
    return [
        CrystalLayer(i, str(getattr(layer, "id", "") or ""),
                     str(getattr(layer, "name", "") or ""),
                     str(getattr(layer, "type", "") or ""),
                     getattr(layer, "is_enabled", True) is not False)
        for i, layer in enumerate(getattr(chunk, "layers", None) or [])
    ]


def build_parts_from_crystal(node: Any, *, name: str = "", source_path: str = "") -> List[MeshPart]:
    """Extract layers without removing stack entries referenced by modifiers."""
    chunk = _body_dict(node).get(_CHUNK_LAYERS)
    layers = list(getattr(chunk, "layers", None) or [])
    parts = []
    for info, layer in zip(crystal_layers(node), layers):
        if info.type not in ("Geometry", "Trigger"):
            continue
        content = layer.content
        trigger = info.type == "Trigger"
        groups, materials, sources = [], [], []
        mesh = build_mesh_from_crystal(
            node, name=info.name or f"{name}_{info.index}", source_path=source_path,
            visible_only=False, layer_index=info.index, include_disabled=True,
            face_groups=groups, material_indices=materials, face_sources=sources,
        )
        if mesh is None:
            wrapper = getattr(content, "crystal", None)
            geometry = getattr(wrapper, "embeddedCrystal", None)
            if geometry is not None and not (
                getattr(geometry, "vertices", None) and getattr(geometry, "faces", None)
            ):
                continue  # An intentionally empty source layer, including VOID blocks.
            log.warning("Crystal layer %s in %s has no supported geometry", info.name, source_path)
        if trigger and mesh is not None:
            mesh.materials.clear()
            materials.clear()
            for lod in mesh.lods:
                for submesh in lod.submeshes:
                    submesh.material_index = 0
        parts.append(MeshPart(
            name=info.name or f"{name}_{info.index}", mesh=mesh,
            role="trigger" if trigger else "geometry",
            visible=False if trigger else bool(getattr(content, "isVisible", True)),
            collidable=False if trigger else bool(getattr(content, "isCollidable", True)),
            enabled=info.enabled, layer_index=info.index, layer_id=info.id,
            layer_type=info.type, face_groups=groups, material_indices=materials,
            face_sources=sources,
        ))
    return parts


__all__ = ["build_mesh_from_crystal", "build_parts_from_crystal", "crystal_layers", "CRYSTAL_CLASS_ID"]
