"""Hide vista terrain tiles replaced by other blocks.

Vista maps store their ground as zone blocks (``Land``/``Sea``/``Dirt``
flats, ``Beach``/``*Hill*``/``*Cliff*`` frontiers, transitions). Placing any
other block on such a cell keeps the zone record in the map. Whether the
game still draws it depends on the zone kind and on the occupant's ground
variant ``auto_terrain_zone_ids``:

* Flat tiles (``CGameCtnBlockInfoFlat``) are replaced. Hill/cliff
  frontiers are replaced only by a fully covering occupant which explicitly
  lists that zone. Terrain replacement follows the variant metadata, not
  whether its selected geometry contains a terrain-array material.
  Transitions (``Dirt0_Dirt2_Dirt4``) use the same full-coverage guard with
  their ``VoidTo*`` zone, e.g. under ``DecoTerrainHD``.
* Beach frontiers are also replaced when one occupant explicitly lists
  ``Beach`` and covers every unit of the frontier. ``DecoBeachMangrove``
  brings its own shoreline; keeping the Beach tile blocks its inlet and
  overlaps its water mesh. Partial coverage must not hide a whole frontier.
* The occupant must list a zone the vista terrain uses (``VoidToDirt``,
  ``VoidToLand``...): those ground variants bring their own floor.
  Stadium ground blocks listing ``VoidToGrass`` replace matching Grass.
  Custom blocks retain their archetype's replacement footprint,
  including ``VoidToGrass`` (an empty custom VOID block removes grass).
* Ground clips can supply replacement Stadium Grass around a wall or pillar.
  A separate geometry pass hides only matching plain Grass, never Vista
  terrain. A ground unit's explicit bottom clip may omit the area occupied
  by the block. Standalone clips must supply all four corners of a full tile.
"""

from __future__ import annotations

import logging
import math
import os

from ..model.block import Block, BlockVariant
from ..model.map import MapBlock
from ..model.clip import Clip
from ..model.mesh import Mesh
from ..model.prefab import Prefab
from .inherited_modifiers import unit_cells
from .placement import absolute_matrix, multiply
from .scene import Matrix4, Placement, ScenePlan

log = logging.getLogger("gbxbpy.map_import_v4")

FLAT_CLASS_ID = 0x0304F000
FRONTIER_CLASS_ID = 0x03050000
TRANSITION_CLASS_ID = 0x0314C000
# Baked face, vertical and horizontal connectors may leave map.is_clip unset.
CLIP_CLASS_ID = 0x03053000
CLIP_CLASS_IDS = frozenset({CLIP_CLASS_ID, 0x03340000, 0x0335B000})
# CGameCtnBlockInfoFlat / Frontier / Transition
TERRAIN_CLASS_IDS = frozenset({FLAT_CLASS_ID, FRONTIER_CLASS_ID, TRANSITION_CLASS_ID})


def is_terrain(asset: object) -> bool:
    return isinstance(asset, Block) and asset.class_id in TERRAIN_CLASS_IDS


def _is_clip(asset: object) -> bool:
    return isinstance(asset, Clip) or getattr(asset, "class_id", None) in CLIP_CLASS_IDS


