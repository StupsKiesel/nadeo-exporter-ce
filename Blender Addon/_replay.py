"""Recorded replay motion with optional partial authored vehicle animation."""

import json
import logging
import math

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Quaternion

from . import ADDON_SHORT_NAME, _progress

log = logging.getLogger("trackmania_blender_bridge.replay")


def _curve(bag, path, index, frames, values):
    curve = bag.fcurves.new(path, index=index)
    curve.keyframe_points.add(len(frames))
    curve.keyframe_points.foreach_set("co", [component for pair in zip(frames, values) for component in pair])
    for point in curve.keyframe_points:
        point.interpolation = "LINEAR"
    curve.update()


def replay_map_mismatch(replay, scene):
    from ._map_panel import map_root, map_info

    root = map_root(scene)
    if root is not None:
        uid = map_info(root).get("uid", "")
        if uid and replay.map_uid and uid != replay.map_uid:
            return (f"The active Map scene does not match this replay's map "
                    f"(scene UID: {uid}; replay UID: {replay.map_uid}). "
                    "Different revisions may have different geometry; motion is not retargeted")
    return ""


def replay_height(replay, scene, apply_deco_height, *, allow_different_map_revision=False):
    mismatch = replay_map_mismatch(replay, scene)
    if mismatch and not allow_different_map_revision:
        raise ValueError(mismatch + ". Enable Allow Different Map Revision to import anyway")
    if not apply_deco_height:
        return 0.0
    from ._map_panel import map_root

    root = map_root(scene)
    if root is not None:
        if "tm_decoration_height_m" in root:
            height = float(root["tm_decoration_height_m"])
        else:
            # Older imports did not store their height option; use the replay map.
            height = replay.decoration_height_m
    else:
        height = replay.decoration_height_m
    if height is None:
        raise ValueError("Decoration height unavailable: import the matching map or disable Apply Decoration Height")
    if not math.isfinite(height):
        raise ValueError("Replay decoration height is not finite")
    return height


