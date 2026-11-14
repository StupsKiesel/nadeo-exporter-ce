"""Loader for ``CGameItemModel`` (class id ``0x2E002000``).

Handles both flavours:

* **Ingame items** — store a list of ``EntityModel`` variants pointing at
  external ``StaticObject`` / ``VegetTreeModel`` files. Each variant is
  surfaced as an :class:`EntityVariant` with a GameData-relative
  ``entity_model_path``.
* **Custom items / embedded** — the item carries its rendered geometry
  inline, either as a ``CPlugCrystal`` mesh editor model
  (``EntityModelEdition.meshCrystal``) or as a ``StaticObject`` containing a
  ``CPlugSolid2Model`` mesh (``EntityModel.staticObject.Mesh``). When
  present, the second is parsed via the :mod:`GBXBPY.load.mesh` loader and
  exposed as semantic :attr:`Item.parts`. Crystal editor geometry is
  reconstructed through the shared crystal/deformation extractor.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from ..coords import blender_transform_matrix, gbx_iso_to_blender_matrix, gbx_to_blender_vec3
from ..model import CustomBlock, EntityVariant, Item, ItemPlacement, Prefab, Vec3
from ..registry import register
from ..model.geometry import MeshPart
from ._crystal import build_parts_from_crystal, crystal_layers
from ..errors import ParseError
from .mesh import load_mesh as _load_mesh
from .prefab import _load_prefab_internal as _load_prefab_internal
from .prefab import _parse_anim_driver
from .gameskin import load_game_skin
from .surface import object_mesh, surface_parts

log = logging.getLogger("gbxbpy.load.item")

CLASS_ID = 0x2E002000

# Header chunks
_HDR_ICON = 0x2E001004
_HDR_ITEM_TYPE = 0x2E002000

# Body chunks (CGameCtnObjectInfo + CGameItemModel)
_CHUNK_PAGE_PATH = 0x2E001009
_CHUNK_AUTHOR = 0x2E00100B
_CHUNK_NAME = 0x2E00100C
_CHUNK_SKIN_DIR = 0x2E001010
_CHUNK_CATALOG = 0x2E001011
_CHUNK_GROUND = 0x2E002012
_CHUNK_ITEM_TYPE = 0x2E002015
_CHUNK_ENTITY_MODEL = 0x2E002019
_CHUNK_PLACEMENT = 0x2E00201C

# Inner static-object chunk inside an EntityModel body
_CHUNK_STATIC_OBJECT = 0x2E027000

# CGameBlockItem: custom-block edition model
_CLASS_BLOCK_ITEM = 0x2E025000
_CHUNK_BLOCK_ITEM = 0x2E025000


# ---------------------------------------------------------------------------
#  Helpers
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


def _node_path(node: Any) -> str:
    """Pull a GameData-relative path off a NodeRef-like object."""
    if node is None:
        return ""
    for attr in ("gamedata_path", "filePath", "filepath"):
        value = getattr(node, attr, "")
        if value:
            return str(value)
    rel = getattr(node, "_relativeFilePath", "") or ""
    if rel:
        return str(rel)
    return ""


# ---------------------------------------------------------------------------
#  Header parsing
# ---------------------------------------------------------------------------

def _extract_header(parsed: Any) -> tuple[bytes, str]:
    """Return ``(icon_bytes, item_type_string)`` from the file header."""
    header = getattr(parsed, "header", None)
    if header is None:
        return (b"", "")
    data = getattr(header, "data", None)
    if not isinstance(data, (list, tuple)):
        return (b"", "")
    icon = b""
    item_type = ""
    for entry in data:
        cid = getattr(entry, "chunkId", 0) or 0
        if cid == _HDR_ICON:
            icon = bytes(getattr(entry, "data", b"") or b"")
        elif cid == _HDR_ITEM_TYPE:
            it = getattr(entry, "itemType", "") or ""
            if it:
                item_type = str(it)
    return (icon, item_type)


# ---------------------------------------------------------------------------
#  EntityModel parsing
# ---------------------------------------------------------------------------

def _parse_entity_model(
    chunk: Any,
    *,
    source_path: str,
    resolver: Any,
) -> tuple[List[EntityVariant], List[MeshPart], Optional[Prefab]]:
    """Return ``(entity_variants, parts, embedded_prefab)``."""
    variants: List[EntityVariant] = []
    embedded: List[MeshPart] = []
    embedded_prefab: Optional[Prefab] = None

    em_ref = getattr(chunk, "EntityModel", None)
    if em_ref is not None and hasattr(em_ref, "body"):
        em_class_id = int(getattr(em_ref, "classId", 0) or 0)
        em_body = em_ref.body

        # Custom-animated items: EntityModel is a CPlugPrefab whose
        # entities mix dyna meshes and procedural anim drivers.
        if em_class_id == 0x09145000:
            try:
                embedded_prefab = _load_prefab_internal(
                    em_ref,
                    source_path=source_path,
                    resolver=resolver,
                    cache={},
                    nested=True,
                )
                _remap_prefab_animation(
                    embedded_prefab, getattr(chunk, "MaterialModifier", None),
                    source_path=source_path, resolver=resolver,
                )
            except Exception as e:  # noqa: BLE001
                raise ParseError(f"Failed to parse embedded prefab for {source_path}: {e}") from e
            return (variants, [], embedded_prefab)

        # Ingame items: variants live directly on the body container.
        raw_variants = getattr(em_body, "variants", None)
        if raw_variants:
            for v in raw_variants:
                ev = EntityVariant()
                tags_raw = getattr(v, "Tags", None) or []
                for t in tags_raw:
                    ev.tags.append((
                        str(getattr(t, "key", "") or ""),
                        str(getattr(t, "value", "") or ""),
                    ))
                ev.entity_model_path = _node_path(getattr(v, "EntityModel", None))
                ev.hidden_in_manual_cycle = bool(
                    getattr(v, "HiddenInManualCycle", False)
                )
                variants.append(ev)
            return (variants, [], None)

        # Embedded items: EntityModel → staticObject → Mesh
        if isinstance(em_body, dict):
            so_chunk = em_body.get(_CHUNK_STATIC_OBJECT)
            if so_chunk is not None:
                static_obj = getattr(so_chunk, "staticObject", None)
                if static_obj is not None:
                    mesh_node = _find_mesh_node(static_obj)
                    if mesh_node is not None:
                        try:
                            mesh = _load_mesh(
                                mesh_node,
                                source_path=source_path,
                                resolver=resolver,
                            )
                            mesh = object_mesh(mesh, static_obj, source_path=source_path)
                            embedded.append(MeshPart(name=mesh.name or "Geometry", mesh=mesh,
                                                     collidable=mesh.collidable))
                            embedded.extend(mesh.collision_parts)
                        except Exception as e:  # noqa: BLE001
                            raise ParseError(f"Failed to parse item geometry for {source_path}: {e}") from e
                props = getattr(so_chunk, "props", None)
                embedded.extend(surface_parts(
                    getattr(props, "triggerShape", None), name="Trigger", role="trigger",
                    source_path=source_path,
                ))
                return (variants, embedded, None)

    # Custom items: chunk.EntityModelEdition is a CGameCommonItemEntityModelEdition
    # node whose body holds 0x2E026000 → meshCrystal (CPlugCrystal). We feed it
    # to the crystal extractor to produce a single-LOD Mesh.
    edition_ref = getattr(chunk, "EntityModelEdition", None)
    if edition_ref is not None and hasattr(edition_ref, "body"):
        if int(getattr(edition_ref, "classId", 0) or 0) == 0x2F086000:
            path = _node_path(edition_ref)
            if path:
                variants.append(EntityVariant(entity_model_path=path))
            else:
                log.warning("Vegetation edition model has no external path: %s", source_path)
            return (variants, [], None)
        ed_body = edition_ref.body
        crystal_node = None
        if isinstance(ed_body, dict):
            for v in ed_body.values():
                if v is None:
                    continue
                cand = getattr(v, "meshCrystal", None)
                if cand is not None:
                    crystal_node = cand
                    break
        if crystal_node is None:
            crystal_node = getattr(ed_body, "meshCrystal", None)

        if crystal_node is not None:
            try:
                embedded = build_parts_from_crystal(
                    crystal_node,
                    name=_file_stem(source_path),
                    source_path=source_path,
                )
            except Exception as e:  # noqa: BLE001
                raise ParseError(f"Failed to build crystal geometry for {source_path}: {e}") from e
            return (variants, embedded, None)

    return (variants, embedded, embedded_prefab)


def _parse_custom_block(chunk: Any, *, source_path: str) -> Optional[CustomBlock]:
    """Read a ``CGameBlockItem`` edition (archetype + customized crystals)."""
    edition = getattr(chunk, "EntityModelEdition", None)
    body = getattr(edition, "body", None)
    if int(getattr(edition, "classId", 0) or 0) != _CLASS_BLOCK_ITEM or not isinstance(body, dict):
        return None
    data = body.get(_CHUNK_BLOCK_ITEM)
    if data is None:
        return None
    block = CustomBlock(
        archetype_id=str(getattr(data, "ArchetypeBlockInfoId", "") or ""),
        archetype_collection=str(getattr(data, "ArchetypeBlockInfoCollectionId", "") or ""),
    )
    stem = _file_stem(source_path)
    for entry in getattr(data, "CustomizedVariants", None) or []:
        key = int(getattr(entry, "key", 0) or 0)
        try:
            parts = build_parts_from_crystal(
                getattr(entry, "value", None), name=f"{stem}_{key}", source_path=source_path,
            )
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"Failed to build custom block variant {key} for {source_path}: {e}") from e
        block.variants[key] = parts
    return block


def _modifier_path(node: Any, resolver: Any) -> str:
    """GameData-relative path of an external ``MaterialModifier`` reference."""
    path = str(getattr(node, "filepath", "") or "") if node is not None else ""
    if path and resolver is not None and os.path.isabs(path):
        try:
            return resolver.to_gamedata_relative(path)
        except Exception as e:  # noqa: BLE001
            log.debug("Modifier %s is outside GameData: %s", path, e)
    return path


def _remap_prefab_animation(
    prefab: Prefab, modifier: Any, *, source_path: str, resolver: Any,
) -> None:
    """Apply GameSkin constraint replacements to this item's domain prefab only."""
    if modifier is None:
        return
    modifier_body = getattr(modifier, "body", None)
    chunk = modifier_body.get(0x0915D000) if isinstance(modifier_body, dict) else None
    if chunk is None:
        return
    remapping = getattr(chunk, "Remapping", None)
    folder = str(getattr(chunk, "RemapFolder", "") or "")
    if remapping is None or not folder:
        return
    skin = load_game_skin(remapping)
    replacements = {fid.directory.replace("\\", "/").lower(): fid
                    for fid in skin.fids if fid.class_id == 0x2F0CA000}
    if not replacements:
        return
    from ..io import read_gbx

    def visit(current: Prefab) -> None:
        for entity in current.entities:
            if entity.sub_prefab is not None:
                visit(entity.sub_prefab)
            if entity.anim_driver is None:
                continue
            path = entity.model_path
            if resolver is not None and os.path.isabs(path):
                path = resolver.to_gamedata_relative(path)
            fid = replacements.get(path.replace("\\", "/").lower())
            if fid is None:
                continue
            extension = os.path.basename(fid.directory).partition(".")[2]
            replacement = os.path.join(folder, f"{fid.name}.{extension}")
            resolved = resolver.resolve(replacement, source_path=source_path) if resolver else None
            if resolved is None:
                log.warning("Missing animation remap %s for %s", replacement, source_path)
                continue
            parsed = read_gbx(resolved, recursive=False)
            if parsed.classId != 0x2F0CA000:
                raise ValueError(f"Animation remap {resolved} is not a KinematicConstraint")
            body = parsed.body
            entity.anim_driver = _parse_anim_driver(body.get(0x2F0CA000, body))

    visit(prefab)


