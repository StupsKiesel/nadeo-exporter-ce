"""Render the scene from the current 3D viewport view.

Blender can only render through a camera object, so a temporary camera
matching the viewport (view matrix, lens, clipping, aspect) is made the
scene camera for the render. ``render_complete`` / ``render_cancel`` run on
the render thread, so they only schedule a main-thread timer that removes the
camera once the render job has ended. When the viewport already looks through the scene camera
that camera is used unchanged.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix, Vector
TEMP_CAMERA = "TM_RenderFromView"
# The viewport uses a 36 mm sensor with a 2x zoom factor in perspective
# (CAMERA_PARAM_ZOOM_INIT_PERSP), i.e. an effective 72 mm sensor.
VIEWPORT_SENSOR = 72.0

_restore: dict | None = None


def _find_view(context):
    area = context.area if context.area and context.area.type == "VIEW_3D" else None
    if area is None:
        for candidate in context.window.screen.areas:
            if candidate.type == "VIEW_3D":
                area = candidate
                break
    if area is None:
        return None, None, None
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    space = area.spaces.active
    return area, region, space


def _depth_behind(view_world) -> float:
    """Distance (+1 m margin) the furthest visible geometry lies behind the view."""
    inv = view_world.inverted()
    furthest = 0.0
    # Depsgraph instances include collection-instanced geometry (V4 maps).
    for inst in bpy.context.evaluated_depsgraph_get().object_instances:
        obj = inst.object
        if obj.type in {"CAMERA", "LIGHT", "EMPTY"}:
            continue
        to_view = inv @ inst.matrix_world
        for corner in obj.bound_box:
            # Camera looks down -Z; positive Z is behind it.
            furthest = max(furthest, (to_view @ Vector(corner)).z)
    return furthest + 1.0


def _make_camera(scene, region, space):
    rv3d = space.region_3d
    data = bpy.data.cameras.new(TEMP_CAMERA)
    cam = bpy.data.objects.new(TEMP_CAMERA, data)
    scene.collection.objects.link(cam)
    cam.matrix_world = rv3d.view_matrix.inverted()
    data.sensor_fit = "AUTO"
    data.sensor_width = VIEWPORT_SENSOR
    data.clip_start = space.clip_start
    data.clip_end = space.clip_end
    if rv3d.view_perspective == "ORTHO" or not rv3d.is_perspective:
        # The ortho viewport also shows geometry behind the view point; pull
        # the camera back just past it. A fixed large offset (e.g. clip_end)
        # costs float precision: 100 km shifted the image by ~3% of its width.
        data.type = "ORTHO"
        data.ortho_scale = rv3d.view_distance * VIEWPORT_SENSOR / space.lens
        back = _depth_behind(cam.matrix_world)
        cam.matrix_world = cam.matrix_world @ Matrix.Translation((0.0, 0.0, back))
        data.clip_start = 0.001
        data.clip_end = back + space.clip_end
    else:
        data.lens = space.lens
    return cam


def _remove_handlers() -> None:
    for handlers in (bpy.app.handlers.render_complete, bpy.app.handlers.render_cancel):
        if _on_render_end in handlers:
            handlers.remove(_on_render_end)


def _on_render_end(*_args) -> None:
    # Render handlers run on the render thread; editing bpy.data there (removing
    # the temporary camera, switching scene.camera) crashes Blender. Defer the
    # restore to a main-thread timer that waits for the render job to finish.
    if not bpy.app.timers.is_registered(_deferred_cleanup):
        bpy.app.timers.register(_deferred_cleanup, first_interval=0.1)


def _deferred_cleanup():
    if bpy.app.is_job_running("RENDER"):
        return 0.1
    _cleanup()
    return None


def _cleanup(*_args) -> None:
    global _restore
    state, _restore = _restore, None
    _remove_handlers()
    if state is None:
        return
    scene = bpy.data.scenes.get(state["scene"])
    if scene is not None:
        scene.camera = bpy.data.objects.get(state["camera"]) if state["camera"] else None
        scene.render.resolution_x, scene.render.resolution_y = state["resolution"]
    cam = bpy.data.objects.get(TEMP_CAMERA)
    if cam is not None:
        data = cam.data
        bpy.data.objects.remove(cam)
        if data is not None and data.users == 0:
            bpy.data.cameras.remove(data)


class TMBRIDGE_OT_RenderFromView(bpy.types.Operator):
    """Render an image from the active 3D viewport view"""

    bl_idname = "trackmania_blender_bridge.render_from_view"
    bl_label = "Render from View"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.window is not None and not bpy.app.is_job_running("RENDER")

    def execute(self, context):
        global _restore
        area, region, space = _find_view(context)
        if space is None or region is None:
            self.report({"ERROR"}, "No 3D viewport found")
            return {"CANCELLED"}
        scene = context.scene
        if _restore is not None:
            _cleanup()

        if space.region_3d.view_perspective == "CAMERA" and scene.camera is not None:
            return bpy.ops.render.render("INVOKE_DEFAULT")

        render = scene.render
        _restore = {
            "scene": scene.name,
            "camera": scene.camera.name if scene.camera else "",
            "resolution": (render.resolution_x, render.resolution_y),
        }
        cam = _make_camera(scene, region, space)
        scene.camera = cam
        # Match the viewport aspect, keeping the configured width.
        render.resolution_y = max(1, round(render.resolution_x * region.height / max(1, region.width)))
        bpy.app.handlers.render_complete.append(_on_render_end)
        bpy.app.handlers.render_cancel.append(_on_render_end)
        result = bpy.ops.render.render("INVOKE_DEFAULT")
        if "RUNNING_MODAL" not in result and "FINISHED" not in result:
            _cleanup()
        return {"FINISHED"}


def unregister() -> None:
    if bpy.app.timers.is_registered(_deferred_cleanup):
        bpy.app.timers.unregister(_deferred_cleanup)
    _cleanup()


CLASSES = (TMBRIDGE_OT_RenderFromView,)
