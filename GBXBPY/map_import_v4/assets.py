"""Reusable, variant-specific definitions with already loaded domain geometry."""

from __future__ import annotations

import os
import random

from .. import library

from ..load.fx import FX_SYSTEM_CLASS_ID, read_fx_system
from ..load.mesh import strip_grass_fins
from ..model.block import Block, BlockMobil, BlockVariant
from ..model.asset import Asset
from ..model.clip import Clip
from ..model.map import BlockSkin
from ..model.mesh import Mesh
from ..model.item import AIR_VARIANT_KEY_OFFSET, CustomBlock, Item
from ..model.prefab import Prefab, PrefabEntity
from ..model.vegetation import VegetationModel
from ..resolve.modifier_lookup import (
    resolve_modifier_folder, resolve_modifier_materials, resolve_modifier_renames,
)
from ..resolve.skins import ResolvedSkin, SkinLibrary
from .dependencies import Dependencies
from .placement import footprint, mobil_matrix
from .scene import AssetDefinition, GeometryPart, MaterialContext


def pick_variant(asset: Block | Clip, kind: str, index: int) -> BlockVariant | None:
    variants = [v for v in asset.all_variants() if v.kind == kind]
    return next((v for v in variants if v.index == index), variants[0] if variants else None)


def has_mobil_geometry(variant: BlockVariant) -> bool:
    return any(m.solid_path or m.prefab_path for group in variant.mobils for m in group)


def model_stem(reference: str) -> str:
    """``...\\BushBigA.VegetTreeModel.Gbx`` -> ``BushBigA``."""
    return os.path.basename(reference.replace("\\", "/")).split(".", 1)[0]


def _has_geometry(mesh: Mesh) -> bool:
    return any(lod.vertices and any(s.indices for s in lod.submeshes) for lod in mesh.lods)


def matching_variants(item: Item, required: dict[str, str]) -> list:
    """Item variants whose tags contain every ``required`` key/value pair."""
    return [
        variant for variant in item.entity_variants
        if variant.entity_model_path
        and all(dict(variant.tags).get(key) == value for key, value in required.items())
    ]


def slot_layout(prefab: Prefab) -> int | None:
    """The ``iLayout`` whose placement slots are filled for ``prefab``.

    Vista prefabs carry several complete alternative layouts (Land base:
    0-3 palm forest, 4-5 bush grove, 6-7 edge strips). The game's choice is
    unknown, so one is drawn from an RNG seeded by prefab name, which keeps
    repeated imports stable instead of stacking every layout.
    """
    layouts = sorted({e.placement_layout for e in prefab.entities if e.placement_options})
    if not layouts:
        return None
    return layouts[random.Random(f"{(prefab.name or '').casefold()}#layout").randrange(len(layouts))]


def select_mobil_group(variant: BlockVariant, index: int) -> int:
    """Return ``mobils[mobilIndex]``, even when that group has no geometry.

    Stacked pillars and VFC walls keep a deliberately empty group (the ``-``
    slot after Top/Bottom pieces) for cells covered by a taller ``*Air2..32``
    piece below; the map selects it to draw nothing there. Only an index
    outside the variant falls back to the first group with geometry.
    """
    def has_paths(group: list[BlockMobil]) -> bool:
        return any(m.solid_path or m.prefab_path for m in group)

    if 0 <= index < len(variant.mobils):
        return index
    return next((i for i, group in enumerate(variant.mobils) if has_paths(group)), -1)


def select_mobil_alternative(group: list[BlockMobil], index: int) -> int:
    """Pick one entry of a mobil group: ``mobils[mobilIndex][mobilVariantIndex]``.

    Entries of a group are interchangeable alternatives of the same piece
    (``*_AirB``, ``*V2``), not parts to combine; the map stores which one
    the game shows in the block's ``mobilVariantIndex`` flag bits.
    """
    def has_path(mobil: BlockMobil) -> bool:
        return bool(mobil.solid_path or mobil.prefab_path)

    if 0 <= index < len(group) and has_path(group[index]):
        return index
    return next((i for i, mobil in enumerate(group) if has_path(mobil)), -1)


