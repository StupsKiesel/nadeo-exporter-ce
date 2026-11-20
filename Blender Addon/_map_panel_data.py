"""Root-backed map editing and environment controls for the redesigned sidebar."""

import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, replace

import bpy
from mathutils import Vector

from . import _map_panel

log = logging.getLogger("trackmania_blender_bridge.map_controls")
ERROR_KEY = "tm_map_controls_error"
_dynamic_cache = {}


def info(root):
    value = json.loads(root.get("tm_map_info", "") or "{}")
    if not isinstance(value, dict):
        raise ValueError("Map metadata must be a JSON object")
    return value


def fail(root, message):
    root[ERROR_KEY] = str(message)
    log.error("Map %s: %s", root.name, message)


def metadata_get(key, default=""):
    def get(settings):
        root = settings.id_data
        if key == "map_name":
            return root.get("tm_map_name", "")
        data = info(root)
        if key == "author":
            return data.get("author_nickname") or data.get("author_login", "")
        if key.endswith("_time"):
            return _map_panel.format_time(data.get(key, -1))
        return data.get(key, default)
    return get


def parse_time(value):
    value = value.strip()
    if value in ("", "-"):
        return -1
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2})(?:\.(\d{1,3}))?", value)
    if match is None or (match[1] is not None and int(match[2]) >= 60):
        raise ValueError("Use m:ss.mmm for medal times, or '-' to clear")
    return (int(match[1] or 0) * 60 + int(match[2])) * 1000 + int((match[3] or "").ljust(3, "0"))


def metadata_set(key):
    def set_value(settings, value):
        root = settings.id_data
        try:
            data = info(root)
            if key == "map_name":
                root["tm_map_name"] = value
            else:
                target = "author_nickname" if key == "author" else key
                data[target] = parse_time(value) if key.endswith("_time") else value
                root["tm_map_info"] = json.dumps(data)
            root[ERROR_KEY] = ""
        except (ValueError, TypeError) as exc:
            fail(root, exc)
    return set_value


def scene_for(root):
    scene = bpy.context.scene
    if scene is not None and root.name in scene.collection.children:
        return scene
    owners = [s for s in bpy.data.scenes if root.name in s.collection.children]
    if len(owners) != 1:
        raise ValueError("Map root must belong to one scene")
    return owners[0]


@contextmanager
def resources(context, *, fetch_missing=False):
    from . import _get_gamedata_dir, _openplanet_bridge
    from GBXBPY import library
    from GBXBPY.library import LibraryProvider
    from GBXBPY.resolve import Resolver

    path = _get_gamedata_dir(context)
    if not path or not os.path.isdir(path):
        raise ValueError(f"Game library folder unavailable: {path}")
    server = None
    try:
        queue = _openplanet_bridge.queue()
        if fetch_missing and not _openplanet_bridge.extract_dir():
            raise ValueError("Set the Openplanet folder in addon preferences")
        if queue is None and fetch_missing:
            server = _openplanet_bridge.start_import_server()
            queue = server.queue
        provider = LibraryProvider(
            path, queue, fetch=bool(_openplanet_bridge.extract_dir()) and
            (fetch_missing or (queue is not None and queue.plugin_connected())),
            extract_dir=_openplanet_bridge.extract_dir(),
        )
        with library.active(provider):
            if fetch_missing:
                from .map_import_v4.operator import _prepare_library
                _prepare_library(provider)
            else:
                provider.prepare()
            yield Resolver(path)
    finally:
        if server is not None:
            _openplanet_bridge.stop_import_server(server)


def _record_issues(root, issues):
    root["tm_map_controls_issues"] = json.dumps(issues)
    for issue in issues:
        log.warning("Map %s: %s", root.name, issue)


def resolve(root, context, *, dynamic=False, reset=False):
    from GBXBPY.map_import_v4.environment import resolve_environment

    settings = root.tm_bridge_map_controls
    issues = []
    with resources(context) as resolver:
        def one(mood, time):
            return resolve_environment(
                resolver, root.get("tm_decoration_collection", ""),
                root.get("tm_decoration", ""), mood=mood, day_time=time,
                import_sky=settings.sky, import_clouds=settings.clouds and settings.sky,
                issue=lambda code, source, message: issues.append(f"{message} ({source})"),
            )
        if dynamic:
            result = [one(mood, None) for mood in ("Night", "Sunrise", "Day", "Sunset")]
        else:
            result = one(
                _map_panel._map_mood(root) if reset else settings.mood.capitalize(),
                _map_panel._map_day_time(root) if reset else settings.time_of_day,
            )
    _record_issues(root, issues)
    return result


