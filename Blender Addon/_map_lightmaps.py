"""Packed, inspection-only images from a map's embedded lightmap cache."""

import json
import logging
import os
import tempfile

import bpy
from GBXBPY.errors import GbxbpyError

log = logging.getLogger("trackmania_blender_bridge.lightmaps")
READ_ERRORS = (OSError, RuntimeError, ValueError, KeyError, TypeError, GbxbpyError)
IMAGES_KEY = "tm_lightmap_images"
ERROR_KEY = "tm_lightmap_error"
_pending = set()


def entries(root):
    return json.loads(root.get(IMAGES_KEY, "[]"))


def grouped_entries(root):
    groups = {}
    for item in entries(root):
        set_number, slot = map(int, item["label"].removeprefix("Light Map ").split("."))
        groups.setdefault(set_number, []).append((slot, item))
    return [(number, sorted(items, key=lambda pair: pair[0]))
            for number, items in sorted(groups.items())]


def embedded_images(parsed):
    from GBXBPY.load._gbx_helpers import body_dict

    chunk = body_dict(parsed).get(0x0304305B)
    cache = chunk.get("lightmaps") if chunk else None
    for frame_index, frame in enumerate(cache.get("lightmapFrames", []) if cache else []):
        for channel in range(1, 4):
            payload = frame.get(f"frame{channel}", b"")
            if payload:
                yield f"Light Map {frame_index + 1}.{channel}", payload


def load_images(root):
    import GBXBPY

    source = bpy.path.abspath(root.get("tm_source_path", ""))
    if not source or not os.path.isfile(source):
        raise ValueError("Source map unavailable")
    asset = GBXBPY.load(source)
    created = []
    result = []
    try:
        with tempfile.TemporaryDirectory(prefix="tm_lightmaps_") as folder:
            for index, (label, payload) in enumerate(embedded_images(asset.raw)):
                # The loader detects the format from the bytes (WebP, DDS, etc.).
                path = os.path.join(folder, f"{index}.img")
                with open(path, "wb") as stream:
                    stream.write(payload)
                image = bpy.data.images.load(path, check_existing=False)
                created.append(image)
                if min(image.size) <= 0 or not image.has_data:
                    raise ValueError(f"{label} could not be decoded")
                image.name = f"{root.get('tm_map_name', root.name)} - {label}"
                image.pack()
                image.use_fake_user = True
                result.append({"label": label, "image": image.name})
    except (OSError, RuntimeError, ValueError):
        for image in created:
            bpy.data.images.remove(image)
        raise
    previous = entries(root)
    root[IMAGES_KEY] = json.dumps(result)
    root[ERROR_KEY] = ""
    for item in previous:
        image = bpy.data.images.get(item["image"])
        if image is not None:
            image.use_fake_user = False
            if image.users == 0:
                bpy.data.images.remove(image)
    return result


def _discover():
    from . import _progress

    if _progress.active():
        return 0.25
    names = tuple(_pending)
    _pending.clear()
    for name in names:
        root = bpy.data.collections.get(name)
        if root is None:
            continue
        try:
            load_images(root)
        except READ_ERRORS as exc:
            log.exception("Could not read lightmaps for %s", name)
            root[ERROR_KEY] = str(exc)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return None


def request_discovery(root):
    _pending.add(root.name)
    if not bpy.app.timers.is_registered(_discover):
        bpy.app.timers.register(_discover, first_interval=0.1)


def unregister():
    if bpy.app.timers.is_registered(_discover):
        bpy.app.timers.unregister(_discover)
    _pending.clear()


def show_image(context, image):
    screens = [context.screen] + [
        window.screen for window in context.window_manager.windows
        if window.screen != context.screen
    ]
    for screen in screens:
        for area in screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = image
                area.tag_redraw()
                return area
    if bpy.app.background or context.area is None:
        raise RuntimeError("An interactive Blender window is required")
    before = {window.as_pointer() for window in context.window_manager.windows}
    result = bpy.ops.screen.area_dupli("INVOKE_DEFAULT")
    if result != {"FINISHED"}:
        raise RuntimeError("Could not open an Image Editor window")
    window = next(
        (window for window in context.window_manager.windows if window.as_pointer() not in before),
        None,
    )
    if window is None:
        raise RuntimeError("Image Editor window unavailable")
    area = window.screen.areas[0]
    area.type = "IMAGE_EDITOR"
    area.ui_type = "IMAGE_EDITOR"
    area.spaces.active.image = image
    area.tag_redraw()
    return area
