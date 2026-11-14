"""Build and validate a scene plan for blocks and items before Blender runs."""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from .. import load
from ..errors import GbxbpyError
from ..color_tables import element_color_index
from ..model.block import Block
from ..model.clip import Clip
from ..model.item import Item
from ..model.map import Map
from ..resolve import Resolver
from ..resolve.modifier_lookup import resolve_terrain_material_remap
from ..resolve.skins import SkinLibrary
from .assets import AssetLibrary, has_mobil_geometry, pick_variant
from .absolute import plan_absolute
from .clips import hide_ghost_baked_duplicates, plan_clips, uses_baked_clips
from .decoration_map import collection_grid, plan_decoration_map, resolve_absolute_height
from .dependencies import Dependencies
from .environment import plan_environment
from .inherited_modifiers import baked_modifiers
from .placement import grid_matrix
from .scene import ImportOptions, Placement, ScenePlan
from .terrain import hide_clip_covered_grass, hide_occupied_terrain

log = logging.getLogger("gbxbpy.map_import_v4")

# ``progress(fraction, message)`` with ``fraction`` in 0..1; optional everywhere.
ProgressCallback = Callable[[float, str], None]


def _no_progress(_fraction: float, _message: str) -> None:
    pass


def plan_grid_block(
    block, name: str, section: str, library: AssetLibrary,
    inherited: tuple[str, ...] = (), records: list | None = None, occupancy: list | None = None,
    grid: tuple[float, float] = (32.0, 8.0),
) -> Placement:
    """Plan one grid block; ``records``/``occupancy`` collect clip and terrain inputs."""
    dependencies = library.dependencies
    plan = library.plan
    placement = Placement(
        name=name, asset_id=None, section=section,
        metadata={
            "tm_block_name": block.name, "tm_block_dir": block.direction,
            "tm_block_is_ground": block.is_ground, "tm_block_is_baked": block.is_baked,
            "tm_block_variant_index": block.block_variant_index,
            "tm_block_mobil_index": block.mobil_index,
            "tm_block_mobil_variant_index": block.mobil_variant_index,
            "tm_block_color": block.color,
            "tm_block_coord": block.coords,
            "tm_block_is_free": False,
            "tm_block_is_ghost": block.is_ghost,
            "tm_color_index": element_color_index(block.color),
        },
    )
    plan.placements.append(placement)
    kind = "ground" if block.is_ground else "air"
    path, asset = dependencies.stock_block(
        block.name, block.is_clip, kind, block.block_variant_index,
        block.mobil_index, block.mobil_variant_index,
    )
    size = (1, 1, 1)
    if path is None:
        plan.issue("missing_block", block.name, "Stock block/clip definition not found")
    elif isinstance(asset, (Block, Clip)):
        variant = pick_variant(asset, kind, block.block_variant_index)
        if variant is None:
            plan.issue("missing_variant", path, f"No {kind} variant")
        else:
            definition = library.definition(
                path, variant, block.mobil_index,
                mobil_variant_index=block.mobil_variant_index,
                context=library.material_context(asset, block.skin, path, inherited),
            )
            placement.asset_id = definition.asset_id
            size = definition.footprint
            if records is not None:
                records.append((placement, block, variant, path))
            if occupancy is not None and any(part.payload is not None for part in definition.parts):
                # A missing vista prefab can't replace the ground.
                occupancy.append((placement, block, variant, asset))
            if not has_mobil_geometry(variant) and not any(
                getattr(u, f"clips_{face}") for u in variant.units
                for face in ("north", "east", "south", "west", "top", "bottom")
            ):
                plan.issue("empty_variant", path, f"No geometry for {kind}[{variant.index}]")
    elif isinstance(asset, Item) and asset.custom_block is not None:
        archetype, variant = library.custom_block_archetype(path, asset, kind, block.block_variant_index)
        definition = library.custom_block_definition(
            path, asset, kind, block.block_variant_index, variant,
        )
        placement.asset_id = definition.asset_id
        placement.metadata["tm_custom_block_archetype"] = asset.custom_block.archetype_id
        size = definition.footprint
        if occupancy is not None and variant is not None:
            # Embedded geometry is never missing; an empty crystal (VOID)
            # still replaces the ground like its archetype.
            occupancy.append((placement, block, variant, archetype))
    elif asset is not None:
        plan.issue("unsupported_block", path, f"Expected Block/Clip, got {type(asset).__name__}")
    placement.matrix = grid_matrix(block.coords, block.direction, size, grid)
    return placement