def _store_dynamic(root, environments):
    values = []
    for environment in environments:
        sky = replace(environment.sky, clouds=None) if environment.sky else None
        values.append(asdict(replace(environment, sky=sky)))
    raw = json.dumps(values)
    root["tm_dynamic_environments"] = raw
    _dynamic_cache[root.as_pointer()] = (raw, environments)


def _dynamic_environments(root):
    from GBXBPY.map_import_v4.environment import Environment, Sky, SunHalo

    raw = root.get("tm_dynamic_environments", "")
    cached = _dynamic_cache.get(root.as_pointer())
    if cached is not None and cached[0] == raw:
        return cached[1]
    if not raw:
        raise ValueError("Dynamic lighting needs rebuilding; toggle Dynamic off and on")
    environments = []
    for value in json.loads(raw):
        sky = value["sky"]
        if sky is not None:
            sky["sun_halos"] = tuple(SunHalo(**halo) for halo in sky["sun_halos"])
            value["sky"] = Sky(**sky)
        environments.append(Environment(**value))
    _dynamic_cache[root.as_pointer()] = (raw, environments)
    return environments


def cycle_time(scene, settings):
    fps = scene.render.fps / scene.render.fps_base
    elapsed = (scene.frame_current_final - scene.frame_start) / fps
    return (settings.time_of_day + elapsed / settings.cycle_seconds) % 1.0


def dynamic_environment(environments, time):
    from GBXBPY.map_import_v4.environment import light_direction, moon_direction

    ordered = sorted(environments, key=lambda env: env.day_time)
    left = ordered[-1]
    right = ordered[0]
    for index, env in enumerate(ordered):
        if time < env.day_time:
            left, right = ordered[index - 1], env
            break
    span = (right.day_time - left.day_time) % 1.0
    factor = ((time - left.day_time) % 1.0) / span if span else 0.0
    chosen = left if factor < 0.5 else right
    latitude = left.latitude * (1.0 - factor) + right.latitude * factor
    direction, moon = light_direction(time, latitude)
    sky = chosen.sky
    if sky is not None:
        sky = replace(sky, moon_direction=moon_direction(time, latitude))
    def blend(a, b):
        return tuple(x * (1.0 - factor) + y * factor for x, y in zip(a, b))
    return replace(
        chosen, day_time=time, latitude=latitude, light_direction=direction, is_moon=moon,
        light_color=blend(left.light_color, right.light_color),
        light_strength=left.light_strength * (1.0 - factor) + right.light_strength * factor,
        ambient=blend(left.ambient, right.ambient), sky=sky,
    )


def _lighting(root):
    return next((coll for coll in root.children if coll.get("tm_section") == "Lighting"), None)


def apply(root, context, *, reset=False):
    from .map_import_v4.bridge import apply_environment

    scene = scene_for(root)
    settings = root.tm_bridge_map_controls
    if settings.animate_time and not reset:
        environments = resolve(root, context, dynamic=True)
        _store_dynamic(root, environments)
        environment = dynamic_environment(environments, cycle_time(scene, settings))
    else:
        environment = resolve(root, context, reset=reset)
    issues = []
    apply_environment(environment, scene, root, root.get("tm_map_name", root.name), issue=issues.append)
    scene.world["tm_map_controls_root"] = root.get("tm_map_id", root.name)
    _record_issues(root, json.loads(root.get("tm_map_controls_issues", "[]")) + issues)
    root["tm_controls_sky"] = settings.sky
    root["tm_controls_clouds"] = settings.clouds
    root["tm_controls_last_time"] = environment.day_time
    if reset:
        root["tm_controls_time"] = environment.day_time
    if settings.animate_time:
        scene.render.use_lock_interface = True
    root[ERROR_KEY] = ""


