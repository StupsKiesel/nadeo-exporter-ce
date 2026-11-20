"""Map-owned MediaTracker documents and approximate 3D previews."""

from __future__ import annotations

import base64
import json
import logging
import math
from dataclasses import asdict
from pathlib import Path

import bpy
from bpy.props import CollectionProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Euler, Quaternion, Vector

log = logging.getLogger("trackmania_blender_bridge.mediatracker")
PREVIEW_COLLECTION = "TM MediaTracker Preview"
MAP_TRACKER_COLLECTION = "MediaTracker"
_PREVIEW_CLASS_IDS = {
    0x03084000: "Game Camera",
    0x030A0000: "Orbital Camera",
    0x030A1000: "Camera Path",
    0x030A2000: "Custom Camera",
    0x030A5000: "Image",
    0x030A8000: "Text",
    0x030AB000: "Fade",
}


class TMBRIDGE_PG_MediaTracker(bpy.types.PropertyGroup):
    source_path: StringProperty(name="Clip", subtype="FILE_PATH")
    source_label: StringProperty(name="Source", default="")
    read_only: bpy.props.BoolProperty(name="Read Only", default=False)
    selected_track: IntProperty(name="Track", default=0, min=0)
    selected_block: IntProperty(name="Block", default=0, min=0)
    message: StringProperty(name="Status", default="")
    unsupported_blocks: StringProperty(name="Unsupported Blocks", default="")


def _json_default(value):
    if isinstance(value, bytes):
        return {"__gbxbpy_bytes__": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Cannot encode {type(value).__name__} in MediaTracker data")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key == "__gbxbpy_bytes__":
            return base64.b64decode(value)
        try:
            key = int(key)
        except (TypeError, ValueError):
            pass
        result[key] = value
    return result


def _load_document(filepath):
    from GBXBPY import load_media_clip

    return load_media_clip(filepath)


def _selected_tracker(context):
    obj = context.active_object
    while obj is not None:
        if obj.type == "EMPTY" and obj.get("tm_mediatracker_slot"):
            return obj
        obj = obj.parent
    return None


def _settings(context):
    root = _selected_tracker(context)
    if root is None:
        raise ValueError("Select a MediaTracker Empty first")
    return root.tm_mediatracker


def _is_tracker(_self, obj):
    return obj.type == "EMPTY" and bool(obj.get("tm_mediatracker_slot"))


def _store_document(root, document):
    data = asdict(document)
    if not document.editable:
        data["source_bytes"] = b""
    root["tm_mediatracker_document"] = json.dumps(data, default=_json_default)
    settings = root.tm_mediatracker
    settings.source_path = document.source_path
    settings.source_label = document.source_label
    settings.read_only = not document.editable


def _read_document(root):
    from GBXBPY.model.mediatracker import (
        MediaTrackerBlock, MediaTrackerClip, MediaTrackerClipDocument, MediaTrackerTrack,
    )

    data = json.loads(root["tm_mediatracker_document"], object_pairs_hook=_json_object)
    clip = data.pop("clip")
    tracks = []
    for track in clip.pop("tracks"):
        blocks = [MediaTrackerBlock(**block) for block in track.pop("blocks")]
        tracks.append(MediaTrackerTrack(blocks=blocks, **track))
    return MediaTrackerClipDocument(clip=MediaTrackerClip(tracks=tracks, **clip), **data)


def _map_tracker_collection(collection):
    destination = next((
        child for child in collection.children if child.get("tm_mediatracker_collection")
    ), None)
    if destination is None:
        destination = bpy.data.collections.new(MAP_TRACKER_COLLECTION)
        destination["tm_mediatracker_collection"] = True
        collection.children.link(destination)
    owners = {collection, *collection.children_recursive}
    roots = [
        obj for obj in collection.all_objects
        if obj.type == "EMPTY" and obj.get("tm_mediatracker_slot")
        and obj["tm_mediatracker_slot"] != "standalone"
    ]
    for root in roots:
        objects = [root, *(child for child in root.children_recursive if child.get("tm_mediatracker_preview"))]
        for obj in objects:
            if obj.name not in destination.objects:
                destination.objects.link(obj)
            for owner in list(obj.users_collection):
                if owner != destination and owner in owners:
                    owner.objects.unlink(obj)
    return destination


@bpy.app.handlers.persistent
def _organize_map_trackers(_unused=None):
    for scene in bpy.data.scenes:
        for collection in scene.collection.children:
            if collection.get("tm_import_version") == 4 and any(
                obj.type == "EMPTY" and obj.get("tm_mediatracker_slot")
                and obj["tm_mediatracker_slot"] != "standalone"
                for obj in collection.all_objects
            ):
                _map_tracker_collection(collection)
    for root in bpy.data.objects:
        if _is_tracker(None, root):
            _remove_legacy_timeline(root)


def _create_tracker(collection, slot, label):
    if collection.get("tm_import_version") == 4 and slot != "standalone":
        collection = _map_tracker_collection(collection)
    root = bpy.data.objects.new(f"MediaTracker: {label}", None)
    collection.objects.link(root)
    root.empty_display_type = "PLAIN_AXES"
    root["tm_mediatracker_slot"] = slot
    return root


def populate_map_preview(scene, filepath, collection=None):
    """Create all five map-owned MediaTracker empties with independent documents."""
    from GBXBPY.mediatracker import load_map_media_trackers, MAP_MEDIA_SLOTS
    from ._map_panel import map_root

    collection = collection if collection is not None else map_root(scene)
    if collection is None:
        raise ValueError("No map collection is available for MediaTracker slots")
    documents = load_map_media_trackers(filepath)
    collection = _map_tracker_collection(collection)
    for slot, label in MAP_MEDIA_SLOTS:
        root = next((
            obj for obj in collection.objects if obj.get("tm_mediatracker_slot") == slot
        ), None)
        if root is None:
            root = _create_tracker(collection, slot, label)
        _build_preview(scene, documents[slot], root=root)
    return documents


def _time_range(block):
    start, end = block.timeline_start, block.timeline_end
    if start is None:
        start = 0.0
    if end is None:
        end = start + 1.0
    return float(start), max(float(end), float(start) + 1.0 / 60.0)


def _retime(value, old_start, old_end, new_start, new_end):
    old_duration = old_end - old_start
    scale = (new_end - new_start) / old_duration if old_duration else 1.0
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"time", "start", "end"} and isinstance(child, (int, float)):
                value[key] = new_start + (float(child) - old_start) * scale
            elif key == "times" and isinstance(child, list):
                value[key] = [
                    new_start + (float(time) - old_start) * scale if type(time) in (int, float) else time
                    for time in child
                ]
            elif key != "recordData":
                _retime(child, old_start, old_end, new_start, new_end)
    elif isinstance(value, list):
        for child in value:
            _retime(child, old_start, old_end, new_start, new_end)