def plan_texture_mod(map_asset: Map, skins: SkinLibrary | None, plan: ScenePlan):
    """Locate, download if needed, and index the map's texture mod."""
    from ..resolve.locators import LocatorCache
    from ..resolve.skins import default_cache_dir, default_packs_dirs, default_user_dirs
    from ..resolve.texture_mod import load_texture_mod

    if skins is None:
        cache = default_cache_dir()
        locators = LocatorCache(os.path.join(cache, "Locators")) if plan.options.download_locators else None
        skins = SkinLibrary(default_packs_dirs(), default_user_dirs(), cache, locators)
    mod, problem = load_texture_mod(
        map_asset.mod_pack_path, map_asset.mod_pack_url, skins,
        os.path.join(skins.cache_dir, "TextureMods"),
    )
    if mod is None:
        plan.issue("missing_texture_mod", map_asset.mod_pack_path or map_asset.mod_pack_url, problem)
        return None
    log.info("V4 texture mod %s: %d files (%s)", mod.source, len(mod.files), mod.archive_path)
    return mod


def plan_map(
    filepath: str, *, resolver: Resolver, options: ImportOptions | None = None,
    skins: SkinLibrary | None = None, progress: ProgressCallback | None = None,
) -> ScenePlan:
    progress = progress or _no_progress
    progress(0.0, "Reading map")
    asset = load(filepath, resolver=resolver, recursive=False)
    if not isinstance(asset, Map):
        raise ValueError(f"Expected Map, got {type(asset).__name__}: {filepath}")
    return plan_asset(asset, resolver=resolver, options=options, skins=skins, progress=progress)


def plan_asset(
    map_asset: Map, *, resolver: Resolver, options: ImportOptions | None = None,
    skins: SkinLibrary | None = None, progress: ProgressCallback | None = None,
) -> ScenePlan:
    # Deferred: NICE is importable only once the addon has set up sys.path.
    from NICE.src.parser import shared_external_cache

    # Block infos and their prefabs reference the same meshes (Stadium256
    # stands: 18 s per parse), so parse each external once per import.
    with shared_external_cache():
        return _plan_asset(map_asset, resolver, options, skins, progress)