def environment_update(settings, context):
    root = settings.id_data
    try:
        apply(root, context)
    except Exception as exc:
        # Blender property callbacks cannot propagate an operator report.
        log.exception("Automatic map environment update failed")
        fail(root, exc)
    finally:
        for key in ("tm_controls_pending_time", "tm_controls_pending_sky", "tm_controls_pending_clouds"):
            root.pop(key, None)


def mood_get(settings):
    root = settings.id_data
    mood = root.get("tm_mood") or _map_panel._map_mood(root)
    return ("SUNRISE", "DAY", "SUNSET", "NIGHT").index(mood.upper())


def mood_set(settings, value):
    settings.id_data["tm_mood"] = ("Sunrise", "Day", "Sunset", "Night")[value]
    settings.id_data.pop("tm_controls_time", None)


def preset_update(settings, context):
    root = settings.id_data
    try:
        # A preset chooses the actual mood XML's time, not the previous slider value.
        from .map_import_v4.bridge import apply_environment
        from GBXBPY.map_import_v4.environment import resolve_environment

        with resources(context) as resolver:
            issues = []
            environment = resolve_environment(
                resolver, root.get("tm_decoration_collection", ""),
                root.get("tm_decoration", ""), mood=settings.mood.capitalize(),
                import_sky=settings.sky, import_clouds=settings.sky and settings.clouds,
                issue=lambda code, source, message: issues.append(f"{message} ({source})"),
            )
        root["tm_controls_time"] = environment.day_time
        if settings.animate_time:
            apply(root, context)
        else:
            apply_environment(environment, scene_for(root), root, root.get("tm_map_name", root.name),
                              issue=issues.append)
            scene_for(root).world["tm_map_controls_root"] = root.get("tm_map_id", root.name)
            root["tm_controls_sky"] = settings.sky
            root["tm_controls_clouds"] = settings.clouds
            root["tm_controls_last_time"] = environment.day_time
            _record_issues(root, issues)
            root[ERROR_KEY] = ""
    except Exception as exc:
        log.exception("Mood preset update failed")
        fail(root, exc)


def time_get(settings):
    root = settings.id_data
    original = _map_panel._map_day_time(root)
    applied = root.get("tm_day_time_applied", original if original is not None else 0.6)
    if settings.animate_time or root.get("tm_controls_pending_time") or root.get("tm_controls_last_time") == applied:
        return root.get("tm_controls_time", applied)
    return applied


def time_set(settings, value):
    settings.id_data["tm_controls_time"] = value
    settings.id_data["tm_controls_pending_time"] = True


def sky_get(settings):
    root = settings.id_data
    scene = scene_for(root)
    owned = scene.world is not None and scene.world.get("tm_map_controls_root") == root.get("tm_map_id", root.name)
    if "tm_controls_sky" in root and (root.get("tm_controls_pending_sky") or owned):
        return root["tm_controls_sky"]
    return bool(scene.world is not None and "tm_sky_color" in scene.world)


def sky_set(settings, value):
    settings.id_data["tm_controls_sky"] = value
    settings.id_data["tm_controls_pending_sky"] = True


def clouds_get(settings):
    root = settings.id_data
    scene = scene_for(root)
    owned = scene.world is not None and scene.world.get("tm_map_controls_root") == root.get("tm_map_id", root.name)
    if "tm_controls_clouds" in root and (root.get("tm_controls_pending_clouds") or owned):
        return root["tm_controls_clouds"]
    lighting = _lighting(root)
    return bool(lighting is not None and any("tm_cloud_sprites" in obj for obj in lighting.objects))


def clouds_set(settings, value):
    settings.id_data["tm_controls_clouds"] = value
    settings.id_data["tm_controls_pending_clouds"] = True


def decoration_sections(root):
    return [coll for coll in root.children_recursive if coll.get("tm_section") == "Decoration"]


def surroundings_get(settings):
    sections = decoration_sections(settings.id_data)
    return bool(sections) and any(not c.hide_render and not c.hide_viewport for c in sections)


