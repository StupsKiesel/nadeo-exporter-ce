"""Decoration surroundings: the blocks of the decoration's own map.

Stadium decorations link a map in chunk ``0x0303801B``
(``Base48x48Screen155Day`` -> ``Stadium/GameCtnDecoration/Map/Deco48x48_Screen155.Map.Gbx``).
That map uses the ``Stadium256`` collection (256 m x 64 m grid, read from
``Collections/<name>.Collection.Gbx``) and places the stands as ordinary
EDClassic blocks (``Stade1536[_Screen155]``, ``Stade4096``, ``NoStadium``).
Vista decorations instead link a DecorationSize Scene3d containing the
legacy solid ocean surface and seabed (the ``Warp`` object).

Alignment (measured, the engine rule is not documented):

* horizontally the deco map is centred on the play field: the stands of
  ``Stade1536`` end exactly at the 1536 m field of a 48x48 map centred in
  the 16x12 x 256 m deco map;
* vertically the deco ground (Stade1536 ground plane at local z=128 on
  row 2, NoStadium border tops) lies at :data:`DECO_GROUND_HEIGHT` and is
  put on the main map's ground row (``deco_base_height_offset + 1``).
"""

from __future__ import annotations

import logging
import os
import struct

from .. import library, load
from ..errors import GbxbpyError, ParseError, ResolveError
from ..model.decoration import Decoration, DecorationSize
from ..model.map import Map
from ..model.mesh import Mesh
from ..model.prefab import Prefab
from ..resolve.block_lookup import _aliases_for
from ..resolve import Resolver
from .assets import AssetLibrary
from .dependencies import Dependencies
from .environment import find_decoration
from .placement import multiply, translation
from .scene import AssetDefinition, GeometryPart, Placement

log = logging.getLogger("gbxbpy.map_import_v4")

DEFAULT_GRID = (32.0, 8.0)
_COLLECTION_CHUNK = 0x03033009
# Deco-map height (grid frame) that sits on the main map's ground.
DECO_GROUND_HEIGHT = {"stadium256": 256.0}
SECTION = "Decoration"


def read_collection_grid(data: bytes) -> tuple[float, float] | None:
    """``(square size, square height)`` from a ``*.Collection.Gbx``.

    Chunk ``0x03033009`` (not described by NICE) starts the body with
    version, collection id, four u32 flags and a u32 0, followed by the two
    floats at byte 32 (Stadium 32/8, Stadium256 256/64).
    """
    from ..library.validate import gbx_body

    body = gbx_body(data)
    if body is None or len(body) < 40:
        return None
    if struct.unpack_from("<I", body, 0)[0] != _COLLECTION_CHUNK:
        return None
    square, height = struct.unpack_from("<2f", body, 32)
    if not (1.0 <= square <= 4096.0 and 1.0 <= height <= 4096.0):
        return None
    return float(square), float(height)


def collection_grid(gamedata_dir: str, collection: str) -> tuple[float, float]:
    for name in _aliases_for(collection or "Stadium"):
        path = os.path.join(gamedata_dir, "Collections", f"{name}.Collection.Gbx")
        if not library.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                grid = read_collection_grid(handle.read())
        except OSError:
            grid = None
        if grid is not None:
            return grid
    return DEFAULT_GRID


def _height_from_size(map_asset: Map, size: DecorationSize, grid_height: float) -> float:
    # DecorationSize.base_height_offset is the editor default; the map
    # stores its chosen value, so adding both would double Stadium's offset.
    return (size.base_height_base + int(size.offset_block_y)
            + map_asset.deco_base_height_offset) * grid_height