def _frame(seconds, scene):
    return 1.0 + seconds * scene.render.fps / scene.render.fps_base


def _vec3(value):
    if isinstance(value, dict) and all(axis in value for axis in ("x", "y", "z")):
        return (value["x"], value["y"], value["z"])
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(value[:3])
    return None


def _first_block_chunk(block, class_id):
    value = block.chunks.get(class_id)
    return value if isinstance(value, dict) else None


def _first_effect(block, class_id):
    chunk = _first_block_chunk(block, class_id)
    if not chunk:
        return None
    effect = chunk.get("effect")
    if isinstance(effect, dict):
        body = effect.get("body", {})
        return body if isinstance(body, dict) else None
    return None


def _add_preview_text(collection, name, body, location, size=1.0):
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = body
    curve.size = size
    obj = bpy.data.objects.new(name, curve)
    collection.objects.link(obj)
    obj.location = location
    obj["tm_mediatracker_preview"] = True
    return obj


def _map_camera_height(scene, root):
    map_root = next((
        collection for collection in scene.collection.children
        if collection.get("tm_import_version") == 4 and root.name in collection.all_objects
    ), None)
    if map_root is None:
        return 0.0
    if "tm_decoration_height_m" in map_root:
        return float(map_root["tm_decoration_height_m"])
    if "tm_mediatracker_height_m" in map_root:
        return float(map_root["tm_mediatracker_height_m"])
    source = map_root.get("tm_source_path", "")
    if not source:
        return 0.0
    # Older saved imports applied the map height without persisting it.
    # Recover it once, so their already-imported cameras match the geometry.
    import GBXBPY
    from GBXBPY.errors import GbxbpyError
    from GBXBPY.map_import_v4.decoration_map import resolve_absolute_height
    from GBXBPY.model.map import Map
    from GBXBPY.resolve import Resolver
    from . import _get_gamedata_dir

    gamedata = _get_gamedata_dir(bpy.context)
    if not gamedata:
        raise ValueError("Set the GameData folder in addon preferences to recover camera height")
    try:
        asset = GBXBPY.load(bpy.path.abspath(source))
        if not isinstance(asset, Map):
            raise ValueError("Camera height source is not a map")
        height = resolve_absolute_height(asset, Resolver(gamedata))
    except (GbxbpyError, OSError) as exc:
        raise ValueError(f"Cannot recover camera height; reopen the source map or reimport: {exc}") from exc
    map_root["tm_mediatracker_height_m"] = height
    log.info("Recovered MediaTracker height for %s: %.3f m", map_root.name, height)
    return height


