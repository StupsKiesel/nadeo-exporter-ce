"""V4 file import UI; no Trackmania editing properties or update callbacks."""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

log = logging.getLogger("trackmania_blender_bridge.map_import_v4")

MAP_CONTENT = {
    "lod_level": 1, "import_all_lods": False,
    "import_baked": True, "apply_deco_height": True,
    "import_spectators": True, "spectator_occupancy": 30,
    "import_vista_vegetation": True, "fill_vegetation_slots": False,
    "import_grass_fins": False, "import_terrain_grass": False,
    "terrain_grass_density": 10,
    "import_lighting": True, "import_sky": True,
    "import_clouds": True, "import_decoration": True,
    "import_skins": True, "apply_texture_mod": True, "club_textures": 0,
}


def effective_content(operator):
    """Resolve mode at execution time without overwriting Custom properties."""
    if operator.import_mode == "MAP":
        return dict(MAP_CONTENT)
    return {name: getattr(operator, name) for name in MAP_CONTENT}


def import_options(operator):
    from GBXBPY.map_import_v4 import ImportOptions

    content = effective_content(operator)
    content["spectator_occupancy"] /= 100.0
    content["terrain_grass_density"] /= 100.0
    return ImportOptions(**content, download_locators=operator.download_locators)


SETTINGS_FILE = "map_import_options.json"
_NOT_REMEMBERED = {"rna_type", "filepath", "filter_glob", "files", "directory", "filename", "from_dialog"}


def settings_path():
    folder = bpy.utils.user_resource("CONFIG", path="trackmania_blender_bridge", create=True)
    return os.path.join(folder, SETTINGS_FILE)


def _remembered(operator):
    return [name for name in getattr(type(operator), "__annotations__", {}) if name not in _NOT_REMEMBERED]


