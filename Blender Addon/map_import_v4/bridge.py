"""Execute a validated V4 plan using the existing Blender geometry builders."""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import time
import uuid

import bpy
from mathutils import Matrix, Vector

from GBXBPY.map_import_v4.scene import Instance, ScenePlan
from GBXBPY.model.mesh import Mesh
from GBXBPY.model.prefab import Prefab
from .._text import plain_name

log = logging.getLogger("trackmania_blender_bridge.map_import_v4")


def _is_identity(matrix, tolerance: float = 1e-6) -> bool:
    return all(
        abs(matrix[r][c] - (1.0 if r == c else 0.0)) <= tolerance
        for r in range(4) for c in range(4)
    )


def _no_progress(_fraction: float, _message: str) -> None:
    pass


def build(plan: ScenePlan, *, context, resolver, progress=None) -> bpy.types.Collection:
    """Build isolated map placement scene and assets in a shared GameData scene.

    ``progress(fraction, message)`` is called with ``fraction`` in 0..1.
    Runs to completion; :class:`Builder` runs the same steps incrementally.
    """
    builder = Builder(plan, context=context, resolver=resolver, progress=progress)
    while not builder.step(budget=None):
        pass
    return builder.root


# ID collections an import may add to; used to undo a cancelled build.
_ID_COLLECTIONS = (
    "objects", "collections", "meshes", "materials", "images", "node_groups",
    "lights", "cameras", "curves", "textures", "scenes", "worlds",
)


def _snapshot() -> set[int]:
    return {
        block.as_pointer()
        for name in _ID_COLLECTIONS
        for block in getattr(bpy.data, name)
    }


def _build_environment(plan: ScenePlan, scene, root) -> None:
    """Sun (or moon) light and world colour from the decoration mood."""
    if plan.environment is None:
        return
    apply_environment(
        plan.environment, scene, root, plan.name,
        issue=lambda message: plan.issue("sky_failed", plan.source_path, message),
    )


def _remove_environment(scene, root) -> None:
    """Drop the root's Lighting collection (sun, clouds) and the scene's TM mood world."""
    for child in list(root.children):
        if child.get("tm_section") != "Lighting":
            continue
        for obj in list(child.objects):
            data = obj.data
            groups = [m.node_group for m in getattr(obj, "modifiers", ()) if getattr(m, "node_group", None)]
            materials = list(getattr(data, "materials", ()) or ())
            bpy.data.objects.remove(obj)
            if data is not None and data.users == 0:
                if isinstance(data, bpy.types.Light):
                    bpy.data.lights.remove(data)
                elif isinstance(data, bpy.types.Mesh):
                    bpy.data.meshes.remove(data)
            for group in groups:
                if group.users == 0:
                    bpy.data.node_groups.remove(group)
            for material in materials:
                if material is not None and material.users == 0:
                    bpy.data.materials.remove(material)
        bpy.data.collections.remove(child)
    world = scene.world
    if world is not None and "tm_mood" in world:
        scene.world = None
        if world.users == 0:
            bpy.data.worlds.remove(world)