def _camera_rotation(key):
    raw = key.get("rotation")
    if isinstance(raw, dict) and all(axis in raw for axis in ("x", "y", "z", "w")):
        orientation = Quaternion((raw["w"], raw["x"], raw["y"], raw["z"]))
        if orientation.magnitude < 1e-8:
            raise ValueError("MediaTracker camera has a zero rotation quaternion")
        orientation.normalize()
    else:
        angles = _vec3(key.get("pitchYawRoll")) or _vec3(key.get("camPitchYawRoll"))
        if angles is None:
            return None
        orientation = Euler(angles, "YXZ").to_quaternion()
    # Camera local -Z/Y axes are already the camera convention: convert the
    # world basis only (not the usual C R C^-1 object-basis conversion).
    return Quaternion((1.0, 0.0, 0.0), math.pi / 2) @ orientation


def _add_preview_camera(collection, scene, track_index, block_index, block, height=0.0):
    camera_data = bpy.data.cameras.new(f"MT Camera {track_index + 1}.{block_index + 1}")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    collection.objects.link(camera)
    camera["tm_mediatracker_preview"] = True
    camera["tm_mediatracker_track"] = track_index
    camera["tm_mediatracker_block"] = block_index
    camera["tm_mediatracker_range"] = _time_range(block)
    keys = []
    for cid, chunk in block.chunks.items():
        if not isinstance(chunk, dict):
            continue
        for key in chunk.get("keys", []):
            if isinstance(key, dict) and isinstance(key.get("time"), (float, int)):
                keys.append(key)
    if not keys:
        keys = [chunk for chunk in block.chunks.values() if isinstance(chunk, dict)]
    keys.sort(key=lambda key: key.get("time", _time_range(block)[0]))
    for key in keys:
        time = key.get("time", _time_range(block)[0])
        position = _vec3(key.get("position")) or _vec3(key.get("camPosition"))
        target = _vec3(key.get("targetPosition"))
        if block.class_id == 0x030A0000:
            radius = float(key.get("radius", 0.0))
            longitude = float(key.get("longitude", 0.0))
            latitude = float(key.get("latitude", 0.0))
            target = target or (0.0, 0.0, 0.0)
            position = (
                target[0] + radius * math.cos(latitude) * math.sin(longitude),
                target[1] + radius * math.sin(latitude),
                target[2] + radius * math.cos(latitude) * math.cos(longitude),
            )
        if position is not None:
            from GBXBPY.coords import gbx_to_blender_vec3

            camera.location = gbx_to_blender_vec3(*position).as_tuple()
            camera.location.z += height
            frame = _frame(float(time), scene)
            camera.keyframe_insert(data_path="location", frame=frame)
            rotation = _camera_rotation(key)
            if rotation is not None:
                camera.rotation_euler = rotation.to_euler("XYZ", camera.rotation_euler)
                camera.keyframe_insert(data_path="rotation_euler", frame=frame)
            elif target is not None and key.get("target", 0) >= 0:
                target = gbx_to_blender_vec3(*target).as_tuple()
                direction = Vector((target[0], target[1], target[2] + height)) - camera.location
                if direction.length > 1e-6:
                    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
                    camera.keyframe_insert(data_path="rotation_euler", frame=frame)
        fov = key.get("fov", key.get("camFov"))
        if isinstance(fov, (int, float)) and 0 < fov < 180:
            camera.data.lens = 36.0 / (2.0 * math.tan(math.radians(fov) / 2.0))
            camera.data.keyframe_insert(data_path="lens", frame=_frame(float(time), scene))
        near = key.get("nearZ", key.get("camNearClipPlane"))
        far = key.get("camFarClipPlane")
        if isinstance(near, (float, int)) and near > 0:
            camera.data.clip_start = near
        if isinstance(far, (float, int)) and far > camera.data.clip_start:
            camera.data.clip_end = far
    for animated in (camera, camera.data):
        action = animated.animation_data.action if animated.animation_data else None
        if action is not None:
            action["tm_mediatracker_preview"] = True
            from . import _find_fcurve
            channels = (("location", 3), ("rotation_euler", 3)) if animated == camera else (("lens", 1),)
            for path, size in channels:
                for index in range(size):
                    curve = _find_fcurve(action, path, index)
                    if curve is not None:
                        for point in curve.keyframe_points:
                            point.interpolation = "LINEAR"
    return camera


