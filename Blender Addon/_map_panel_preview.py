"""Compact live map sidebar, kept alongside the original Map panel."""

import json
import os
import re
import textwrap

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import ADDON_SHORT_NAME, _map_panel, _progress
from . import _map_panel_data as data
from . import _map_lightmaps as lightmaps
from ._text import plain_name

OP = "trackmania_blender_bridge."


class TMBRIDGE_MapPreviewSettings(bpy.types.PropertyGroup):
    show_thumbnail: BoolProperty(name="Thumbnail", description="Show the imported map thumbnail")
    show_details: BoolProperty(name="Details", description="Show map identifiers and environment details")
    asset_search: StringProperty(name="Search", options={"TEXTEDIT_UPDATE"})
    asset_index: IntProperty(name="Asset", min=0)


def map_blend_filename(name):
    """Strip ManiaPlanet text styles while preserving escaped dollar signs."""
    plain = plain_name(name)
    plain = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", plain).strip(" .")
    if not plain:
        plain = "Map"
    if plain.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
    }:
        plain = "_" + plain
    return plain + ".Map.blend"


class TMBRIDGE_OT_SaveMapBlend(bpy.types.Operator):
    bl_idname = OP + "save_map_blend"
    bl_label = "Save Map As"
    bl_description = "Open Save As with the map name, without Trackmania formatting"

    @classmethod
    def poll(cls, context):
        return _map_panel.map_root(context.scene) is not None and not _progress.active()

    def invoke(self, context, event):
        root = _map_panel.map_root(context.scene)
        name = root.get("tm_map_name") or data.info(root).get("map_name") or "Map"
        folder = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else bpy.path.abspath("//")
        result = bpy.ops.wm.save_as_mainfile(
            "INVOKE_DEFAULT", filepath=os.path.join(folder, map_blend_filename(name)),
            check_existing=True,
        )
        return {"FINISHED"} if result == {"RUNNING_MODAL"} else result


class TMBRIDGE_MapControls(bpy.types.PropertyGroup):
    map_name: StringProperty(name="Name", get=data.metadata_get("map_name"), set=data.metadata_set("map_name"))
    author: StringProperty(name="Author", get=data.metadata_get("author"), set=data.metadata_set("author"))
    map_type: StringProperty(name="Type", get=data.metadata_get("map_type"), set=data.metadata_set("map_type"))
    style: StringProperty(name="Style", get=data.metadata_get("map_style"), set=data.metadata_set("map_style"))
    laps: IntProperty(name="Laps", min=0, get=data.metadata_get("laps", 0), set=data.metadata_set("laps"))
    author_time: StringProperty(name="Author", get=data.metadata_get("author_time"), set=data.metadata_set("author_time"))
    gold_time: StringProperty(name="Gold", get=data.metadata_get("gold_time"), set=data.metadata_set("gold_time"))
    silver_time: StringProperty(name="Silver", get=data.metadata_get("silver_time"), set=data.metadata_set("silver_time"))
    bronze_time: StringProperty(name="Bronze", get=data.metadata_get("bronze_time"), set=data.metadata_set("bronze_time"))
    mood: EnumProperty(
        name="Mood",
        items=[("SUNRISE", "Sunrise", ""), ("DAY", "Day", ""),
               ("SUNSET", "Sunset", ""), ("NIGHT", "Night", "")],
        get=data.mood_get, set=data.mood_set, update=data.preset_update,
    )
    animate_time: BoolProperty(
        name="Dynamic", description="Cycle lighting with the timeline using the configured duration",
        update=data.environment_update,
    )
    cycle_seconds: FloatProperty(
        name="Cycle", description="Seconds per full day; Time of Day is the phase at the scene's start frame",
        default=300.0, min=1.0, unit="TIME", update=data.environment_update,
    )
    time_of_day: FloatProperty(
        name="Time of Day", min=0.0, max=1.0, subtype="FACTOR",
        get=data.time_get, set=data.time_set, update=data.environment_update,
    )
    clouds: BoolProperty(
        name="Clouds", description="Build cloud sprites and shadows; requires Sky",
        get=data.clouds_get, set=data.clouds_set, update=data.environment_update,
    )
    surroundings: BoolProperty(
        name="Surroundings", description="Show scenery in the viewport and renders; load it if missing",
        get=data.surroundings_get, set=data.surroundings_set,
    )
    sky: BoolProperty(
        name="Sky", description="Build the mood sky, sun, moon and stars",
        get=data.sky_get, set=data.sky_set, update=data.environment_update,
    )
    particles: BoolProperty(
        name="Particle FX", description="Include imported particle effects in renders; disabled when this map has no supported emitters. Viewport visibility is unchanged",
        get=data.particles_get, set=data.particles_set,
    )


