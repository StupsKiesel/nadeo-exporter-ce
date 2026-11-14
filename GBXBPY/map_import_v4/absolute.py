"""Free-block and anchored-item geometry with explicit absolute transforms."""

from __future__ import annotations

from ..color_tables import element_color_index
from ..model.block import Block
from ..model.clip import Clip
from ..model.item import Item
from ..model.map import Map, MapBlock
from .assets import AssetLibrary, has_mobil_geometry, pick_variant
from .clips import plan_clips, uses_baked_clips
from .placement import absolute_matrix, multiply, translation
from .scene import Placement


def plan_absolute(map_asset: Map, library: AssetLibrary) -> None:
    dependencies = library.dependencies
    plan = library.plan
    height = plan.decoration_height_m
    for index, block in enumerate(map_asset.free_blocks):
        if block.is_baked and not plan.options.import_baked:
            continue
        placement = Placement(
            name=f"FreeBlocks_{index:05d}_{block.name}", asset_id=None, section="FreeBlocks",
            matrix=absolute_matrix(block.position, block.rotation, 1.0, height),
            metadata={
                "tm_block_name": block.name, "tm_block_is_ground": block.is_ground,
                "tm_block_is_baked": block.is_baked,
                "tm_block_is_free": True,
                "tm_block_variant_index": block.block_variant_index,
                "tm_block_mobil_index": block.mobil_index,
                "tm_block_mobil_variant_index": block.mobil_variant_index,
                "tm_block_color": block.color, "tm_block_is_ghost": block.is_ghost,
                "tm_color_index": element_color_index(block.color),
            },
        )
        plan.placements.append(placement)
        kind = "ground" if block.is_ground else "air"
        path, asset = dependencies.stock_block(
            block.name, False, kind, block.block_variant_index,
            block.mobil_index, block.mobil_variant_index,
        )
        if path is None:
            plan.issue("missing_block", block.name, "Free block definition not found in map resources or GameData")
            continue
        if isinstance(asset, Item) and asset.custom_block is not None:
            _archetype, variant = library.custom_block_archetype(path, asset, kind, block.block_variant_index)
            definition = library.custom_block_definition(
                path, asset, kind, block.block_variant_index, variant,
            )
            placement.asset_id = definition.asset_id
            placement.metadata["tm_custom_block_archetype"] = asset.custom_block.archetype_id
            continue
        if not isinstance(asset, (Block, Clip)):
            if asset is not None:
                plan.issue("unsupported_block", path, f"Expected Block/Clip, got {type(asset).__name__}")
            continue
        variant = pick_variant(asset, kind, block.block_variant_index)
        if variant is None:
            plan.issue("missing_variant", path, f"No {kind} variant")
            continue
        definition = library.definition(
            path, variant, block.mobil_index,
            mobil_variant_index=block.mobil_variant_index,
            context=library.material_context(asset, block.skin, path),
        )
        placement.asset_id = definition.asset_id
        # Free blocks have no grid neighbours; only internal unit seams are culled.
        if not uses_baked_clips(map_asset, plan.options):
            plan_clips([(placement, MapBlock(is_ground=block.is_ground), variant, path)], library)
        if not has_mobil_geometry(variant) and not placement.clips:
            plan.issue("empty_variant", path, f"No geometry for free {kind}[{variant.index}]")

    for index, item in enumerate(map_asset.anchored_items):
        matrix = absolute_matrix(item.position, item.rotation, item.scale, height)
        placement = Placement(
            name=f"AnchoredItems_{index:05d}_{item.item_model_id}",
            asset_id=None, section="AnchoredItems",
            matrix=multiply(matrix, translation(item.pivot.x, item.pivot.y, item.pivot.z)),
            metadata={
                "tm_item_model_id": item.item_model_id,
                "tm_item_collection": item.item_model_collection,
                "tm_item_author": item.item_model_author,
                "tm_item_variant_index": item.variant_index,
                "tm_item_color": item.color,
                "tm_color_index": element_color_index(item.color),
                "tm_item_anim_phase_offset": item.anim_phase_offset,
                "tm_item_flags": item.flags, "tm_item_anchor_tree_id": item.anchor_tree_id,
            },
        )
        plan.placements.append(placement)
        path = dependencies.item(item.item_model_id, item.item_model_collection)
        if path is None:
            continue
        asset = dependencies.read(path)
        if isinstance(asset, Item):
            definition = library.item_definition(path, asset, item.variant_index)
            placement.asset_id = definition.asset_id
            if item.anim_phase_offset and asset.embedded_prefab is not None:
                plan.issue("unsupported_animation_phase", path, "Per-instance item animation phase is not applied")
        elif asset is not None:
            plan.issue("unsupported_item", path, f"Expected Item, got {type(asset).__name__}")