def _remove_legacy_timeline(root):
    """Discard generated timing proxies, not user animation or preview keyframes."""
    def generated(action):
        return action is not None and action.get("tm_mediatracker_preview") and all(
            name in action for name in (
                "tm_mediatracker_track", "tm_mediatracker_block", "tm_mediatracker_lane",
            )
        )

    legacy_names = set()
    if "tm_mediatracker_nla_layout" in root:
        try:
            layout = json.loads(root["tm_mediatracker_nla_layout"])
            if not isinstance(layout, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                for entry in layout
            ):
                raise ValueError("Invalid legacy timing layout")
            legacy_names = {entry["name"] for entry in layout}
        except (ValueError, TypeError) as exc:
            log.warning("Cannot identify old empty timing tracks for %s: %s", root.name, exc)
    removed = False
    animation = root.animation_data
    if animation is not None:
        for track in list(animation.nla_tracks):
            removed_strips = False
            for strip in list(track.strips):
                if generated(strip.action):
                    track.strips.remove(strip)
                    removed_strips = removed = True
            if not track.strips and (removed_strips or track.name in legacy_names):
                animation.nla_tracks.remove(track)
                removed = True
        if generated(animation.action):
            animation.action = None
            removed = True
    for name in ("tm_mediatracker_strip_value", "tm_mediatracker_nla_layout"):
        if name in root:
            del root[name]
    for action in list(bpy.data.actions):
        if generated(action) and action.users == 0:
            bpy.data.actions.remove(action)
    if removed:
        log.info("Removed legacy MediaTracker timing strips from %s; using saved document", root.name)


def _clear_tracker_preview(root):
    for obj in list(root.children):
        if not obj.get("tm_mediatracker_preview"):
            continue
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)
    _remove_legacy_timeline(root)
    for action in list(bpy.data.actions):
        if action.get("tm_mediatracker_preview") and action.users == 0:
            bpy.data.actions.remove(action)


