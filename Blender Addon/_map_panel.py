"""User sidebar map metadata and mood selection.

Map Import V4 stores the header metadata (``tm_map_info`` JSON), decoration
and thumbnail on its root collection (``bridge._store_map_info``). The Mood
subpanel re-resolves the decoration mood (``resolve_environment``) and
replaces the sun/moon and sky world (``bridge.apply_environment``).
"""

from __future__ import annotations

import json
import logging

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from . import ADDON_SHORT_NAME

log = logging.getLogger("trackmania_blender_bridge.map_panel")

CATEGORY = ADDON_SHORT_NAME
OP = "trackmania_blender_bridge."
MOODS = ("Day", "Sunrise", "Sunset", "Night")


def map_root(scene):
    """Prefer the selected object's containing map; otherwise use the newest."""
    if scene is None:
        return None
    roots = [c for c in scene.collection.children if c.get("tm_import_version") == 4]
    obj = bpy.context.active_object if bpy.context.scene == scene else None
    while obj is not None:
        owner = obj.get("tm_map_root")
        for root in roots:
            if owner == root or owner == root.name or (owner and owner == root.get("tm_map_id")):
                return root
        obj = obj.parent
    active = bpy.context.active_object if bpy.context.scene == scene else None
    if active is not None and len(roots) > 1:
        collections = set(active.users_collection)
        for root in roots:
            if root in collections or any(child in collections for child in root.children_recursive):
                return root
    return roots[-1] if roots else None


def map_info(root) -> dict:
    try:
        return json.loads(root.get("tm_map_info", "") or "{}")
    except (TypeError, ValueError):
        return {}


def format_time(ms) -> str:
    """Race time ``m:ss.mmm``; unset (negative) times show as ``-``."""
    if not isinstance(ms, int) or ms < 0:
        return "-"
    minutes, rest = divmod(ms, 60000)
    return f"{minutes}:{rest // 1000:02d}.{rest % 1000:03d}"


def _map_mood(root) -> str:
    from GBXBPY.map_import_v4.environment import mood_name

    return mood_name(root.get("tm_decoration", "") or "Day")


def _map_day_time(root):
    value = root.get("tm_map_day_time", -1.0)
    return None if value is None or value < 0 else float(value)


class TMBRIDGE_MoodSettings(bpy.types.PropertyGroup):
    mood: EnumProperty(
        name="Mood",
        description="Decoration mood whose light and sky are applied",
        items=[("MAP", "Map", "The mood the map was saved with")]
        + [(m.upper(), m, f"The {m} mood of the decoration") for m in MOODS],
        default="MAP",
    )
    use_custom_time: BoolProperty(
        name="Custom Time",
        description="Override the time of day; off uses the map's time, or the mood's default",
        default=False,
    )
    day_time: FloatProperty(
        name="Time of Day",
        description=(
            "Game DayTime01: night ~0.15, sunrise ~0.52, day ~0.6, sunset ~0.75. "
            "The sun is up between about 0.49 and 0.77"
        ),
        min=0.0, max=1.0, default=0.6, subtype="FACTOR",
    )
    sky: BoolProperty(
        name="Sky",
        description="Build the mood's sky dome, sun, moon and stars; off uses a flat ambient colour",
        default=True,
    )
    clouds: BoolProperty(
        name="Clouds",
        description="Add cloud sprites and shadows when applying a mood; requires Sky",
        default=False,
    )


def _selected(settings, root) -> tuple[str, float | None]:
    mood = _map_mood(root) if settings.mood == "MAP" else settings.mood.capitalize()
    if settings.use_custom_time:
        return mood, settings.day_time
    return mood, _map_day_time(root) if settings.mood == "MAP" else None


