"""Experimental material/texture evidence and editable trim rectangles."""

from array import array
import json
import logging
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

from . import ADDON_SHORT_NAME, _progress

log = logging.getLogger("trackmania_blender_bridge.trimsheet")
_handle = None


class TMBRIDGE_TrimRegion(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Trim")
    bounds: FloatVectorProperty(name="UV Rectangle", size=4, default=(0, 0, 1, 1), min=0, max=1, precision=5)
    evidence: StringProperty(default="MANUAL")
    confirmed: BoolProperty(name="Confirmed", description="Manually checked region; not an authored GBX trim definition")
    repeat: EnumProperty(name="Repeat", items=(
        ("UNKNOWN", "Unknown", "Not determined by this prototype"),
        ("NONE", "None", "Do not repeat"),
        ("U", "U", "Repeat horizontally"),
        ("V", "V", "Repeat vertically"),
        ("BOTH", "Both", "Repeat on both axes"),
    ), default="UNKNOWN")


class TMBRIDGE_TrimSheet(bpy.types.PropertyGroup):
    selected_material: PointerProperty(
        name="Material", type=bpy.types.Material,
        description="Blender material whose diffuse image is used to infer trim regions",
    )
    metadata: StringProperty()
    image: PointerProperty(type=bpy.types.Image)
    regions: CollectionProperty(type=TMBRIDGE_TrimRegion)
    active_region: IntProperty(default=0, min=0)
    show_overlay: BoolProperty(name="Show Rectangles", default=True)


def mesh_candidates(context, image_paths, reference_image=None):
    from GBXBPY.trimsheet import infer_uv_regions
    candidates, skipped, matches = [], 0, 0
    paths = {str(Path(path).resolve()).casefold() for path in image_paths}
    for obj in context.selected_objects:
        if obj.type != "MESH":
            continue
        uv = obj.data.uv_layers.get("BaseMaterial")
        if uv is None:
            continue
        for slot_index, slot in enumerate(obj.material_slots):
            material = slot.material
            if material is None or material.node_tree is None:
                continue
            images = [node.image for node in material.node_tree.nodes
                      if node.type == "TEX_IMAGE" and node.image is not None]
            if not any(image == reference_image or
                       (bool(image.filepath) and
                        str(Path(bpy.path.abspath(image.filepath)).resolve()).casefold() in paths)
                       for image in images):
                continue
            faces = [[tuple(uv.data[i].uv) for i in polygon.loop_indices]
                     for polygon in obj.data.polygons if polygon.material_index == slot_index]
            found, ignored = infer_uv_regions(faces)
            candidates.extend(found)
            skipped += ignored
            matches += len(faces)
    return candidates, skipped, matches


def _diffuse_image(material):
    if material is None:
        raise ValueError("Select a Blender material")
    if not material.use_nodes or material.node_tree is None:
        raise ValueError("The selected material has no shader nodes")

    nodes = material.node_tree.nodes
    pending = [link.from_node for node in nodes if node.type == "BSDF_PRINCIPLED"
               for link in node.inputs["Base Color"].links]
    visited, images = set(), []
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        if node.type == "TEX_IMAGE":
            if node.image is not None:
                images.append(node)
        else:
            pending.extend(link.from_node for socket in node.inputs for link in socket.links)

    if not images:
        images = [node for node in nodes if node.type == "TEX_IMAGE" and node.image is not None
                  and (node.label.casefold() in {"diffuse", "diffusetri"}
                       or node.name.casefold() in {"diffuse", "diffusetri"})]
    labelled = [node for node in images if node.label.casefold() in {"diffuse", "diffusetri"}
                or node.name.casefold() in {"diffuse", "diffusetri"}]
    choices = {node.image for node in labelled or images}
    if not choices:
        raise ValueError("No diffuse image found in the selected material")
    if len(choices) != 1:
        raise ValueError("Multiple base-color images found; label the diffuse image node 'Diffuse'")
    return choices.pop()


def generate(context, blender_material):
    from GBXBPY.trimsheet import infer_texture_regions

    image = _diffuse_image(blender_material)
    width, height = image.size
    if min(width, height) < 2 or len(image.pixels) != width * height * 4:
        raise ValueError(f"Invalid image buffer: {image.name}")
    buffer = array("f", [0]) * (width * height * 4)
    image.pixels.foreach_get(buffer)
    # Sample without scaling/mutating the shared material image.
    sample_w, sample_h = min(width, 256), min(height, 256)
    pixels = []
    for y in range(sample_h):
        row = []
        for x in range(sample_w):
            offset = 4 * (int(y * height / sample_h) * width + int(x * width / sample_w))
            row.append(buffer[offset:offset + 3])
        pixels.append(row)
    candidates = infer_texture_regions(pixels)
    image_path = bpy.path.abspath(image.filepath, library=image.library) if image.filepath else ""
    uv_candidates, skipped, matching_faces = mesh_candidates(context, [image_path] if image_path else [], image)
    candidates.extend(uv_candidates)
    diagnostics = ["No authored trim-region table decoded; all generated rectangles require review.",
                   "Texture silhouettes/bands are colour heuristics; bounds can include unused pixels.",
                   "Mesh rectangles bound UV islands, not trim semantics.",
                   "Candidates use the selected Blender material's diffuse image; GBX metadata is unavailable.",
                   "Inference uses at most 256 samples per axis; adjust rectangle edges against the full-resolution image.",
                   "Repeat directions and semantic names are unknown until manually assigned."]
    if image.packed_file is None and (image.source == "GENERATED" or image.is_dirty):
        diagnostics.append("Save or pack the image to preserve its pixels in the blend file.")
    if not candidates:
        diagnostics.append("No confident boundaries or in-tile mesh UV regions found; add rectangles manually.")
    if skipped:
        diagnostics.append(f"Skipped {skipped} degenerate/out-of-tile mesh UV islands; wrapping was not guessed.")
    metadata = {
        "schema": 1, "material_name": blender_material.name, "material_path": None,
        "base_material_path": None, "texture_size": None, "material_flags": None,
        "material_chain": None, "texture_slots": None,
        "textures": [{"role": "Diffuse", "image_path": image_path, "image_name": image.name}],
        "image_size": [width, height], "sample_size": [sample_w, sample_h],
        "shader_params": None,
        "matching_mesh_faces": matching_faces, "diagnostics": diagnostics,
    }
    encoded = json.dumps(metadata)
    sheet = context.scene.tm_trimsheet
    sheet.metadata = encoded
    sheet.image = image
    sheet.regions.clear()
    for index, candidate in enumerate(dict.fromkeys(candidates), start=1):
        region = sheet.regions.add()
        region.name = f"Candidate {index:02d}"
        region.bounds = candidate.bounds
        region.evidence = candidate.evidence
    sheet.active_region = 0
    text = bpy.data.texts.get("Trimsheet Diagnostics") or bpy.data.texts.new("Trimsheet Diagnostics")
    text.clear()
    text.write("\n".join(diagnostics))
    for message in diagnostics:
        log.info("%s", message)
    if context.area is not None and context.area.type == "IMAGE_EDITOR":
        context.space_data.image = image
        context.area.tag_redraw()
    return len(sheet.regions)


class TMBRIDGE_OT_GenerateTrimSheet(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.generate_trimsheet"
    bl_label = "Generate Candidates"
    bl_description = "Infer unconfirmed texture/mesh UV rectangles; replaces current trim regions"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        if context.scene.tm_trimsheet.regions:
            return context.window_manager.invoke_confirm(self, _event)
        return self.execute(context)

    @_progress.tracked("Generating Trimsheet")
    def execute(self, context):
        try:
            count = generate(context, context.scene.tm_trimsheet.selected_material)
        except Exception as exc:
            log.exception("Trimsheet generation failed")
            self.report({"ERROR"}, f"Trimsheet generation failed: {exc}")
            return {"CANCELLED"}
        self.report({"WARNING"}, f"{count} unconfirmed region(s); review rectangles; see Trimsheet Diagnostics")
        return {"FINISHED"}


class TMBRIDGE_OT_AddTrim(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.add_trim_region"
    bl_label = "Add Trim Region"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sheet = context.scene.tm_trimsheet
        sheet.regions.add()
        sheet.active_region = len(sheet.regions) - 1
        return {"FINISHED"}


class TMBRIDGE_OT_RemoveTrim(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.remove_trim_region"
    bl_label = "Remove Trim Region"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sheet = context.scene.tm_trimsheet
        if not 0 <= sheet.active_region < len(sheet.regions):
            self.report({"ERROR"}, "Select a trim region to remove")
            return {"CANCELLED"}
        sheet.regions.remove(sheet.active_region)
        sheet.active_region = max(0, min(sheet.active_region, len(sheet.regions) - 1))
        return {"FINISHED"}


def sheet_data(sheet):
    from GBXBPY.trimsheet import validate_bounds
    if not sheet.metadata:
        raise ValueError("Generate a material trimsheet first")
    data = json.loads(sheet.metadata)
    regions = []
    for region in sheet.regions:
        validate_bounds(region.bounds)
        if not region.name.strip():
            raise ValueError("Trim region names cannot be empty")
        regions.append({"name": region.name, "bounds": list(region.bounds), "repeat": region.repeat,
                        "evidence": region.evidence, "confirmed": region.confirmed})
    data["uv_origin"] = "BOTTOM_LEFT"
    data["regions"] = regions
    return data


class TMBRIDGE_OT_ExportTrimSheet(bpy.types.Operator, ExportHelper):
    bl_idname = "trackmania_blender_bridge.export_trimsheet"
    bl_label = "Export Trimsheet JSON"
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        try:
            data = sheet_data(context.scene.tm_trimsheet)
            if Path(self.filepath).suffix.casefold() != ".json":
                raise ValueError("Trimsheet export requires a .json filename")
            Path(self.filepath).write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (ValueError, OSError) as exc:
            log.exception("Trimsheet export failed")
            self.report({"ERROR"}, f"Trimsheet export failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, "Exported trim data; evidence/confirmation retained")
        return {"FINISHED"}


def zenuv_addon(context):
    return next((addon for addon in context.preferences.addons
                 if addon.module.rsplit(".", 1)[-1].casefold() == "zenuv"), None)


def load_into_zenuv(context):
    import importlib
    from types import SimpleNamespace

    addon = zenuv_addon(context)
    if addon is None:
        raise ValueError("Enable ZenUV before loading a trimsheet")
    sheet = context.scene.tm_trimsheet
    data = sheet_data(sheet)
    if sheet.image is None or sheet.image.library is not None:
        raise ValueError("A local trimsheet image is required")
    if not data["regions"]:
        raise ValueError("Add at least one trim region")
    names = [region["name"] for region in data["regions"]]
    if len(names) != len(set(names)):
        raise ValueError("ZenUV transfer requires unique trim names")
    utils = importlib.import_module(addon.module + ".ops.trimsheets.trimsheet_utils").ZuvTrimsheetUtils
    owner = sheet.image.zen_uv
    original = {trim.name: trim.to_dict() for trim in owner.trimsheet}
    original_index = owner.trimsheet_index
    original_mode = addon.preferences.trimsheet.mode
    trims = {}
    for region in data["regions"]:
        u0, v0, u1, v1 = region["bounds"]
        trims[region["name"]] = {"rect": (u0, v1, u1, v0),
                                 "color": utils.getTrimsheetRandomColor()}
    operation = SimpleNamespace(mode="CLEAR")
    try:
        utils.import_from_trim_dict(owner, operation, trims)
        owner.trimsheet_index = min(sheet.active_region, len(owner.trimsheet) - 1)
        addon.preferences.trimsheet.mode = "IMAGE"
    except Exception:
        log.exception("ZenUV transfer failed; restoring previous image trims")
        utils.import_from_trim_dict(owner, operation, original)
        owner.trimsheet_index = original_index
        addon.preferences.trimsheet.mode = original_mode
        raise
    if context.area is not None and context.area.type == "IMAGE_EDITOR":
        context.space_data.image = sheet.image
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {"IMAGE_EDITOR", "VIEW_3D"}:
                area.tag_redraw()
    return len(trims)


class TMBRIDGE_OT_LoadZenUVTrimSheet(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.load_zenuv_trimsheet"
    bl_label = "Load into ZenUV"
    bl_description = "Copy names and rectangles to the image's ZenUV trimsheet and switch ZenUV to Image mode"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        image = context.scene.tm_trimsheet.image
        owner = getattr(image, "zen_uv", None)
        if owner is not None and len(owner.trimsheet):
            return context.window_manager.invoke_confirm(
                self, event, message="Replace this image's existing ZenUV trims?")
        return self.execute(context)

    def execute(self, context):
        try:
            count = load_into_zenuv(context)
        except Exception as exc:
            log.exception("Could not load trimsheet into ZenUV")
            self.report({"ERROR"}, f"ZenUV transfer failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Loaded {count} trims into ZenUV (Image mode); names and rectangles only")
        return {"FINISHED"}


class TMBRIDGE_UL_Trims(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname):
        layout.prop(item, "name", text="", emboss=False,
                    icon="CHECKMARK" if item.confirmed else "QUESTION")


class TMBRIDGE_PT_TrimSheet(bpy.types.Panel):
    bl_label = "Trimsheet (Experimental)"
    bl_idname = "TMBRIDGE_PT_trimsheet"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME

    def draw(self, context):
        from GBXBPY.trimsheet import validate_bounds
        sheet = context.scene.tm_trimsheet
        layout = self.layout
        layout.prop(sheet, "selected_material")
        layout.operator(TMBRIDGE_OT_GenerateTrimSheet.bl_idname, icon="VIEWZOOM")
        layout.label(text="Inferred rectangles - review required", icon="INFO")
        if sheet.image is not None:
            layout.label(text=f"{sheet.image.size[0]} x {sheet.image.size[1]}  |  {len(sheet.regions)} regions")
        layout.prop(sheet, "show_overlay")
        row = layout.row()
        row.template_list("TMBRIDGE_UL_Trims", "", sheet, "regions", sheet, "active_region", rows=4)
        buttons = row.column(align=True)
        buttons.operator(TMBRIDGE_OT_AddTrim.bl_idname, text="", icon="ADD")
        buttons.operator(TMBRIDGE_OT_RemoveTrim.bl_idname, text="", icon="REMOVE")
        if 0 <= sheet.active_region < len(sheet.regions):
            region = sheet.regions[sheet.active_region]
            box = layout.box()
            box.prop(region, "name")
            box.label(text=f"Evidence: {region.evidence}")
            for index, label in enumerate(("U Min", "V Min", "U Max", "V Max")):
                box.prop(region, "bounds", index=index, text=label)
            box.prop(region, "repeat")
            box.prop(region, "confirmed")
            try:
                validate_bounds(region.bounds)
            except ValueError:
                box.label(text="Invalid rectangle: min must be < max", icon="ERROR")
        export = layout.row()
        export.enabled = bool(sheet.metadata)
        export.operator(TMBRIDGE_OT_ExportTrimSheet.bl_idname, icon="EXPORT")
        zen = layout.row()
        zen.enabled = bool(sheet.metadata and sheet.image and sheet.regions)
        zen.operator(TMBRIDGE_OT_LoadZenUVTrimSheet.bl_idname, icon="UV")


def draw_overlay():
    import gpu
    from gpu_extras.batch import batch_for_shader
    context = bpy.context
    if context.region is None or context.region.type != "WINDOW" or context.scene is None:
        return
    sheet = context.scene.tm_trimsheet
    if not sheet.show_overlay or sheet.image is None or context.space_data.image != sheet.image:
        return
    from GBXBPY.trimsheet import validate_bounds
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    previous = gpu.state.blend_get()
    gpu.state.blend_set("ALPHA")
    try:
        shader.bind()
        for index, region in enumerate(sheet.regions):
            try:
                validate_bounds(region.bounds)
            except ValueError:
                continue
            u0, v0, u1, v1 = region.bounds
            points = [context.region.view2d.view_to_region(u, v, clip=False)
                      for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1), (u0, v0))]
            shader.uniform_float("color", (1, .6, .1, 1) if index == sheet.active_region else
                                 ((.2, 1, .4, .8) if region.confirmed else (.2, .7, 1, .6)))
            batch_for_shader(shader, "LINE_STRIP", {"pos": points}).draw(shader)
    finally:
        gpu.state.blend_set(previous)


CLASSES = (TMBRIDGE_TrimRegion, TMBRIDGE_TrimSheet, TMBRIDGE_OT_GenerateTrimSheet,
           TMBRIDGE_OT_AddTrim, TMBRIDGE_OT_RemoveTrim, TMBRIDGE_OT_ExportTrimSheet,
           TMBRIDGE_OT_LoadZenUVTrimSheet,
           TMBRIDGE_UL_Trims, TMBRIDGE_PT_TrimSheet)


def register():
    global _handle
    bpy.types.Scene.tm_trimsheet = PointerProperty(type=TMBRIDGE_TrimSheet)
    _handle = bpy.types.SpaceImageEditor.draw_handler_add(draw_overlay, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    del bpy.types.Scene.tm_trimsheet
