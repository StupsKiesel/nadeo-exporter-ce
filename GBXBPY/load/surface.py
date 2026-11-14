"""CPlugSurface geometry; collision and trigger roles are not render materials."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from ..coords import gbx_iso_to_blender_matrix, gbx_to_blender_vec3, multiply_matrices
from ..errors import ParseError
from ..model import MaterialBinding, Mesh, MeshLOD, Submesh, Vertex
from ..model.geometry import IDENTITY, MeshPart
from ._gbx_helpers import body_dict, surface_id
from .mesh import _parse_material_binding

log = logging.getLogger("gbxbpy.load.surface")


def _surface_binding(chunk: Any, index: int, direct: Any) -> MaterialBinding:
    entries = list(getattr(chunk, "materials", None) or [])
    if 0 <= index < len(entries):
        entry = entries[index]
        if getattr(entry, "hasMaterial", False):
            return _parse_material_binding(entry.material)
        direct = getattr(entry, "materialId", direct)
    else:
        ids = (getattr(chunk, "surfaceIds", None)
               or getattr(chunk, "surfaceIds2", None) or [])
        if 0 <= index < len(ids):
            direct = ids[index]
        else:
            physics = getattr(chunk, "physicIds", None) or []
            if 0 <= index < len(physics):
                return MaterialBinding(surface_physic_id=surface_id(physics[index]))
    return MaterialBinding(
        surface_physic_id=surface_id(getattr(direct, "physicsId", None)),
        surface_gameplay_id=surface_id(getattr(direct, "gameplayId", None), gameplay=True),
    )


def surface_parts(node: Any, *, name: str, role: str = "collision",
                  source_path: str = "") -> list[MeshPart]:
    if node is None or int(getattr(node, "_index", 0)) == -1:
        return []
    chunk = body_dict(node).get(0x0900C003)
    if chunk is None:
        path = str(getattr(node, "filepath", "") or getattr(node, "_relativeFilePath", "") or "")
        log.warning("Undecoded %s surface %s in %s", role, path or name, source_path)
        return [MeshPart(name=name, role=role, visible=False, collidable=role != "trigger")]
    result = []

    def visit(surf, label, matrix=IDENTITY, bone_index=-1):
        kind = str(surf.type)
        data = surf.data
        if kind == "Compound":
            if len(data.surfaces) != len(data.locs):
                raise ParseError(f"Compound surface transform count mismatch: {source_path}")
            bones = list(getattr(data, "boneIndexes", None) or [])
            for i, (child, loc) in enumerate(zip(data.surfaces, data.locs)):
                visit(child, f"{label}_{i}", multiply_matrices(matrix, gbx_iso_to_blender_matrix(loc)),
                      int(bones[i]) if i < len(bones) else -1)
            return
        part = MeshPart(name=label, role=role, visible=False, collidable=role != "trigger",
                        matrix=matrix, surface_type=kind, bone_index=bone_index)
        result.append(part)
        faces = []
        if kind == "Mesh":
            positions = data.vertices
            for tri in data.triangles:
                faces.append(([int(tri.face.x), int(tri.face.y), int(tri.face.z)],
                              _surface_binding(chunk, int(tri.materialIndex), tri.materialId)))
        elif kind == "ConvexPolyhedron":
            positions = data.vertices
            binding = _surface_binding(chunk, int(getattr(data, "u03", -1)), None)
            for start, count in data.faces:
                indices = list(data.facesIndicies[start:start + count])
                if start < 0 or count < 3 or len(indices) != count:
                    raise ParseError(f"Invalid convex surface face: {source_path}")
                faces.append((indices, binding))
        else:
            # The original analytic/unknown surface remains in the source Asset.raw.
            log.warning("Unsupported %s surface %s (%s); preserved without mesh", role, label, kind)
            return
        vertices = [Vertex(position=gbx_to_blender_vec3(v.x, v.y, v.z)) for v in positions]
        materials, slots, submeshes = [], {}, []
        for indices, binding in faces:
            if not all(0 <= index < len(vertices) for index in indices):
                raise ParseError(f"Surface face index out of range: {source_path}")
            key = (binding.material_path, binding.surface_physic_id, binding.surface_gameplay_id)
            slot = slots.get(key)
            if slot is None:
                slot = len(materials)
                slots[key] = slot
                materials.append(binding)
                submeshes.append(Submesh(material_index=slot))
            for i in range(1, len(indices) - 1):
                submeshes[slot].indices.extend((indices[0], indices[i], indices[i + 1]))
        if not vertices or not submeshes:
            log.warning("Empty %s surface %s in %s", role, label, source_path)
            return
        if role == "trigger":
            materials.clear()
            for submesh in submeshes:
                submesh.material_index = 0
        part.mesh = Mesh(name=label, source_path=source_path, raw=node,
                         lods=[MeshLOD(vertices=vertices, submeshes=submeshes)],
                         materials=materials, collidable=role != "trigger")

    visit(chunk.surf, name)
    return result


def object_mesh(mesh: Mesh, node: Any, *, source_path: str, dynamic: bool = False) -> Mesh:
    """Keep the rendered mesh and attach the object's separate physical meshes."""
    body = getattr(node, "body", None)
    if body is None:
        return mesh
    collidable = bool(getattr(body, "isMeshCollidable", True)) and not dynamic
    parts = []
    fields = ("DynaShape", "StaticShape") if dynamic else ("Shape",)
    for field in fields:
        shape = getattr(body, field, None)
        if shape is not None:
            parts.extend(surface_parts(shape, name=f"{mesh.name}_{field}", source_path=source_path))
    return replace(mesh, collidable=collidable, collision_parts=parts, raw=node)
