"""Restricted, preserving export of baked editor-crystal items.

Runtime meshes and unknown modifier types are intentionally rejected. This
writer preserves the original item and replaces only its editable geometry,
lightmap coordinates and face smoothing groups.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path

from construct import Container

from ..load._crystal import build_mesh_from_crystal
from ..model.mesh import Mesh


class UnsupportedItemError(ValueError):
    """The preserving editor-crystal writer cannot round-trip this item."""


@dataclass
class BakedLayer:
    index: int
    mesh: Mesh
    face_groups: list[int] = field(default_factory=list)
    material_indices: list[int] = field(default_factory=list)
    visible: bool = True
    collidable: bool = True
    deformation_baked: bool = False
    face_smoothing: list[int] = field(default_factory=list)


@dataclass
class LayerGeometry:
    positions: list[tuple[float, float, float]]
    faces: list[list[int]]
    texture_uvs: list[list[tuple[float, float]]]
    lightmap_uvs: list[list[tuple[float, float]]]
    materials: list[int]
    groups: list[int]
    smoothing_groups: list[int]


def crystal(parsed):
    entity = parsed.body.get(0x2E002019)
    if entity is None or entity.get("EntityModel") is not None:
        raise UnsupportedItemError("Only editor-crystal items without a runtime EntityModel are supported")
    edition = entity.get("EntityModelEdition")
    body = getattr(edition, "body", {})
    model = body.get(0x2E026000)
    result = getattr(model, "meshCrystal", None)
    if result is None or result.classId != 0x09003000:
        raise UnsupportedItemError("Item has no supported embedded editor crystal")
    return result


def read_item(path):
    from NICE.src.parser import parse_bytes

    raw = Path(path).read_bytes()
    parsed = parse_bytes(raw, filepath=str(path), recursive=False)
    if parsed.classId != 0x2E002000 or parsed._errors or parsed._warns:
        raise ValueError(f"Item must parse without diagnostics: {parsed._errors}, {parsed._warns}")
    node = crystal(parsed)
    supported_chunks = {0x09051000, 0x09003003, 0x09003004, 0x09003005,
                        0x09003006, 0x09003007, 0xFACADE01}
    if any(isinstance(key, int) and key not in supported_chunks for key in node.body):
        raise UnsupportedItemError("Crystal has unsupported chunks; export refused to avoid stale geometry caches")
    layers = node.body[0x09003005].layers
    for layer in layers:
        if str(layer.type) not in {"Geometry", "Deformation"}:
            raise UnsupportedItemError(f"Unsupported crystal layer: {layer.type}; export refused")
        if layer.get("u02"):
            raise UnsupportedItemError("Cached crystal layers are not supported")
        if layer.type == "Geometry":
            geometry = layer.content.crystal
            if not geometry.isEmbeddedCrystal or geometry.version != 37:
                raise UnsupportedItemError("Only version 37 embedded geometry is supported")
            if geometry.u14 or geometry.embeddedCrystal.u22:
                raise UnsupportedItemError("Anchored geometry or additional geometry data is not supported")
    return parsed, hashlib.sha256(raw).hexdigest()


def bake_layers(parsed, source):
    node = crystal(parsed)
    result = []
    deformation_baked = any(
        layer.type == "Deformation" and layer.is_enabled
        for layer in node.body[0x09003005].layers
    )
    smoothing = node.body.get(0x09003007)
    if smoothing is None or not smoothing.u01 or not smoothing.u02:
        raise ValueError("Item has no original face smoothing data to preserve")
    if any(not 0 <= index < len(smoothing.u01) for index in smoothing.u02):
        raise ValueError("Original face smoothing data has invalid group indices")
    uniform = len(set(smoothing.u02)) == 1
    source_face_count = sum(
        len(layer.content.crystal.embeddedCrystal.faces)
        for layer in node.body[0x09003005].layers
        if layer.type == "Geometry" and layer.is_enabled and layer.content.isVisible
    )
    if not uniform and (deformation_baked or len(smoothing.u02) != source_face_count):
        raise ValueError(
            "Non-uniform original smoothing cannot be mapped safely through baked deformation "
            "or mismatched face counts"
        )
    source_offset = 0
    for index, layer in enumerate(node.body[0x09003005].layers):
        if layer.type != "Geometry" or not layer.is_enabled:
            continue
        if layer.content.crystal.version != 37:
            raise ValueError("Only version 37 editable geometry is supported")
        isolated = deepcopy(node)
        isolated.body[0x09003005].layers = [
            other for i, other in enumerate(isolated.body[0x09003005].layers)
            if other.type != "Geometry" or i == index
        ]
        groups, material_indices, sources = [], [], []
        mesh = build_mesh_from_crystal(
            isolated, source_path=str(source), name=layer.name, visible_only=False,
            face_groups=groups, material_indices=material_indices,
            face_sources=sources,
        )
        if mesh is None:
            raise ValueError(f"Layer {layer.name} has no supported geometry")
        face_smoothing = [0] * len(sources)
        if layer.content.isVisible:
            face_smoothing = (
                [smoothing.u02[0]] * len(sources) if uniform else
                [smoothing.u02[source_offset + face] for face in sources]
            )
            source_offset += len(layer.content.crystal.embeddedCrystal.faces)
        result.append(BakedLayer(
            index, mesh, groups, material_indices,
            visible=bool(layer.content.isVisible),
            collidable=bool(layer.content.isCollidable),
            deformation_baked=deformation_baked,
            face_smoothing=face_smoothing,
        ))
    if not result:
        raise ValueError("Item has no enabled geometry layers")
    return result


def _validate_geometry(geometry, layer, material_count, smoothing_count):
    count = len(geometry.faces)
    if not count or not geometry.positions:
        raise ValueError("Cannot export empty geometry")
    if any(len(values) != count for values in (
        geometry.texture_uvs, geometry.lightmap_uvs, geometry.materials, geometry.groups,
        geometry.smoothing_groups,
    )):
        raise ValueError("Face attribute counts do not match")
    if any(not all(math.isfinite(v) for v in point) for point in geometry.positions):
        raise ValueError("Geometry contains non-finite coordinates")
    for i, face in enumerate(geometry.faces):
        if len(face) != 3 or len(set(face)) != 3:
            raise ValueError("Export requires non-degenerate triangles")
        if any(v < 0 or v >= len(geometry.positions) for v in face):
            raise ValueError("Invalid vertex index")
        a, b, c = [geometry.positions[v] for v in face]
        ab, ac = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
        if all(ab[i] * ac[(i + 1) % 3] == ab[(i + 1) % 3] * ac[i] for i in range(3)):
            raise ValueError("Export requires non-degenerate triangles")
        if not 0 <= geometry.materials[i] < material_count:
            raise ValueError("Material assignments were changed outside the original slot table")
        if not 0 <= geometry.groups[i] < len(layer.content.crystal.groups):
            raise ValueError("Invalid crystal group")
        if not 0 <= geometry.smoothing_groups[i] < smoothing_count:
            raise ValueError("Invalid original smoothing group")
        for uvs in (geometry.texture_uvs[i], geometry.lightmap_uvs[i]):
            if len(uvs) != 3 or any(not all(math.isfinite(v) for v in uv) for uv in uvs):
                raise ValueError("Invalid UV coordinates")
        if any(not 0 <= v <= 1 for uv in geometry.lightmap_uvs[i] for v in uv):
            raise ValueError("Lightmap UVs must lie inside 0..1")


def _cross(a, b, point):
    return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])


def _intersection_area(first, second):
    polygon = list(first)
    orientation = 1 if _cross(*second) >= 0 else -1
    for a, b in zip(second, second[1:] + second[:1]):
        clipped = []
        for start, end in zip(polygon, polygon[1:] + polygon[:1]):
            ds, de = orientation * _cross(a, b, start), orientation * _cross(a, b, end)
            if ds >= 0:
                clipped.append(start)
            if (ds >= 0) != (de >= 0):
                factor = ds / (ds - de)
                clipped.append(tuple(start[i] + factor * (end[i] - start[i]) for i in range(2)))
        polygon = clipped
        if not polygon:
            return 0.0
    return abs(sum(a[0] * b[1] - a[1] * b[0]
                   for a, b in zip(polygon, polygon[1:] + polygon[:1]))) * 0.5


class LightmapOverlapError(ValueError):
    def __init__(self, first, second):
        self.triangles = (first, second)
        super().__init__(f"Lightmap triangles {first} and {second} overlap after quantization")


def validate_lightmap(triangles):
    """Reject collapsed/overlapping UVs after the game's 16-bit quantization."""
    triangles = [[tuple(round(v * 65535) / 65535 for v in uv) for uv in triangle]
                 for triangle in triangles]
    cells = {}
    size = max(1, int(math.sqrt(len(triangles))))
    for index, triangle in enumerate(triangles):
        if abs(_cross(*triangle)) < 1e-12:
            raise ValueError("Lightmap triangle collapses at 16-bit precision")
        low = [min(uv[axis] for uv in triangle) for axis in (0, 1)]
        high = [max(uv[axis] for uv in triangle) for axis in (0, 1)]
        keys = [(x, y) for x in range(int(low[0] * size), int(high[0] * size) + 1)
                for y in range(int(low[1] * size), int(high[1] * size) + 1)]
        candidates = set(candidate for key in keys for candidate in cells.get(key, ()))
        for other in candidates:
            if _intersection_area(triangle, triangles[other]) > 1e-12:
                raise LightmapOverlapError(other, index)
        for key in keys:
            cells.setdefault(key, []).append(index)