class TMBRIDGE_OT_ApplyMood(bpy.types.Operator):
    """Replace the map's sun/moon and sky with the selected mood"""

    bl_idname = OP + "apply_mood"
    bl_label = "Apply Mood"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def execute(self, context):
        from . import _get_gamedata_dir, _openplanet_bridge
        from GBXBPY import library
        from GBXBPY.library import LibraryProvider
        from GBXBPY.map_import_v4.environment import resolve_environment
        from GBXBPY.resolve import Resolver
        from .map_import_v4.bridge import apply_environment

        scene = context.scene
        root = map_root(scene)
        settings = scene.tm_bridge_mood
        mood, day_time = _selected(settings, root)
        gamedata = _get_gamedata_dir(context)
        if not gamedata:
            self.report({"ERROR"}, "Set the GameData folder in addon preferences")
            return {"CANCELLED"}
        queue = _openplanet_bridge.queue()
        issues: list[str] = []
        provider = LibraryProvider(gamedata, queue, fetch=bool(_openplanet_bridge.extract_dir()) and
                                   queue is not None and queue.plugin_connected(),
                                   extract_dir=_openplanet_bridge.extract_dir())
        with library.active(provider):
            provider.prepare()
            environment = resolve_environment(
                Resolver(gamedata), root.get("tm_decoration_collection", ""),
                root.get("tm_decoration", ""), mood=mood, day_time=day_time,
                import_sky=settings.sky, import_clouds=settings.clouds,
                issue=lambda _code, source, message: issues.append(f"{message} ({source})"),
            )
            apply_environment(
                environment, scene, root, root.get("tm_map_name", "Map"), issue=issues.append,
            )
        for message in issues:
            log.warning("Mood: %s", message)
        light = "moon" if environment.is_moon else "sun"
        text = f"{environment.mood} mood applied ({light}, time {environment.day_time:.2f})"
        if issues:
            self.report({"WARNING"}, f"{text}; {len(issues)} issue(s): {issues[0]}")
        else:
            self.report({"INFO"}, text)
        return {"FINISHED"}


class TMBRIDGE_OT_ResetMood(bpy.types.Operator):
    """Select the map's own mood and time of day and apply it"""

    bl_idname = OP + "reset_mood"
    bl_label = "Reset to Map"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def execute(self, context):
        settings = context.scene.tm_bridge_mood
        settings.mood = "MAP"
        settings.use_custom_time = False
        return bpy.ops.trackmania_blender_bridge.apply_mood()


class TMBRIDGE_OT_CopyMapUid(bpy.types.Operator):
    """Copy the map UID to the clipboard"""

    bl_idname = OP + "copy_map_uid"
    bl_label = "Copy UID"

    @classmethod
    def poll(cls, context):
        root = map_root(context.scene)
        return root is not None and bool(map_info(root).get("uid"))

    def execute(self, context):
        context.window_manager.clipboard = map_info(map_root(context.scene))["uid"]
        self.report({"INFO"}, "Map UID copied")
        return {"FINISHED"}


def _row(layout, label: str, value, icon: str = "NONE") -> None:
    split = layout.split(factor=0.35, align=True)
    split.label(text=label)
    split.label(text=str(value) if value not in (None, "") else "-", icon=icon)


