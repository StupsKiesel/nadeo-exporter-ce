"""Decoration scene surrounds, without duplicating world sky or lighting."""

from __future__ import annotations

from typing import Any

from ..coords import gbx_to_blender_quat, gbx_to_blender_vec3
from ..errors import ParseError
from ..model.common import IsoLoc
from ..model.mesh import Mesh
from ..model.prefab import Prefab, PrefabEntity
from ..registry import dispatch, register
from ._gbx_helpers import body_dict, file_stem, node_path


@register(0x0A003000)
def load_scene(parsed: Any, *, source_path: str = "", resolver: Any = None) -> Prefab:
    """Read the authored Warp surround; sky mirrors and shadow casters are engine helpers."""
    chunk = body_dict(parsed).get(0x0A00301C)
    if chunk is None or not hasattr(chunk, "objects"):
        raise ParseError(f"Scene has no supported layout chunk: {source_path}")
    entities = []
    for obj in chunk.objects:
        if obj.id != "Warp":
            continue
        node = next((getattr(obj, key, None) for key in ("prefab", "mesh", "solid")
                     if node_path(getattr(obj, key, None))
                     or getattr(getattr(obj, key, None), "classId", None)), None)
        if node is None:
            raise ParseError(f"Scene Warp has no geometry: {source_path}")
        pos, rot = obj.loc.pos, obj.loc.rot
        entity = PrefabEntity(
            name=obj.id, model_path=node_path(node),
            transform=IsoLoc(
                position=gbx_to_blender_vec3(pos.x, pos.y, pos.z),
                rotation=gbx_to_blender_quat(rot.x, rot.y, rot.z, rot.w),
            ),
        )
        if not entity.model_path:
            payload = dispatch(node, source_path=source_path, resolver=resolver)
            if isinstance(payload, Prefab):
                entity.sub_prefab = payload
            else:
                if not isinstance(payload, Mesh):
                    raise ParseError(f"Unsupported scene geometry: {type(payload).__name__}")
                entity.mesh = payload
        entities.append(entity)
    return Prefab(
        source_path=source_path or None, class_id=0x0A003000,
        name=file_stem(source_path), raw=parsed, entities=entities,
    )
