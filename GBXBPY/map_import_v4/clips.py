"""Neighbour-dependent clip planning, including internal multi-cell seams."""

from __future__ import annotations

import logging
import math

from ..model.block import BlockVariant
from ..model.clip import Clip
from ..model.map import Map, MapBlock
from .assets import AssetLibrary, has_mobil_geometry
from .placement import footprint, multiply, rotation, translation, world_cell
from .scene import Cell, ImportOptions, Instance, Matrix4, Placement, ScenePlan

log = logging.getLogger("gbxbpy.map_import_v4")

FACE_DELTA: dict[str, Cell] = {
    "north": (0, 0, 1), "south": (0, 0, -1),
    "east": (1, 0, 0), "west": (-1, 0, 0),
    "top": (0, 1, 0), "bottom": (0, -1, 0),
}
ROTATE_FACE = {"north": "west", "west": "south", "south": "east", "east": "north",
               "top": "top", "bottom": "bottom"}
STEPS = {"North": 0, "East": 1, "South": 2, "West": 3}


def add(a: Cell, b: Cell) -> Cell:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def uses_baked_clips(map_asset: Map, options: ImportOptions) -> bool:
    """Whether the map's baked list replaces generated free clips.

    The editor's free-clip pass (pairing/deletion rules) writes every clip
    that survives into the BakedBlocks list, so a face missing there was
    deleted by the engine. Generating clips on top would add pieces the game
    never shows; the heuristic is only a fallback for maps without a list.
    """
    return options.import_baked and bool(map_asset.baked_blocks)


def hide_ghost_baked_duplicates(baked: list[MapBlock], plan: ScenePlan) -> int:
    """Hide ghost clips that repeat a normal baked clip on the same face.

    Ghost blocks may overlap normal ones (Winter 2026 - 03 stacks ghost
    ``DecoWallBasePillar`` on normal pillars). The editor bakes a ghost
    ``DecoWallBaseVFC`` with its own stack piece onto the identical cell
    and face as the normal one, so both meshes would overlap. Ghost clips
    without a normal twin finish ghost-only blocks and stay visible.
    """
    def name(index: int, block: MapBlock) -> str:
        return f"BakedBlocks_{index:05d}_{block.name}"

    normal = {
        (block.name, tuple(block.coords), block.direction): name(index, block)
        for index, block in enumerate(baked)
        if not block.is_ghost and not block.is_free
    }
    duplicates = {
        name(index, block): twin
        for index, block in enumerate(baked)
        if block.is_ghost and not block.is_free
        and (twin := normal.get((block.name, tuple(block.coords), block.direction)))
    }
    hidden = 0
    for placement in plan.placements:
        twin = duplicates.get(placement.name) if placement.section == "BakedBlocks" else None
        if twin is not None:
            placement.hidden = True
            placement.metadata["tm_ghost_duplicate_of"] = twin
            hidden += 1
    if hidden:
        log.info("V4 clips: hid %d ghost baked clip(s) duplicating normal clips", hidden)
    return hidden


def world_face(face: str, direction: str) -> str:
    for _ in range(STEPS[direction]):
        face = ROTATE_FACE[face]
    return face


def face_matrix(face: str, cell: Cell) -> Matrix4:
    x, y, z = cell
    tx, ty, tz, angle = {
        "west": (-32.0, -32.0, 0.0, math.pi/2),
        "south": (0.0, 32.0, 0.0, 0.0),
        "east": (64.0, 0.0, 0.0, -math.pi/2),
        "north": (32.0, -64.0, 0.0, math.pi),
        "top": (0.0, 0.0, 8.0, 0.0),
        "bottom": (0.0, 0.0, -8.0, 0.0),
    }[face]
    return multiply(translation(x*32.0 + tx, -z*32.0 + ty, y*8.0 + tz), rotation("Z", angle))


def visible_faces(
    block: MapBlock, variant: BlockVariant, owner: int, occupied: dict[Cell, int],
):
    size = footprint(variant)
    local = {u.relative_offset for u in variant.units}
    for unit_index, unit in enumerate(variant.units):
        for face, delta in FACE_DELTA.items():
            refs = getattr(unit, f"clips_{face}")
            reference = next((ref for ref in refs if ref), "")
            if not reference or add(unit.relative_offset, delta) in local:
                continue
            cell = world_cell(block.coords, block.direction, unit.relative_offset, size)
            neighbour = add(cell, FACE_DELTA[world_face(face, block.direction)])
            other = occupied.get(neighbour)
            if other is None or other == owner:
                yield unit_index, unit.relative_offset, face, reference


def plan_clips(
    records: list[tuple[Placement, MapBlock, BlockVariant, str]], library: AssetLibrary,
) -> None:
    occupied: dict[Cell, int] = {}
    for owner, (placement, block, variant, _source) in enumerate(records):
        if placement.asset_id is None:
            raise ValueError("Clip planning requires a resolved block definition")
        definition = library.plan.assets[placement.asset_id]
        for unit in variant.units:
            occupied.setdefault(world_cell(block.coords, block.direction, unit.relative_offset, definition.footprint), owner)

    for owner, (placement, block, variant, source) in enumerate(records):
        # Match normal import: clip fallback only for variants without mobil paths.
        if has_mobil_geometry(variant):
            continue
        for unit_index, offset, face, reference in visible_faces(block, variant, owner, occupied):
            path, asset = library.dependencies.reference(reference, source)
            target = None
            if isinstance(asset, Clip):
                # The engine gives a clip the owner's ground bit only on the
                # owner's bottom row; upper units of a ground block get air clips.
                ground = block.is_ground and offset[1] == 0
                preferred = asset.variant_ground if ground else asset.variant_air
                clip_variant = preferred or asset.variant_air or asset.variant_ground
                if clip_variant is not None:
                    # The first group with geometry is the single-cell piece
                    # (Bottom/Middle); other groups are top/stack alternatives,
                    # not parts to combine.
                    definition = library.definition(
                        path, clip_variant, -1, clip=True,
                        context=library.material_context(asset, None, path),
                    )
                    target = definition.asset_id
                    if not definition.parts:
                        library.plan.issue("empty_clip", path, "Clip variant has no usable mobils")
                        target = None
                else:
                    library.plan.issue("missing_variant", path, "Clip has neither ground nor air variant")
            elif asset is not None:
                library.plan.issue("unsupported_clip", path, f"Expected Clip, got {type(asset).__name__}")
            placement.clips.append(Instance(
                f"unit{unit_index:02d}_clip_{face}", target, face_matrix(face, offset),
                {"tm_clip_source": reference, "tm_clip_face": face, "tm_clip_unit": unit_index},
            ))