class AssetLibrary:
    def __init__(self, dependencies: Dependencies, skins: SkinLibrary | None = None) -> None:
        self.dependencies = dependencies
        self.plan = dependencies.plan
        self.skins = skins
        self._audited_prefabs: set[int] = set()
        self._contexts: dict[tuple, MaterialContext] = {}

    def material_context(
        self, asset: Block | Clip | Item, skin: BlockSkin | None, source: str,
        inherited: tuple[str, ...] = (),
    ) -> MaterialContext:
        """Material redirection for one placed block or clip.

        Modifiers come from the block info, followed by ``inherited`` ones a
        baked filler takes from the block it finishes (see
        :mod:`.inherited_modifiers`). A modifier with an empty
        ``RemapFolder`` (``TrackWallFromParent``) takes its folder from the
        skin text, which names a sibling folder (``PlatformGrass\\``).
        """
        modifiers = tuple(dict.fromkeys(
            path for path in (
                getattr(asset, "material_modifier_path", ""),
                getattr(asset, "material_modifier2_path", ""),
                *inherited,
            ) if path
        ))
        cache_key = (modifiers, skin)
        cached = self._contexts.get(cache_key)
        if cached is not None:
            return cached
        resolver = self.dependencies.resolver
        folders: list[str] = []
        allowed: list[tuple[str, ...] | None] = []
        renames: list[tuple[tuple[str, str], ...]] = []
        for path in modifiers:
            folder = resolve_modifier_folder(path, resolver)
            if not folder and skin is not None and skin.text:
                folder = self._skin_text_folder(path, skin.text, source)
            if not folder:
                continue
            materials = resolve_modifier_materials(path, resolver)
            pairs = resolve_modifier_renames(path, resolver)
            keys = [f.casefold() for f in folders]
            if folder.casefold() in keys:
                i = keys.index(folder.casefold())
                previous = allowed[i]
                allowed[i] = None if previous is None or materials is None else tuple(
                    dict.fromkeys(previous + materials)
                )
                renames[i] = tuple(dict.fromkeys(renames[i] + pairs))
                continue
            folders.append(folder)
            allowed.append(materials)
            renames.append(pairs)
        context = MaterialContext(
            tuple(folders),
            self._skin(skin.pack_path, skin.pack_url, source) if skin else None,
            self._skin(skin.foreground_path, skin.foreground_url, source) if skin else None,
            tuple(allowed),
            tuple(renames),
        )
        self._contexts[cache_key] = context
        return context

    def _skin_text_folder(self, modifier: str, text: str, source: str) -> str:
        resolver = self.dependencies.resolver
        modifier_file = resolver.resolve(modifier)
        if modifier_file:
            folder = os.path.join(os.path.dirname(modifier_file), *text.replace("\\", "/").split("/"))
            if library.isdir(folder):
                return resolver.to_gamedata_relative(folder)
        self.plan.issue("missing_skin_folder", source, f"Skin text {text!r} names no folder next to {modifier}")
        return ""

    def _skin(self, path: str, url: str, source: str) -> ResolvedSkin | None:
        if self.skins is None:
            return None
        if not path and url:
            path = url.rsplit("/", 1)[-1]
        if not path:
            return None
        resolved = self.skins.resolve(path, url)
        if resolved is None:
            self.plan.issue("missing_skin", path, self.skins.problems.get(path, "Skin not found"))
        return resolved

    def vegetation_mesh(self, model: VegetationModel, source: str) -> Mesh | None:
        # VegetTreeModel's loader exposes one visual variant containing both LODs.
        if len(model.variants) == 1:
            mesh = model.variants[0].mesh
            lod = mesh.lod(self.plan.options.lod_level)
            if lod is not None and lod.vertices and any(s.indices for s in lod.submeshes):
                return mesh
        self.plan.issue(
            "unsupported_vegetation", source,
            "Expected one vegetation variant with nonempty geometry at the requested LOD",
        )
        return None

    def audit_prefab(self, prefab: Prefab, source: str) -> None:
        if id(prefab) in self._audited_prefabs:
            return
        self._audited_prefabs.add(id(prefab))
        options = self.plan.options
        layout = slot_layout(prefab)
        for index, entity in enumerate(prefab.entities):
            if entity.mesh is not None and not options.import_grass_fins:
                if strip_grass_fins(entity.mesh) and not _has_geometry(entity.mesh):
                    entity.mesh = None
                    continue
            if entity.placement_group is not None:
                self.populate_spectators(prefab, index, entity, source)
                continue
            if (
                entity.placement_options and entity.mesh is None
                and entity.sub_prefab is None and not entity.model_path
            ):
                if (
                    options.import_vista_vegetation and options.fill_vegetation_slots
                    and entity.placement_layout == layout
                ):
                    self.fill_placement(prefab, index, entity, source)
                continue
            if (
                entity.mesh is None and entity.sub_prefab is None and entity.model_path
                and entity.model_class_id == 0x2F086000
            ):
                self.vegetation_entity(entity, entity.model_path, source)
                continue
            if entity.sub_prefab is not None:
                self.audit_prefab(entity.sub_prefab, source)
            elif entity.mesh is None and entity.model_path and entity.model_class_id == FX_SYSTEM_CLASS_ID:
                self.fx_entity(entity, source)
            elif entity.mesh is None and entity.anim_driver is None and entity.model_path:
                self.plan.issue(
                    "unsupported_prefab_reference", source,
                    f"Entity {entity.name or index} references {entity.model_path} "
                    "without inline geometry; shared stock prefab builder skips it",
                )

    def fx_entity(self, entity: PrefabEntity, source: str) -> None:
        """Attach a decoded ``CPlugFxSystem`` (fogger/sparkler particles) to ``entity``."""
        path = self.dependencies.resolve_path(entity.model_path, source)
        fx = read_fx_system(path) if path else None
        if fx is None:
            if path:
                self.plan.issue("fx_unreadable", path, "FxSystem could not be decoded (pack-encrypted?)")
            return
        for emitter in fx.emitters:
            if emitter.model is None or not emitter.model.emissions:
                self.plan.issue(
                    "fx_emitter_unreadable", emitter.model_path or path,
                    f"Particle model of emitter {emitter.name} could not be decoded",
                )
        entity.fx = fx

    def vegetation_entity(
        self, entity: PrefabEntity, reference: str, source: str, tags: dict[str, str] | None = None,
    ) -> None:
        """Load a VegetTreeModel onto ``entity``, or mark it when geometry is unusable.

        Vista ``*.VegetTreeModel.Gbx`` files ship truncated (mesh data is
        streamed), so their trees and bushes become markers named after the
        model when :attr:`ImportOptions.import_vista_vegetation` is set.
        """
        path, model = self.dependencies.reference(reference, source)
        if isinstance(model, VegetationModel):
            entity.mesh = self.vegetation_mesh(model, path)
        elif model is not None:
            self.plan.issue("unsupported_vegetation", path, f"Expected vegetation, got {type(model).__name__}")
        if entity.mesh is None and self.plan.options.import_vista_vegetation:
            entity.marker_name = model_stem(reference)
            entity.marker_tags = dict(tags or {})

    def fill_placement(self, prefab: Prefab, index: int, entity: PrefabEntity, source: str) -> None:
        """Resolve an ``NPlugItemPlacement_SPlacement`` slot to one vegetation model.

        The slot's ``RequiredTags`` (``Placement``/``Type``/``Size``) select
        the variants of ``<Placement>.Item.Gbx`` whose tags contain them all.
        The game chooses at runtime; we draw option and variant from an RNG
        seeded by prefab name and entity index so repeated imports are stable.
        """
        rng = random.Random(f"{(prefab.name or '').casefold()}#{index}")
        option = entity.placement_options[rng.randrange(len(entity.placement_options))]
        placement = option.get("Placement", "")
        if not placement:
            self.plan.issue("unsupported_placement", source, f"Placement slot without Placement tag: {option}")
            return
        item_path, item = self.dependencies.placement_item(placement, source)
        if not isinstance(item, Item):
            if item is not None:
                self.plan.issue("unsupported_placement", item_path, f"Expected item, got {type(item).__name__}")
            return
        candidates = matching_variants(item, option)
        if not candidates:
            self.plan.issue("unmatched_placement", item_path, f"No variant matches {option}")
            return
        variant = candidates[rng.randrange(len(candidates))]
        self.vegetation_entity(entity, variant.entity_model_path, item_path, dict(variant.tags))

    def populate_spectators(self, prefab: Prefab, index: int, entity: PrefabEntity, source: str) -> None:
        """Fill ``Type=Spectator`` seats with a synthetic sub-prefab.

        A fixed shuffled seat order sets the count, rounded down per group.
        Variants are drawn for every seat so increasing occupancy only adds
        spectators. Identical prefabs share a layout; imports are stable.
        """
        options = self.plan.options
        group = entity.placement_group
        if not options.import_spectators or group is None:
            return
        seats = [
            i for i in range(len(group.slots))
            if any(tags.get("Type") == "Spectator" for tags in group.slot_tags(i))
        ]
        if not seats:
            return
        variants = [
            mesh for _path, mesh in self.dependencies.spectator_meshes()
            if isinstance(mesh, Mesh) and mesh.lod(options.lod_level) is not None
        ]
        if not variants:
            self.plan.issue(
                "missing_spectators", source,
                f"No Spectators\\*.Mesh.Gbx with LOD {options.lod_level} in GameData",
            )
            return
        rng = random.Random(f"{(prefab.name or '').casefold()}#{index}")
        shuffled = seats.copy()
        rng.shuffle(shuffled)
        occupied = set(shuffled[:int(len(seats) * options.spectator_occupancy)])
        entities = []
        for seat in seats:
            variant = variants[rng.randrange(len(variants))]
            if seat in occupied:
                entities.append(PrefabEntity(
                    name=f"Spectator{seat:03d}", mesh=variant, transform=group.slots[seat],
                ))
        entity.sub_prefab = Prefab(name=f"{prefab.name or 'Prefab'} Spectators", entities=entities)

    def part(self, mobil: BlockMobil, source: str, name: str, *, clip: bool = False) -> GeometryPart:
        for ref in (mobil.solid_path, mobil.prefab_path):
            if not ref:
                continue
            path, payload = self.dependencies.reference(ref, source)
            if isinstance(payload, (Mesh, Prefab)):
                if isinstance(payload, Prefab):
                    self.audit_prefab(payload, path)
                elif not self.plan.options.import_grass_fins:
                    strip_grass_fins(payload)
                return GeometryPart(name, path, payload, mobil_matrix(mobil, clip=clip))
            if payload is not None:
                self.plan.issue("unsupported_geometry", path, f"Expected mesh or prefab, got {type(payload).__name__}")
        self.plan.issue("missing_geometry", source, f"No usable geometry for {name}")
        return GeometryPart(name, source, None, mobil_matrix(mobil, clip=clip))

    def definition(
        self, source: str, variant: BlockVariant, mobil_index: int, *, clip: bool = False,
        context: MaterialContext | None = None, mobil_variant_index: int = 0,
    ) -> AssetDefinition:
        group = select_mobil_group(variant, mobil_index)
        alternative = (
            -1 if group < 0
            else select_mobil_alternative(variant.mobils[group], mobil_variant_index)
        )
        context = context or MaterialContext()
        key = repr((os.path.normcase(os.path.abspath(source)), variant.kind, variant.index, group, clip))
        if alternative > 0:
            key = repr((key, "alt", alternative))
        if context:
            key = repr((key, context))
        if key in self.plan.assets:
            return self.plan.assets[key]
        definition = AssetDefinition(
            key, os.path.basename(source), source, variant.kind, variant.index,
            group, footprint(variant), max(alternative, 0), material_context=context,
            is_embedded=self.dependencies.is_embedded(source),
        )
        self.plan.assets[key] = definition
        if alternative >= 0 and alternative != mobil_variant_index:
            self.plan.issue(
                "missing_mobil_variant", source,
                f"Requested mobil variant {mobil_variant_index} of group {group}; using {alternative}",
            )
        if alternative >= 0:
            mobil = variant.mobils[group][alternative]
            definition.parts.append(self.part(mobil, source, f"mobil{alternative:02d}", clip=clip))
            definition.is_embedded |= self.dependencies.is_embedded(definition.parts[-1].source_path)
        return definition

    def custom_block_archetype(
        self, source: str, item: Item, kind: str, variant_index: int,
    ) -> tuple[Block | None, BlockVariant | None]:
        """Stock block info a custom block is built on; it supplies units and terrain zones."""
        custom = item.custom_block
        if custom is None or not custom.archetype_id:
            self.plan.issue("missing_custom_block_archetype", source, "Custom block names no archetype")
            return None, None
        path, archetype = self.dependencies.stock_block(custom.archetype_id, False, kind, variant_index, 0, 0)
        if not isinstance(archetype, Block):
            self.plan.issue(
                "missing_custom_block_archetype", source,
                f"Archetype {custom.archetype_id} ({custom.archetype_collection}) not found",
            )
            return None, None
        return archetype, pick_variant(archetype, kind, variant_index)

    def custom_block_definition(
        self, source: str, item: Item, kind: str, variant_index: int, archetype: BlockVariant | None,
    ) -> AssetDefinition:
        """Geometry of a custom block placement: its customized crystal for that variant.

        An empty crystal (``VOID`` blocks) yields a definition without parts.
        """
        custom = item.custom_block
        selected = custom.variant_key(kind, variant_index) if custom is not None else None
        size = footprint(archetype) if archetype is not None else (1, 1, 1)
        key = repr((os.path.normcase(os.path.abspath(source)), "custom_block", selected, size))
        if key in self.plan.assets:
            return self.plan.assets[key]
        definition = AssetDefinition(
            key, item.name or "CustomBlock", source, kind,
            variant_index if selected is None else selected % AIR_VARIANT_KEY_OFFSET, 0, size,
            is_embedded=self.dependencies.is_embedded(source),
        )
        self.plan.assets[key] = definition
        if selected is None:
            self.plan.issue("unsupported_item_geometry", source, "Custom block has no customized variants")
        elif selected != CustomBlock.key(kind, variant_index):
            self.plan.issue(
                "missing_custom_block_variant", source,
                f"No {kind} variant {variant_index}; using customized variant {selected}",
            )
        parts = custom.variants.get(selected, []) if custom is not None and selected is not None else []
        for index, part in enumerate(parts):
            definition.parts.append(GeometryPart(
                f"custom_block_{selected}_{index}", source, part.mesh, part.matrix, semantics=part,
            ))
        return definition

    def item_definition(self, source: str, item: Item, variant_index: int) -> AssetDefinition:
        selected = variant_index
        if item.embedded_prefab is not None or item.parts:
            selected = 0
        elif item.entity_variants and not 0 <= selected < len(item.entity_variants):
            self.plan.issue(
                "missing_item_variant", source,
                f"Requested item variant {variant_index}; using variant 0",
            )
            selected = 0
        key = repr((os.path.normcase(os.path.abspath(source)), "item", selected))
        if key in self.plan.assets:
            return self.plan.assets[key]
        definition = AssetDefinition(
            key, item.name or "Item", source, "item", selected, 0, (1, 1, 1),
            material_context=self.material_context(item, None, source),
            item_placement=item.default_placement,
            item_data=item,
            is_embedded=self.dependencies.is_embedded(source),
        )
        self.plan.assets[key] = definition
        if item.parts and item.embedded_prefab is None:
            for part in item.parts:
                definition.parts.append(GeometryPart(
                    part.name, source, part.mesh, part.matrix, semantics=part,
                ))
            return definition
        payload: Asset | None = item.embedded_prefab
        geometry_source = source
        if payload is None and item.entity_variants:
            reference = item.entity_variants[selected].entity_model_path
            if reference:
                geometry_source, payload = self.dependencies.reference(reference, source)
        if isinstance(payload, VegetationModel):
            payload = self.vegetation_mesh(payload, geometry_source)
        definition.is_embedded |= self.dependencies.is_embedded(geometry_source)
        if isinstance(payload, (Mesh, Prefab)):
            if isinstance(payload, Prefab):
                self.audit_prefab(payload, geometry_source)
            elif not self.plan.options.import_grass_fins:
                strip_grass_fins(payload)
            definition.parts.append(GeometryPart("item_geometry", geometry_source, payload))
        else:
            self.plan.issue(
                "unsupported_item_geometry", source,
                f"No supported mesh/prefab for item variant {selected}"
                + (f" (got {type(payload).__name__})" if payload is not None else ""),
            )
            definition.parts.append(GeometryPart("missing_item_geometry", geometry_source, None))
        return definition
