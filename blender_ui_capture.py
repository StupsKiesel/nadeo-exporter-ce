"""Capture Blender's N-panel region or the whole Blender window."""

from array import array
import os
from datetime import datetime

import bpy
from bpy.props import StringProperty


bl_info = {
    "name": "Blender UI Capture",
    "author": "Nadeo Exporter CE",
    "version": (1, 0, 0),
    "blender": (3, 6, 2),
    "location": "F3 search in the target editor",
    "description": "Capture the visible N-panel or Blender window",
    "category": "3D View",
}


def _capture_paths(context, prefix):
    preferences = context.preferences.addons[__name__].preferences
    directory = bpy.path.abspath(preferences.output_directory)
    os.makedirs(directory, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{prefix}_{timestamp}"
    return (
        os.path.join(directory, f"{stem}_source.png"),
        os.path.join(directory, f"{stem}.png"),
    )


def _crop_image(source_path, output_path, crop_box):
    source = bpy.data.images.load(source_path, check_existing=False)
    cropped = None
    try:
        source_width, source_height = map(int, source.size)
        x, y, width, height = crop_box
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > source_width
            or y + height > source_height
        ):
            raise RuntimeError(
                f"N-panel bounds {crop_box} exceed screenshot "
                f"dimensions {source_width}x{source_height}"
            )

        source_pixels = array("f", [0.0]) * (source_width * source_height * 4)
        source.pixels.foreach_get(source_pixels)
        output_pixels = array("f", [0.0]) * (width * height * 4)
        row_size = width * 4
        for row in range(height):
            source_start = ((y + row) * source_width + x) * 4
            output_start = row * row_size
            output_pixels[output_start:output_start + row_size] = (
                source_pixels[source_start:source_start + row_size]
            )

        cropped = bpy.data.images.new(
            name="Blender UI Capture",
            width=width,
            height=height,
            alpha=True,
        )
        cropped.pixels.foreach_set(output_pixels)
        cropped.filepath_raw = output_path
        cropped.file_format = "PNG"
        cropped.save()
    finally:
        if cropped is not None:
            bpy.data.images.remove(cropped)
        bpy.data.images.remove(source)


class BlenderUICapturePreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    output_directory: StringProperty(
        name="Output Folder",
        description="Folder where screenshots are saved",
        subtype="DIR_PATH",
        default="//screenshots",
    )

    def draw(self, context):
        self.layout.prop(self, "output_directory")


class WM_OT_capture_n_panel(bpy.types.Operator):
    bl_idname = "wm.capture_n_panel"
    bl_label = "Capture Visible N-Panel"
    bl_description = "Save only the visible N-panel region of this editor"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.area is not None

    def execute(self, context):
        if not hasattr(bpy.ops.screen, "screenshot_area"):
            self.report(
                {"ERROR"},
                "N-panel capture is unavailable in this Blender version",
            )
            return {"CANCELLED"}

        area = context.area
        region = next(
            (item for item in area.regions if item.type == "UI"),
            None,
        )
        if region is None or region.width <= 1 or region.height <= 1:
            self.report({"ERROR"}, "Open the N-panel before capturing it")
            return {"CANCELLED"}

        source_path, output_path = _capture_paths(context, "n_panel")
        result = bpy.ops.screen.screenshot_area(
            "EXEC_DEFAULT",
            filepath=source_path,
            check_existing=False,
        )
        if "FINISHED" not in result:
            self.report({"ERROR"}, "Blender did not capture the editor")
            return {"CANCELLED"}

        crop_box = (
            region.x - area.x,
            region.y - area.y,
            region.width,
            region.height,
        )
        try:
            _crop_image(source_path, output_path, crop_box)
        finally:
            if os.path.exists(source_path):
                os.remove(source_path)

        self.report({"INFO"}, f"Saved N-panel screenshot: {output_path}")
        return {"FINISHED"}


class WM_OT_capture_blender_window(bpy.types.Operator):
    bl_idname = "wm.capture_blender_window"
    bl_label = "Capture Blender Window"
    bl_description = "Save an image of the whole Blender window"

    @classmethod
    def poll(cls, context):
        return context.window is not None

    def execute(self, context):
        source_path, output_path = _capture_paths(context, "window")
        result = bpy.ops.screen.screenshot(
            "EXEC_DEFAULT",
            filepath=source_path,
            check_existing=False,
        )
        if "FINISHED" not in result:
            self.report({"ERROR"}, "Blender did not capture the window")
            return {"CANCELLED"}

        os.replace(source_path, output_path)
        self.report({"INFO"}, f"Saved Blender-window screenshot: {output_path}")
        return {"FINISHED"}


_CLASSES = (
    BlenderUICapturePreferences,
    WM_OT_capture_n_panel,
    WM_OT_capture_blender_window,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