def definitions(context, root):
    library = _map_panel.embedded_library(context.scene, root)
    return library, [coll for coll in library.children if coll.get("tm_asset_id")] if library else []


def selected_definition(context, root):
    library, _items = definitions(context, root)
    index = context.scene.tm_bridge_map_preview.asset_index
    if library is not None and 0 <= index < len(library.children):
        coll = library.children[index]
        needle = context.scene.tm_bridge_map_preview.asset_search.casefold()
        if coll.get("tm_asset_id") and needle in (coll.get("tm_asset_name") or coll.name).casefold():
            return coll
    return None


def diagnostics(root):
    issues = json.loads(root.get("tm_diagnostics", "[]"))
    issues += [{"code": "environment", "message": message}
               for message in json.loads(root.get("tm_map_controls_issues", "[]"))]
    if root.get(data.ERROR_KEY):
        issues.append({"code": "error", "message": root[data.ERROR_KEY]})
    return issues


class TMBRIDGE_OT_MapPreviewAction(bpy.types.Operator):
    bl_idname = OP + "map_preview_action"
    bl_label = "Map Action"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(items=[
        ("RESET", "Reset to Map", "Restore the map's original mood and time"),
        ("SELECT_ASSET", "Select Asset in Scene", "Select this map's placements of the embedded asset"),
        ("FRAME_ASSET", "Frame Instances", "Select and frame this asset's placements"),
        ("ISOLATE_ASSET", "Isolate Instances", "Toggle Local View for this asset's placements"),
        ("REVEAL_ASSET", "Reveal Definition", "Show the source collection in the Outliner"),
        ("WARNINGS", "Show Import Warnings", "View diagnostics and environment errors"),
    ])
    root_name: StringProperty(options={"HIDDEN"})
    asset_name: StringProperty(options={"HIDDEN"})

    @classmethod
    def description(cls, context, properties):
        return {
            "RESET": "Reset to the map's original mood and time",
            "SELECT_ASSET": "Select the embedded asset's placements in this map",
            "FRAME_ASSET": "Select and frame this asset's placements in the viewport",
            "ISOLATE_ASSET": "Isolate this asset's placements in Local View; click again to leave Local View",
            "REVEAL_ASSET": "Find the source collection in the Outliner without modifying the asset",
            "WARNINGS": "Show map import and environment diagnostics",
        }.get(properties.action, "Map action")

    @classmethod
    def poll(cls, context):
        return _map_panel.map_root(context.scene) is not None and not _progress.active()

    def invoke(self, context, event):
        if self.action == "WARNINGS":
            return context.window_manager.invoke_props_dialog(self, width=520)
        return self.execute(context)

    def draw(self, context):
        root = bpy.data.collections.get(self.root_name)
        if root is None:
            self.layout.label(text="Map unavailable", icon="ERROR")
            return
        issues = diagnostics(root)
        if not issues:
            self.layout.label(text="No warnings", icon="CHECKMARK")
        for issue in issues:
            box = self.layout.box()
            box.label(text=issue.get("code", "Warning"), icon="ERROR")
            for line in textwrap.wrap(issue.get("message", ""), width=72):
                box.label(text=line)
            if issue.get("source"):
                box.label(text=issue["source"], icon="FILE")

    def execute(self, context):
        root = bpy.data.collections.get(self.root_name)
        if root is None or root.name not in context.scene.collection.children:
            self.report({"ERROR"}, "Map unavailable in this scene")
            return {"CANCELLED"}
        try:
            if self.action == "RESET":
                root.tm_bridge_map_controls["animate_time"] = False
                data.apply(root, context, reset=True)
            elif self.action in {"SELECT_ASSET", "FRAME_ASSET", "ISOLATE_ASSET", "REVEAL_ASSET"}:
                definition = bpy.data.collections.get(self.asset_name)
                if definition is None or definition not in definitions(context, root)[1]:
                    raise ValueError("Embedded asset unavailable")
                if self.action == "REVEAL_ASSET":
                    outliner = next((area for area in context.screen.areas if area.type == "OUTLINER"), None)
                    if outliner is None:
                        raise ValueError("No Outliner available in this workspace")
                    space = outliner.spaces.active
                    space.display_mode = "LIBRARIES"
                    space.use_filter_id_type = True
                    space.filter_id_type = "COLLECTION"
                    space.filter_text = definition.name
                    outliner.tag_redraw()
                    return {"FINISHED"}
                viewport = None
                if self.action in {"FRAME_ASSET", "ISOLATE_ASSET"}:
                    viewport = context.area
                    if viewport is None or viewport.type != "VIEW_3D":
                        raise ValueError("Use this action from a 3D View")
                    region = next(region for region in viewport.regions if region.type == "WINDOW")
                    if self.action == "ISOLATE_ASSET" and viewport.spaces.active.local_view is not None:
                        with context.temp_override(area=viewport, region=region):
                            bpy.ops.view3d.localview(frame_selected=False)
                        return {"FINISHED"}
                placements = [obj for obj in root.all_objects if obj.instance_collection is not None
                              and (obj.instance_collection == definition or (
                                  obj.instance_collection.get("tm_asset_id") == definition.get("tm_asset_id")
                                  and obj.instance_collection.get("tm_fx_owner")))
                              and obj.name in context.view_layer.objects
                              and not obj.hide_get(view_layer=context.view_layer)]
                if not placements:
                    raise ValueError("No visible placements for this embedded asset")
                for obj in context.selected_objects:
                    obj.select_set(False)
                for obj in placements:
                    obj.select_set(True)
                context.view_layer.objects.active = placements[0]
                if viewport is not None:
                    with context.temp_override(area=viewport, region=region):
                        if self.action == "FRAME_ASSET":
                            bpy.ops.view3d.view_selected(use_all_regions=False)
                        else:
                            bpy.ops.view3d.localview(frame_selected=True)
            elif self.action == "WARNINGS":
                for issue in diagnostics(root):
                    data.log.warning("%s: %s (%s)", issue.get("code", ""), issue.get("message", ""),
                                     issue.get("source", ""))
            if self.action != "WARNINGS":
                root[data.ERROR_KEY] = ""
        except Exception as exc:
            data.log.exception("Map action failed")
            data.fail(root, exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TMBRIDGE_OT_MapMediaPreview(bpy.types.Operator):
    bl_idname = OP + "map_media_preview"
    bl_label = "Preview MediaTracker"
    bl_description = "Build and play this slot's approximate MediaTracker preview; click again to stop"
    bl_options = {"REGISTER"}
    target: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from . import _mediatracker as media

        tracker = context.scene.objects.get(self.target)
        if tracker is None or not tracker.get("tm_mediatracker_document"):
            self.report({"ERROR"}, "MediaTracker document unavailable")
            return {"CANCELLED"}
        try:
            scene = context.scene
            if scene.get("tm_map_preview_tracker") == tracker.name and scene.get("tm_mediatracker_preview_active"):
                if context.screen is not None and context.screen.is_animation_playing:
                    bpy.ops.screen.animation_cancel(restore_frame=False)
                scene["tm_mediatracker_preview_active"] = False
                scene.camera = bpy.data.objects.get(scene.get("tm_mediatracker_preview_camera_original", ""))
                for obj in scene.objects:
                    if obj.get("tm_mediatracker_preview"):
                        obj.hide_set(True)
                scene["tm_map_preview_tracker"] = ""
            else:
                for obj in context.selected_objects:
                    obj.select_set(False)
                tracker.select_set(True)
                context.view_layer.objects.active = tracker
                document = media._read_document(tracker)
                media._build_preview(scene, document, root=tracker)
                scene["tm_map_preview_tracker"] = tracker.name
                ranges = [media._time_range(block) for track in document.clip.tracks
                          for block in track.blocks]
                start = media._frame(max(0.0, min((begin for begin, _end in ranges), default=0.0)), scene)
                scene.frame_set(int(start), subframe=start % 1)
                if not any(child.type == "CAMERA" for child in tracker.children):
                    self.report({"WARNING"}, "No supported camera in this slot; keeping scene camera")
                if not bpy.app.background and context.screen is not None:
                    if scene.camera is not None:
                        bpy.ops.trackmania_blender_bridge.view_mediatracker_camera()
                    if not context.screen.is_animation_playing:
                        bpy.ops.screen.animation_play()
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            data.log.exception("MediaTracker preview failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def _action(layout, root, action, icon, text="", asset=None):
    op = layout.operator(TMBRIDGE_OT_MapPreviewAction.bl_idname, text=text, icon=icon)
    op.action = action
    op.root_name = root.name
    if asset is not None:
        op.asset_name = asset.name


class TMBRIDGE_UL_MapEmbedded(bpy.types.UIList):
    def draw_item(self, context, layout, data_owner, item, icon, active_data, active_propname, index):
        layout.label(text=item.get("tm_asset_name") or item.name, icon="OUTLINER_COLLECTION")

    def filter_items(self, context, data_owner, propname):
        needle = context.scene.tm_bridge_map_preview.asset_search.casefold()
        items = getattr(data_owner, propname)
        return [
            self.bitflag_filter_item
            if item.get("tm_asset_id") and needle in (item.get("tm_asset_name") or item.name).casefold()
            else 0 for item in items
        ], []


class TMBRIDGE_PT_MapPreview(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview"
    bl_label = ""
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_order = 51

    @classmethod
    def poll(cls, context):
        return _map_panel.map_root(context.scene) is not None and not _progress.active()

    def draw_header(self, context):
        root = _map_panel.map_root(context.scene)
        name = root.get("tm_map_name") or data.info(root).get("map_name") or "Map"
        self.layout.label(text=f"Map: {plain_name(name)}.Map.Gbx", icon="WORLD")

    def draw_header_preset(self, context):
        root = _map_panel.map_root(context.scene)
        self.layout.operator(TMBRIDGE_OT_SaveMapBlend.bl_idname, text="", icon="FILE_TICK")
        if root.get(data.ERROR_KEY):
            _action(self.layout, root, "WARNINGS", "ERROR")

    def draw(self, context):
        pass


class TMBRIDGE_PT_MapPreviewMetadata(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview_metadata"
    bl_label = "Metadata"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_MapPreview.bl_idname
    bl_order = 0
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, context):
        root = _map_panel.map_root(context.scene)
        row = self.layout.row(align=True)
        row.enabled = bool(bpy.data.images.get(root.get("tm_thumbnail", "")))
        row.prop(context.scene.tm_bridge_map_preview, "show_thumbnail", text="", icon="IMAGE_DATA", toggle=True)

    def draw(self, context):
        root = _map_panel.map_root(context.scene)
        settings = root.tm_bridge_map_controls
        ui = context.scene.tm_bridge_map_preview
        layout = self.layout
        image = bpy.data.images.get(root.get("tm_thumbnail", ""))
        if ui.show_thumbnail and image is not None:
            image.preview_ensure()
            layout.template_icon(icon_value=image.preview.icon_id, scale=8.0)
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        for name in ("map_name", "author", "map_type", "style", "laps"):
            col.prop(settings, name)
        medals = layout.column(align=True)
        for name, icon in (
            ("author_time", "SOLO_ON"), ("gold_time", "KEYTYPE_KEYFRAME_VEC"),
            ("silver_time", "KEYTYPE_BREAKDOWN_VEC"), ("bronze_time", "KEYTYPE_EXTREME_VEC"),
        ):
            row = medals.row(align=True)
            row.label(text="", icon=icon)
            row.prop(settings, name)
        layout.prop(ui, "show_details", emboss=False, icon="TRIA_DOWN" if ui.show_details else "TRIA_RIGHT")
        if ui.show_details:
            metadata = data.info(root)
            row = layout.row(align=True)
            row.label(text="UID")
            row.label(text=metadata.get("uid") or "-")
            row.operator(_map_panel.TMBRIDGE_OT_CopyMapUid.bl_idname, text="", icon="COPYDOWN")
            for label, value in (
                ("Environment", root.get("tm_decoration_collection", "")),
                ("Size", " x ".join(str(v) for v in root.get("tm_map_size", ()))),
                ("Zone", metadata.get("author_zone", "").replace("|", " / ")),
                ("Texture Mod", metadata.get("mod", "")),
            ):
                if value:
                    _map_panel._row(layout, label, value)


class TMBRIDGE_PT_MapPreviewMood(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview_mood"
    bl_label = "Mood"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_MapPreview.bl_idname
    bl_order = 1
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, context):
        _action(self.layout, _map_panel.map_root(context.scene), "RESET", "LOOP_BACK")

    def draw(self, context):
        root = _map_panel.map_root(context.scene)
        settings = root.tm_bridge_map_controls
        layout = self.layout
        row = layout.row()
        row.column(align=True).prop(settings, "mood", expand=True)
        toggles = row.column(align=True)
        toggles.prop(settings, "sky")
        clouds = toggles.row(align=True)
        clouds.enabled = settings.sky
        clouds.prop(settings, "clouds")
        toggles.prop(settings, "surroundings")
        toggles.prop(settings, "animate_time")
        if settings.animate_time:
            layout.prop(settings, "cycle_seconds")
        layout.prop(settings, "time_of_day", slider=True)
        row = layout.row(align=True)
        row.enabled = bool(data.particle_modifiers(root))
        row.prop(settings, "particles")


class TMBRIDGE_PT_MapPreviewMediaTracker(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview_mediatracker"
    bl_label = "MediaTracker"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_MapPreview.bl_idname
    bl_order = 2
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        from GBXBPY.mediatracker import MAP_MEDIA_SLOTS

        root = _map_panel.map_root(context.scene)
        trackers = {obj.get("tm_mediatracker_slot"): obj for obj in root.all_objects
                    if obj.type == "EMPTY" and obj.get("tm_mediatracker_slot")}
        col = self.layout.column(align=True)
        for slot, label in sorted(MAP_MEDIA_SLOTS, key=lambda item: item[0] != "clipAmbiance"):
            tracker = trackers.get(slot)
            row = col.row(align=True)
            row.label(text=label)
            buttons = row.row(align=True)
            buttons.enabled = tracker is not None and bool(tracker.get("tm_mediatracker_document"))
            op = buttons.operator(OP + "edit_mediatracker", text="", icon="GREASEPENCIL")
            op.target = tracker.name if tracker else ""
            playing = tracker is not None and context.scene.get("tm_map_preview_tracker") == tracker.name and context.scene.get("tm_mediatracker_preview_active")
            op = buttons.operator(TMBRIDGE_OT_MapMediaPreview.bl_idname, text="", icon="PAUSE" if playing else "PLAY")
            op.target = tracker.name if tracker else ""


class TMBRIDGE_PT_MapPreviewEmbedded(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview_embedded"
    bl_label = "Embedded Data"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_MapPreview.bl_idname
    bl_order = 3
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, context):
        _library, items = definitions(context, _map_panel.map_root(context.scene))
        self.layout.label(text=str(len(items)), icon="PACKAGE")

    def draw(self, context):
        root = _map_panel.map_root(context.scene)
        ui = context.scene.tm_bridge_map_preview
        library, items = definitions(context, root)
        layout = self.layout
        if not items:
            layout.label(text="No embedded assets", icon="PACKAGE")
            return
        layout.prop(ui, "asset_search", text="", icon="VIEWZOOM")
        row = layout.row()
        row.template_list(TMBRIDGE_UL_MapEmbedded.__name__, "", library, "children", ui, "asset_index", rows=4)
        definition = selected_definition(context, root)
        tools = row.column(align=True)
        tools.enabled = definition is not None
        _action(tools, root, "SELECT_ASSET", "RESTRICT_SELECT_OFF", asset=definition)
        _action(tools, root, "FRAME_ASSET", "VIEWZOOM", asset=definition)
        _action(tools, root, "ISOLATE_ASSET", "HIDE_OFF", asset=definition)
        _action(tools, root, "REVEAL_ASSET", "OUTLINER_COLLECTION", asset=definition)
        if definition is not None:
            _map_panel._row(layout, "Name", definition.get("tm_asset_name") or definition.name)


class TMBRIDGE_OT_MapLightMap(bpy.types.Operator):
    bl_idname = OP + "map_lightmap"
    bl_label = "Show Light Map"
    bl_description = "Show this embedded lightmap in the Image Editor"

    image_name: StringProperty(options={"HIDDEN"})
    root_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        try:
            root = bpy.data.collections.get(self.root_name)
            if root is None:
                raise ValueError("Map unavailable")
            if not self.image_name:
                lightmaps.load_images(root)
            else:
                if not any(item["image"] == self.image_name for item in lightmaps.entries(root)):
                    raise ValueError("Lightmap does not belong to this map")
                image = bpy.data.images.get(self.image_name)
                if image is None:
                    raise ValueError("Lightmap image unavailable; refresh Light Map")
                lightmaps.show_image(context, image)
        except lightmaps.READ_ERRORS as exc:
            lightmaps.log.exception("Lightmap action failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TMBRIDGE_PT_MapPreviewLightMap(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview_lightmap"
    bl_label = "Light Map"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_MapPreview.bl_idname
    bl_order = 4
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        root = _map_panel.map_root(context.scene)
        layout = self.layout
        error = root.get(lightmaps.ERROR_KEY)
        if error:
            layout.label(text=error, icon="ERROR")
            return
        if lightmaps.IMAGES_KEY not in root:
            lightmaps.request_discovery(root)
            layout.label(text="Reading lightmaps...", icon="TIME")
            return
        images = lightmaps.entries(root)
        if not images:
            layout.label(text="No embedded lightmaps", icon="IMAGE_DATA")
        for number, items in lightmaps.grouped_entries(root):
            box = layout.box()
            box.label(text=f"Texture Set {number}", icon="TEXTURE")
            col = box.column(align=True)
            for slot, item in items:
                op = col.operator(TMBRIDGE_OT_MapLightMap.bl_idname, text=f"Image {slot}", icon="IMAGE_DATA")
                op.root_name = root.name
                op.image_name = item["image"]


class TMBRIDGE_PT_MapPreviewStatistics(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_preview_statistics"
    bl_label = "Import Statistics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = TMBRIDGE_PT_MapPreview.bl_idname
    bl_order = 5
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, context):
        root = _map_panel.map_root(context.scene)
        status = root.get("tm_import_status", "")
        warning_count = len(diagnostics(root))
        if status == "COMPLETE" and warning_count == 0:
            self.layout.label(text="", icon="CHECKMARK")
        else:
            _action(self.layout, root, "WARNINGS", "ERROR", text=str(warning_count))

    def draw(self, context):
        root = _map_panel.map_root(context.scene)
        col = self.layout.column(align=True)
        for label, value in (
            ("Import", root.get("tm_import_status", "-")),
            ("Placements", root.get("tm_placement_count", 0)),
            ("Assets", root.get("tm_asset_count", 0)),
            ("Block Clips", root.get("tm_clip_count", 0)),
            ("Warnings", len(diagnostics(root))),
        ):
            _map_panel._row(col, label, value)


CLASSES = (
    TMBRIDGE_MapPreviewSettings, TMBRIDGE_MapControls,
    TMBRIDGE_OT_MapPreviewAction, TMBRIDGE_OT_MapMediaPreview, TMBRIDGE_OT_SaveMapBlend,
    TMBRIDGE_OT_MapLightMap,
    TMBRIDGE_UL_MapEmbedded,
    TMBRIDGE_PT_MapPreview, TMBRIDGE_PT_MapPreviewMetadata, TMBRIDGE_PT_MapPreviewMood,
    TMBRIDGE_PT_MapPreviewMediaTracker, TMBRIDGE_PT_MapPreviewEmbedded,
    TMBRIDGE_PT_MapPreviewLightMap,
    TMBRIDGE_PT_MapPreviewStatistics,
)


def register_scene_props() -> None:
    bpy.types.Scene.tm_bridge_map_preview = PointerProperty(type=TMBRIDGE_MapPreviewSettings)
    bpy.types.Collection.tm_bridge_map_controls = PointerProperty(type=TMBRIDGE_MapControls)
    data.register()


def unregister_scene_props() -> None:
    lightmaps.unregister()
    data.unregister()
    del bpy.types.Collection.tm_bridge_map_controls
    del bpy.types.Scene.tm_bridge_map_preview
