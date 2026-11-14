"""Loader for ``CPlugPrefab`` (class id ``0x09145000``).

A prefab's body is a flat container (not chunk-keyed) carrying a list of
sub-entities. Each entity places a child model (an embedded
``CPlugStaticObjectModel``, a nested ``CPlugPrefab``, or a parametric
entity with no model) at a quaternion-based transform.

The loader walks ``body.Ents`` and produces a list of
:class:`~GBXBPY.model.prefab.PrefabEntity`:

* Static-object entities (``0x09159000``): the inner ``Mesh`` node is fed
  through the mesh loader and stored as :attr:`PrefabEntity.mesh`.
* Nested prefab entities (``0x09145000``): recursed through this same
  loader and stored as :attr:`PrefabEntity.sub_prefab`.
* External references (any node with a ``gamedata_path``): leave the
  path on :attr:`PrefabEntity.model_path` and let the addon resolve it.
* Parametric entities (no model): recorded with only their placement.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, List, Optional

from ..coords import gbx_to_blender_quat, gbx_to_blender_vec3
from ..errors import ParseError
from ..model import (
    DynaAnimDriver,
    EntityLink,
    IsoLoc,
    Mesh,
    PlacementGroup,
    Prefab,
    PrefabEntity,
    Quat,
    Vec3,
)
from ..registry import register
from .mesh import load_mesh as _load_mesh
from .kinematic import _parse_sub_funcs

log = logging.getLogger("gbxbpy.load.prefab")

CLASS_ID = 0x09145000
_CLASSID_STATIC_OBJECT = 0x09159000
_CLASSID_PREFAB = 0x09145000
_CLASSID_VEGET_TREE = 0x2F086000
# Custom-animated item entity flavours
_CLASSID_DYNA_OBJECT = 0x09144000     # CPlugDynaObjectModel (mesh that animates)
_CLASSID_DYNA_POINT = 0x2F0CA000      # NPlugDyna_DynaPoint (procedural anim driver)
# params chunk-ids attached to entities
_PARAMS_KINEMATIC = 0x2F0B6000        # NPlugItemPlacement_S kinematic data
_PARAMS_LINK = 0x2F0C8000             # NPlugDyna_LinkPoint (Ent1/Ent2/Pos1/Pos2)
_PARAMS_PLACEMENT_GROUP = 0x2F0D8000  # NPlugItemPlacement_SPlacementGroup
_PARAMS_PLACEMENT = 0x2F0A9000        # NPlugItemPlacement_SPlacement


def _parse_placement_options(chunk: Any) -> List[dict]:
    return [
        {str(t.key): str(t.value) for t in (getattr(option, "RequiredTags", None) or [])}
        for option in (getattr(chunk, "Options", None) or [])
    ]


def _parse_placement_group(chunk: Any) -> PlacementGroup:
    def loc(raw: Any) -> IsoLoc:
        p, r = raw.pos, raw.rot
        return IsoLoc(
            position=gbx_to_blender_vec3(float(p.x), float(p.y), float(p.z)),
            rotation=gbx_to_blender_quat(float(r.x), float(r.y), float(r.z), float(r.w)),
        )

    return PlacementGroup(
        placements=[
            [
                {str(t.key): str(t.value) for t in (getattr(option, "RequiredTags", None) or [])}
                for option in (getattr(placement, "Options", None) or [])
            ]
            for placement in (getattr(chunk, "Placements", None) or [])
        ],
        slot_placements=[int(i) for i in (getattr(chunk, "u01", None) or [])],
        slots=[loc(raw) for raw in (getattr(chunk, "u02", None) or [])],
    )


def _body_container(parsed: Any) -> Any:
    """Return the flat body container, unwrapping chunk dicts if needed."""
    body = getattr(parsed, "body", None)
    if body is None:
        return parsed
    if isinstance(body, dict):
        # Prefab bodies are flat but NICE may still wrap them in an
        # OrderedDict with a single placeholder key; pick the first child
        # that actually carries the Ents array.
        for v in body.values():
            if v is None:
                continue
            if hasattr(v, "Ents"):
                return v
        return body
    return body


def _node_path(node: Any) -> str:
    if node is None:
        return ""
    for attr in ("gamedata_path", "filePath", "filepath"):
        value = getattr(node, attr, "")
        if value:
            return str(value)
    return ""


def _find_mesh_node(static_obj: Any) -> Optional[Any]:
    """Drill into a CPlugStaticObjectModel body and return its Mesh node."""
    body = getattr(static_obj, "body", None)
    candidate = getattr(static_obj, "Mesh", None)
    if candidate is None and body is not None:
        candidate = getattr(body, "Mesh", None)
    if candidate is None and isinstance(body, dict):
        for v in body.values():
            if v is None:
                continue
            inner = getattr(v, "Mesh", None)
            if inner is not None:
                candidate = inner
                break
    return candidate


def _find_shape_node(static_obj: Any) -> Optional[Any]:
    body = getattr(static_obj, "body", None)
    return body.get("Shape") if hasattr(body, "get") else getattr(body, "Shape", None)


def _attach_terrain_slots(mesh: Any, static_obj: Any, source_path: str) -> None:
    """Fill :attr:`Mesh.terrain_slot_votes` for vista terrain meshes."""
    from .terrain_slots import terrain_slot_votes

    try:
        mesh.terrain_slot_votes = terrain_slot_votes(mesh, _find_shape_node(static_obj))
    except Exception as e:  # noqa: BLE001 - slot votes are best effort
        log.warning("terrain slot vote failed for %s: %s", source_path, e)


@register(_CLASSID_STATIC_OBJECT)
def load_static_object(parsed: Any, *, source_path: str = "", resolver=None) -> Mesh:
    """Expose a standalone static-object wrapper through the shared mesh loader."""
    node = _find_mesh_node(parsed)
    if node is None or not getattr(node, "body", None):
        raise ParseError(f"Static object has no decoded mesh; load recursively: {source_path}")
    mesh = _load_mesh(node, source_path=source_path, resolver=resolver)
    _attach_terrain_slots(mesh, parsed, source_path)
    from .surface import object_mesh
    return object_mesh(mesh, parsed, source_path=source_path)


def _vec3_from_container(c: Any) -> Vec3:
    if c is None:
        return Vec3()
    return Vec3(
        float(getattr(c, "x", 0.0) or 0.0),
        float(getattr(c, "y", 0.0) or 0.0),
        float(getattr(c, "z", 0.0) or 0.0),
    )


def _enum_str(value: Any, default: str = "") -> str:
    """Stringify an EnumIntegerString, falling back to ``default``."""
    if value is None:
        return default
    name = getattr(value, "intvalue", None)  # not present; just defensive
    s = str(value) if value is not None else default
    # EnumIntegerString.__str__ returns the symbolic name
    return s or default


def _parse_anim_subfunc(func: Any) -> tuple[str, bool, int]:
    """Return ``(easing_name, reverse, duration_ms)`` for the first SubFunc.

    DynaPoint anim funcs hold a list of piecewise-linear curve segments;
    for procedural single-segment items there is exactly one entry.
    Returns ``("Constant", False, 0)`` when nothing is defined, which the
    Blender side treats as "channel inactive".
    """
    if func is None:
        return ("Constant", False, 0)
    subs = getattr(func, "SubFuncs", None) or []
    if not subs:
        return ("Constant", False, 0)
    first = subs[0]
    return (
        _enum_str(getattr(first, "ease", None), "Constant"),
        bool(getattr(first, "reverse", False)),
        int(getattr(first, "duration", 0) or 0),
    )


def _parse_anim_driver(body: Any) -> DynaAnimDriver:
    """Build a :class:`DynaAnimDriver` from an NPlugDyna_DynaPoint body."""
    drv = DynaAnimDriver()
    if body is None:
        return drv

    trans_func = getattr(body, "TransAnimFunc", None)
    rot_func = getattr(body, "RotAnimFunc", None)

    drv.time_is_duration = bool(
        getattr(trans_func, "TimeIsDuration", True) if trans_func is not None else True
    )
    drv.rot_time_is_duration = bool(
        getattr(rot_func, "TimeIsDuration", True) if rot_func is not None else True
    )

    t_ease, t_rev, t_dur = _parse_anim_subfunc(trans_func)
    drv.trans_easing = t_ease
    drv.trans_reverse = t_rev
    drv.trans_duration_ms = t_dur
    drv.trans_sub_funcs = _parse_sub_funcs(trans_func)

    r_ease, r_rev, r_dur = _parse_anim_subfunc(rot_func)
    drv.rot_easing = r_ease
    drv.rot_reverse = r_rev
    drv.rot_duration_ms = r_dur
    drv.rot_sub_funcs = _parse_sub_funcs(rot_func)

    drv.trans_axis = _enum_str(getattr(body, "TransAxis", None), "")
    drv.trans_min = float(getattr(body, "TransMin", 0.0) or 0.0)
    drv.trans_max = float(getattr(body, "TransMax", 0.0) or 0.0)

    drv.rot_axis = _enum_str(getattr(body, "RotAxis", None), "")
    drv.rot_angle_min_deg = float(getattr(body, "AngleMinDeg", 0.0) or 0.0)
    drv.rot_angle_max_deg = float(getattr(body, "AngleMaxDeg", 0.0) or 0.0)

    return drv


def _apply_params(entity: PrefabEntity, raw: Any) -> None:
    """Per-entity params chunk; model-less placement slots carry only this."""
    params = getattr(raw, "params", None)
    if params is not None:
        chunk = getattr(params, "chunk", None)
        chunk_id = int(getattr(params, "chunkId", 0) or 0)
        entity.params_chunk_id = chunk_id
        if chunk is not None:
            if chunk_id == _PARAMS_LINK:
                e1 = getattr(chunk, "Ent1", None)
                e2 = getattr(chunk, "Ent2", None)
                entity.link = EntityLink(
                    ent1=int(e1) if e1 is not None else -1,
                    ent2=int(e2) if e2 is not None else -1,
                    pos1=_vec3_from_container(getattr(chunk, "Pos1", None)),
                    pos2=_vec3_from_container(getattr(chunk, "Pos2", None)),
                )
            elif chunk_id == _PARAMS_KINEMATIC:
                if bool(getattr(chunk, "IsKinematic", False)):
                    entity.is_kinematic = True
            elif chunk_id == _PARAMS_PLACEMENT_GROUP:
                entity.placement_group = _parse_placement_group(chunk)
            elif chunk_id == _PARAMS_PLACEMENT:
                entity.placement_layout = int(getattr(chunk, "iLayout", -1))
                entity.placement_options = _parse_placement_options(chunk)


def _parse_entity(
    raw: Any,
    *,
    source_path: str,
    resolver: Any,
    cache: dict,
) -> PrefabEntity:
    entity = PrefabEntity()

    # --- Transform ---------------------------------------------------------
    pos = getattr(raw, "pos", None)
    if pos is not None:
        entity_pos = gbx_to_blender_vec3(
            float(getattr(pos, "x", 0.0) or 0.0),
            float(getattr(pos, "y", 0.0) or 0.0),
            float(getattr(pos, "z", 0.0) or 0.0),
        )
    else:
        entity_pos = Vec3()

    rot = getattr(raw, "rot", None)
    if rot is not None:
        entity_rot = gbx_to_blender_quat(
            float(getattr(rot, "x", 0.0) or 0.0),
            float(getattr(rot, "y", 0.0) or 0.0),
            float(getattr(rot, "z", 0.0) or 0.0),
            float(getattr(rot, "w", 1.0) or 1.0),
        )
    else:
        entity_rot = Quat.identity()

    entity.transform = IsoLoc(position=entity_pos, rotation=entity_rot)

    # --- Model reference ---------------------------------------------------
    model = getattr(raw, "model", None)
    if model is None:
        _apply_params(entity, raw)
        return entity

    entity.model_class_id = int(getattr(model, "classId", 0) or 0)
    entity.model_path = _node_path(model)

    if entity.model_class_id == _CLASSID_STATIC_OBJECT:
        mesh_node = _find_mesh_node(model)
        if mesh_node is not None:
            mesh_key = (id(model), id(mesh_node))
            cached_mesh = cache.get(mesh_key)
            if cached_mesh is not None:
                entity.mesh = cached_mesh
            else:
                try:
                    entity.mesh = _load_mesh(
                        mesh_node,
                        source_path=source_path,
                        resolver=resolver,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "Failed to parse embedded mesh for prefab entity in %s: %s",
                        source_path, e,
                    )
                if entity.mesh is not None:
                    _attach_terrain_slots(entity.mesh, model, source_path)
                    from .surface import object_mesh
                    entity.mesh = object_mesh(entity.mesh, model, source_path=source_path)
                    cache[mesh_key] = entity.mesh
    elif entity.model_class_id == _CLASSID_PREFAB:
        # If the nested prefab is just a NodeRef with a gamedata_path
        # (no inline body / Ents), don't recurse \u2014 that produces an
        # empty Prefab dataclass that the addon would render as an
        # invisible "ghost" container. Leave it as an external reference
        # via ``entity.model_path`` and let the addon resolve it.
        nested_body = getattr(model, "body", None)
        has_inline_ents = False
        if nested_body is not None:
            if hasattr(nested_body, "Ents"):
                has_inline_ents = True
            elif isinstance(nested_body, dict):
                for _v in nested_body.values():
                    if _v is not None and hasattr(_v, "Ents"):
                        has_inline_ents = True
                        break

        if has_inline_ents:
            sub_key = id(model)
            cached_sub = cache.get(sub_key)
            if cached_sub is not None:
                entity.sub_prefab = cached_sub
            else:
                try:
                    entity.sub_prefab = _load_prefab_internal(
                        model,
                        source_path=source_path,
                        resolver=resolver,
                        cache=cache,
                        nested=True,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "Failed to parse nested prefab in %s: %s",
                        source_path, e,
                    )
                if entity.sub_prefab is not None:
                    cache[sub_key] = entity.sub_prefab
        # else: external reference \u2014 ``entity.model_path`` already set above.
    elif entity.model_class_id == _CLASSID_DYNA_OBJECT:
        # CPlugDynaObjectModel — mesh that animates at runtime. Body is a
        # flat container holding Mesh / DynaShape / StaticShape / LocAnim
        # plus kinematic flags. We use the visual Mesh and forward the
        # flags onto the entity.
        entity.is_dynamic = True
        body = getattr(model, "body", None)
        if body is not None:
            entity.is_kinematic = bool(getattr(body, "IsKinematic", False))
        mesh_node = _find_mesh_node(model)
        if mesh_node is not None:
            mesh_key = (id(model), id(mesh_node))
            cached_mesh = cache.get(mesh_key)
            if cached_mesh is not None:
                entity.mesh = cached_mesh
            else:
                try:
                    entity.mesh = _load_mesh(
                        mesh_node,
                        source_path=source_path,
                        resolver=resolver,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "Failed to parse dyna-object mesh for prefab entity in %s: %s",
                        source_path, e,
                    )
                if entity.mesh is not None:
                    from .surface import object_mesh
                    entity.mesh = object_mesh(entity.mesh, model, source_path=source_path, dynamic=True)
                    cache[mesh_key] = entity.mesh
    elif entity.model_class_id == _CLASSID_DYNA_POINT:
        # NPlugDyna_DynaPoint — procedural animation driver. No geometry;
        # exposes per-axis translation and rotation parameters.
        entity.anim_driver = _parse_anim_driver(getattr(model, "body", None))
    # 0x2F086000 (VegetTreeModel) and other externally-referenced types are
    # left as model_path-only; the addon resolves them through the resolver.

    _apply_params(entity, raw)
    params = getattr(raw, "params", None)
    chunk = getattr(params, "chunk", None)
    texture_id = getattr(chunk, "TextureId", None)
    if entity.mesh is not None and texture_id is not None and int(texture_id) > 0:
        key = ("texture_variant", id(entity.mesh), int(texture_id))
        variant = cache.get(key)
        if variant is None:
            variant = replace(entity.mesh, materials=[
                replace(binding, texture_array_index=int(texture_id))
                for binding in entity.mesh.materials
            ])
            cache[key] = variant
        entity.mesh = variant
    return entity


@register(CLASS_ID)
def load_prefab(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> Prefab:
    """Build a :class:`Prefab` from a parsed CPlugPrefab container."""
    return _load_prefab_internal(
        parsed,
        source_path=source_path,
        resolver=resolver,
        cache={},
        nested=False,
    )


def _load_prefab_internal(
    parsed: Any,
    *,
    source_path: str,
    resolver: Any,
    cache: dict,
    nested: bool,
) -> Prefab:
    body = _body_container(parsed)

    entities: List[PrefabEntity] = []
    for raw_ent in getattr(body, "Ents", None) or []:
        entities.append(_parse_entity(
            raw_ent,
            source_path=source_path,
            resolver=resolver,
            cache=cache,
        ))

    name = ""
    if source_path:
        import os
        base = os.path.basename(source_path)
        name = base.split(".", 1)[0] if base else ""

    prefab = Prefab(
        source_path=source_path or None,
        class_id=CLASS_ID,
        name=name,
        raw=parsed,
        entities=entities,
        prefab_version=int(getattr(body, "version", 0) or 0),
        url=str(getattr(body, "url", "") or ""),
    )

    summary = (
        "prefab '%s' entities=%d "
        "(%d embedded-mesh, %d nested, %d external, %d parametric, "
        "%d dynamic, %d anim-driver, %d linked)"
    ) % (
        name,
        len(entities),
        sum(1 for e in entities if e.mesh is not None and not e.is_dynamic),
        sum(1 for e in entities if e.sub_prefab is not None),
        sum(1 for e in entities
            if e.mesh is None and e.sub_prefab is None and e.model_path
            and e.anim_driver is None),
        sum(1 for e in entities
            if e.mesh is None and e.sub_prefab is None
            and not e.model_path and e.anim_driver is None),
        sum(1 for e in entities if e.is_dynamic),
        sum(1 for e in entities if e.anim_driver is not None),
        sum(1 for e in entities if e.link is not None),
    )
    if source_path:
        summary = f"{summary} <- {source_path}"
    is_trivial_wrapper = (
        len(entities) == 1
        and entities[0].mesh is not None
        and not entities[0].is_dynamic
        and entities[0].sub_prefab is None
        and entities[0].anim_driver is None
        and entities[0].link is None
    )
    if nested:
        # Skip noisy "1 entity / 1 embedded-mesh" wrappers — they're
        # emitted dozens of times per map and add no information.
        if not is_trivial_wrapper:
            log.debug("Loaded nested %s", summary)
    else:
        log.info("Loaded %s", summary)
    return prefab


__all__ = ["load_prefab", "CLASS_ID"]