def resolve_absolute_height(map_asset: Map, resolver: Resolver) -> float:
    """Collection base plus the map's editable height offset, in metres."""
    grid_height = collection_grid(resolver.gamedata_dir, map_asset.decoration_collection)[1]
    offset = map_asset.deco_base_height_offset
    if not map_asset.decoration or "Stadium" in _aliases_for(map_asset.decoration_collection):
        return offset * grid_height
    path = find_decoration(
        resolver.gamedata_dir, map_asset.decoration_collection, map_asset.decoration,
    )
    if path is None:
        raise ResolveError(f"Decoration not found: {map_asset.decoration_collection}/{map_asset.decoration}")
    aliases = {name.casefold() for name in _aliases_for(map_asset.decoration_collection)}
    if aliases and os.path.basename(os.path.dirname(os.path.dirname(path))).casefold() not in aliases:
        raise ResolveError(f"Decoration not found in collection {map_asset.decoration_collection}: {path}")
    decoration = load(path, resolver=resolver, recursive=False)
    if not isinstance(decoration, Decoration):
        raise ParseError(f"Expected Decoration: {path}")
    errors = tuple(getattr(decoration.raw, "_errors", ()) or ())
    if errors:
        raise ParseError(f"Decoration incomplete: {path}: {'; '.join(map(str, errors))}")
    size_path = resolver.resolve(decoration.deco_size_path, source_path=path)
    if size_path is None:
        raise ResolveError(f"Decoration size not found: {decoration.deco_size_path} (linked by {path})")
    size = load(size_path, resolver=resolver, recursive=False)
    if not isinstance(size, DecorationSize):
        raise ParseError(f"Expected DecorationSize: {size_path}")
    errors = tuple(getattr(size.raw, "_errors", ()) or ())
    if errors:
        raise ParseError(f"Decoration size incomplete: {size_path}: {'; '.join(map(str, errors))}")
    return _height_from_size(map_asset, size, grid_height)


def deco_map_origin(
    main_size: tuple[int, int, int], main_grid: tuple[float, float], main_base_offset: float,
    deco_size: tuple[int, int, int], deco_grid: tuple[float, float], deco_collection: str,
) -> tuple[float, float, float]:
    """Blender translation that moves the deco map's grid frame onto the main map's."""
    main_x, main_z = main_size[0] * main_grid[0], main_size[2] * main_grid[0]
    deco_x, deco_z = deco_size[0] * deco_grid[0], deco_size[2] * deco_grid[0]
    ground = DECO_GROUND_HEIGHT.get(deco_collection.casefold(), 4 * deco_grid[1])
    main_ground = (main_base_offset + 1.0) * main_grid[1]
    # Game +Z is Blender -Y, so a negative game offset becomes a positive Y.
    return ((main_x - deco_x) / 2.0, -(main_z - deco_z) / 2.0, main_ground - ground)


def _load_decoration(map_asset: Map, dependencies: Dependencies) -> tuple[str, Decoration] | None:
    resolver = dependencies.resolver
    decoration_id = map_asset.decoration or ""
    if not decoration_id:
        return None
    collection = map_asset.decoration_collection or "Stadium"
    decoration_path = find_decoration(resolver.gamedata_dir, collection, decoration_id)
    if decoration_path is None:
        return None
    try:
        decoration = load(decoration_path, resolver=resolver, recursive=False)
    except (GbxbpyError, OSError) as exc:
        dependencies.plan.issue("parse_failed", decoration_path, f"Decoration: {exc}")
        return None
    if not isinstance(decoration, Decoration):
        return None
    return decoration_path, decoration


def _decoration_field_size(
    map_asset: Map, decoration: Decoration, source: str, dependencies: Dependencies,
) -> tuple[int, int, int]:
    """Block size the deco map is centred on: the DecorationSize, not the map.

    Maps can store a larger size than their decoration (e.g. 64x64 with a
    48x48 stadium); the game still centres the stadium on the 48x48 field.
    """
    resolver, plan = dependencies.resolver, dependencies.plan
    size_path = resolver.resolve(decoration.deco_size_path, source_path=source) if decoration.deco_size_path else None
    if size_path is None:
        return map_asset.map_size
    try:
        size = load(size_path, resolver=resolver, recursive=False)
    except (GbxbpyError, OSError) as exc:
        plan.issue("parse_failed", size_path, f"Decoration size: {exc}")
        return map_asset.map_size
    if not isinstance(size, DecorationSize) or len(size.size) != 3:
        return map_asset.map_size
    return tuple(size.size)