def load_settings(operator, path=None):
    """Apply saved dialog options; unknown or invalid entries are ignored."""
    try:
        with open(path or settings_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    for name in _remembered(operator):
        if name in saved:
            try:
                setattr(operator, name, saved[name])
            except (TypeError, ValueError):
                log.warning("Ignoring saved map import option %s=%r", name, saved[name])


def save_settings(operator, path=None):
    path = path or settings_path()
    data = {name: getattr(operator, name) for name in _remembered(operator)}
    try:
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(temp, path)
    except (OSError, TypeError) as exc:
        log.warning("Could not save map import options to %s: %s", path, exc)


class TMBRIDGE_OT_ImportMapV4(bpy.types.Operator, ImportHelper):
    bl_idname = "trackmania_blender_bridge.import_map_v4"
    bl_label = "Trackmania Map (.Map.Gbx)"
    bl_description = "Import grid/free blocks and anchored items using a validated scene plan; missing or unsupported assets remain diagnostic placeholders"
    bl_options = {"REGISTER", "UNDO"}
    _worker = None
    _provider = None
    _timer = None
    _owner = None
    _log_owner = None
    _import_server = None

    filename_ext = ".Gbx"
    filter_glob: StringProperty(default="*.Map.Gbx;*.map.gbx", options={"HIDDEN"})
    import_mode: EnumProperty(
        name="Mode",
        items=[
            ("MAP", "Map", "Map content with surroundings, mood, sky, clouds and 30% spectator occupancy; no grass"),
            ("CUSTOM", "Custom", "Choose which map content to import"),
        ],
        default="MAP",
    )
    lod_level: IntProperty(name="LOD Level", default=1, min=1)
    import_all_lods: BoolProperty(
        name="Import All LODs",
        description="Also build every other LOD of each mesh as hidden objects parented to the visible one (LOD Level), tagged with their level and the game's switch distance",
        default=False,
    )
    import_baked: BoolProperty(name="Import Baked Blocks", default=True)
    apply_deco_height: BoolProperty(name="Apply Deco Height to Absolute Placements", default=True)
    import_spectators: BoolProperty(
        name="Populate Spectators",
        description="Fill authored spectator spawn slots with stock meshes. Seats and variants are chosen deterministically, not copied from the live game's random crowd",
        default=False,
    )
    spectator_occupancy: IntProperty(
        name="Spectator Occupancy", subtype="PERCENTAGE", default=10, min=0, max=100,
    )
    import_vista_vegetation: BoolProperty(
        name="Vista Vegetation",
        description="Import the trees and bush clumps that vista prefabs place directly, as in-game (hills, cliffs, deco blocks). Trees without usable geometry become empties named after their model",
        default=True,
    )
    fill_vegetation_slots: BoolProperty(
        name="Fill Vegetation Slots",
        description="Also fill the tagged vegetation slots of vista terrain (one layout per block type). The game leaves them empty, so this adds palms and bushes not seen in-game",
        default=False,
    )
    import_grass_fins: BoolProperty(
        name="Stadium Grass Fins",
        description="Import the short grass cards over Stadium grass zones. The game draws them only near the camera; across a map they add millions of alpha-cutout polygons and slow down the viewport",
        default=False,
    )
    import_lighting: BoolProperty(
        name="Mood Lighting",
        description="Add a sun (or moon) and world colour from the map's decoration mood. The sun direction approximates the game's time of day",
        default=True,
    )
    import_sky: BoolProperty(
        name="Sky",
        description="Build the mood's sky in the world shader: sky dome texture, sun disc and glow, moon and night stars (an approximation of the game's sky)",
        default=True,
    )
    import_clouds: BoolProperty(
        name="Clouds",
        description="Add the mood's cloud sprites and cloud shadows; requires Mood Lighting and Sky",
        default=False,
    )
    import_decoration: BoolProperty(
        name="Decoration Surroundings",
        description="Import Stadium stands from the decoration map and vista ocean surrounds from the decoration scene",
        default=True,
    )
    import_terrain_grass: BoolProperty(
        name="Terrain Grass",
        description="Scatter the game's grass and plant billboards (3D decals) over vista terrain layers such as Land. They turn to face the scene's active camera",
        default=False,
    )
    terrain_grass_density: IntProperty(
        name="Grass Density", subtype="PERCENTAGE", default=10, min=0, max=100,
        description="Share of the game's grass pattern to keep; lower it for a lighter viewport",
    )
    texture_resolution: EnumProperty(
        name="Texture Resolution",
        items=[("0", "Original", ""), ("2048", "2048", ""), ("1024", "1024", ""),
               ("512", "512", ""), ("256", "256", ""), ("128", "128", "")],
        default="512",
    )
    import_skins: BoolProperty(
        name="Apply Map Skins",
        description="Use the skins stored on screen/sign blocks (resolved from the game's Packs folder, see addon preferences)",
        default=True,
    )
    apply_texture_mod: BoolProperty(
        name="Apply Texture Mod",
        description="Replace game textures with the map's texture mod (a ZIP in the user Skins folder, or downloaded from the map's URL)",
        default=True,
    )
    club_textures: IntProperty(
        name="Club Textures", default=0, min=0,
        description="Club ID for downloading public checkpoint, sponsor and decoration-screen textures. 0 disables downloads; map-specific sign skins keep priority",
    )
    download_locators: BoolProperty(
        name="Download Custom Signs & Mods",
        description="Download sign images and texture mods that are not on disk from the URLs stored in the map, as the game does",
        default=True,
    )
    fetch_missing: BoolProperty(
        name="Fetch Missing Files from Trackmania",
        description="Request missing or broken game files, with one hook retry for failed GBX files except known crashers; hook extraction can crash Trackmania",
        default=True,
    )
    from_dialog: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        load_settings(self)
        self.from_dialog = True
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        from .. import ADDON_ID

        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False

        layout.row(align=True).prop(self, "import_mode", expand=True)
        layout.prop(self, "fetch_missing", text="Fetch Missing Files")
        layout.prop(self, "texture_resolution", text="Texture Size")
        layout.prop(self, "download_locators", text="Download Signs & Mods")
        if self.import_mode != "CUSTOM":
            return

        def section(key, title, *, closed=True):
            # Blender 4.0 has no inline collapsible layout panels.
            if hasattr(layout, "panel"):
                header, body = layout.panel("tm_import_v4_" + key, default_closed=closed)
                header.label(text=title)
                return body
            body = layout.box()
            body.label(text=title)
            return body

        body = section("geometry", "Geometry", closed=False)
        if body is not None:
            body.prop(self, "lod_level")
            body.prop(self, "import_all_lods")

        body = section("environment", "Environment", closed=False)
        if body is not None:
            body.prop(self, "import_decoration", text="Surroundings")
            body.prop(self, "import_lighting", text="Mood Lighting")
            row = body.row(align=True)
            row.enabled = self.import_lighting
            row.prop(self, "import_sky", text="Sky")
            row = row.row(align=True)
            row.enabled = self.import_lighting and self.import_sky
            row.prop(self, "import_clouds")

        body = section("materials", "Materials & Textures")
        if body is not None:
            body.prop(self, "import_skins", text="Map Signs / Skins")
            body.prop(self, "apply_texture_mod", text="Map Texture Mod")
            body.prop(self, "club_textures")

        body = section("detail", "Extra Detail")
        if body is not None:
            body.prop(self, "import_vista_vegetation", text="Vista Trees & Bushes")
            body.prop(self, "import_terrain_grass", text="Terrain Grass")
            row = body.row()
            row.enabled = self.import_terrain_grass
            row.prop(self, "terrain_grass_density", text="Density")
            body.prop(self, "import_grass_fins", text="Stadium Grass Cards")
            if self.import_grass_fins:
                body.label(text="May add millions of polygons", icon="ERROR")

            body.prop(self, "import_spectators", text="Populate Stands")
            row = body.row()
            row.enabled = self.import_spectators
            row.prop(self, "spectator_occupancy", text="Occupancy")

        addon = context.preferences.addons.get(ADDON_ID)
        if addon is not None and addon.preferences.developer_ui:
            body = section("developer", "Developer Settings")
            if body is not None:
                body.prop(self, "import_baked")
                body.prop(self, "apply_deco_height", text="Apply Decoration Height")
                row = body.row()
                row.enabled = self.import_vista_vegetation
                row.prop(self, "fill_vegetation_slots", text="Extra Vegetation")
                if self.fill_vegetation_slots:
                    body.label(text="Extra vegetation is not present in-game", icon="INFO")

    @classmethod
    def poll(cls, _context):
        return _running is None

    def execute(self, context):
        from .. import _file_logging, _show_console_on_import
        from GBXBPY.linux_paths import AmbiguousPathError

        _show_console_on_import(context)
        owner = _file_logging.begin(self.filepath)
        try:
            result = self._execute(context)
        except AmbiguousPathError as exc:
            self._stop(context)
            _file_logging.end(owner)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except BaseException:
            self._stop(context)
            _file_logging.end(owner)
            raise
        if "RUNNING_MODAL" in result:
            self._log_owner = owner
        else:
            self._release_server()
            _file_logging.end(owner)
        return result

    def _execute(self, context):
        from .. import _get_gamedata_dir, _make_skin_library, _openplanet_bridge
        from GBXBPY.library import LibraryProvider

        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, f"Map not found: {self.filepath}")
            return {"CANCELLED"}
        if self.from_dialog:
            save_settings(self)
        gamedata = _get_gamedata_dir(context)
        if not gamedata or not os.path.isdir(gamedata):
            self.report({"ERROR"}, f"Game library folder unavailable: {gamedata}")
            return {"CANCELLED"}
        if self.fetch_missing:
            if not _openplanet_bridge.extract_dir():
                self.report({"ERROR"}, "Set the Openplanet folder in addon preferences")
                return {"CANCELLED"}
            try:
                self._import_server = _openplanet_bridge.start_import_server()
            except OSError:
                self.report({"ERROR"}, _openplanet_bridge.server_error)
                return {"CANCELLED"}
        queue = self._import_server.queue if self._import_server is not None else None
        self._provider = LibraryProvider(
            gamedata, queue, fetch=self.fetch_missing,
            extract_dir=_openplanet_bridge.extract_dir(),
        )
        self._started = time.perf_counter()
        try:
            from GBXBPY.resolve import Resolver
            from GBXMAT import set_max_texture_size

            set_max_texture_size(int(self.texture_resolution))
            self._resolver = Resolver(gamedata)
            self._skins = _make_skin_library(context, download=self.download_locators)
            if self._skins is not None:
                log.info(
                    "V4 skins: packs=%s user=%s cache=%s",
                    self._skins.packs_dirs, self._skins.user_dirs, self._skins.cache_dir,
                )
            self._options = import_options(self)
            log.info(
                "Map import starting:\n  Map: %s\n  GameData: %s\n"
                "  Mode: %s\n  Content: %s\n  Map skins: %s\n"
                "  Live requests and GBX hook recovery: %s\n"
                "  Texture limit: %s",
                self.filepath, gamedata, self.import_mode, self._options,
                effective_content(self)["import_skins"], self.fetch_missing,
                self.texture_resolution,
            )
        except Exception as exc:
            log.exception("V4 import failed")
            self.report({"ERROR"}, f"V4 import failed; see console or enabled file log: {exc}")
            return {"CANCELLED"}
        if bpy.app.background or context.window is None:
            return self._execute_blocking(context)
        return self._start_modal(context)

    # -- blocking path (background mode, scripts without a window) ----------

    def _execute_blocking(self, context):
        from .. import _progress
        from GBXBPY import library
        from GBXBPY.map_import_v4 import plan_map
        from .bridge import build

        with _progress.task("Importing Map") as progress, library.active(self._provider):
            try:
                _prepare_library(self._provider)
                plan = plan_map(
                    self.filepath, resolver=self._resolver, skins=self._skins,
                    options=self._options, progress=progress.sub(0.0, 0.5),
                )
                root = build(plan, context=context, resolver=self._resolver,
                             progress=progress.sub(0.5, 1.0))
            except Exception as exc:
                log.exception("V4 import failed")
                self.report({"ERROR"}, f"V4 import failed; see console or enabled file log: {exc}")
                return {"CANCELLED"}
        return self._finish(plan, root, context)

    # -- modal path: plan in a worker thread, build in time slices ----------

    def _start_modal(self, context):
        global _running
        from .. import _progress

        from GBXBPY import library

        library.activate(self._provider)
        self._worker = _PlanWorker(self.filepath, self._resolver, self._skins, self._options, self._provider)
        self._builder = None
        self._plan = None
        self._cancel_requested = False
        self._owner = _progress.begin(
            "Importing Map", modal=True,
            cancel_operator=TMBRIDGE_OT_CancelImportMapV4.bl_idname,
        )
        self._progress = _progress.Progress()
        self._timer = context.window_manager.event_timer_add(TIMER_INTERVAL, window=context.window)
        context.window_manager.modal_handler_add(self)
        _running = self
        self._worker.start()
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        # Open menus receive Esc before this window-level handler.
        if self._cancel_requested or (event.type == "ESC" and event.value == "PRESS"):
            return self._abort(context, "V4 import cancelled")
        if event.type == "TIMER":
            try:
                return self._tick(context)
            except Exception as exc:
                log.exception("V4 import failed")
                self._stop(context)
                self.report({"ERROR"}, f"V4 import failed; see console or enabled file log: {exc}")
                return {"CANCELLED"}
        if self._builder is None or event.type in NAVIGATION_EVENTS:
            # Planning never touches Blender data, so the UI stays fully usable.
            return {"PASS_THROUGH"}
        # Building: allow viewport navigation only; edits could break the build.
        return {"RUNNING_MODAL"}

    def _tick(self, context):
        if self._builder is None:
            fraction, message = self._worker.status()
            self._progress(0.5 * fraction, message)
            if self._worker.is_alive():
                return {"RUNNING_MODAL"}
            if self._worker.error is not None:
                raise self._worker.error
            from .bridge import Builder

            self._plan = self._worker.plan
            self._builder = Builder(
                self._plan, context=context, resolver=self._resolver,
                progress=self._progress.sub(0.5, 1.0),
            )
        if not self._builder.step(budget=BUILD_BUDGET):
            return {"RUNNING_MODAL"}
        result = self._finish(self._plan, self._builder.root, context)
        self._stop(context)
        return result

    def _abort(self, context, message):
        removed = self._builder.abort() if self._builder is not None else 0
        log.info("%s after %.2fs; removed %d datablocks", message,
                 time.perf_counter() - self._started, removed)
        self._stop(context)
        self.report({"WARNING"}, message)
        return {"CANCELLED"}

    def cancel(self, context):
        # Also called when the file browser closes before execute() runs.
        self._stop(context)

    def _stop(self, context):
        global _running
        from .. import _progress
        from GBXBPY import library

        if self._worker is not None:
            self._worker.cancel()
        if self._provider is not None:
            self._provider.cancel()
        self._release_server()
        if self._provider is not None and library.current() is self._provider:
            library.activate(None)
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._owner is not None:
            _progress.end(self._owner)
            self._owner = None
        if self._log_owner is not None:
            from .. import _file_logging

            _file_logging.end(self._log_owner)
            self._log_owner = None
        if _running is self:
            _running = None

    def _release_server(self):
        from .. import _openplanet_bridge

        _openplanet_bridge.stop_import_server(self._import_server)
        self._import_server = None

    def _finish(self, plan, root, context):
        if not bpy.app.background:
            from .viewport import frame_map

            frame_map(context, plan)
        media_preview = ""
        if root is not None and plan.source_path:
            map_scene = bpy.data.scenes.get(root.get("tm_map_scene", ""))
            if map_scene is not None:
                try:
                    from .. import _mediatracker

                    documents = _mediatracker.populate_map_preview(map_scene, plan.source_path, root)
                    track_count = sum(len(doc.clip.tracks) for doc in documents.values())
                    media_preview = (
                        f" {track_count} MediaTracker track(s) loaded into five map slots."
                    )
                except Exception as exc:
                    log.exception("Could not populate map MediaTracker editor")
                    media_preview = f" MediaTracker preview unavailable: {exc}"
        log.info("V4 import finished in %.2fs", time.perf_counter() - self._started)
        provider = getattr(self, "_provider", None)
        fetched = ""
        if provider is not None and (provider.fetched or provider.failed):
            log.info("V4 library: %s", provider.summary())
            for rel, reason in sorted(provider.failed.items()):
                log.debug("Unavailable game file %s: %s", rel, reason)
            fetched = f" {provider.summary()}."
        status = "PARTIAL" if plan.partial else "COMPLETE"
        missing = int(root.get("tm_missing_textures", 0) or 0) if root is not None else 0
        textures = f" {missing} textures missing from the game library (see console)." if missing else ""
        self.report(
            {"WARNING"} if plan.partial or missing else {"INFO"},
            f"V4 {status}: {len(plan.placements)} placements, "
            f"{len(plan.diagnostics)} diagnostics.{fetched}{textures}{media_preview} "
            f"Summary on collection '{root.name}'.",
        )
        return {"FINISHED"}