def _plan_asset(
    map_asset: Map, resolver: Resolver, options: ImportOptions | None,
    skins: SkinLibrary | None, progress: ProgressCallback | None,
) -> ScenePlan:
    progress = progress or _no_progress
    options = options or ImportOptions()
    plan = ScenePlan(map_asset.map_name or "Map", map_asset.source_path or "", options)
    plan.color_palette = getattr(map_asset, "color_palette", "") or "Classic"
    plan.map_info = getattr(map_asset, "info", None)
    plan.thumbnail = getattr(map_asset, "thumbnail", b"") or b""
    plan.decoration = map_asset.decoration or ""
    plan.decoration_collection = map_asset.decoration_collection or ""
    plan.map_size = tuple(map_asset.map_size)
    plan.day_time = getattr(map_asset, "day_time", None)
    parser_errors = getattr(map_asset.raw, "_errors", ()) or ()
    if parser_errors:
        plan.issue("parser_diagnostics", plan.source_path, "; ".join(map(str, parser_errors)))
    from GBXBPY.load.map import _CHUNK_CORE, body_dict

    if getattr(map_asset.raw, "body", None) is not None and _CHUNK_CORE not in body_dict(map_asset.raw):
        # NICE stopped before the block chunk; an empty scene would look
        # like a successful import.
        plan.issue("map_body_incomplete", plan.source_path,
                   "the map body could not be parsed up to the block list (chunk 0x0304301F); "
                   "blocks and items are missing")
    dependencies = Dependencies(resolver, plan, map_asset.embedded_items)
    dependencies.blocks.collection = map_asset.decoration_collection
    if options.apply_deco_height:
        try:
            plan.decoration_height_m = resolve_absolute_height(map_asset, resolver)
        except (GbxbpyError, OSError) as exc:
            plan.issue("decoration_height_unavailable", plan.source_path,
                       f"{exc}; absolute placements use only the map offset")
            plan.decoration_height_m = (
                map_asset.deco_base_height_offset
                * collection_grid(resolver.gamedata_dir, map_asset.decoration_collection)[1]
            )
    if plan.decoration_collection:
        plan.terrain_material_remap = resolve_terrain_material_remap(plan.decoration_collection, resolver)
    if options.apply_texture_mod and (map_asset.mod_pack_path or map_asset.mod_pack_url):
        progress(0.01, "Loading texture mod")
        plan.texture_mod = plan_texture_mod(map_asset, skins, plan)
    if options.club_textures:
        from ..resolve.club_textures import load_club_textures
        from ..resolve.skins import default_cache_dir
        import urllib.error

        cache = skins.cache_dir if skins is not None else default_cache_dir()
        try:
            plan.club_textures, problems = load_club_textures(
                options.club_textures, os.path.join(cache, "ClubTextures"),
                progress=lambda f, m: progress(0.01 + 0.009 * f, m),
            )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            plan.issue("club_textures_unavailable", str(options.club_textures), str(exc))
        else:
            for problem in problems:
                plan.issue("club_textures_unavailable", str(options.club_textures), problem)
    library = AssetLibrary(dependencies, skins)
    records = []
    occupancy = []
    started = time.perf_counter()
    lists = [("Blocks", map_asset.blocks)]
    inherited: dict[int, tuple[str, ...]] = {}
    plan_start = 0.05
    if options.import_baked:
        lists.append(("BakedBlocks", map_asset.baked_blocks))
        # This pass reads (and caches) every block definition first, so it
        # takes the share of the bar the block loop would otherwise need.
        plan_start = 0.6
        progress(0.02, "Resolving baked modifiers")
        inherited = baked_modifiers(
            map_asset, dependencies,
            progress=lambda f, m: progress(0.02 + (plan_start - 0.02) * f, m),
        )
    total = max(1, sum(len(blocks) for _section, blocks in lists))
    done = 0
    for section, blocks in lists:
        for index, block in enumerate(blocks):
            done += 1
            progress(plan_start + (0.8 - plan_start) * done / total, f"Planning blocks {done}/{total}")
            if block.is_free:
                continue
            plan_grid_block(
                block, f"{section}_{index:05d}_{block.name}", section, library,
                inherited.get(index, ()) if section == "BakedBlocks" else (),
                records, occupancy,
            )
    log.info("V4 definitions/placements planned in %.2fs", time.perf_counter() - started)
    if options.import_baked:
        hide_ghost_baked_duplicates(map_asset.baked_blocks, plan)
    hide_occupied_terrain(occupancy)
    if not uses_baked_clips(map_asset, options):
        progress(0.8, "Planning clips")
        plan_clips(records, library)
    hide_clip_covered_grass(occupancy, plan)

    progress(0.85, "Planning free blocks and items")
    plan_absolute(map_asset, library)
    if options.import_decoration:
        progress(0.9, "Planning decoration surroundings")
        plan_decoration_map(map_asset, dependencies, skins)
    if options.import_lighting:
        progress(0.95, "Resolving decoration mood")
        plan.environment = plan_environment(map_asset, dependencies)
    plan.validate()
    progress(1.0, "Plan complete")
    log.info(
        "V4 plan: %d placements, %d definitions, %d clip instances, %d diagnostics (%.2fs)",
        len(plan.placements), len(plan.assets), sum(len(p.clips) for p in plan.placements),
        len(plan.diagnostics), time.perf_counter() - started,
    )
    return plan