def apply_environment(environment, scene, root, name: str, *, issue=None) -> None:
    """Replace the mood light and world of ``root``/``scene`` with ``environment``."""
    _remove_environment(scene, root)
    root["tm_mood"] = environment.mood
    root["tm_day_time_applied"] = environment.day_time
    light_name = "Moon" if environment.is_moon else "Sun"
    light = bpy.data.lights.new(f"TM {light_name} {name}", type="SUN")
    light.color = environment.light_color
    light.energy = environment.light_strength
    obj = bpy.data.objects.new(f"TM {light_name}", light)
    # A sun shines along its local -Z, so +Z points towards the light.
    obj.rotation_euler = Vector(environment.light_direction).to_track_quat("Z", "Y").to_euler()
    obj.location = (0.0, 0.0, 300.0)
    obj["tm_decoration"] = environment.decoration
    obj["tm_mood"] = environment.mood
    obj["tm_day_time"] = environment.day_time
    obj["tm_mood_path"] = environment.mood_path
    lighting = bpy.data.collections.new("Lighting")
    lighting["tm_section"] = "Lighting"
    root.children.link(lighting)
    lighting.objects.link(obj)

    if environment.sky is not None and environment.sky.clouds is not None:
        from .clouds import build_clouds

        size = list(root.get("tm_map_size", (48, 255, 48)))
        center = (size[0] * 16.0, -size[2] * 16.0) if len(size) >= 3 else (768.0, -768.0)
        fps = scene.render.fps / (scene.render.fps_base or 1.0)
        try:
            build_clouds(environment, lighting, center, name, fps)
        except Exception as exc:  # noqa: BLE001 - clouds must not fail the import
            log.exception("clouds failed")
            if issue is not None:
                issue(f"Clouds failed: {exc}")

    world = bpy.data.worlds.new(f"TM {environment.decoration or 'Mood'} {environment.mood}")
    world["tm_decoration"] = environment.decoration
    world["tm_mood"] = environment.mood
    world.color = environment.ambient
    if world.node_tree is None and hasattr(world, "use_nodes"):
        world.use_nodes = True
    if environment.sky is not None:
        from .sky import build_sky_world

        try:
            build_sky_world(world, environment)
            scene.world = world
            return
        except Exception as exc:  # noqa: BLE001 - the sky must not fail the import
            log.exception("sky failed")
            if issue is not None:
                issue(f"Sky failed: {exc}")
            tree = world.node_tree
            tree.nodes.clear()
            background = tree.nodes.new("ShaderNodeBackground")
            background.name = "Background"
            tree.links.new(background.outputs[0], tree.nodes.new("ShaderNodeOutputWorld").inputs["Surface"])
    background = world.node_tree.nodes.get("Background") if world.node_tree else None
    if background is not None:
        background.inputs["Color"].default_value = (*environment.ambient, 1.0)
        background.inputs["Strength"].default_value = 1.0
    scene.world = world

def _build_terrain_grass(plan: ScenePlan, context, root, library):
    """Grass billboards on visible terrain; returns the sprite collection or ``None``."""
    from .terrain_grass import build_terrain_grass

    try:
        return build_terrain_grass(
            plan, context.evaluated_depsgraph_get(), root, library,
            plan.options.terrain_grass_density,
        )
    except Exception as exc:  # noqa: BLE001 - decoration must not fail the import
        log.exception("terrain grass failed")
        plan.issue("terrain_grass_failed", plan.source_path, f"Terrain grass failed: {exc}")
        return None


class Builder:
    """Build a plan in slices so a modal operator can return to the event loop.

    Each :meth:`step` runs build steps (one asset or one placement each) under
    a context override for the new map scene until ``budget`` seconds pass.
    """

    def __init__(self, plan: ScenePlan, *, context, resolver, progress=None) -> None:
        plan.validate()
        self._before = _snapshot()
        self.root: bpy.types.Collection | None = None
        gamedata = next(
            (scene for scene in bpy.data.scenes if scene.get("tm_scene_role") == "GAMEDATA"),
            None,
        )
        if gamedata is None:
            gamedata = bpy.data.scenes.new("GameData")
            gamedata["tm_scene_role"] = "GAMEDATA"
        map_scene = bpy.data.scenes.new((plain_name(plan.name).strip() or "Map") + ".Map.Gbx")
        map_scene["tm_scene_role"] = "MAP"
        map_scene["tm_source_path"] = plan.source_path
        map_scene["tm_gamedata_scene"] = gamedata.name
        # Preserve animation timing and the invoking scene's world.
        map_scene.render.fps = context.scene.render.fps
        map_scene.render.fps_base = context.scene.render.fps_base
        if context.scene.world is not None:
            map_scene.world = context.scene.world
        self.gamedata = gamedata
        self.map_scene = map_scene
        self._steps = _build(
            plan, context=bpy.context, resolver=resolver, gamedata=gamedata,
            progress=progress or _no_progress,
        )

    def step(self, budget: float | None = 0.1) -> bool:
        """Run steps for ``budget`` seconds (``None``: until done); True when finished."""
        if self.root is not None:
            return True
        deadline = None if budget is None else time.perf_counter() + budget
        with bpy.context.temp_override(
            scene=self.map_scene, view_layer=self.map_scene.view_layers[0],
        ):
            try:
                while True:
                    next(self._steps)
                    if deadline is not None and time.perf_counter() >= deadline:
                        return False
            except StopIteration as done:
                self.root = done.value
        window = bpy.context.window
        if window is not None:
            window.scene = self.map_scene
        log.info("V4 scene split: assets=%s map=%s", self.gamedata.name, self.map_scene.name)
        return True

    def abort(self) -> int:
        """Remove every datablock created since construction; returns the count."""
        self._steps.close()
        window = bpy.context.window
        if window is not None and window.scene == self.map_scene:
            return 0
        created = [
            block
            for name in _ID_COLLECTIONS
            for block in getattr(bpy.data, name)
            if block.as_pointer() not in self._before
        ]
        bpy.data.batch_remove(created)
        return len(created)