def load_surroundings(root, context):
    from GBXBPY import load
    from GBXBPY.model.map import Map
    from GBXBPY.map_import_v4 import ImportOptions, ScenePlan
    from GBXBPY.map_import_v4.decoration_map import plan_decoration_map
    from GBXBPY.map_import_v4.dependencies import Dependencies
    from .map_import_v4 import bridge
    from . import _make_skin_library
    from NICE.src.parser import shared_external_cache

    source = root.get("tm_source_path", "")
    if not os.path.isfile(source):
        raise ValueError(f"Source map unavailable: {source}")
    scene = scene_for(root)
    original_scene = {key: scene.get(key) for key in ("tm_asset_collection", "tm_import_status")}
    diagnostics = json.loads(root.get("tm_diagnostics", "[]"))
    with resources(context, fetch_missing=True) as resolver, shared_external_cache():
        asset = load(source, resolver=resolver, recursive=False)
        if not isinstance(asset, Map):
            raise ValueError("Surroundings source is not a map")
        options = ImportOptions(import_lighting=False, import_terrain_grass=False)
        plan = ScenePlan(root.get("tm_map_name", root.name), source, options)
        plan.decoration = asset.decoration
        plan.decoration_collection = asset.decoration_collection
        plan.map_size = tuple(asset.map_size)
        dependencies = Dependencies(resolver, plan)
        dependencies.blocks.collection = asset.decoration_collection
        skins = _make_skin_library(context, download=False)
        plan.color_palette = root.get("tm_color_palette", "Classic")
        from GBXBPY.resolve.modifier_lookup import resolve_terrain_material_remap
        from GBXBPY.map_import_v4.pipeline import plan_texture_mod
        plan.terrain_material_remap = resolve_terrain_material_remap(asset.decoration_collection, resolver)
        if asset.mod_pack_path or asset.mod_pack_url:
            plan.texture_mod = plan_texture_mod(asset, skins, plan)
        plan_decoration_map(asset, dependencies, skins)
        if not plan.placements:
            raise ValueError("No surroundings resolved; check the game library and Openplanet connection")
        plan.validate()
        before = bridge._snapshot()
        staging = None
        try:
            gamedata = bpy.data.scenes.get(root.get("tm_gamedata_scene", ""))
            if gamedata is None:
                gamedata = bpy.data.scenes.new("GameData")
                gamedata["tm_scene_role"] = "GAMEDATA"
            with context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
                steps = bridge._build(
                    plan, context=bpy.context, resolver=resolver,
                    gamedata=gamedata, progress=bridge._no_progress,
                )
                while True:
                    try:
                        next(steps)
                    except StopIteration as done:
                        staging = done.value
                        break
            sections = decoration_sections(staging)
            if not sections:
                raise ValueError("Surroundings build produced no decoration collection")
            for section in sections:
                root.children.link(section)
                staging.children.unlink(section)
                for obj in section.all_objects:
                    obj["tm_map_root"] = root.get("tm_map_id", root.name)
            root["tm_surroundings_library"] = staging.get("tm_asset_collection", "")
            root["tm_placement_count"] = root.get("tm_placement_count", 0) + len(plan.placements)
            root["tm_asset_count"] = root.get("tm_asset_count", 0) + len(plan.assets)
            diagnostics.extend(asdict(issue) for issue in plan.diagnostics)
            root["tm_diagnostics"] = json.dumps(diagnostics)
            root["tm_diagnostic_count"] = len(diagnostics)
            if plan.partial:
                root["tm_import_status"] = "PARTIAL"
            bpy.data.collections.remove(staging)
        except Exception:
            created = [block for name in bridge._ID_COLLECTIONS for block in getattr(bpy.data, name)
                       if block.as_pointer() not in before]
            bpy.data.batch_remove(created)
            raise
        finally:
            for key, value in original_scene.items():
                if value is None:
                    scene.pop(key, None)
                else:
                    scene[key] = value


def surroundings_set(settings, value):
    root = settings.id_data
    try:
        if value and not decoration_sections(root):
            load_surroundings(root, bpy.context)
        for coll in decoration_sections(root):
            coll.hide_viewport = coll.hide_render = not value
        root[ERROR_KEY] = ""
    except Exception as exc:
        log.exception("Surroundings update failed")
        fail(root, exc)


