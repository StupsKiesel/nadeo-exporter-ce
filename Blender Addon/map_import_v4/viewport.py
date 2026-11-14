"""Frame the playable footprint, not the distant decoration geometry."""

import logging
import math

from mathutils import Vector

log = logging.getLogger("trackmania_blender_bridge.map_import_v4")


def frame_map(context, plan):
    width, _, depth = plan.map_size
    if width <= 0 or depth <= 0:
        log.warning("Cannot frame map: buildable map dimensions are unavailable")
        return
    width, depth = width * 32.0, depth * 32.0
    heights = [
        placement.matrix[2][3] for placement in plan.placements
        if not placement.hidden and placement.section != "Decoration"
        and 0 <= placement.matrix[0][3] <= width
        and -depth <= placement.matrix[1][3] <= 0
    ]
    bottom, top = (min(heights), max(heights)) if heights else (0.0, 0.0)
    center = Vector((width / 2, -depth / 2, (bottom + top) / 2))
    corners = [Vector((x, y, z, 1)) for x in (0, width)
               for y in (-depth, 0) for z in (bottom, top + 32)]
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        space = area.spaces.active
        rv = space.region_3d
        rv.view_perspective = "PERSP"
        rv.view_rotation = Vector((1, 1, 1)).to_track_quat("Z", "Y")
        rv.view_location = center
        rv.view_distance = math.sqrt(width * width + depth * depth + (top - bottom) ** 2)
        space.clip_end = max(space.clip_end, rv.view_distance * 10)
        # Fit against Blender's actual projection, including the viewport aspect.
        for _ in range(40):
            rv.update()
            projected = [rv.perspective_matrix @ corner for corner in corners]
            if all(p.w > 0 and max(abs(p.x), abs(p.y)) <= p.w * 0.85 for p in projected):
                break
            rv.view_distance *= 1.2
        else:
            log.warning("Viewport map framing did not converge")
        area.tag_redraw()