def _build_preview(scene, document, root=None):
    original_camera = (
        bpy.data.objects.get(scene.get("tm_mediatracker_preview_camera_original", ""))
        if scene.get("tm_mediatracker_preview_active")
        else scene.camera
    )
    if scene.get("tm_mediatracker_preview_active"):
        scene.camera = original_camera
    if root is None:
        collection = bpy.data.collections.new(PREVIEW_COLLECTION)
        scene.collection.children.link(collection)
        collection["tm_mediatracker_preview"] = True
        root = _create_tracker(collection, "standalone", document.source_label or "Clip")
    collection = next((
        owner for owner in root.users_collection if owner.get("tm_mediatracker_collection")
    ), root.users_collection[0])
    has_camera = any(
        block.class_id in (0x03084000, 0x030A0000, 0x030A1000, 0x030A2000)
        for track in document.clip.tracks for block in track.blocks
    )
    height = _map_camera_height(scene, root) if has_camera else 0.0
    _clear_tracker_preview(root)
    settings = root.tm_mediatracker
    supported, unsupported = 0, set()
    for track_index, track_data in enumerate(document.clip.tracks):
        for block_index, block in sorted(
            enumerate(track_data.blocks), key=lambda item: _time_range(item[1])[0],
        ):
            start, end = _time_range(block)
            scene.frame_end = max(scene.frame_end, math.ceil(_frame(end, scene)))
            preview_type = _PREVIEW_CLASS_IDS.get(block.class_id)
            if preview_type is None:
                unsupported.add(block.class_id)
                continue
            if block.class_id == 0x03084000 and any(
                isinstance(chunk, dict) and chunk.get("clipEntId", -1) >= 0
                for chunk in block.chunks.values()
            ):
                # Game cameras need a vehicle pose, not their zero local offset.
                unsupported.add(block.class_id)
                continue
            supported += 1
            if block.class_id in (0x03084000, 0x030A0000, 0x030A1000, 0x030A2000):
                _add_preview_camera(
                    collection, scene, track_index, block_index, block, height=height,
                )
            elif block.class_id in (0x030A8000, 0x030A5000):
                chunk_id = 0x030A8001 if block.class_id == 0x030A8000 else block.class_id
                effect = _first_effect(block, chunk_id)
                content = ""
                if block.class_id == 0x030A8000:
                    chunk = _first_block_chunk(block, chunk_id)
                    content = str(chunk.get("text", "")) if chunk else ""
                else:
                    content = "Image preview placeholder"
                keys = []
                if effect:
                    for chunk in effect.values():
                        if isinstance(chunk, dict):
                            keys.extend(chunk.get("keys", []))
                if not keys:
                    keys = [{"time": start}]
                for key in keys:
                    pos = key.get("position")
                    pos2 = (pos.get("x", 0.0), pos.get("y", 0.0)) if isinstance(pos, dict) else (0.0, 0.0)
                    from GBXBPY.coords import gbx_to_blender_vec3

                    location = gbx_to_blender_vec3(float(pos2[0]), 0.0, float(pos2[1]))
                    obj = _add_preview_text(
                        collection,
                        f"MT {preview_type} {track_index + 1}.{block_index + 1}",
                        content or preview_type,
                        location.as_tuple(),
                    )
                    obj["tm_mediatracker_range"] = (start, end)
                    if isinstance(key.get("time"), (float, int)):
                        obj.keyframe_insert(data_path="location", frame=_frame(float(key["time"]), scene))
                if obj.animation_data and obj.animation_data.action:
                    obj.animation_data.action["tm_mediatracker_preview"] = True
            elif block.class_id == 0x030AB000:
                marker = _add_preview_text(
                    collection,
                    f"MT Fade {track_index + 1}.{block_index + 1}",
                    "Fade / transition (timeline marker)",
                    (0.0, 0.0, 0.0),
                    size=0.5,
                )
                marker["tm_mediatracker_range"] = (start, end)

    scene["tm_mediatracker_preview_active"] = True
    scene["tm_mediatracker_preview_camera_original"] = original_camera.name if original_camera else ""
    for obj in collection.all_objects:
        if obj.get("tm_mediatracker_preview") and obj.parent is None:
            obj.parent = root
    settings.message = f"Preview representations: {supported}; unsupported block types: {len(unsupported)}."
    settings.unsupported_blocks = ", ".join(f"0x{cid:08X}" for cid in sorted(unsupported))
    _store_document(root, document)
    if scene.get("tm_map_preview_tracker"):
        scene["tm_map_preview_tracker"] = root.name
    _update_preview_camera(scene)