class TMBRIDGE_PT_Map(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map"
    bl_label = "Map"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_order = 50

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def draw(self, context):
        pass


class TMBRIDGE_PT_MapMetadata(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_metadata"
    bl_label = "Metadata"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = TMBRIDGE_PT_Map.bl_idname
    bl_order = 0
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def draw(self, context):
        layout = self.layout
        root = map_root(context.scene)
        if root is None:
            return
        info = map_info(root)

        image = bpy.data.images.get(root.get("tm_thumbnail", "") or "")
        if image is not None:
            image.preview_ensure()
            layout.template_icon(icon_value=image.preview.icon_id, scale=8.0)

        col = layout.column(align=True)
        _row(col, "Name", root.get("tm_map_name", ""))
        author = info.get("author_nickname") or info.get("author_login", "")
        _row(col, "Author", author)
        zone = (info.get("author_zone") or "").split("|")
        _row(col, "Zone", " / ".join(zone[-2:]) if zone != [""] else "")
        size = root.get("tm_map_size")
        _row(col, "Environment", root.get("tm_decoration_collection", ""))
        _row(col, "Source GBX", root.get("tm_source_path", ""))
        _row(col, "Size", " x ".join(str(v) for v in size) if size else "")
        map_type = (info.get("map_type") or "").rsplit("\\", 1)[-1]
        _row(col, "Type", map_type)
        if info.get("map_style"):
            _row(col, "Style", info["map_style"])
        if info.get("laps"):
            _row(col, "Laps", info["laps"])
        if info.get("mod"):
            _row(col, "Texture Mod", info["mod"])

        box = layout.box()
        col = box.column(align=True)
        if info.get("validated") or (info.get("author_time", -1) or -1) >= 0:
            for label, key, icon in (
                ("Author", "author_time", "SOLO_ON"),
                ("Gold", "gold_time", "KEYTYPE_KEYFRAME_VEC"),
                ("Silver", "silver_time", "KEYTYPE_BREAKDOWN_VEC"),
                ("Bronze", "bronze_time", "KEYTYPE_EXTREME_VEC"),
            ):
                _row(col, label, format_time(info.get(key, -1)), icon)
        else:
            col.label(text="Not validated (no medal times)", icon="INFO")

        if info.get("uid"):
            row = layout.row(align=True)
            split = row.split(factor=0.35, align=True)
            split.label(text="UID")
            split.label(text=info["uid"])
            row.operator(TMBRIDGE_OT_CopyMapUid.bl_idname, text="", icon="COPYDOWN")


class TMBRIDGE_PT_MapImportStatistics(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_import_statistics"
    bl_label = "Import Statistics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = TMBRIDGE_PT_Map.bl_idname
    bl_order = 1
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def draw(self, context):
        root = map_root(context.scene)
        col = self.layout.column(align=True)
        status = root.get("tm_import_status", "")
        _row(col, "Import", status, "CHECKMARK" if status == "COMPLETE" else "ERROR" if status else "NONE")
        _row(col, "Placements", root.get("tm_placement_count", ""))
        _row(col, "Assets", root.get("tm_asset_count", ""))
        _row(col, "Block Clips", root.get("tm_clip_count", ""))
        _row(col, "Warnings", root.get("tm_diagnostic_count", ""))
        row = self.layout.row(align=True)
        row.label(text="Particle FX", icon="PARTICLES")
        row.operator(TMBRIDGE_OT_FxViewport.bl_idname, text="Show").show = True
        row.operator(TMBRIDGE_OT_FxViewport.bl_idname, text="Hide").show = False


class TMBRIDGE_OT_FxViewport(bpy.types.Operator):
    """Show or hide fogger/sparkler particle emitters in the viewport (renders always include them)"""

    bl_idname = OP + "fx_viewport"
    bl_label = "Particle FX in Viewport"
    bl_options = {"REGISTER", "UNDO"}

    show: BoolProperty(name="Show", default=True)

    def execute(self, context):
        from .fx_particles import fx_modifiers

        modifiers = list(fx_modifiers(bpy.data.objects))
        for modifier in modifiers:
            modifier.show_viewport = self.show
        self.report({"INFO"}, f"{'Showing' if self.show else 'Hiding'} {len(modifiers)} particle emitters")
        return {"FINISHED"}


class TMBRIDGE_PT_MapMood(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_mood"
    bl_label = "Mood"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = TMBRIDGE_PT_Map.bl_idname
    bl_order = 2
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def draw(self, context):
        layout = self.layout
        root = map_root(context.scene)
        _row(layout, "Decoration", root.get("tm_decoration", ""))
        settings = context.scene.tm_bridge_mood
        applied = root.get("tm_mood")
        if applied:
            time = root.get("tm_day_time_applied")
            suffix = f", time {time:.2f}" if isinstance(time, float) else ""
            layout.label(text=f"Current: {applied}{suffix}", icon="LIGHT_SUN")
        else:
            layout.label(text="No mood lighting imported", icon="INFO")
        layout.row().prop(settings, "mood", expand=True)
        row = layout.row(align=True)
        row.prop(settings, "use_custom_time", text="")
        sub = row.row(align=True)
        sub.enabled = settings.use_custom_time
        sub.prop(settings, "day_time")
        layout.prop(settings, "sky")
        row = layout.row()
        row.enabled = settings.sky
        row.prop(settings, "clouds")
        row = layout.row(align=True)
        row.operator(TMBRIDGE_OT_ApplyMood.bl_idname, icon="WORLD")
        row.operator(TMBRIDGE_OT_ResetMood.bl_idname, text="", icon="LOOP_BACK")


class TMBRIDGE_PT_MapMediaTracker(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_mediatracker"
    bl_label = "MediaTracker"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = TMBRIDGE_PT_Map.bl_idname
    bl_order = 3
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def draw(self, context):
        from GBXBPY.mediatracker import MAP_MEDIA_SLOTS

        root = map_root(context.scene)
        trackers = {obj.get("tm_mediatracker_slot"): obj for obj in root.all_objects
                    if obj.type == "EMPTY" and obj.get("tm_mediatracker_slot")}
        for slot, label in MAP_MEDIA_SLOTS:
            tracker = trackers.get(slot)
            row = self.layout.row(align=True)
            row.label(text=label)
            button = row.row(align=True)
            button.enabled = tracker is not None and bool(tracker.get("tm_mediatracker_document"))
            op = button.operator(OP + "edit_mediatracker", text="", icon="GREASEPENCIL")
            op.target = tracker.name if tracker is not None else ""
        if not trackers:
            self.layout.label(text="No MediaTracker data imported", icon="INFO")


def embedded_library(scene, root):
    """Find this map's existing source library without creating data during draw."""
    name = root.get("tm_embedded_asset_collection")
    library = (root.children.get(name) or scene.collection.children.get(name)) if name else None
    if library is not None:
        return library
    map_id = root.get("tm_map_id")
    return next((coll for coll in (*root.children, *scene.collection.children)
                 if map_id and coll.get("tm_embedded") and coll.get("tm_map_id") == map_id), None)


class TMBRIDGE_PT_MapEmbeddedData(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_map_embedded_data"
    bl_label = "Embedded Data"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = TMBRIDGE_PT_Map.bl_idname
    bl_order = 4
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return map_root(context.scene) is not None

    def draw(self, context):
        library = embedded_library(context.scene, map_root(context.scene))
        if library is None:
            self.layout.label(text="No embedded data imported", icon="INFO")
            return
        definitions = [coll for coll in library.children_recursive if coll.get("tm_asset_id")]
        _row(self.layout, "Collection", library.name)
        _row(self.layout, "Assets", len(definitions))
        for definition in definitions:
            self.layout.label(text=definition.get("tm_asset_name") or definition.name, icon="OUTLINER_COLLECTION")


CLASSES = (
    TMBRIDGE_MoodSettings,
    TMBRIDGE_OT_ApplyMood,
    TMBRIDGE_OT_ResetMood,
    TMBRIDGE_OT_CopyMapUid,
    TMBRIDGE_OT_FxViewport,
    TMBRIDGE_PT_Map,
    TMBRIDGE_PT_MapMetadata,
    TMBRIDGE_PT_MapImportStatistics,
    TMBRIDGE_PT_MapMood,
    TMBRIDGE_PT_MapMediaTracker,
    TMBRIDGE_PT_MapEmbeddedData,
)


def register_scene_props() -> None:
    bpy.types.Scene.tm_bridge_mood = PointerProperty(type=TMBRIDGE_MoodSettings)


def unregister_scene_props() -> None:
    if hasattr(bpy.types.Scene, "tm_bridge_mood"):
        del bpy.types.Scene.tm_bridge_mood