def build_replay(replay, context, start_frame=1, *, apply_deco_height=True,
                 allow_different_map_revision=False, import_car_model=False, skin_override="",
                 download_skins=False, animate_vehicle=True):
    if not any(ghost.samples for ghost in replay.ghosts):
        raise ValueError("Replay has no vehicle motion samples")
    from .map_import_v4.bridge import _snapshot, _ID_COLLECTIONS

    before = _snapshot()
    actions = set(bpy.data.actions)
    scene = context.scene
    height = replay_height(replay, scene, apply_deco_height,
                           allow_different_map_revision=allow_different_map_revision)
    mismatch = replay_map_mismatch(replay, scene)
    if mismatch:
        log.warning("%s", mismatch)
    previous_end = scene.frame_end
    fps = scene.render.fps / scene.render.fps_base
    try:
        collection = bpy.data.collections.new(f"Replay {replay.name}")
        scene.collection.children.link(collection)
        collection["tm_source_path"] = replay.source_path or ""
        collection["tm_replay_map_uid"] = replay.map_uid
        collection["tm_replay_map_name"] = replay.map_name
        collection["tm_replay_map_warning"] = mismatch
        collection["tm_decoration_height_m"] = height
        collection["tm_replay_diagnostics"] = json.dumps(replay.diagnostics)
        collection["tm_replay_preview"] = "Recorded motion/telemetry; vehicle rest pose"
        roots = []
        visual_diagnostics = []
        ghost_count = sum(bool(ghost.samples) for ghost in replay.ghosts)
        if import_car_model:
            from . import _get_gamedata_dir, _make_skin_library, _build_blender_mesh_from_gbxbpy
            from GBXBPY import Resolver
            from GBXBPY.errors import ParseError
            from GBXBPY.replay_vehicle import load_replay_vehicle
            import zipfile

            resolver = Resolver(_get_gamedata_dir(context))
            skins = _make_skin_library(context, download=download_skins)
            skins.user_dirs.append(resolver.gamedata_dir)
        for index, ghost in enumerate(replay.ghosts):
            if not ghost.samples:
                continue
            bar = _progress.Progress().sub(0.35 + 0.6 * len(roots) / ghost_count,
                                           0.35 + 0.6 * (len(roots) + 1) / ghost_count)
            bar(0, "Loading replay car model")
            vehicle = None
            if import_car_model:
                try:
                    vehicle = load_replay_vehicle(ghost, resolver, skins, skin_override=skin_override)
                except (ParseError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    message = f"Ghost {index + 1}: car model unavailable: {exc}; using a labelled placeholder"
                    log.warning("%s", message)
                    visual_diagnostics.append(message)
                if vehicle is not None:
                    visual_diagnostics.extend(vehicle.diagnostics)
                    for diagnostic in vehicle.diagnostics:
                        log.warning("%s", diagnostic)
            obj = bpy.data.objects.new(f"Replay Car {ghost.nickname or index + 1}", None)
            collection.objects.link(obj)
            obj.empty_display_type = "ARROWS"
            obj.empty_display_size = 2
            obj.rotation_mode = "QUATERNION"
            obj["tm_replay_ghost"] = True
            obj["tm_replay_map_warning"] = mismatch
            obj["tm_decoration_height_m"] = height
            obj["tm_source_path"] = replay.source_path or ""
            obj["tm_replay_model"] = ghost.player_model
            obj["tm_replay_skins"] = json.dumps(ghost.skins)
            obj["tm_replay_sample_count"] = len(ghost.samples)
            obj["tm_replay_start_ms"] = ghost.samples[0].time_ms
            obj["tm_replay_end_ms"] = ghost.samples[-1].time_ms
            obj["tm_replay_entity_classes"] = json.dumps([hex(cid) for cid in ghost.entity_class_ids])
            if vehicle is None:
                marker = bpy.data.objects.new("Carbody PLACEHOLDER (not game geometry)", None)
                marker.empty_display_type = "CUBE"
                marker.scale = (1, 2, 0.5)
                marker["tm_placeholder"] = True
                marker.show_name = True
            else:
                marker = _build_blender_mesh_from_gbxbpy(
                    vehicle.mesh, resolver=resolver, source_path=vehicle.source_path,
                    progress=bar.sub(0, 0.4), preserve_bone_indices=animate_vehicle)
                marker.name = "Replay Carbody"
                marker["tm_replay_vehicle_source"] = vehicle.source_path
                marker["tm_replay_skin_source"] = vehicle.skin_path
                marker["tm_replay_skin_members"] = json.dumps(vehicle.archive_members)
            collection.objects.link(marker)
            marker.parent = obj
            if vehicle is not None:
                from GBXBPY.replay_driver import load_replay_driver
                from ._replay_driver import build_driver
                from mathutils import Matrix, Vector

                try:
                    seat = next((joint for joint in vehicle.joints if joint.name == "PlayerSeat_Pilot01"), None)
                    if seat is None:
                        raise ParseError("Vehicle has no authored PlayerSeat_Pilot01 attachment")
                    driver = load_replay_driver(resolver)
                    driver_obj = build_driver(driver, obj, collection, resolver,
                                              _build_blender_mesh_from_gbxbpy, bar.sub(0.4, 0.45))
                    attachment = Matrix(seat.basis).to_4x4()
                    attachment.translation = Vector(seat.position.as_tuple())
                    driver_obj.matrix_basis = attachment
                    driver_note = driver_obj["tm_replay_driver_note"]
                    log.warning("%s", driver_note)
                    visual_diagnostics.append(driver_note)
                except (ParseError, OSError) as exc:
                    driver_note = f"Driver unavailable: {exc}"
                    log.warning("%s", driver_note)
                    visual_diagnostics.append(driver_note)
            obj["tm_replay_vehicle_preview"] = "Rest-pose car model" if vehicle else "Placeholder vehicle"
            bar(0.45, "Preparing replay animation")
            frames = [start_frame + sample.time_ms * fps / 1000 for sample in ghost.samples]
            if vehicle is not None and animate_vehicle:
                from ._replay_rig import build_vehicle_rig
                from GBXBPY.replay_spoiler import load_spoiler_parameter
                spoiler_parameter = None
                if any(joint.name.startswith("Spoiler") for joint in vehicle.joints):
                    try:
                        spoiler_parameter = load_spoiler_parameter(resolver)
                    except (ParseError, OSError) as exc:
                        diagnostic = f"Spoiler presentation parameter unavailable: {exc}"
                        log.warning("%s", diagnostic)
                        visual_diagnostics.append(diagnostic)
                try:
                    rig = build_vehicle_rig(vehicle, marker, obj, collection, frames, ghost.samples, _curve,
                                            bar.sub(0.45, 0.6), spoiler_parameter=spoiler_parameter)
                except ValueError as exc:
                    # A partly built rig cannot safely masquerade as a rest pose.
                    raise ValueError(f"Vehicle animation unavailable: {exc}. Disable Animate Wheels and Suspension") from exc
                obj["tm_replay_vehicle_preview"] = "Authored skeleton | verified wheel/suspension subset"
                if rig.get("tm_replay_spoiler"):
                    obj["tm_replay_vehicle_preview"] = "Authored wheels/suspension/spoiler"
                collection["tm_replay_preview"] = "Recorded motion/telemetry; partial authored vehicle animation"
                log.warning("%s", rig["tm_replay_rig_note"])
                visual_diagnostics.append(rig["tm_replay_rig_note"])
            rotations = []
            previous = None
            for sample in ghost.samples:
                q = sample.rotation
                rotation = Quaternion((q.w, q.x, q.y, q.z))
                rotation.normalize()
                if previous is not None and previous.dot(rotation) < 0:
                    rotation.negate()
                rotations.append(rotation)
                previous = rotation
            action = bpy.data.actions.new(obj.name)
            obj.animation_data_create()
            obj.animation_data.action = action
            if hasattr(action, "slots"):
                slot = action.slots.new(id_type="OBJECT", name=obj.name)
                strip = action.layers.new("Replay Motion").strips.new(type="KEYFRAME")
                bag = strip.channelbag(slot, ensure=True)
                obj.animation_data.action_slot = slot
            else:
                bag = action
            positions = [(sample.position.x, sample.position.y, sample.position.z + height)
                         for sample in ghost.samples]
            total_curves = 7 + len(ghost.samples[0].telemetry)
            completed_curves = 0
            for axis in range(3):
                _curve(bag, "location", axis, frames, [pos[axis] for pos in positions])
                completed_curves += 1
                bar(0.6 + 0.4 * completed_curves / total_curves, "Keying replay position")
            for axis in range(4):
                _curve(bag, "rotation_quaternion", axis, frames, [rot[axis] for rot in rotations])
                completed_curves += 1
                bar(0.6 + 0.4 * completed_curves / total_curves, "Keying replay rotation")
            for name in ghost.samples[0].telemetry:
                key = f"tm_replay_{name}"
                obj[key] = ghost.samples[0].telemetry[name]
                _curve(bag, f'["{key}"]', 0, frames, [sample.telemetry[name] for sample in ghost.samples])
                completed_curves += 1
                bar(0.6 + 0.4 * completed_curves / total_curves, "Keying replay telemetry")
            obj.location = positions[0]
            obj.rotation_quaternion = rotations[0]
            roots.append(obj)
        collection["tm_replay_visual_diagnostics"] = json.dumps(visual_diagnostics)
        if not import_car_model:
            visual_diagnostics.append("Car model import disabled; using placeholder vehicles")
            collection["tm_replay_visual_diagnostics"] = json.dumps(visual_diagnostics)
        _progress.update(0.97, "Updating replay scene")
        scene.frame_end = max(scene.frame_end, math.ceil(max(
            start_frame + ghost.samples[-1].time_ms * fps / 1000
            for ghost in replay.ghosts if ghost.samples)))
        scene.frame_set(scene.frame_current)
        context.view_layer.update()
        for obj in context.selected_objects:
            obj.select_set(False)
        roots[0].select_set(True)
        context.view_layer.objects.active = roots[0]
        return collection
    except Exception:
        log.exception("Replay scene build failed: %s", replay.source_path)
        scene.frame_end = previous_end
        bpy.data.batch_remove([block for name in _ID_COLLECTIONS for block in getattr(bpy.data, name)
                               if block.as_pointer() not in before]
                              + [action for action in bpy.data.actions if action not in actions])
        raise


class TMBRIDGE_OT_ImportReplay(bpy.types.Operator, ImportHelper):
    bl_idname = "trackmania_blender_bridge.import_replay"
    bl_label = "Import Replay"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".Replay.Gbx"
    filter_glob: StringProperty(default="*.Replay.Gbx;*.replay.gbx", options={"HIDDEN"})  # pyright: ignore[reportInvalidTypeForm]
    start_frame: IntProperty(name="Start Frame", default=1)  # pyright: ignore[reportInvalidTypeForm]
    apply_deco_height: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Apply Decoration Height", default=True,
        description="Align replay motion with the map's decoration height; respects the imported map's height option")
    allow_different_map_revision: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Allow Different Map Revision", default=False,
        description="Import despite a different map UID; geometry may differ and recorded motion is not retargeted")
    import_car_model: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Import Car Model", default=True,
        description="Load the skin's MainBody mesh or the default GameData car, in its rest pose")
    skin_override: StringProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Skin ZIP Override", subtype="FILE_PATH",
        description="Optional local skin ZIP replacing the replay's recorded skin")
    download_skins: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Download Missing Skin", default=False,
        description="Use recorded locator URLs when a skin is unavailable locally")
    animate_vehicle: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Animate Wheels and Suspension", default=True,
        description="Use authored wheel, suspension and spoiler dynamics; steering and linkage alignment remain unsupported. Spoiler is reconstructed at scene FPS")

    @_progress.tracked("Importing Replay")
    def execute(self, context):
        import GBXBPY
        from GBXBPY.model.replay import Replay
        try:
            replay = GBXBPY.load(self.filepath, recursive=False)
            if not isinstance(replay, Replay):
                raise ValueError("Select a Replay.Gbx file")
            _progress.update(0.35, "Building replay animation")
            collection = build_replay(
                replay, context, self.start_frame, apply_deco_height=self.apply_deco_height,
                allow_different_map_revision=self.allow_different_map_revision,
                import_car_model=self.import_car_model, skin_override=bpy.path.abspath(self.skin_override),
                download_skins=self.download_skins, animate_vehicle=self.animate_vehicle)
        except Exception as exc:
            log.exception("Replay import failed: %s", self.filepath)
            self.report({"ERROR"}, f"Replay import failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, "Imported replay motion/telemetry")
        for diagnostic in json.loads(collection["tm_replay_visual_diagnostics"]):
            self.report({"WARNING"}, diagnostic)
        if collection["tm_replay_map_warning"]:
            self.report({"WARNING"}, collection["tm_replay_map_warning"])
        for diagnostic in replay.diagnostics:
            log.warning("%s", diagnostic)
        return {"FINISHED"}


def selected_replay(context):
    obj = context.active_object
    while obj is not None:
        if obj.get("tm_replay_ghost"):
            return obj
        obj = obj.parent
    return None


class TMBRIDGE_PT_Replay(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_replay"
    bl_label = "Replay"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME

    @classmethod
    def poll(cls, context):
        return selected_replay(context) is not None

    def draw(self, context):
        obj = selected_replay(context)
        self.layout.label(text=obj.get("tm_replay_vehicle_preview", "Placeholder vehicle"), icon="INFO")
        if any(child.get("tm_replay_vehicle_source") for child in obj.children):
            driver = next((child for child in obj.children if child.get("tm_replay_driver")), None)
            self.layout.label(text="Driver: authored seated pose" if driver else "Driver unavailable",
                              icon="INFO" if driver else "ERROR")
        rig = next((child for child in obj.children if child.get("tm_replay_rig")), None)
        if rig is not None:
            self.layout.label(text="Spoiler: authored replay reconstruction" if rig.get("tm_replay_spoiler")
                                   else "Spoiler animation unavailable", icon="INFO" if rig.get("tm_replay_spoiler") else "ERROR")
            self.layout.label(text="Steering/linkage alignment pending", icon="ERROR")
        if obj.get("tm_replay_map_warning"):
            self.layout.label(text="Different map revision: motion not retargeted", icon="ERROR")
        for key, label in (("tm_replay_model", "Player Model"), ("tm_replay_sample_count", "Samples"),
                           ("tm_replay_start_ms", "Start (ms)"), ("tm_replay_end_ms", "End (ms)")):
            self.layout.label(text=f"{label}: {obj.get(key, '')}")
        self.layout.label(text=f"Decoration height (m): {obj.get('tm_decoration_height_m', 0):g}")
        for key in sorted(obj.keys()):
            if key.startswith("tm_replay_") and isinstance(obj[key], float):
                self.layout.label(text=f"{key.removeprefix('tm_replay_')}: {obj[key]:.3f}")
        for skin in json.loads(obj.get("tm_replay_skins", "[]")):
            self.layout.label(text=f"Skin: {skin}")


CLASSES = (TMBRIDGE_OT_ImportReplay, TMBRIDGE_PT_Replay)