def write_item(parsed, geometries):
    from NICE.src.parser import generate_file, parse_bytes

    output = deepcopy(parsed)
    node = crystal(output)
    layers = node.body[0x09003005].layers
    expected = {i for i, layer in enumerate(layers) if layer.type == "Geometry" and layer.is_enabled}
    if set(geometries) != expected:
        raise ValueError("Every enabled original geometry layer must be present exactly once")
    lightmap_coords = []
    face_smoothing = []
    original_smoothing = node.body[0x09003007]
    visible_triangles = []
    for index in sorted(geometries):
        geometry = geometries[index]
        layer = layers[index]
        _validate_geometry(geometry, layer, len(node.body[0x09003003].materials),
                           len(original_smoothing.u01))
        embedded = layer.content.crystal.embeddedCrystal
        embedded.vertices = [Container(x=x, y=z, z=-y) for x, y, z in geometry.positions]
        edges = {tuple(sorted((face[i], face[(i + 1) % 3])))
                 for face in geometry.faces for i in range(3)}
        embedded.edgesCount = len(edges)
        embedded.unfacedEdgesCount = 0
        embedded.unfacedEdges = []
        embedded.facesCount = len(geometry.faces)
        embedded.faces = [
            Container(vertCount=3, inds=face, uv=None, material_index=material, group_index=group)
            for face, material, group in zip(geometry.faces, geometry.materials, geometry.groups)
        ]
        layer.content.crystal.u30 = max(layer.content.crystal.u30, max(geometry.materials))
        layer.content.crystal.u31 = max(layer.content.crystal.u31, max(geometry.groups))
        embedded.uvsCoords = [Container(x=u, y=v) for face in geometry.texture_uvs for u, v in face]
        embedded.faceCornersCount = len(embedded.uvsCoords)
        embedded.uvsIndicies = list(range(embedded.faceCornersCount))
        if layer.content.isVisible:
            visible_triangles.extend(geometry.lightmap_uvs)
            lightmap_coords.extend(uv for face in geometry.lightmap_uvs for uv in face)
            face_smoothing.extend(geometry.smoothing_groups)
    validate_lightmap(visible_triangles)
    node.body[0x09003005].layers = [layer for layer in layers if layer.type != "Deformation"]
    # Chunk 006 stores unsigned normalized 16-bit UVs. NICE exposes their
    # bit patterns as signed shorts; retain those bits when serializing.
    node.body[0x09003006] = Container(
        version=2, u01=None,
        u02=[[(round(v * 65535) + 32768) % 65536 - 32768 for v in uv] for uv in lightmap_coords],
        u03Count=len(lightmap_coords), u03=list(range(len(lightmap_coords))),
    )
    original_smoothing.u02 = face_smoothing
    raw = generate_file(output, remove_external=False)
    reopened = parse_bytes(raw, recursive=False)
    if reopened._errors or reopened._warns:
        raise ValueError(f"Export failed reparse: {reopened._errors}, {reopened._warns}")
    check = crystal(reopened)
    if sum(len(layer.content.crystal.embeddedCrystal.faces)
           for layer in check.body[0x09003005].layers if layer.type == "Geometry" and layer.is_enabled
           ) != sum(len(g.faces) for g in geometries.values()):
        raise ValueError("Exported geometry face count differs after reparse")
    return raw