def _store_map_info(plan: ScenePlan, root) -> None:
    """Header metadata, decoration and thumbnail on the root for the Map panel."""
    info = plan.map_info
    if info is not None:
        root["tm_map_info"] = json.dumps(asdict(info))
    root["tm_map_name"] = plan.name
    root["tm_decoration_height_m"] = plan.decoration_height_m
    root["tm_decoration"] = plan.decoration
    root["tm_decoration_collection"] = plan.decoration_collection
    root["tm_map_size"] = list(plan.map_size)
    root["tm_map_day_time"] = -1.0 if plan.day_time is None else float(plan.day_time)
    if plan.thumbnail:
        try:
            image = thumbnail_image(plan.thumbnail, f"TM Thumbnail {plan.name}")
        except Exception:  # noqa: BLE001 - a broken thumbnail must not fail the import
            log.warning("Map thumbnail unreadable", exc_info=True)
        else:
            root["tm_thumbnail"] = image.name


def thumbnail_image(jpeg: bytes, name: str):
    """Packed Blender image from the header JPEG, flipped upright.

    The game stores the JPEG upside down. The flipped pixels go into a
    generated image because ``pack()`` on a loaded file packs the original
    bytes and would drop the flip."""
    import os
    import tempfile

    import numpy

    handle, path = tempfile.mkstemp(suffix=".jpg")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(jpeg)
        source = bpy.data.images.load(path)
        width, height = source.size
        buffer = numpy.empty(width * height * 4, dtype=numpy.float32)
        source.pixels.foreach_get(buffer)
        bpy.data.images.remove(source)
        image = bpy.data.images.new(name, width, height)
        image.pixels.foreach_set(numpy.ascontiguousarray(buffer.reshape(height, width * 4)[::-1]).ravel())
        image.pack()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return image


def embedded_assets_collection(scene, root):
    """Return the map-owned source container, creating it when needed."""
    name = root.get("tm_embedded_asset_collection")
    library = (root.children.get(name) or scene.collection.children.get(name)) if name else None
    if library is None:
        library = next((coll for coll in (*root.children, *scene.collection.children)
                        if coll.get("tm_embedded") and coll.get("tm_map_id") == root.get("tm_map_id")), None)
    if library is None:
        library = bpy.data.collections.new("EmbeddedData")
        root.children.link(library)
        library["tm_source_path"] = root.get("tm_source_path", "")
        library["tm_map_scene"] = scene.name
        library["tm_map_id"] = root.get("tm_map_id", "")
        library["tm_import_status"] = root.get("tm_import_status", "COMPLETE")
        library["tm_embedded"] = True
    root["tm_embedded_asset_collection"] = library.name
    scene["tm_embedded_asset_collection"] = library.name
    return library


def collection_layer(layer, collection):
    if layer.collection == collection:
        return layer
    for child in layer.children:
        found = collection_layer(child, collection)
        if found is not None:
            return found
    return None


def exclude_embedded_assets(scene, library):
    """Hide source copies without hiding their collection instances."""
    for view_layer in scene.view_layers:
        view_layer.update()
        layer = collection_layer(view_layer.layer_collection, library)
        if layer is None:
            raise ValueError(f"Embedded collection unavailable in {view_layer.name}")
        layer.exclude = True