def _find_mesh_node(static_obj: Any) -> Optional[Any]:
    """Drill into a StaticObject body and return its embedded ``Mesh`` node."""
    body = getattr(static_obj, "body", None)
    candidate = getattr(static_obj, "Mesh", None)
    if candidate is None:
        candidate = getattr(body, "Mesh", None) if body is not None else None
    if candidate is None and isinstance(body, dict):
        for v in body.values():
            if v is None:
                continue
            inner = getattr(v, "Mesh", None)
            if inner is not None:
                candidate = inner
                break
    return candidate


# ---------------------------------------------------------------------------
#  Placement parsing
# ---------------------------------------------------------------------------

def _parse_placement(chunk: Any) -> ItemPlacement:
    node = getattr(chunk, "defaultPlacement", chunk)
    body = getattr(node, "body", {})
    raw = body.get(0x2E020000, node) if isinstance(body, dict) else node
    pivot_data = body.get(0x2E020001, raw) if isinstance(body, dict) else raw
    placement = ItemPlacement()
    flags = getattr(raw, "flags", None)
    if flags is not None:
        placement.ghost_mode = bool(getattr(flags, "GhostMode", False))

    placement.cube_size = float(getattr(raw, "cubeSize", 0.0) or 0.0)
    placement.grid_snap_h_step = float(getattr(raw, "gridSnap_HStep", 32.0))
    placement.grid_snap_v_step = float(getattr(raw, "gridSnap_VStep", 2.0))
    placement.grid_snap_h_offset = float(getattr(raw, "gridSnap_HOffset", 0.0) or 0.0)
    placement.fly_step = float(getattr(raw, "flyStep", 2.0))
    placement.pivot_snap_distance = float(
        getattr(raw, "pivotSnapDistance", -1.0)
    )

    pivots: List[Vec3] = []
    for p in getattr(pivot_data, "pivotPositions", None) or []:
        pivots.append(gbx_to_blender_vec3(
            float(getattr(p, "x", 0.0) or 0.0),
            float(getattr(p, "y", 0.0) or 0.0),
            float(getattr(p, "z", 0.0) or 0.0),
        ))
    placement.pivot_positions = pivots
    return placement


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