def _update_preview_camera(scene):
    if not scene.get("tm_mediatracker_preview_active"):
        return
    tracker_name = scene.get("tm_map_preview_tracker", "")
    tracker = scene.objects.get(tracker_name) if tracker_name else _selected_tracker(bpy.context)
    cameras = [
        obj for obj in scene.objects
        if obj.get("tm_mediatracker_preview") and obj.type == "CAMERA"
        and obj.parent == tracker and "tm_mediatracker_range" in obj
    ]
    seconds = (scene.frame_current_final - 1) * scene.render.fps_base / scene.render.fps
    active = max((
        obj for obj in cameras
        if obj.get("tm_mediatracker_range", (0.0, 0.0))[0] - 1e-6 <= seconds
        <= obj.get("tm_mediatracker_range", (0.0, 0.0))[1] + 1e-6
    ), key=lambda obj: obj["tm_mediatracker_range"][0], default=None)
    if active is None and cameras and seconds >= 0:
        first = min(cameras, key=lambda obj: obj["tm_mediatracker_range"][0])
        if seconds < first["tm_mediatracker_range"][0]:
            active = first
    if active is not None:
        scene.camera = active
    elif scene.camera is None or scene.camera.get("tm_mediatracker_preview"):
        # Only hand back a camera the preview took over; never replace one the
        # user (or Render View's temporary camera) has set in the meantime.
        original_name = scene.get("tm_mediatracker_preview_camera_original", "")
        original = bpy.data.objects.get(original_name) if original_name else None
        if original is not None or scene.camera is not None:
            scene.camera = original
    for obj in scene.objects:
        if not obj.get("tm_mediatracker_preview"):
            continue
        time_range = obj.get("tm_mediatracker_range")
        if time_range is not None and obj.type != "CAMERA":
            obj.hide_set(
                obj.parent != tracker
                or not (time_range[0] <= seconds <= time_range[1])
            )


@bpy.app.handlers.persistent
def _frame_change(scene, _depsgraph=None):
    # Camera/visibility changes affect graph relations and must happen before
    # evaluation, not while Blender is refreshing evaluated object bases.
    # Renders step frames (motion blur, velocity) on the render thread and run
    # this handler too; changing scene.camera then crashes EEVEE's camera sync.
    if (_depsgraph is not None and _depsgraph.mode == "RENDER") or bpy.app.is_job_running("RENDER"):
        return
    try:
        _update_preview_camera(scene)
    except Exception:
        log.exception("MediaTracker preview update failed")


class TMBRIDGE_OT_ImportMediaClip(bpy.types.Operator, ImportHelper):
    """Import a standalone MediaTracker clip into the editor"""

    bl_idname = "trackmania_blender_bridge.import_media_clip"
    bl_label = "Import MediaTracker Clip"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Clip.Gbx"
    filter_glob: StringProperty(default="*.Clip.Gbx;*.clip.gbx", options={"HIDDEN"})
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    target: StringProperty(name="MediaTracker", options={"SKIP_SAVE"})

    def draw(self, context):
        self.layout.prop_search(self, "target", context.scene, "objects")
        self.layout.label(text="Clip tracks are appended to this MediaTracker.")

    def invoke(self, context, event):
        root = _selected_tracker(context)
        self.target = root.name if root is not None else ""
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        from . import _show_console_on_import

        _show_console_on_import(context)
        try:
            root = context.scene.objects.get(self.target) if self.target else _selected_tracker(context)
            from ._map_panel import map_root

            if self.target and root is None:
                raise ValueError("The selected MediaTracker no longer exists in this scene")
            if root is not None and not _is_tracker(self, root):
                raise ValueError("The import target must be a MediaTracker Empty")
            if map_root(context.scene) is not None and root is None:
                raise ValueError("Choose a map MediaTracker Empty as the import target")
            paths = [
                str(Path(bpy.path.abspath(self.directory)) / file.name)
                for file in self.files
            ] or [self.filepath]
            documents = [(filepath, _load_document(filepath)) for filepath in paths]
            if root is not None:
                document = _read_document(root)
                if root["tm_mediatracker_slot"] == "standalone" and not document.clip.tracks:
                    document, additional = documents[0][1], documents[1:]
                else:
                    additional = documents
            else:
                document = documents[0][1]
                additional = documents[1:]
            for _filepath, extra in additional:
                document.clip.tracks.extend(extra.clip.tracks)
                document.editable = False
            if root is None:
                collection = bpy.data.collections.new(PREVIEW_COLLECTION)
                context.scene.collection.children.link(collection)
                root = _create_tracker(collection, "standalone", document.source_label)
            _build_preview(context.scene, document, root=root)
            settings = root.tm_mediatracker
            for obj in context.selected_objects:
                obj.select_set(False)
            root.select_set(True)
            context.view_layer.objects.active = root
            settings.message = f"Added {len(paths)} clip(s) to {root.name}. Right-click its Empty to edit the MediaTracker."
            self.report({"INFO"}, settings.message)
            return {"FINISHED"}
        except Exception as exc:
            log.exception("Could not load MediaTracker clip")
            self.report({"ERROR"}, f"Could not load clip: {exc}")
            return {"CANCELLED"}