class TMBRIDGE_OT_CancelImportMapV4(bpy.types.Operator):
    """Cancel the running map import and remove what it created"""

    bl_idname = "trackmania_blender_bridge.cancel_import_map_v4"
    bl_label = "Cancel Map Import"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        return _running is not None

    def execute(self, _context):
        _running._cancel_requested = True
        return {"FINISHED"}


TIMER_INTERVAL = 0.05
# Seconds of build work per timer tick; keeps the window at ~10 fps or better.
BUILD_BUDGET = 0.1
NAVIGATION_EVENTS = frozenset({
    "MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "MIDDLEMOUSE", "WHEELUPMOUSE",
    "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE", "TRACKPADPAN",
    "TRACKPADZOOM", "WINDOW_DEACTIVATE", "TIMER", "TIMER_REPORT", "NONE",
})

# The running modal import; blocks starting a second one (see ``poll``).
_running = None


class ImportCancelled(BaseException):
    """Raised inside the worker's progress callback; BaseException so planner
    code catching ``Exception`` per block cannot swallow it."""


PLUGIN_CONNECT_TIMEOUT = 5.0


def _prepare_library(provider, cancelled=lambda: False):
    """Give the plugin a retry window after starting this import's server."""
    if provider.fetch_enabled:
        deadline = time.monotonic() + PLUGIN_CONNECT_TIMEOUT
        while not provider.connected() and time.monotonic() < deadline:
            if cancelled():
                raise ImportCancelled
            provider.status_message = "Waiting for the Openplanet plugin"
            time.sleep(0.1)
        provider.status_message = ""
    if cancelled():
        raise ImportCancelled
    provider.prepare()