def hide_occupied_terrain(
    records: list[tuple[Placement, MapBlock, BlockVariant, object]],
) -> int:
    """Hide covered flats and explicitly replaced, fully covered frontiers.

    ``records`` holds ``(placement, map_block, variant, block_info)``. Clips
    neither occupy nor get hidden. An occupant replaces the terrain only when
    its variant's auto-terrain zones overlap the zones of the map's terrain
    blocks. Stock ``VoidToGrass`` replacement requires a ground placement.
    Custom blocks retain that zone to replace their archetype's ground. Callers leave out
    stock occupants whose geometry is missing from
    GameData. Returns the number of hidden placements.
    """
    def cells(block: MapBlock, variant: BlockVariant) -> list[tuple[int, int, int]]:
        return unit_cells(block, [tuple(u.relative_offset) for u in variant.units])

    def zones(variant: BlockVariant) -> set[str]:
        return set(getattr(variant, "auto_terrain_zone_ids", ()) or ())

    terrain_zones: set[str] = set()
    for _placement, block, variant, asset in records:
        if not block.is_clip and is_terrain(asset):
            terrain_zones |= zones(variant)
    occupied: dict[tuple[int, int, int], dict[str, str]] = {}
    beach_replacements: dict[
        tuple[int, int, int], list[tuple[str, set[tuple[int, int, int]]]]
    ] = {}
    frontier_replacements: dict[
        tuple[int, int, int], list[tuple[str, set[tuple[int, int, int]], set[str]]]
    ] = {}
    for placement, block, variant, asset in records:
        if block.is_clip or block.is_free or is_terrain(asset):
            continue
        if _is_clip(asset):
            continue
        replacement_zones = zones(variant)
        if not block.is_ground and "tm_custom_block_archetype" not in placement.metadata:
            replacement_zones.discard("VoidToGrass")
        if not replacement_zones & terrain_zones:
            continue
        footprint = set(cells(block, variant))
        for cell in footprint:
            cell_zones = occupied.setdefault(cell, {})
            for zone in replacement_zones:
                cell_zones.setdefault(zone, block.name)
            if "Beach" in zones(variant) and variant.units:
                beach_replacements.setdefault(cell, []).append((block.name, footprint))
            if variant.units:
                frontier_replacements.setdefault(cell, []).append(
                    (block.name, footprint, replacement_zones),
                )
    hidden = 0
    for placement, block, variant, asset in records:
        if block.is_clip:
            continue
        class_id = getattr(asset, "class_id", None)
        footprint = cells(block, variant)
        if class_id == FLAT_CLASS_ID:
            grass = "VoidToGrass" in zones(variant)
            occupant = next((
                name for cell in footprint
                for zone, name in occupied.get(cell, {}).items()
                # Preserve Vista's shared replacement footprint, but never
                # let Stadium compatibility replace Land (or vice versa).
                if (zone == "VoidToGrass") == grass and zone in terrain_zones
            ), None)
        elif class_id == FRONTIER_CLASS_ID and "Beach" in zones(variant) and variant.units:
            occupant = next((
                name for name, covered in beach_replacements.get(footprint[0], ())
                if set(footprint) <= covered
            ), None)
        elif class_id == FRONTIER_CLASS_ID and variant.units:
            occupant = next((
                name for name, covered, replacement_zones
                in frontier_replacements.get(footprint[0], ())
                if set(footprint) <= covered and any(
                    zone in replacement_zones and not zone.startswith("VoidTo")
                    for zone in zones(variant)
                )
            ), None)
        elif class_id == TRANSITION_CLASS_ID and variant.units:
            # Transitions only list ``VoidTo*`` zones (Dirt0_Dirt2_Dirt4 ->
            # VoidToDirt); DecoTerrainHD brings its own reshaped terrain.
            occupant = next((
                name for name, covered, replacement_zones
                in frontier_replacements.get(footprint[0], ())
                if set(footprint) <= covered and zones(variant) & replacement_zones
            ), None)
        else:
            continue
        if occupant is not None:
            placement.hidden = True
            placement.metadata["tm_terrain_hidden_by"] = occupant
            hidden += 1
    if hidden:
        log.info("V4 terrain: hid %d terrain tile(s) replaced by other blocks", hidden)
    return hidden


def _grass_surfaces(payload: Mesh | Prefab, matrix: Matrix4):
    """Yield transformed vertices of planar Grass submeshes, not GrassFence fins."""
    if isinstance(payload, Prefab):
        for entity in payload.entities:
            local = absolute_matrix(entity.transform.position, entity.transform.rotation, 1.0, 0.0)
            world = multiply(matrix, local)
            if entity.mesh is not None:
                yield from _grass_surfaces(entity.mesh, world)
            if entity.sub_prefab is not None:
                yield from _grass_surfaces(entity.sub_prefab, world)
        return
    if not payload.lods:
        return
    lod = min(payload.lods, key=lambda level: level.lod)
    for submesh in lod.submeshes:
        if not 0 <= submesh.material_index < len(payload.materials):
            continue
        binding = payload.materials[submesh.material_index]
        name = (binding.material_name or binding.material_path).replace("\\", "/").rsplit("/", 1)[-1]
        if name.split(".", 1)[0].casefold() != "grass" or not submesh.indices:
            continue
        vertices = []
        for index in set(submesh.indices):
            v = lod.vertices[index].position
            vertices.append(tuple(
                sum(matrix[i][j] * value for j, value in enumerate((v.x, v.y, v.z, 1.0)))
                for i in range(3)
            ))
        yield vertices