class TMBRIDGE_OT_ExportMediaClip(bpy.types.Operator, ExportHelper):
    """Export the loaded MediaTracker clip with applied block data edits"""

    bl_idname = "trackmania_blender_bridge.export_media_clip"
    bl_label = "Export MediaTracker Clip"
    bl_options = {"REGISTER"}

    filename_ext = ".Clip.Gbx"

    def invoke(self, context, _event):
        root = _selected_tracker(context)
        if root is None:
            self.report({"ERROR"}, "Select a MediaTracker Empty first")
            return {"CANCELLED"}
        source = root.tm_mediatracker.source_path
        self.filepath = str(Path(source).with_name(f"{Path(source).stem}.edited.Clip.Gbx")) if source else ""
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            settings = _settings(context)
            if settings.read_only:
                raise ValueError("Map or combined clip data cannot be exported as a standalone clip")
            document = _read_document(settings.id_data)
            from GBXBPY import write_media_clip

            write_media_clip(document, self.filepath)
            _store_document(settings.id_data, document)
            settings.message = f"Exported {self.filepath}"
            self.report({"INFO"}, settings.message)
            return {"FINISHED"}
        except Exception as exc:
            log.exception("Could not export MediaTracker clip")
            self.report({"ERROR"}, f"Could not export clip: {exc}")
            return {"CANCELLED"}


class TMBRIDGE_OT_ViewMediaPreviewCamera(bpy.types.Operator):
    """Look through the active MediaTracker preview camera"""

    bl_idname = "trackmania_blender_bridge.view_mediatracker_camera"
    bl_label = "View Preview Camera"

    def execute(self, context):
        area = context.area
        if area is None or area.type != "VIEW_3D":
            area = next(
                (candidate for candidate in context.screen.areas if candidate.type == "VIEW_3D"),
                None,
            )
        if area is None:
            self.report({"ERROR"}, "No 3D View area is open")
            return {"CANCELLED"}
        area.spaces.active.region_3d.view_perspective = "CAMERA"
        area.spaces.active.use_local_camera = False
        return {"FINISHED"}


CLASSES = (
    TMBRIDGE_PG_MediaTracker,
    TMBRIDGE_OT_ImportMediaClip,
    TMBRIDGE_OT_ExportMediaClip,
    TMBRIDGE_OT_ViewMediaPreviewCamera,
)


def register_scene_props():
    bpy.types.Object.tm_mediatracker = PointerProperty(type=TMBRIDGE_PG_MediaTracker)
    if _frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change)
    if _frame_change not in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.append(_frame_change)
    if _organize_map_trackers not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_organize_map_trackers)
    # Blender restricts bpy.data during addon registration.
    if not bpy.app.timers.is_registered(_organize_map_trackers):
        bpy.app.timers.register(_organize_map_trackers, first_interval=0.0)


def unregister_scene_props():
    if bpy.app.timers.is_registered(_organize_map_trackers):
        bpy.app.timers.unregister(_organize_map_trackers)
    if _organize_map_trackers in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_organize_map_trackers)
    if _frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change)
    if _frame_change in bpy.app.handlers.frame_change_pre:
        bpy.app.handlers.frame_change_pre.remove(_frame_change)
    for scene in bpy.data.scenes:
        if not scene.get("tm_mediatracker_preview_active"):
            continue
        original_name = scene.get("tm_mediatracker_preview_camera_original", "")
        scene.camera = bpy.data.objects.get(original_name) if original_name else None
        scene["tm_mediatracker_preview_active"] = False
    if hasattr(bpy.types.Object, "tm_mediatracker"):
        del bpy.types.Object.tm_mediatracker


__all__ = [
    "CLASSES",
    "register_scene_props",
    "unregister_scene_props",
    "populate_map_preview",
]