def reachable_objects(root):
    seen_collections = set()
    seen_objects = set()
    def visit(coll):
        if coll.as_pointer() in seen_collections:
            return
        seen_collections.add(coll.as_pointer())
        for obj in coll.objects:
            if obj.as_pointer() not in seen_objects:
                seen_objects.add(obj.as_pointer())
                yield obj
            if obj.instance_type == "COLLECTION" and obj.instance_collection is not None:
                yield from visit(obj.instance_collection)
        for child in coll.children:
            yield from visit(child)
    yield from visit(root)


def particle_modifiers(root):
    from .fx_particles import fx_modifiers
    return list(fx_modifiers(reachable_objects(root)))


def _localize_particles(root):
    """Copy only FX-bearing instance branches, leaving shared GameData unchanged."""
    from .fx_particles import fx_modifiers

    memo = {}
    owner = root.get("tm_map_id", root.name)
    def private(coll):
        pointer = coll.as_pointer()
        if pointer in memo:
            return memo[pointer]
        if coll.get("tm_fx_owner") == owner or not particle_modifiers(coll):
            memo[pointer] = coll
            return coll
        copy = bpy.data.collections.new(f"{coll.name} FX")
        for key in coll.keys():
            copy[key] = coll[key]
        copy["tm_fx_owner"] = owner
        memo[pointer] = copy
        object_copies = {}
        for obj in coll.objects:
            nested = private(obj.instance_collection) if obj.instance_collection else None
            has_fx = bool(list(fx_modifiers([obj])))
            duplicate = obj.copy() if has_fx or nested != obj.instance_collection else obj
            if duplicate != obj:
                object_copies[obj] = duplicate
                if nested is not None:
                    duplicate.instance_collection = nested
            copy.objects.link(duplicate)
        for original, duplicate in object_copies.items():
            if original.parent in object_copies:
                duplicate.parent = object_copies[original.parent]
        for child in coll.children:
            copy.children.link(private(child))
        return copy
    for obj in list(root.all_objects):
        if obj.instance_type == "COLLECTION" and obj.instance_collection is not None:
            obj.instance_collection = private(obj.instance_collection)


def particles_get(settings):
    modifiers = particle_modifiers(settings.id_data)
    return bool(modifiers) and all(mod.show_render for mod in modifiers)


def particles_set(settings, value):
    root = settings.id_data
    try:
        _localize_particles(root)
        for modifier in particle_modifiers(root):
            modifier.show_render = value
        root[ERROR_KEY] = ""
    except Exception as exc:
        log.exception("Particle render visibility update failed")
        fail(root, exc)


@bpy.app.handlers.persistent
def frame_change(scene, _depsgraph=None):
    from .map_import_v4.sky import build_sky_world

    for root in scene.collection.children:
        if root.get("tm_import_version") != 4 or not root.tm_bridge_map_controls.animate_time:
            continue
        if scene.world is None or scene.world.get("tm_map_controls_root") != root.get("tm_map_id", root.name):
            root.tm_bridge_map_controls["animate_time"] = False
            log.info("Dynamic disabled for %s: another environment is active", root.name)
            continue
        try:
            environment = dynamic_environment(
                _dynamic_environments(root), cycle_time(scene, root.tm_bridge_map_controls),
            )
            lighting = _lighting(root)
            light = next((o for o in lighting.objects if o.type == "LIGHT" and o.data.type == "SUN"), None) if lighting else None
            if light is None or scene.world is None:
                raise ValueError("Dynamic lighting was removed; toggle Dynamic to rebuild")
            light.rotation_euler = Vector(environment.light_direction).to_track_quat("Z", "Y").to_euler()
            light.data.color = environment.light_color
            light.data.energy = environment.light_strength
            light["tm_day_time"] = environment.day_time
            if environment.sky:
                build_sky_world(scene.world, environment)
            else:
                background = scene.world.node_tree.nodes.get("Background")
                if background:
                    background.inputs["Color"].default_value = (*environment.ambient, 1.0)
            scene.world.color = environment.ambient
            root["tm_day_time_applied"] = environment.day_time
        except Exception as exc:
            log.exception("Dynamic map lighting failed")
            root.tm_bridge_map_controls["animate_time"] = False
            fail(root, exc)


def register():
    if frame_change not in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.append(frame_change)


def unregister():
    if frame_change in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.remove(frame_change)
    _dynamic_cache.clear()