@register(CLASS_ID)
def load_item(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> Item:
    """Build an :class:`Item` from a parsed ``CGameItemModel`` container."""
    body = _body_dict(parsed)

    icon, header_item_type = _extract_header(parsed)
    item_type = header_item_type

    page_path = ""
    skin_directory = ""
    is_internal = False
    is_advanced = False
    catalog_position = 0
    ground_point = Vec3()
    orbital_center_height = 0.0
    orbital_radius_base = 0.0
    entity_variants: List[EntityVariant] = []
    parts: List[MeshPart] = []
    embedded_prefab: Optional[Prefab] = None
    placement = ItemPlacement()

    chunk = body.get(_CHUNK_PAGE_PATH)
    if chunk is not None:
        page_path = str(getattr(chunk, "pagePath", "") or "")

    chunk = body.get(_CHUNK_SKIN_DIR)
    if chunk is not None:
        skin_directory = str(getattr(chunk, "skinDirectory", "") or "")

    chunk = body.get(_CHUNK_CATALOG)
    if chunk is not None:
        is_internal = bool(getattr(chunk, "isInternal", False))
        is_advanced = bool(getattr(chunk, "isAdvanced", False))
        catalog_position = int(getattr(chunk, "catalogPosition", 0) or 0)

    chunk = body.get(_CHUNK_GROUND)
    if chunk is not None:
        gp = getattr(chunk, "groundPoint", None)
        if gp is not None:
            ground_point = gbx_to_blender_vec3(
                float(getattr(gp, "x", 0.0) or 0.0),
                float(getattr(gp, "y", 0.0) or 0.0),
                float(getattr(gp, "z", 0.0) or 0.0),
            )
        orbital_center_height = float(
            getattr(chunk, "orbitalCenterHeightFromGround", 0.0) or 0.0
        )
        orbital_radius_base = float(getattr(chunk, "orbitalRadiusBase", 0.0) or 0.0)

    chunk = body.get(_CHUNK_ITEM_TYPE)
    if chunk is not None:
        it = getattr(chunk, "itemType", None)
        if it:
            item_type = str(it)

    chunk = body.get(_CHUNK_ENTITY_MODEL)
    custom_block: Optional[CustomBlock] = None
    material_modifier_path = ""
    if chunk is not None:
        entity_variants, parts, embedded_prefab = _parse_entity_model(
            chunk, source_path=source_path, resolver=resolver
        )
        custom_block = _parse_custom_block(chunk, source_path=source_path)
        material_modifier_path = _modifier_path(getattr(chunk, "MaterialModifier", None), resolver)

    chunk = body.get(_CHUNK_PLACEMENT)
    if chunk is not None:
        placement = _parse_placement(chunk)

    item = Item(
        source_path=source_path or None,
        class_id=CLASS_ID,
        name=_file_stem(source_path),
        raw=parsed,
        item_type=item_type,
        page_path=page_path,
        skin_directory=skin_directory,
        is_internal=is_internal,
        is_advanced=is_advanced,
        catalog_position=catalog_position,
        ground_point=ground_point,
        orbital_center_height=orbital_center_height,
        orbital_radius_base=orbital_radius_base,
        icon=icon,
        entity_variants=entity_variants,
        parts=parts,
        embedded_prefab=embedded_prefab,
        custom_block=custom_block,
        material_modifier_path=material_modifier_path,
        default_placement=placement,
    )

    entity_chunk = body.get(_CHUNK_ENTITY_MODEL)
    if entity_chunk is not None:
        edition = getattr(entity_chunk, "EntityModelEdition", None)
        edition_data = _body_dict(edition).get(0x2E026000)
        crystal = getattr(edition_data, "meshCrystal", None)
        if crystal is not None:
            item.crystal_layers = crystal_layers(crystal)
            layers = _body_dict(crystal).get(0x09003005)
            for layer in getattr(layers, "layers", None) or []:
                if str(layer.type) != "SpawnPosition" or getattr(layer, "is_enabled", True) is False:
                    continue
                from .map import _yaw_pitch_roll_to_blender_quat
                content = layer.content.content
                position = content.position
                item.spawn_transform = blender_transform_matrix(
                    gbx_to_blender_vec3(position.x, position.y, position.z),
                    _yaw_pitch_roll_to_blender_quat(
                        float(content.yaw), float(content.pitch), float(getattr(content, "roll", 0) or 0),
                    ),
                )
        model = getattr(entity_chunk, "EntityModel", None)
        runtime = _body_dict(model).get(_CHUNK_STATIC_OBJECT)
        spawn = getattr(getattr(runtime, "props", None), "spawnLoc", None)
        if spawn is not None:
            item.spawn_transform = gbx_iso_to_blender_matrix(spawn)
    waypoint = body.get(0x2E00201F)
    if waypoint is not None:
        item.waypoint_type = str(getattr(waypoint, "waypointType", "No") or "No")

    log.debug(
        "Loaded item %s type=%r variants=%d parts=%d embedded_prefab=%s page=%r",
        item.name, item.item_type,
        len(entity_variants),
        len(parts),
        embedded_prefab is not None,
        page_path,
    )
    return item


__all__ = ["load_item", "CLASS_ID"]