def _build(plan: ScenePlan, *, context, resolver, gamedata, progress):
    """Generator yielding after every asset and placement; returns the root collection."""
    from GBXMAT.images import set_path_override

    from .. import _gbxmat_adapter
    from .. import (
        _attach_extra_lod_objects,
        _attach_light_instances,
        _build_blender_mesh_from_gbxbpy,
        _build_extra_lod_meshes,
        _build_light_templates,
        _build_prefab_object_impl,
    )

    plan.validate()
    display_name = plain_name(plan.name).strip() or "Map"
    root = bpy.data.collections.new(display_name)
    context.scene.collection.children.link(root)
    root["tm_import_version"] = 4
    root["tm_map_id"] = uuid.uuid4().hex
    root["tm_source_path"] = plan.source_path
    root["tm_import_status"] = "BUILDING"
    _store_map_info(plan, root)
    library = bpy.data.collections.new(f"Library {display_name}")
    gamedata.collection.children.link(library)
    library["tm_source_path"] = plan.source_path
    library["tm_map_scene"] = context.scene.name
    embedded_library = None
    if any(definition.is_embedded for definition in plan.assets.values()):
        embedded_library = embedded_assets_collection(context.scene, root)
    root["tm_gamedata_scene"] = gamedata.name
    root["tm_asset_collection"] = library.name
    root["tm_map_scene"] = context.scene.name
    context.scene["tm_asset_collection"] = library.name
    context.scene["tm_import_status"] = "BUILDING"
    library["tm_import_status"] = "BUILDING"
    targets: dict[str, bpy.types.Collection] = {}
    mesh_cache: dict = {}
    prefab_cache: dict = {}
    prefab_sources: dict[bool, bpy.types.Collection] = {}
    sections: dict[str, bpy.types.Collection] = {}

    def report(level: str, message: str) -> None:
        log.warning("%s: %s", level, message)
        plan.issue("builder_diagnostic", plan.source_path, message)

    def _build_parts(definition, coll, material_context) -> None:
        from .._item_geometry import build_part, configure_part, attach_collision_parts
        from GBXBPY.model.geometry import MeshPart
        for part in definition.parts:
            payload = part.payload
            if part.semantics is not None and (part.semantics.role != "geometry" or payload is None):
                obj = build_part(part.semantics, plan.options.lod_level,
                                 resolver=resolver, source_path=part.source_path)
                coll.objects.link(obj)
                obj.matrix_local = Matrix(part.matrix)
                obj["tm_source_path"] = part.source_path
                continue
            lights = []
            extra_lods = []
            cache_key = (part.source_path, material_context, definition.is_embedded)
            if isinstance(payload, Mesh):
                # Custom blocks keep one mesh per customized variant in one file.
                cache_key = (part.source_path, id(payload), material_context, definition.is_embedded)
                cached = mesh_cache.get(cache_key)
                if cached is None:
                    obj = _build_blender_mesh_from_gbxbpy(
                        payload, plan.options.lod_level,
                        resolver=resolver, source_path=part.source_path,
                    )
                    lights = _build_light_templates(payload)
                    if plan.options.import_all_lods:
                        extra_lods = _build_extra_lod_meshes(
                            payload, plan.options.lod_level,
                            resolver=resolver, source_path=part.source_path,
                        )
                    mesh_cache[cache_key] = (obj.data, lights, extra_lods)
                else:
                    data, lights, extra_lods = cached
                    obj = bpy.data.objects.new(part.name, data)
            elif isinstance(payload, Prefab):
                prefab_coll = prefab_cache.get(cache_key)
                if prefab_coll is None:
                    sources = prefab_sources.get(definition.is_embedded)
                    if sources is None:
                        prefix = "Embedded Prefab Sources" if definition.is_embedded else "Prefab Sources"
                        sources = bpy.data.collections.new(f"{prefix} {display_name}")
                        owner = embedded_library if definition.is_embedded else library
                        assert owner is not None
                        owner.children.link(sources)
                        prefab_sources[definition.is_embedded] = sources
                    prefab_coll = bpy.data.collections.new(f"Prefab {plain_name(payload.name)}")
                    sources.children.link(prefab_coll)
                    _build_prefab_object_impl(
                        payload, context, plan.options.lod_level,
                        target_collection=prefab_coll, resolver=resolver,
                        source_path=part.source_path, report=report,
                        import_all_lods=plan.options.import_all_lods,
                    )
                    prefab_cache[cache_key] = prefab_coll
                if _is_identity(part.matrix):
                    # A child collection costs no instance level; Blender stops
                    # recursing at 8 and deep stock prefabs (checkpoints nest 5)
                    # otherwise warn "Maximum instance recursion level reached".
                    if coll.children.get(prefab_coll.name) is None:
                        coll.children.link(prefab_coll)
                    continue
                obj = bpy.data.objects.new(part.name, None)
                obj.instance_type = "COLLECTION"
                obj.instance_collection = prefab_coll
            elif payload is None:
                obj = bpy.data.objects.new(part.name, None)
                obj.empty_display_type = "CUBE"
                obj["tm_placeholder"] = True
            else:
                raise TypeError(f"Unsupported planned payload: {type(payload).__name__}")
            obj.name = part.name
            if isinstance(payload, Mesh):
                configure_part(obj, part.semantics or MeshPart(
                    name=part.name, mesh=payload, collidable=payload.collidable))
            coll.objects.link(obj)
            obj.matrix_local = Matrix(part.matrix)
            obj["tm_source_path"] = part.source_path
            if isinstance(payload, Mesh) and part.semantics is None:
                attach_collision_parts(obj, payload, coll, lod_level=plan.options.lod_level,
                                       resolver=resolver, source_path=part.source_path)
            if lights:
                _attach_light_instances(obj, lights, coll)
            if extra_lods:
                _attach_extra_lod_objects(
                    obj, payload, plan.options.lod_level, extra_lods, coll,
                )

    def instance(node: Instance, collection, parent=None):
        obj = bpy.data.objects.new(node.name, None)
        collection.objects.link(obj)
        obj.parent = parent
        obj.matrix_local = Matrix(node.matrix)
        # An ID pointer back to the containing collection creates depsgraph
        # cycles that overflow Blender's native stack on large maps.
        obj["tm_map_root"] = root["tm_map_id"]
        for key, value in node.metadata.items():
            obj[key] = value
        target = targets.get(node.asset_id)
        if target is not None:
            obj["tm_source_path"] = target.get("tm_source_path", "")
            obj.instance_type = "COLLECTION"
            obj.instance_collection = target
            obj.empty_display_type = "PLAIN_AXES"
        else:
            obj.empty_display_type = "CUBE"
            obj["tm_placeholder"] = True
        obj.empty_display_size = 0.5
        return obj

    try:
        root["tm_color_palette"] = plan.color_palette
        _gbxmat_adapter.set_color_palette(resolver, plan.color_palette)
        _gbxmat_adapter.set_terrain_material_remap(resolver, plan.terrain_material_remap)
        _gbxmat_adapter.reset_missing_textures()
        texture_mod = plan.texture_mod
        if texture_mod is not None:
            set_path_override(texture_mod.map_path)
            root["tm_texture_mod"] = texture_mod.source
            root["tm_texture_mod_archive"] = texture_mod.archive_path
        club = plan.club_textures
        if club is not None:
            def map_texture(path):
                replacement = club.map_path(path)
                if replacement != path:
                    return replacement
                return texture_mod.map_path(path) if texture_mod is not None else path

            set_path_override(map_texture)
            root["tm_club_id"] = club.club_id
            root["tm_club_name"] = club.name
            root["tm_club_textures_source"] = club.source
        asset_total = max(1, len(plan.assets))
        for asset_done, (key, definition) in enumerate(plan.assets.items(), 1):
            progress(0.85 * asset_done / asset_total, f"Building assets {asset_done}/{asset_total}")
            coll = bpy.data.collections.new(
                f"{plain_name(definition.name)} {definition.kind}{definition.variant_index} "
                f"m{definition.mobil_index}.{definition.mobil_variant_index}"
            )
            owner = embedded_library if definition.is_embedded else library
            assert owner is not None
            owner.children.link(coll)
            coll["tm_asset_id"] = key
            coll["tm_source_path"] = definition.source_path
            coll["tm_embedded"] = definition.is_embedded
            if definition.kind == "item":
                from .._asset_panel import stamp
                coll.gbx_kind = "ITEM"
                coll.gbx_item_data.source_file_path = definition.source_path
                stamp(coll, "Item", definition.source_path, definition.name)
                coll["tm_item_variant_index"] = definition.variant_index
            if definition.item_placement is not None:
                from .._asset_panel import stamp_item_placement
                stamp_item_placement(coll, definition.item_placement)
            if definition.item_data is not None:
                from .._item_geometry import configure_item_collection
                configure_item_collection(coll, definition.item_data, definition.source_path)
            targets[key] = coll
            # Modifier folders and skins change the materials, so geometry is
            # only shared between definitions with the same context.
            material_context = definition.material_context or None
            if material_context is not None:
                coll["tm_material_context"] = json.dumps({
                    "modifier_folders": list(material_context.modifier_folders),
                    "skin": material_context.skin.source if material_context.skin else "",
                    "foreground": (
                        material_context.foreground.source if material_context.foreground else ""
                    ),
                })
            _gbxmat_adapter.set_material_context(resolver, material_context)
            try:
                _build_parts(definition, coll, material_context)
            finally:
                _gbxmat_adapter.clear_material_context(resolver)
            yield

        placement_total = max(1, len(plan.placements))
        for placement_done, placement in enumerate(plan.placements, 1):
            progress(
                0.85 + 0.13 * placement_done / placement_total,
                f"Placing blocks and items {placement_done}/{placement_total}",
            )
            section = sections.get(placement.section)
            if section is None:
                section = bpy.data.collections.new(placement.section)
                section["tm_section"] = placement.section
                root.children.link(section)
                sections[placement.section] = section
            obj = instance(placement, section)
            objects = [obj] + [instance(clip, section, parent=obj) for clip in placement.clips]
            if placement.hidden:
                for hidden in objects:
                    hidden.hide_viewport = hidden.hide_render = True
            yield

        _build_environment(plan, context.scene, root)
        progress(0.98, "Updating view layer")
        context.view_layer.update()
        grass_sprites = None
        if plan.options.import_terrain_grass:
            progress(0.985, "Scattering terrain grass")
            grass_sprites = _build_terrain_grass(plan, context, root, library)
        stock_prefab_sources = prefab_sources.get(False)
        if stock_prefab_sources is not None:
            # Keep source geometry inspectable without drawing it twice in GameData.
            gamedata.view_layers[0].update()
            layer = gamedata.view_layers[0].layer_collection.children[library.name]
            layer.children[stock_prefab_sources.name].exclude = True
        if embedded_library is not None:
            # Hide definitions at their source origin, not their map instances.
            exclude_embedded_assets(context.scene, embedded_library)
        if grass_sprites is not None:
            gamedata.view_layers[0].update()
            layer = gamedata.view_layers[0].layer_collection.children[library.name]
            layer.children[grass_sprites.name].exclude = True

        missing = _gbxmat_adapter.missing_textures()
        root["tm_missing_textures"] = len(missing)
        if missing:
            log.warning(
                "V4 import: %d texture file(s) not found in the game library %s; their materials "
                "render untextured. Point 'Game Library' at a complete GameData folder or connect "
                "the Openplanet plugin to fetch them. Missing: %s%s",
                len(missing), getattr(resolver, "gamedata_dir", "") or "", ", ".join(missing[:15]),
                " ..." if len(missing) > 15 else "",
            )
        root["tm_import_status"] = "PARTIAL" if plan.partial else "COMPLETE"
        context.scene["tm_import_status"] = root["tm_import_status"]
        library["tm_import_status"] = root["tm_import_status"]
        if embedded_library is not None:
            embedded_library["tm_import_status"] = root["tm_import_status"]
    except GeneratorExit:
        # Builder.abort(): the caller removes everything created so far.
        raise
    except Exception:
        # An unexpected execution error must not leave a success-shaped result.
        root["tm_import_status"] = "FAILED"
        context.scene["tm_import_status"] = "FAILED"
        library["tm_import_status"] = "FAILED"
        if embedded_library is not None:
            embedded_library["tm_import_status"] = "FAILED"
        log.exception("V4 scene build failed: %s", plan.name)
        raise
    finally:
        _gbxmat_adapter.set_color_palette(resolver, None)
        _gbxmat_adapter.set_terrain_material_remap(resolver, None)
        set_path_override(None)
        if plan.club_textures is not None:
            import os
            club_paths = {os.path.normcase(os.path.abspath(path))
                          for path in plan.club_textures.paths.values()}
            if root.get("tm_import_status") == "COMPLETE":
                for image in bpy.data.images:
                    path = os.path.normcase(os.path.abspath(bpy.path.abspath(image.filepath)))
                    if path in club_paths and not image.packed_file:
                        try:
                            image.pack()
                        except (OSError, RuntimeError) as exc:
                            plan.issue("club_texture_pack_failed", image.filepath, str(exc))
            root["tm_club_textures_used"] = json.dumps(sorted(plan.club_textures.used))
            log.info("V4 club %d textures used: %s", plan.club_textures.club_id,
                     ", ".join(sorted(plan.club_textures.used)) or "none")
        if plan.texture_mod is not None:
            used = sorted(plan.texture_mod.used, key=str.casefold)
            root["tm_texture_mod_used"] = json.dumps(used)
            log.info("V4 texture mod: %d of %d files used: %s",
                     len(used), len(plan.texture_mod.files), ", ".join(used) or "none")
        root["tm_diagnostics"] = json.dumps([asdict(d) for d in plan.diagnostics])
        root["tm_diagnostic_count"] = len(plan.diagnostics)
        root["tm_placement_count"] = len(plan.placements)
        root["tm_asset_count"] = len(plan.assets)
        root["tm_clip_count"] = sum(len(p.clips) for p in plan.placements)

    log.info(
        "V4 %s: %d placements, %d definitions, %d diagnostics",
        root["tm_import_status"], len(plan.placements), len(plan.assets), len(plan.diagnostics),
    )
    return root