def plan_decoration_scene(
    map_asset: Map, decoration: Decoration, source: str, dependencies: Dependencies, skins=None,
) -> int:
    """Place the authored scene surround at the collection's decoration base."""
    if decoration.deco_map_path or not decoration.deco_size_path:
        return 0
    resolver, plan = dependencies.resolver, dependencies.plan
    size_path = resolver.resolve(decoration.deco_size_path, source_path=source)
    if size_path is None:
        plan.issue("missing_decoration_size", decoration.deco_size_path, f"Linked by {source}")
        return 0
    try:
        size = load(size_path, resolver=resolver, recursive=False)
        if not isinstance(size, DecorationSize):
            plan.issue("unsupported_asset", size_path, "Expected DecorationSize")
            return 0
        if not size.scene_path:
            return 0
        path = resolver.resolve(size.scene_path, source_path=size_path)
        if path is None:
            path = resolver.resolve(os.path.join("..", size.scene_path), source_path=size_path)
        if path is None:
            plan.issue("missing_decoration_scene", size.scene_path, f"Linked by {size_path}")
            return 0
        scene = load(path, resolver=resolver, recursive=False)
    except (GbxbpyError, OSError) as exc:
        plan.issue("parse_failed", size_path, f"Decoration scene: {exc}")
        return 0
    if not isinstance(scene, Prefab):
        plan.issue("unsupported_asset", path, "Expected decoration scene geometry")
        return 0
    if not scene.entities:
        return 0
    errors = dependencies._errors(scene)
    if errors:
        plan.issue("parser_diagnostics", path, "; ".join(map(str, errors)))
    for entity in scene.entities:
        if not entity.model_path or entity.mesh is not None or entity.sub_prefab is not None:
            continue
        external_path, payload = dependencies.reference(entity.model_path, path)
        if isinstance(payload, Mesh):
            entity.mesh = payload
        elif isinstance(payload, Prefab):
            entity.sub_prefab = payload
        elif payload is not None:
            plan.issue("unsupported_geometry", external_path, f"Expected Mesh or Prefab, got {type(payload).__name__}")
    AssetLibrary(dependencies, skins).audit_prefab(scene, path)
    grid = collection_grid(resolver.gamedata_dir, map_asset.decoration_collection)
    height = _height_from_size(map_asset, size, grid[1])
    key = repr((os.path.normcase(os.path.abspath(path)), "decoration_scene"))
    plan.assets[key] = AssetDefinition(
        key, scene.name, path, "Decoration", 0, 0, (0, 0, 0),
        parts=[GeometryPart("Surroundings", path, scene)],
    )
    plan.placements.append(Placement(
        name="Decoration_Surroundings", asset_id=key, matrix=translation(0.0, 0.0, height),
        section=SECTION, metadata={"tm_decoration_scene": scene.name},
    ))
    log.info("V4 decoration scene %s: %d surrounds, height %g", scene.name, len(scene.entities), height)
    return 1


def plan_decoration_map(map_asset: Map, dependencies: Dependencies, skins=None) -> int:
    """Append scene surrounds and deco-map blocks; returns the placement count."""
    from .pipeline import plan_grid_block

    plan = dependencies.plan
    found = _load_decoration(map_asset, dependencies)
    if found is None:
        return 0
    source, decoration = found
    count = plan_decoration_scene(map_asset, decoration, source, dependencies, skins)
    if not decoration.deco_map_path:
        return count
    path = dependencies.resolver.resolve(decoration.deco_map_path, source_path=source)
    if path is None:
        plan.issue("missing_decoration_map", decoration.deco_map_path, f"Linked by {source}")
        return count
    try:
        deco_map = load(path, resolver=dependencies.resolver, recursive=False)
    except (GbxbpyError, OSError) as exc:
        plan.issue("parse_failed", path, f"Decoration map: {exc}")
        return count
    if not isinstance(deco_map, Map):
        plan.issue("unsupported_asset", path, f"Expected Map, got {type(deco_map).__name__}")
        return count
    gamedata = dependencies.resolver.gamedata_dir
    collection = deco_map.decoration_collection or ""
    deco_grid = collection_grid(gamedata, collection)
    main_grid = collection_grid(gamedata, map_asset.decoration_collection or "Stadium")
    origin = translation(*deco_map_origin(
        _decoration_field_size(map_asset, decoration, source, dependencies),
        main_grid, map_asset.deco_base_height_offset,
        deco_map.map_size, deco_grid, collection,
    ))
    deco_dependencies = Dependencies(dependencies.resolver, plan, deco_map.embedded_items)
    deco_dependencies.blocks.collection = collection
    library = AssetLibrary(deco_dependencies, skins)
    stem = os.path.basename(path).split(".")[0]
    # Baked blocks (the Grass zone) are legacy CPlugSolid ground under the
    # stands; the stand prefabs carry the visible ground.
    for index, block in enumerate(deco_map.blocks):
        if block.is_free:
            continue
        placement = plan_grid_block(
            block, f"{SECTION}_{index:03d}_{block.name}", SECTION, library, grid=deco_grid,
        )
        placement.matrix = multiply(origin, placement.matrix)
        placement.metadata["tm_decoration_map"] = stem
        count += 1
    log.info(
        "V4 decoration map %s (%s, grid %gx%g): %d blocks", stem, collection, *deco_grid, count,
    )
    return count