class _PlanWorker(threading.Thread):
    """Runs ``plan_map`` off the main thread. GBXBPY never imports ``bpy``,
    so planning is thread safe; only the progress values cross threads."""

    def __init__(self, filepath, resolver, skins, options, provider=None) -> None:
        super().__init__(name="TM V4 plan", daemon=True)
        self._args = (filepath, resolver, skins, options)
        self._provider = provider
        self._lock = threading.Lock()
        self._status = (0.0, "Reading map")
        self._cancelled = threading.Event()
        self.plan = None
        self.error: Exception | None = None

    def run(self) -> None:
        from GBXBPY.map_import_v4 import plan_map

        filepath, resolver, skins, options = self._args
        try:
            if self._provider is not None:
                # Index refresh (first import) runs here, off the main thread.
                _prepare_library(self._provider, self._cancelled.is_set)
            self.plan = plan_map(filepath, resolver=resolver, skins=skins,
                                 options=options, progress=self._report)
        except ImportCancelled:
            log.info("V4 planning cancelled")
        except Exception as exc:  # noqa: BLE001 - surfaced by the operator
            self.error = exc

    def _report(self, fraction: float, message: str | None = None) -> None:
        if self._cancelled.is_set():
            raise ImportCancelled
        with self._lock:
            self._status = (fraction, message or self._status[1])

    def status(self) -> tuple[float, str]:
        with self._lock:
            fraction, message = self._status
        fetching = self._provider.status_message if self._provider is not None else ""
        return fraction, fetching or message

    def cancel(self) -> None:
        self._cancelled.set()