def hide_clip_covered_grass(
    records: list[tuple[Placement, MapBlock, BlockVariant, object]], plan: ScenePlan,
) -> int:
    """Replace Stadium grass with an owned bottom filler or a full standalone tile."""
    def clip_key(path: str) -> str:
        return os.path.basename(path.replace("\\", "/")).casefold()

    grass = {}
    clips = []
    bottom_owners = {}
    for placement, block, variant, asset in records:
        if (not block.is_clip and getattr(asset, "class_id", None) == FLAT_CLASS_ID
                and "VoidToGrass" in variant.auto_terrain_zone_ids and not placement.hidden):
            for cell in unit_cells(block, [u.relative_offset for u in variant.units]):
                grass.setdefault(cell, []).append(placement)
        if not placement.hidden and _is_clip(asset):
            clips.append((placement, placement.matrix, placement.name))
        if (not placement.hidden and block.is_ground and not block.is_free
                and not block.is_clip and not is_terrain(asset) and not _is_clip(asset)):
            cells = unit_cells(block, [u.relative_offset for u in variant.units])
            for unit, cell in zip(variant.units, cells):
                for reference in unit.clips_bottom:
                    if reference:
                        bottom_owners.setdefault(clip_key(reference), set()).add(cell)
    if not grass:
        return 0
    for placement in plan.placements:
        if not placement.hidden:
            clips.extend((clip, multiply(placement.matrix, clip.matrix), placement.name)
                         for clip in placement.clips)
    hidden = 0
    tolerance = 0.001
    for clip, matrix, name in clips:
        definition = plan.assets.get(clip.asset_id)
        if definition is None:
            continue
        owned_cells = bottom_owners.get(clip_key(definition.source_path), ())
        for part in definition.parts:
            if part.payload is None:
                continue
            for vertices in _grass_surfaces(part.payload, multiply(matrix, part.matrix)):
                lo = tuple(min(v[i] for v in vertices) for i in range(3))
                hi = tuple(max(v[i] for v in vertices) for i in range(3))
                if hi[2] - lo[2] > tolerance:
                    continue
                # Bottom fillers leave room for their owner's wall/cliff; the
                # unit reference proves which cell they replace, not their bbox.
                candidate = (math.floor((lo[0] + hi[0])/64), round(lo[2]/8),
                             math.floor(-(lo[1] + hi[1])/64))
                for cell in (candidate,) if candidate in owned_cells else ():
                    x, y, z = cell
                    if (x*32 - tolerance <= lo[0] < hi[0] <= (x+1)*32 + tolerance
                            and -(z+1)*32 - tolerance <= lo[1] < hi[1] <= -z*32 + tolerance
                            and abs(lo[2] - y*8) <= tolerance):
                        for placement in grass.pop(cell, ()):
                            placement.hidden = True
                            placement.metadata["tm_terrain_hidden_by"] = name
                            hidden += 1
                if (abs(hi[0] - lo[0] - 32.0) > tolerance
                        or abs(hi[1] - lo[1] - 32.0) > tolerance
                        or hi[2] - lo[2] > tolerance):
                    continue
                # Require the four tile corners, not merely a large bounding box.
                if not all(any(abs(v[0]-x) < tolerance and abs(v[1]-y) < tolerance
                               for v in vertices) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])):
                    continue
                cell = (round(lo[0]/32), round(lo[2]/8), round(-hi[1]/32))
                if any(abs(a-b) > tolerance for a, b in zip(
                    (lo[0], lo[2], hi[1]), (cell[0]*32, cell[1]*8, -cell[2]*32),
                )):
                    continue
                for placement in grass.pop(cell, ()):
                    placement.hidden = True
                    placement.metadata["tm_terrain_hidden_by"] = name
                    hidden += 1
    if hidden:
        log.info("V4 terrain: hid %d grass tile(s) replaced by ground clips", hidden)
    return hidden


__all__ = ["TERRAIN_CLASS_IDS", "hide_occupied_terrain", "hide_clip_covered_grass", "is_terrain"]
