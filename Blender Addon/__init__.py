"""Nadeo Exporter Community Edition addon entrypoint."""

from __future__ import annotations

import glob
import logging
import math
import os
import shutil
import sys
import time

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import _progress

ADDON_NAME = "Nadeo Exporter Community Edition"
ADDON_SHORT_NAME = "Nadeo Exporter CE"

bl_info = {
    "name": "Nadeo Exporter Community Edition",
    "author": "StupsKiesel",
    "version": (0, 0, 14),
    "blender": (3, 6, 2),
    "location": "File > Import > Nadeo Exporter CE",
    "description": "Community preview: bring Nadeo GBX meshes and materials into Blender",
    "category": "Import-Export",
}


ADDON_ID = __package__ or __name__
LOG_ROOT = "trackmania_blender_bridge"
_GBXBPY_LOG = "gbxbpy"
_NICE_LOG = "nice"
_GBXMAT_LOG = "gbxmat"
_GBXBPY_BLENDER_LOG = "gbxbpy_blender"

# Build zips produced by build.py at the workspace root, e.g.
# The internal package and operator IDs stay stable across the rebrand.
_BUILD_ZIP_GLOBS = ("nadeo_exporter_ce_*.zip", "trackmania_blender_bridge_*.zip")


def _build_archives(build_dir: str) -> list[str]:
    return [
        path for pattern in _BUILD_ZIP_GLOBS
        for path in glob.glob(os.path.join(build_dir, pattern))
    ]


def _ensure_bundled_packages_on_path() -> None:
    """Make GBXBPY and NICE importable regardless of dev / installed layout.

    Two layouts are supported:

    * Dev (workspace): the addon lives in ``<repo>/Blender Addon/`` and
      GBXBPY/NICE sit next to it in ``<repo>/``.
    * Installed: ``trackmania_blender_bridge/`` ships with GBXBPY/NICE bundled
      inside it.

    Both cases are covered by inserting the addon directory and its parent.
    """
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(addon_dir)
    for candidate in (addon_dir, parent_dir):
        if candidate and candidate not in sys.path:
            sys.path.insert(0, candidate)


_ensure_bundled_packages_on_path()

from . import _dependencies  # noqa: E402
from . import _file_logging  # noqa: E402

# User modules survive addon upgrades and do not require administrator access.
_dependency_dir = bpy.utils.user_resource("SCRIPTS", path="modules")
if _dependency_dir and os.path.isdir(_dependency_dir) and _dependency_dir not in sys.path:
    sys.path.insert(0, _dependency_dir)

try:
    import GBXMAT  # noqa: E402
    from ._gbxmat_adapter import (  # noqa: E402
        is_layered_terrain_binding, layered_terrain_top_layers, spec_from_binding,
    )
except Exception as _e:  # noqa: BLE001 - non-fatal; materials degrade to placeholders
    GBXMAT = None  # type: ignore[assignment]
    spec_from_binding = None  # type: ignore[assignment]
    is_layered_terrain_binding = None  # type: ignore[assignment]
    layered_terrain_top_layers = None  # type: ignore[assignment]
    logging.getLogger("trackmania_blender_bridge").warning(
        "GBXMAT unavailable, materials will be placeholders: %s", _e,
    )


# ---------------------------------------------------------------------------
# Coloured console output
# ---------------------------------------------------------------------------
# Only NICE log records are colourised so the parser's errors / warnings
# stand out against the much chattier GBXBPY / GBXMAT / addon streams.
# Blender's "Toggle System Console" window on Windows 10+ supports VT
# processing, but it must be explicitly enabled on the parent stdout
# handle the first time we want colour.

_ANSI_RESET = "\033[0m"
_ANSI_NICE_LEVEL_COLORS = {
    logging.WARNING:  "\033[33m",    # yellow
    logging.ERROR:    "\033[91m",    # bright red
    logging.CRITICAL: "\033[1;91m",  # bold bright red
}


def _enable_windows_ansi() -> bool:
    """Best-effort ENABLE_VIRTUAL_TERMINAL_PROCESSING on stdout/stderr.

    No-op (and returns True) on non-Windows platforms. Returns False when
    the call fails — callers should disable colour in that case.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes  # local: only needed on Windows

        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ok = True
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            if handle == 0 or handle == -1:
                ok = False
                continue
            mode = ctypes.c_uint32(0)
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ok = False
                continue
            if not kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            ):
                ok = False
        return ok
    except Exception:  # noqa: BLE001
        return False


_ANSI_AVAILABLE = _enable_windows_ansi()


class _ColorFormatter(_file_logging.ConsoleFormatter):
    """Formatter that colourises **only NICE** records by level.

    All other records render as plain text — keeps the addon / GBXBPY /
    GBXMAT logs uncoloured while making parser errors and warnings
    visible at a glance.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not _ANSI_AVAILABLE:
            return text
        if not any(
            record.name == root or record.name.startswith(root + ".")
            for root in (_NICE_LOG, "tm2020.nice")
        ):
            return text
        color = _ANSI_NICE_LEVEL_COLORS.get(record.levelno)
        if not color:
            return text
        return f"{color}{text}{_ANSI_RESET}"


def configure_logging(level_name: str = "INFO") -> logging.Logger:
    """Configure the addon logger hierarchy.

    Also routes GBXBPY, GBXMAT and NICE log records through the addon's handler
    so parser/domain/material messages appear in the same console stream.
    Only NICE warnings/errors are colourised (yellow/red) so parser issues
    stand out in Blender's system console; other loggers stay plain.
    """
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger(LOG_ROOT)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        setattr(handler, "_tmbridge_console_handler", True)
        logger.addHandler(handler)

    for handler in logger.handlers:
        if getattr(handler, "_tmbridge_console_handler", False) or type(handler.formatter).__name__ == "_ColorFormatter":
            setattr(handler, "_tmbridge_console_handler", True)
            handler.setFormatter(_ColorFormatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        for stale in [f for f in handler.filters if type(f).__name__ == "RepeatFilter"]:
            handler.removeFilter(stale)
        handler.addFilter(_file_logging.repeat_filter)

    logger.propagate = False

    # Route domain-layer loggers through the same handler.
    for shared_handler in logger.handlers:
        for name in (_GBXBPY_LOG, _NICE_LOG, _GBXMAT_LOG, _GBXBPY_BLENDER_LOG, "tm2020.nice"):
            child = logging.getLogger(name)
            child.setLevel(level)
            if shared_handler not in child.handlers:
                child.addHandler(shared_handler)
            child.propagate = False

    return logger


log = configure_logging()


def _on_include_prereleases_changed(_self, _context) -> None:
    # A found update may no longer match the selected channel.
    _tools_panel.reset_update_state()


def _on_log_level_changed(self, _context) -> None:
    configure_logging(self.log_level)
    _configure_file_logging(self)


def _configure_file_logging(prefs) -> None:
    _file_logging.configure(
        enabled=prefs.log_to_blend, level=getattr(logging, prefs.log_level),
    )


def _on_file_logging_changed(self, _context) -> None:
    _configure_file_logging(self)


class TMBRIDGE_OT_InstallDependencies(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.install_dependencies"
    bl_label = "Install Dependencies"
    bl_description = (
        "Download missing Python packages into Blender's user modules folder. "
        "Requires internet access; restart Blender after installation"
    )

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        self.layout.label(text="Download missing dependencies from PyPI?")
        self.layout.label(text="Blender will be unresponsive during installation.")
        self.layout.label(text="Save your work first. Restart Blender afterwards.")

    def execute(self, _context):
        try:
            target = bpy.utils.user_resource("SCRIPTS", path="modules", create=True)
            if not target:
                raise RuntimeError("Blender's user modules directory is unavailable")
            _dependencies.install(target)
        except (OSError, RuntimeError) as exc:
            log.exception("Dependency installation failed")
            self.report({"ERROR"}, f"Installation failed; see system console: {exc}")
            return {"CANCELLED"}
        if _dependencies.restart_required:
            self.report({"INFO"}, "Dependencies installed. Restart Blender before importing.")
        else:
            self.report({"INFO"}, "All dependencies are already available.")
        return {"FINISHED"}


def _on_developer_ui_changed(self, _context) -> None:
    _sync_developer_ui(self.developer_ui)


class TMBRIDGE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    developer_ui: BoolProperty(
        name="Developer UI",
        description=(
            "Show the Developer panel with debugging, library repair and maintenance tools"
        ),
        default=False,
        update=_on_developer_ui_changed,
    )

    advanced_settings: BoolProperty(
        name="Advanced Paths",
        description="Show cache directories and the explicit Packs directory override",
        default=False,
    )

    trackmania_dir: StringProperty(
        name="Trackmania",
        description="Trackmania installation folder; its Packs subfolder supplies stock skins when no Packs override is set. Blank uses automatic discovery",
        subtype="DIR_PATH",
        default="",
    )

    openplanet_dir: StringProperty(
        name="Openplanet",
        description="Openplanet user folder containing Plugins and Extract; blank uses your user folder's OpenplanetNext directory",
        subtype="DIR_PATH",
        default="",
    )

    log_to_blend: BoolProperty(
        name="Store Import Logs in Blend",
        description=(
            "Keep one internal Text log per import, readable in the Text Editor "
            "and stored when you save the blend file; no external log files"
        ),
        default=False,
        update=_on_file_logging_changed,
    )

    gamedata_dir: StringProperty(
        name="Game Library",
        description=(
            "GameData folder the importers read game files from. Blank uses Extract\\GameData "
            "under the selected Openplanet root. The plugin always extracts into Openplanet's "
            "Extract folder; files are moved from there into a custom library"
        ),
        subtype="DIR_PATH",
        default="",
    )

    bridge_port: IntProperty(
        name="Port",
        description="Port used by the next opted-in V4 import (127.0.0.1 only); set the same URL in the plugin settings",
        default=1337,
        min=1024,
        max=65535,
    )

    extract_request: StringProperty(
        name="Request",
        description=(
            "Game paths for the Blender Bridge Extractor Openplanet plugin, separated by ';'. "
            "Relative to GameData; end a path with / to request a whole folder"
        ),
        default="",
    )

    log_level: EnumProperty(
        name="Logging Level",
        description="Verbosity used by the addon logger",
        items=[
            ("DEBUG", "Debug", "Show detailed debug logs"),
            ("INFO", "Info", "Show info, warnings, and errors"),
            ("WARNING", "Warning", "Show warnings and errors only"),
            ("ERROR", "Error", "Show errors only"),
            ("CRITICAL", "Critical", "Show critical errors only"),
        ],
        default="INFO",
        update=_on_log_level_changed,
    )

    build_dir: StringProperty(
        name="Build Directory",
        description="Path to the folder containing addon build zips (from build.py)",
        subtype="DIR_PATH",
        default="",
    )

    include_prereleases: BoolProperty(
        name="Include Pre-releases",
        description=(
            "Also offer GitHub pre-releases (preview versions) as updates. "
            "When off, only full releases are offered"
        ),
        default=True,
        update=_on_include_prereleases_changed,
    )

    parse_cache_dir: StringProperty(
        name="Parse Cache Directory",
        description=(
            "Folder where parsed prefab/mesh assets are pickled. Speeds up "
            "repeated map imports by ~100x. Leave empty to disable. "
            "Files are invalidated automatically when the source GBX changes"
        ),
        subtype="DIR_PATH",
        default="",
    )

    packs_dir: StringProperty(
        name="Game Packs Directory",
        description=(
            "Trackmania install 'Packs' folder holding the stock skin archives "
            "(Maniaplanet_Skins.zip). Leave empty to auto-detect Ubisoft, Steam "
            "and Epic installs"
        ),
        subtype="DIR_PATH",
        default="",
    )

    user_skins_dir: StringProperty(
        name="User Trackmania Directory",
        description=(
            "Folder containing your custom 'Skins' folder. Leave empty to use "
            "Documents\\Trackmania"
        ),
        subtype="DIR_PATH",
        default="",
    )

    skin_cache_dir: StringProperty(
        name="Skin Cache Directory",
        description=(
            "Where skin images are extracted from pack archives. Leave empty to use "
            "%LOCALAPPDATA%\\TrackmaniaBlenderBridge\\SkinCache"
        ),
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, _context) -> None:
        _draw_settings(self.layout, self)


def _draw_settings(layout, prefs) -> None:
    """Draw routine of the addon preferences window."""
    box = layout.box()
    box.label(text="Directories", icon="FILE_FOLDER")
    _draw_paths(box, prefs)

    box = layout.box()
    row = box.row()
    row.label(text="Updates", icon="FILE_REFRESH")
    _tools_panel.draw_github_actions(row)
    box.prop(prefs, "include_prereleases")
    _tools_panel.draw_update_status(box)

    box = layout.box()
    _draw_logging(box, prefs)

    box = layout.box()
    row = box.row()
    row.label(text="Python Dependencies", icon="PREFERENCES")
    if _dependencies.restart_required:
        row.label(text="Restart required", icon="INFO")
    else:
        missing = _dependencies.missing_dependencies()
        if missing:
            status = row.row()
            status.alert = True
            status.label(text="Missing: " + ", ".join(missing), icon="ERROR")
        else:
            row.label(text="Ready", icon="CHECKMARK")
    button = row.row()
    button.enabled = not _dependencies.restart_required
    button.operator(TMBRIDGE_OT_InstallDependencies.bl_idname, icon="IMPORT")

    try:
        _openplanet_bridge.draw(layout, prefs)
    except ImportError:
        layout.label(text="Install Python dependencies to configure Openplanet", icon="ERROR")
    layout.prop(prefs, "developer_ui")


def _draw_logging(layout, prefs) -> None:
    """Console level and embedded import logs, available without Developer UI."""
    row = layout.row(align=True)
    row.label(text="Logging", icon="TEXT")
    row.row(align=True).prop(prefs, "log_level", expand=True)
    row.prop(prefs, "log_to_blend", text="Store in Blend")
    if prefs.log_to_blend:
        if _file_logging.error:
            row = layout.row()
            row.alert = True
            row.label(text=_file_logging.error, icon="ERROR")


# Folder/file preferences; caches and the explicit Packs override are advanced.
PATH_PROPERTIES = (
    "trackmania_dir",
    "openplanet_dir",
    "gamedata_dir",
    "parse_cache_dir",
    "packs_dir",
    "user_skins_dir",
    "skin_cache_dir",
    "build_dir",
)


def _draw_paths(layout, prefs) -> None:
    col = layout.column(align=True)
    col.use_property_split = True
    col.use_property_decorate = False
    for name, label in (
        ("trackmania_dir", "Trackmania"),
        ("openplanet_dir", "Openplanet"),
        ("user_skins_dir", "User Files"),
        ("gamedata_dir", "GameData"),
    ):
        col.prop(prefs, name, text=label)
    if prefs.developer_ui:
        col.prop(prefs, "build_dir", text="Upgrade Folder")
    col = layout.column(align=True)
    col.prop(prefs, "advanced_settings", toggle=True,
             icon="TRIA_DOWN" if prefs.advanced_settings else "TRIA_RIGHT")
    if prefs.advanced_settings:
        col.use_property_split = True
        col.use_property_decorate = False
        for name in ("packs_dir", "parse_cache_dir", "skin_cache_dir"):
            col.prop(prefs, name)
        if not prefs.parse_cache_dir:
            col.label(text="Parse cache disabled", icon="INFO")
    for name in ("trackmania_dir", "openplanet_dir", "user_skins_dir", "packs_dir"):
        path = getattr(prefs, name)
        if path and not os.path.isdir(bpy.path.abspath(path)):
            row = layout.row()
            row.alert = True
            row.label(text=f"{prefs.bl_rna.properties[name].name}: folder not found", icon="ERROR")
    if prefs.trackmania_dir and not prefs.packs_dir:
        if not os.path.isdir(os.path.join(bpy.path.abspath(prefs.trackmania_dir), "Packs")):
            layout.label(text="Trackmania folder has no Packs subfolder", icon="ERROR")


def _draw_upgrade(col, prefs) -> None:
    """Build-directory upgrade and pycache tools (preferences and Upgrade panel)."""
    row = col.row()
    row.enabled = bool(prefs.build_dir) and os.path.isdir(bpy.path.abspath(prefs.build_dir))
    op_text = "Upgrade from Build Directory"
    if _check_upgrade_available():
        op_text += "  (newer build available)"
    row.operator(TMBRIDGE_OT_UpgradeFromBuild.bl_idname, text=op_text, icon="FILE_REFRESH")

    # Cleanup: clear __pycache__ from the installed addon tree.
    pc_row = col.row()
    pc_row.operator(
        TMBRIDGE_OT_ClearAddonPyCache.bl_idname,
        text="Clear Python Cache",
        icon="TRASH",
    )


# ---------------------------------------------------------------------------
#  Upgrade-from-build detection (cached, re-checked every 30s)
# ---------------------------------------------------------------------------

_upgrade_available: bool = False
_upgrade_check_time: float = 0.0
_running_build_files: dict[str, tuple[int, int]] = {}
_running_addon_mtime: float = 0.0


def _snapshot_running_build() -> None:
    """Keep the loaded build's identity when an external build replaces its files."""
    import zlib

    global _running_build_files, _running_addon_mtime, _upgrade_check_time
    root = os.path.dirname(__file__)
    files = {}
    for directory, folders, names in os.walk(root):
        folders[:] = [
            name for name in folders
            if not name.startswith(".") and name not in {"__pycache__", "debugLiveLogs"}
        ]
        for name in names:
            if name.startswith(".") or name.endswith((".pyc", ".pyo", ".log")):
                continue
            path = os.path.join(directory, name)
            with open(path, "rb") as handle:
                data = handle.read()
            files[os.path.relpath(path, root).replace("\\", "/")] = (len(data), zlib.crc32(data))
    _running_build_files = files
    _running_addon_mtime = os.path.getmtime(__file__)
    _upgrade_check_time = 0.0


def _check_upgrade_available() -> bool:
    """Return True if a newer build zip exists than the running addon."""
    import zipfile

    global _upgrade_available, _upgrade_check_time
    now = time.monotonic()
    if now - _upgrade_check_time < 30.0:
        return _upgrade_available
    _upgrade_check_time = now
    try:
        addon = bpy.context.preferences.addons.get(ADDON_ID)
        if addon is None:
            _upgrade_available = False
            return False
        build_dir = bpy.path.abspath(addon.preferences.build_dir or "")
        if not build_dir or not os.path.isdir(build_dir):
            _upgrade_available = False
            return False
        zips = _build_archives(build_dir)
        if not zips:
            _upgrade_available = False
            return False
        newest = max(zips, key=os.path.getmtime)
        newest_mtime = os.path.getmtime(newest)
        addon_mtime = _running_addon_mtime
        _upgrade_available = False
        if newest_mtime > addon_mtime:
            # Compare with registration-time contents, not files that an
            # external build may already have replaced beneath the running addon.
            prefix = "trackmania_blender_bridge/"
            with zipfile.ZipFile(newest) as archive:
                for entry in archive.infolist():
                    if entry.is_dir() or not entry.filename.startswith(prefix):
                        continue
                    parts = entry.filename[len(prefix):].split("/")
                    if any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in parts):
                        raise ValueError(f"Invalid addon archive path: {entry.filename}")
                    if _running_build_files.get("/".join(parts)) != (entry.file_size, entry.CRC):
                        _upgrade_available = True
                        break
    except (OSError, ValueError, zipfile.BadZipFile):
        log.exception("Could not check the build directory for addon upgrades")
        _upgrade_available = False
    return _upgrade_available


def _get_gamedata_dir(context) -> str:
    """The game library folder (created when missing, so imports can fill it)."""
    addon = context.preferences.addons.get(ADDON_ID)
    path = _openplanet_bridge.library_dir(addon.preferences if addon is not None else None)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create the game library folder %s: %s", path, exc)
    return path


def _make_skin_library(context, *, download: bool = True):
    """Build a :class:`GBXBPY.resolve.skins.SkinLibrary` from preferences.

    With ``download``, skins only available through the map's locator URLs
    are downloaded into ``<skin cache>/Locators``.
    """
    from GBXBPY.resolve.locators import LocatorCache
    from GBXBPY.resolve.skins import SkinLibrary, default_cache_dir, default_packs_dirs, default_user_dirs

    addon = context.preferences.addons.get(ADDON_ID)
    prefs = addon.preferences if addon is not None else None

    def pref_dir(name: str) -> str:
        value = getattr(prefs, name, "") if prefs is not None else ""
        return bpy.path.abspath(value) if value else ""

    packs = pref_dir("packs_dir")
    game = pref_dir("trackmania_dir")
    if not packs and game:
        packs = os.path.join(game, "Packs")
    if packs and not os.path.isdir(packs):
        log.warning("Configured game Packs directory does not exist: %s", packs)
    user = pref_dir("user_skins_dir")
    cache = pref_dir("skin_cache_dir") or default_cache_dir()
    return SkinLibrary(
        [packs] if packs else default_packs_dirs(),
        [user] if user else default_user_dirs(),
        cache,
        LocatorCache(os.path.join(cache, "Locators")) if download else None,
    )


def _ensure_system_console_visible() -> None:
    """Show Blender's system console on Windows if it isn't already visible.

    Map imports can take 10–40 minutes; users need to see live progress logs.
    No-op on non-Windows platforms or if the console state can't be queried.
    """
    # wm.console_toggle crashes Blender without a window manager UI (-b).
    if sys.platform != "win32" or bpy.app.background:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd or not user32.IsWindowVisible(hwnd):
            try:
                bpy.ops.wm.console_toggle()
            except Exception as e:  # noqa: BLE001
                log.debug("wm.console_toggle failed: %s", e)
    except Exception as e:  # noqa: BLE001
        log.debug("Could not ensure system console visibility: %s", e)


class TMBRIDGE_OT_ImportMaterial(bpy.types.Operator, ImportHelper):
    """Import a Trackmania .Material.Gbx file as a Blender material.

    The file is treated like a mesh binding that links to it, so it goes
    through the same ``spec_from_binding`` -> GBXMAT path as materials on
    imported meshes (parent materials, shader family, projection, textures).
    """

    bl_idname = "trackmania_blender_bridge.import_material"
    bl_label = "Trackmania Material (.Material.Gbx)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Gbx"
    filter_glob: StringProperty(
        default="*.Material.Gbx;*.material.gbx",
        options={"HIDDEN"},
        maxlen=255,
    )
    assign_to_active: BoolProperty(
        name="Assign to Active Object",
        description="Append the material to the active mesh object's material slots",
        default=False,
    )

    @_progress.tracked("Importing Material")
    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, f"File not found: {self.filepath}")
            return {"CANCELLED"}

        gamedata_dir = _get_gamedata_dir(context)
        try:
            import GBXBPY
            from GBXBPY.errors import GbxbpyError
        except ImportError as e:
            log.exception("Failed to import GBXBPY: %s", e)
            self.report({"ERROR"}, f"GBXBPY package not importable: {e}")
            return {"CANCELLED"}

        resolver = GBXBPY.Resolver(gamedata_dir) if gamedata_dir else None

        log.info("Importing material: %s", self.filepath)
        try:
            material = GBXBPY.load(self.filepath, resolver=resolver)
        except GbxbpyError as e:
            log.error("GBXBPY load failed: %s", e)
            self.report({"ERROR"}, f"GBXBPY load failed: {e}")
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001 - surface any unexpected error to the user
            log.exception("Unexpected error during material import")
            self.report({"ERROR"}, f"Unexpected error: {e}")
            return {"CANCELLED"}

        log.info(
            "Loaded material '%s' (class 0x%08X)",
            material.name, material.class_id or 0,
        )
        log.info("  base_material_path : %s", material.base_material_path or "(none)")
        log.info("  texture_size       : %s", material.texture_size)
        log.info("  features           : %s", material.features)
        log.info("  flags              : %s", material.flags)
        log.info("  shader_params      : %d", len(material.shader_params))
        log.info("  texture_slots      : %d", len(material.texture_slots))
        for slot in material.texture_slots:
            log.info("    - %-20s -> %s", slot.name, slot.texture_path or "(unresolved)")

        if GBXMAT is None or spec_from_binding is None:
            self.report({"ERROR"}, "GBXMAT unavailable; see console for the import error")
            return {"CANCELLED"}
        if resolver is None:
            self.report({"WARNING"}, "No GameData directory set; textures and parent materials may be missing")
        from GBXBPY.model.mesh import MaterialBinding

        try:
            _progress.update(0.35, "Resolving material and textures")
            binding = MaterialBinding(material_path=self.filepath)
            spec = spec_from_binding(
                binding,
                resolver=resolver, source_path=self.filepath,
            )
            from ._blendermania import get_or_create_material
            _progress.update(0.65, "Building material")
            mat = get_or_create_material(
                spec, binding=binding, resolver=resolver, source_path=self.filepath,
            )
        except Exception as e:  # noqa: BLE001 - surface any build error to the user
            log.exception("GBXMAT material build failed")
            self.report({"ERROR"}, f"Material build failed: {e}")
            return {"CANCELLED"}
        # A material without users is dropped on save; keep the import.
        mat.use_fake_user = True
        _asset_panel.stamp(mat, "Material", self.filepath, material.name or "")
        context.scene.tm_imported_material = mat

        target = context.active_object
        if self.assign_to_active and target is not None and target.type == "MESH":
            target.data.materials.append(mat)
            target.active_material_index = len(target.data.materials) - 1

        summary = (
            f"{mat.name}: {spec.family}, {len(spec.textures)} textures"
            + (f", assigned to {target.name}" if self.assign_to_active and target is not None
               and target.type == "MESH" else "")
        )
        log.info("Created Blender material %s", summary)
        self.report({"INFO"}, summary)
        return {"FINISHED"}


def _build_blender_mesh_from_gbxbpy(
    gbx_mesh,
    lod_level: int = 1,
    *,
    resolver=None,
    source_path: str = "",
    progress=None,
    preserve_bone_indices: bool = False,
):
    """Create a Blender mesh+object from a GBXBPY Mesh asset.

    Builds the requested LOD (default = highest detail = 1). Falls back to
    the first available LOD if the requested level is missing.

    GBX meshes store one vertex per **loop** — every unique combination
    of position/normal/UV/color in the source produces a distinct vertex
    in ``lod.vertices``. If we feed those positions straight into
    Blender, faces that should share an edge end up with separate
    vertices and the mesh looks like a soup of disconnected triangles.

    To recover proper topology we deduplicate positions and remap
    triangle indices through the merge table, while keeping per-loop
    attributes (normals as custom split normals, UVs, colors) sourced
    from the original GBX vertex behind each loop. Shape-key (pose
    block) data is remapped to the first source vertex of each merged
    target.
    """
    import time as _time
    _t_total = _time.perf_counter()
    _MESH_BUILD_TIMINGS["calls"] += 1
    lod = gbx_mesh.lod(lod_level)
    if lod is None and gbx_mesh.lods:
        lod = gbx_mesh.lods[0]
    if lod is None or not lod.vertices:
        raise ValueError("Mesh has no usable LOD geometry")

    name = gbx_mesh.name or "TM_Mesh"
    me = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, me)

    # Materials first: decal faces need different vertex handling below.
    if progress is not None:
        progress(0.0, "Building materials")
    terrain_slot_layers = _terrain_layers_by_slot(gbx_mesh, lod)
    materials = []
    for i, binding in enumerate(gbx_mesh.materials):
        if progress is not None:
            progress(0.4 * i / max(1, len(gbx_mesh.materials)),
                     f"Building materials {i + 1}/{len(gbx_mesh.materials)}")
        materials.append(_material_for_binding(
            binding, resolver, source_path, terrain_layers=terrain_slot_layers.get(i),
            terrain_slot_votes=getattr(gbx_mesh, "terrain_slot_votes", None),
        ))
    if progress is not None:
        progress(0.4, "Building mesh topology")
    decal_slots = {
        i for i, mat in enumerate(materials)
        if mat is not None and mat.get("gbxmat_family") == "Decal"
    }
    decal_sources: set[int] = set()
    other_sources: set[int] = set()
    for sm in lod.submeshes:
        (decal_sources if sm.material_index in decal_slots else other_sources).update(sm.indices)

    # --- Position dedup -----------------------------------------------------
    # Map source-vertex index -> deduplicated index. We dedup on the raw
    # position tuple; it's the cheapest way to recover topology without
    # touching shading or UV seams (those are reapplied per-loop below).
    # Decal faces are coplanar with their host surface, which the game
    # resolves with a depth bias. They get their own vertices so they can
    # be lifted off the host without moving it.
    pos_to_new: dict[tuple, int] = {}
    old_to_new: list[int] = [0] * len(lod.vertices)
    decal_old_to_new: dict[int, int] = {}
    new_positions: list[tuple[float, float, float]] = []
    new_to_old_first: list[int] = []  # first source vertex per merged target
    for i, v in enumerate(lod.vertices):
        pos = v.position.as_tuple()
        for is_decal in (False, True):
            if is_decal and i not in decal_sources:
                continue
            if not is_decal and i in decal_sources and i not in other_sources:
                continue
            key = (pos, is_decal, v.bone_index) if preserve_bone_indices else (pos, is_decal)
            j = pos_to_new.get(key)
            if j is None:
                j = len(new_positions)
                pos_to_new[key] = j
                new_positions.append(pos)
                new_to_old_first.append(i)
            if is_decal:
                decal_old_to_new[i] = j
            else:
                old_to_new[i] = j

    # --- Triangles + per-loop source-vertex tracking ------------------------
    # Skip triangles that become degenerate after the position merge above
    # (two vertices coincide). Passing degenerate faces to `from_pydata`
    # produces a mesh that crashes Blender 5.1's
    # `normals_split_custom_set` with an access violation in
    # `mesh_normals_corner_custom_set`.
    triangles: list[tuple[int, int, int]] = []
    face_material_indices: list[int] = []
    loop_orig_vertex: list[int] = []   # source-vertex index per Blender loop
    decal_normals: dict[int, list[float]] = {}
    for sm in lod.submeshes:
        idx = sm.indices
        is_decal = sm.material_index in decal_slots
        remap = decal_old_to_new.__getitem__ if is_decal else old_to_new.__getitem__
        for i in range(0, len(idx) - 2, 3):
            a, b, c = idx[i], idx[i + 1], idx[i + 2]
            na, nb, nc = remap(a), remap(b), remap(c)
            if na == nb or nb == nc or na == nc:
                continue
            triangles.append((na, nb, nc))
            loop_orig_vertex.extend((a, b, c))
            face_material_indices.append(sm.material_index)
            if is_decal:
                pa, pb, pc = new_positions[na], new_positions[nb], new_positions[nc]
                e1 = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
                e2 = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
                n = (e1[1] * e2[2] - e1[2] * e2[1],
                     e1[2] * e2[0] - e1[0] * e2[2],
                     e1[0] * e2[1] - e1[1] * e2[0])  # area-weighted
                for vi in (na, nb, nc):
                    acc = decal_normals.setdefault(vi, [0.0, 0.0, 0.0])
                    acc[0] += n[0]; acc[1] += n[1]; acc[2] += n[2]

    vertex_offsets: dict[int, tuple[float, float, float]] = {}
    for vi, (nx, ny, nz) in decal_normals.items():
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length <= 0.0:
            continue
        s = _DECAL_LIFT / length
        vertex_offsets[vi] = (nx * s, ny * s, nz * s)
        x, y, z = new_positions[vi]
        new_positions[vi] = (x + nx * s, y + ny * s, z + nz * s)

    me.from_pydata(new_positions, [], triangles)
    me.update(calc_edges=True)
    if progress is not None:
        progress(0.7, "Applying normals and UVs")

    if face_material_indices:
        for poly, mi in zip(me.polygons, face_material_indices):
            poly.material_index = mi

    # Custom split normals: per Blender loop, look up the source vertex's
    # normal so hard-edge shading is preserved across the merge.
    has_normals = any(
        v.normal.x or v.normal.y or (v.normal.z != 1.0)
        for v in lod.vertices
    )
    if has_normals:
        try:
            # Guard: from_pydata may silently drop faces (e.g. duplicates),
            # leaving fewer Blender loops than `loop_orig_vertex` entries.
            # A mismatched-length normal array crashes Blender 5.1.
            if len(me.loops) == len(loop_orig_vertex):
                loop_normals = [
                    lod.vertices[loop_orig_vertex[loop.index]].normal.as_tuple()
                    for loop in me.loops
                ]
                me.normals_split_custom_set(loop_normals)
        except Exception:  # noqa: BLE001 - normals are optional
            pass

    has_uv1 = any(v.uv0.x or v.uv0.y for v in lod.vertices)
    has_uv2 = any(v.uv1.x or v.uv1.y for v in lod.vertices)
    has_uv3 = any(v.uv2.x or v.uv2.y for v in lod.vertices)

    def _set_uv(layer_name: str, attr: str) -> None:
        uv_layer = me.uv_layers.new(name=layer_name)
        for loop in me.loops:
            uv = getattr(lod.vertices[loop_orig_vertex[loop.index]], attr)
            uv_layer.data[loop.index].uv = (uv.x, uv.y)

    if has_uv1:
        _set_uv("BaseMaterial", "uv0")
    if has_uv2:
        _set_uv("Lightmap", "uv1")
    if has_uv3:
        _set_uv("UV2", "uv2")

    if any(v.color is not None for v in lod.vertices):
        color_attr = me.color_attributes.new(
            name="Color", type="FLOAT_COLOR", domain="CORNER"
        )
        white = (1.0, 1.0, 1.0, 1.0)
        for loop in me.loops:
            c = lod.vertices[loop_orig_vertex[loop.index]].color
            color_attr.data[loop.index].color = c.as_tuple() if c is not None else white

    if terrain_slot_layers and len(me.loops) == len(loop_orig_vertex):
        _set_terrain_layer_weights(me, lod, loop_orig_vertex, terrain_slot_layers)
        _set_terrain_layer_names(
            me, gbx_mesh, terrain_slot_layers, resolver, source_path,
        )

    for mat, binding in zip(materials, gbx_mesh.materials):
        if mat is None:
            slot_name = binding.material_path or "inline"
            slot_name = os.path.basename(slot_name.replace("\\", "/")) or "Material"
            mat = bpy.data.materials.new(name=slot_name)
        me.materials.append(mat)

    if preserve_bone_indices:
        groups = {}
        for index, original in enumerate(new_to_old_first):
            bone_index = lod.vertices[original].bone_index
            if bone_index >= 0:
                groups.setdefault(bone_index, []).append(index)
        for bone_index, indices in groups.items():
            obj.vertex_groups.new(name=f"TM_Bone_{bone_index}").add(indices, 1.0, "REPLACE")

    if lod.pose_blocks:
        if progress is not None:
            progress(0.9, "Building shape keys")
        _add_pose_shape_keys(obj, lod, new_to_old_first, vertex_offsets)

    if progress is not None:
        progress(1.0, "Mesh built")
    _MESH_BUILD_TIMINGS["total"] += _time.perf_counter() - _t_total
    from ._item_geometry import configure_part
    from GBXBPY.model.geometry import MeshPart
    configure_part(obj, MeshPart(name=name, mesh=gbx_mesh, collidable=gbx_mesh.collidable))
    return obj


def _primary_lod(gbx_mesh, primary_level):
    """The LOD that :func:`_build_blender_mesh_from_gbxbpy` builds for ``primary_level``."""
    lod = gbx_mesh.lod(primary_level)
    if lod is None and gbx_mesh.lods:
        lod = gbx_mesh.lods[0]
    return lod


def _build_extra_lod_meshes(gbx_mesh, primary_level, *, resolver=None, source_path=""):
    """Mesh datablocks for every LOD except the one ``primary_level`` builds.

    Returns ``[(level, switch_distance, mesh_data), ...]`` in ascending level
    order; levels without usable geometry are skipped. Used by the "all LODs"
    import mode: the datablocks are cached alongside the primary mesh and
    materialized per placement by :func:`_attach_extra_lod_objects`.
    """
    primary = _primary_lod(gbx_mesh, primary_level)
    extras = []
    for level in gbx_mesh.lod_levels():
        lod = gbx_mesh.lod(level)
        if lod is None or lod is primary:
            continue
        if not lod.vertices or not any(s.indices for s in lod.submeshes):
            continue
        obj = _build_blender_mesh_from_gbxbpy(
            gbx_mesh, level, resolver=resolver, source_path=source_path,
        )
        data = obj.data
        bpy.data.objects.remove(obj)
        data.name = f"{gbx_mesh.name or 'TM_Mesh'} LOD{level}"
        extras.append((level, lod.distance, data))
    return extras


def _attach_extra_lod_objects(primary_obj, gbx_mesh, primary_level, extras, collection):
    """Link hidden LOD-variant objects parented to ``primary_obj``.

    Every object of the LOD set (including ``primary_obj``) is tagged with
    ``tm_lod_level`` and ``tm_lod_distance`` (the game's switch distance in
    metres, 0 if unknown), so the set stays queryable after renames. The
    extra LODs ride along with every placement through the parent link and
    are hidden in viewport and render.
    """
    primary = _primary_lod(gbx_mesh, primary_level)
    if primary is not None:
        primary_obj["tm_lod_level"] = primary.lod
        primary_obj["tm_lod_distance"] = primary.distance
    for level, distance, data in extras:
        lod_obj = bpy.data.objects.new(f"{primary_obj.name} LOD{level}", data)
        collection.objects.link(lod_obj)
        lod_obj.parent = primary_obj
        lod_obj.hide_viewport = lod_obj.hide_render = True
        lod_obj["tm_lod_level"] = level
        lod_obj["tm_lod_distance"] = distance


# Lift of decal faces off their coplanar host surface, in metres. The game
# uses a depth bias instead. Measured in EEVEE with the viewport's default
# 0.01 m near clip: 10 mm keeps a decal visible to ~100 m, 5 mm only ~40 m.
_DECAL_LIFT = 0.01


def _terrain_vertex_weights(vertex) -> tuple[tuple[int, float], ...]:
    """Slot weights of one vista terrain vertex.

    ``BlendIndices`` bytes 0/1 select material slots a/b; ``Color0`` green
    is the weight of slot b (red = slot a). Vertices without ``Color0`` use a.
    """
    t = min(max(float(vertex.color.g), 0.0), 1.0) if vertex.color is not None else 0.0
    a, b = int(vertex.layer_idx_a), int(vertex.layer_idx_b)
    if a == b or t <= 0.0:
        return ((a, 1.0),)
    if t >= 1.0:
        return ((b, 1.0),)
    return ((a, 1.0 - t), (b, t))


def _terrain_layers_by_slot(gbx_mesh, lod) -> dict[int, tuple[int, ...]]:
    """Material slot -> vertex terrain slots (``BlendIndices``) it uses."""
    if is_layered_terrain_binding is None:
        return {}
    layered = {
        i for i, binding in enumerate(gbx_mesh.materials)
        if is_layered_terrain_binding(binding)
    }
    result: dict[int, set[int]] = {}
    for sm in lod.submeshes:
        if sm.material_index not in layered:
            continue
        used = result.setdefault(sm.material_index, set())
        for i in set(sm.indices):
            used.update(layer for layer, _ in _terrain_vertex_weights(lod.vertices[i]))
    return {slot: tuple(sorted(used)) for slot, used in result.items() if used}


def _set_terrain_layer_weights(me, lod, loop_orig_vertex, slot_layers) -> None:
    """Write one ``TMLayer<slot>`` float corner attribute per used terrain slot."""
    layers = sorted({layer for used in slot_layers.values() for layer in used})
    columns = {layer: [0.0] * len(loop_orig_vertex) for layer in layers}
    cache: dict[int, tuple] = {}
    for loop_index, source in enumerate(loop_orig_vertex):
        weights = cache.get(source)
        if weights is None:
            weights = cache[source] = _terrain_vertex_weights(lod.vertices[source])
        for layer, weight in weights:
            column = columns.get(layer)
            if column is not None:
                column[loop_index] = weight
    for layer, values in columns.items():
        attr = me.attributes.new(name=f"TMLayer{layer}", type="FLOAT", domain="CORNER")
        attr.data.foreach_set("value", values)


def _set_terrain_layer_names(me, gbx_mesh, slot_layers, resolver, source_path) -> None:
    """Store ``tm_terrain_layers`` (JSON: vertex slot -> top layer, tile, media dir).

    The terrain grass scatter reads it to find the ``TMLayer<slot>`` weights
    of layers with 3D decals.
    """
    if layered_terrain_top_layers is None:
        return
    import json

    names: dict = {}
    for material_slot, used in slot_layers.items():
        try:
            names.update(layered_terrain_top_layers(
                gbx_mesh.materials[material_slot], used, resolver=resolver,
                source_path=source_path,
                slot_votes=getattr(gbx_mesh, "terrain_slot_votes", None),
            ))
        except Exception as e:  # noqa: BLE001 - metadata only
            log.debug("terrain layer names unavailable: %s", e)
    if names:
        me["tm_terrain_layers"] = json.dumps({str(k): v for k, v in names.items()})


def _material_for_binding(
    binding, resolver, source_path: str, *, terrain_layers=None, terrain_slot_votes=None,
):
    """Build the GBXMAT material for one mesh binding, or ``None`` on failure."""
    if GBXMAT is None or spec_from_binding is None:
        return None
    import time as _time
    slot_name = os.path.basename((binding.material_path or "inline").replace("\\", "/")) or "Material"
    try:
        _t_mat = _time.perf_counter()
        spec = spec_from_binding(
            binding, resolver=resolver, source_path=source_path,
            terrain_layers=terrain_layers, terrain_slot_votes=terrain_slot_votes,
        )
        from ._blendermania import get_or_create_material
        mat = get_or_create_material(
            spec, binding=binding, resolver=resolver, source_path=source_path,
        )
        _MESH_BUILD_TIMINGS["material"] += _time.perf_counter() - _t_mat
        return mat
    except Exception as e:  # noqa: BLE001 - never let materials fail mesh build
        log.exception("GBXMAT material build failed for %s: %s", slot_name, e)
        return None


def _add_pose_shape_keys(obj, lod, new_to_old_first: list[int], vertex_offsets=None) -> None:
    """Turn ``MeshLOD.pose_blocks`` into Blender shape keys + cyclic anim.

    Each pose block is a same-length-as-vertices list of alternative
    positions. The first key is the rest pose (``Basis``); each subsequent
    key activates one stored animation pose. One cyclic keyframe set steps
    through them so timeline playback reproduces the in-engine wind motion.

    Every individual shape-key RNA write costs time proportional to the
    number of objects in the file, so coordinates and keyframes are written
    with bulk ``foreach_set`` calls instead of per-element assignments.
    """
    if obj.data.shape_keys is not None:
        obj.shape_key_clear()

    sk_basis = obj.shape_key_add(name="Basis", from_mix=False)
    if sk_basis.interpolation != "KEY_LINEAR":
        sk_basis.interpolation = "KEY_LINEAR"
    basis_co = [0.0] * (len(sk_basis.data) * 3)
    sk_basis.data.foreach_get("co", basis_co)

    pose_key_names: list[str] = []
    for pose_idx, block in enumerate(lod.pose_blocks, start=1):
        key = obj.shape_key_add(name=f"TM_WindPose_{pose_idx:03d}", from_mix=False)
        if key.interpolation != "KEY_LINEAR":
            key.interpolation = "KEY_LINEAR"
        # Pose blocks are indexed by *source* (pre-dedup) vertex; remap
        # each merged target vertex back to the first source vertex that
        # produced it so the deformation matches.
        co = list(basis_co)
        for new_i, old_i in enumerate(new_to_old_first):
            if new_i * 3 >= len(co) or old_i >= len(block):
                break
            pos = block[old_i]
            off = vertex_offsets.get(new_i) if vertex_offsets else None
            if off is None:
                co[new_i * 3:new_i * 3 + 3] = (pos.x, pos.y, pos.z)
            else:
                co[new_i * 3:new_i * 3 + 3] = (pos.x + off[0], pos.y + off[1], pos.z + off[2])
        key.data.foreach_set("co", co)
        pose_key_names.append(key.name)

    obj.data.shape_keys.use_relative = True
    obj["tm_wind_pose_count"] = len(pose_key_names)
    obj["tm_wind_animation"] = True

    if not pose_key_names:
        return

    shape_keys = obj.data.shape_keys
    if shape_keys.animation_data is None:
        shape_keys.animation_data_create()
    anim_data = shape_keys.animation_data
    while anim_data.drivers:
        anim_data.drivers.remove(anim_data.drivers[0])

    action = bpy.data.actions.new(name=f"{obj.name}_TM_Wind")
    anim_data.action = action

    pose_count = len(pose_key_names)
    # The timeline still spans the previously baked 120 cycles; the CYCLES
    # modifier repeats the single cycle beyond that as well.
    cycles = 120
    period = pose_count

    def weight(pose_i: int, frame: int) -> float:
        return 1.0 if (frame - 1) % period == pose_i else 0.0

    linear = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    for pose_i, key_name in enumerate(pose_key_names):
        data_path = f'key_blocks["{key_name}"].value'
        if hasattr(action, "fcurve_ensure_for_datablock"):
            fcurve = action.fcurve_ensure_for_datablock(shape_keys, data_path)
        else:
            fcurve = action.fcurves.new(data_path)
        # Each pose ramps 0 -> 1 -> 0 around its own frame. Frames 1 and
        # period + 1 bound the cycle so CYCLES repeats exactly ``period``.
        frames = {1, period + 1}
        frames.update(f for f in range(1, period + 2)
                      if any(weight(pose_i, g) for g in (f - 1, f, f + 1)))
        points = sorted(frames)
        fcurve.keyframe_points.add(len(points))
        fcurve.keyframe_points.foreach_set(
            "co", [c for f in points for c in (float(f), weight(pose_i, f))])
        fcurve.keyframe_points.foreach_set("interpolation", [linear] * len(points))
        fcurve.modifiers.new(type="CYCLES")
        fcurve.update()

    end_frame = 1 + cycles * period
    try:
        scene = bpy.context.scene
        if scene.frame_end < end_frame:
            scene.frame_end = end_frame
    except Exception:  # noqa: BLE001
        pass


class TMBRIDGE_OT_ImportMesh(bpy.types.Operator, ImportHelper):
    """Import a Trackmania .Mesh.Gbx (CPlugSolid2Model) via GBXBPY."""

    bl_idname = "trackmania_blender_bridge.import_mesh"
    bl_label = "Trackmania Mesh (.Mesh.Gbx)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Gbx"
    filter_glob: StringProperty(
        default="*.Mesh.Gbx;*.mesh.gbx",
        options={"HIDDEN"},
        maxlen=255,
    )

    lod_level: bpy.props.IntProperty(
        name="LOD Level",
        description="Which LOD level to import (1 = highest detail)",
        default=1,
        min=1,
    )

    @_progress.tracked("Importing Mesh")
    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, f"File not found: {self.filepath}")
            return {"CANCELLED"}

        gamedata_dir = _get_gamedata_dir(context)
        try:
            import GBXBPY
            from GBXBPY.errors import GbxbpyError
        except ImportError as e:
            log.exception("Failed to import GBXBPY: %s", e)
            self.report({"ERROR"}, f"GBXBPY package not importable: {e}")
            return {"CANCELLED"}

        resolver = GBXBPY.Resolver(gamedata_dir) if gamedata_dir else None

        log.info("Importing mesh: %s", self.filepath)
        try:
            asset = GBXBPY.load(self.filepath, resolver=resolver)
        except GbxbpyError as e:
            log.error("GBXBPY load failed: %s", e)
            self.report({"ERROR"}, f"GBXBPY load failed: {e}")
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error during mesh import")
            self.report({"ERROR"}, f"Unexpected error: {e}")
            return {"CANCELLED"}

        if not hasattr(asset, "lods"):
            # VegetationModel: unwrap to its first variant's mesh.
            variants = getattr(asset, "variants", None)
            if variants:
                first_mesh = getattr(variants[0], "mesh", None)
                if first_mesh is not None and hasattr(first_mesh, "lods"):
                    log.info(
                        "Unwrapping %s -> first variant mesh (%d variants total)",
                        type(asset).__name__, len(variants),
                    )
                    asset = first_mesh

        if not hasattr(asset, "lods"):
            self.report(
                {"ERROR"},
                f"Loaded asset is not a Mesh (got {type(asset).__name__})",
            )
            return {"CANCELLED"}

        log.info(
            "Loaded mesh '%s' lods=%s materials=%d",
            asset.name, asset.lod_levels(), len(asset.materials),
        )
        for lod in asset.lods:
            tris = sum(len(s.indices) for s in lod.submeshes) // 3
            log.info(
                "  LOD %d (dist=%.1f): %d verts, %d tris, %d submeshes",
                lod.lod, lod.distance, len(lod.vertices), tris, len(lod.submeshes),
            )

        try:
            obj = _build_blender_mesh_from_gbxbpy(
                asset, self.lod_level,
                resolver=resolver, source_path=self.filepath,
                progress=_progress.Progress(0.35, 0.95),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to build Blender mesh")
            self.report({"ERROR"}, f"Failed to build mesh: {e}")
            return {"CANCELLED"}

        context.collection.objects.link(obj)
        from ._item_geometry import attach_collision_parts
        attach_collision_parts(obj, asset, context.collection, lod_level=self.lod_level,
                               resolver=resolver, source_path=self.filepath)
        kind = "Vegetation" if self.filepath.lower().endswith(".vegettreemodel.gbx") else "Mesh"
        _asset_panel.stamp(obj, kind, self.filepath, asset.name or "")
        obj["tm_lod_level"] = self.lod_level
        context.view_layer.objects.active = obj
        obj.select_set(True)

        summary = (
            f"{asset.name}: LOD {self.lod_level} "
            f"({len(obj.data.vertices)} verts, {len(obj.data.polygons)} tris, "
            f"{len(obj.data.materials)} mats)"
        )
        self.report({"INFO"}, summary)
        return {"FINISHED"}


class TMBRIDGE_OT_ImportVegetTree(bpy.types.Operator, ImportHelper):
    """Import a Trackmania .VegetTreeModel.Gbx (CPlugVegetTreeModel) via GBXBPY.

    Vegetation models carry two LODs (tree1 = high, tree2 = low). The mesh
    import path already unwraps a ``VegetationModel`` to its first variant's
    mesh, which is reused here.
    """

    bl_idname = "trackmania_blender_bridge.import_veget_tree"
    bl_label = "Trackmania Veget Tree (.VegetTreeModel.Gbx)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Gbx"
    filter_glob: StringProperty(
        default="*.Gbx;*.gbx",
        options={"HIDDEN"},
        maxlen=255,
    )

    lod_level: bpy.props.IntProperty(
        name="LOD Level",
        description="Which LOD level to import (1 = highest detail)",
        default=1,
        min=1,
    )

    def execute(self, context):
        # Delegate to the regular mesh-import operator's execute method,
        # which already handles VegetationModel unwrapping.
        return TMBRIDGE_OT_ImportMesh.execute(self, context)


# Per-import cache: abs_path -> parsed asset, used by
# ``_build_external_entity_ref`` so a single import of a complex map
# doesn't re-parse the same external Solid2 / Prefab repeatedly.
_EXTERNAL_REF_PARSE_CACHE: dict[str, object] = {}

# Diagnostic timers: total time spent inside the mesh builder vs the
# share inside GBXMAT material creation. Reset by realize.reset_caches.
_MESH_BUILD_TIMINGS: dict[str, float] = {"total": 0.0, "material": 0.0, "calls": 0}


def _build_external_entity_ref(
    entity,
    label: str,
    target_collection,
    *,
    context,
    lod_level: int,
    mesh_cache: dict,
    subprefab_cache: dict,
    report,
    resolver,
    source_path: str,
):
    """Resolve ``entity.model_path`` and instance the result.

    Returns the created Object (mesh / Empty wrapping a sub-prefab
    Collection) or ``None`` when the ref cannot be resolved or parsed.
    """
    abs_path = resolver.resolve(entity.model_path, source_path=source_path)
    if not abs_path:
        log.debug("external entity %s: cannot resolve %r", label, entity.model_path)
        return None

    if entity.model_class_id == 0x0915C000:
        # FxSystem: hook-refetched when pack-encrypted, then built as particles.
        from GBXBPY.load.fx import read_fx_system
        from .fx_particles import build_fx_object

        fx = read_fx_system(abs_path)
        if fx is None:
            if report is not None:
                report("WARNING", f"Entity {label}: effect {os.path.basename(abs_path)} could not be decoded")
            return None
        return build_fx_object(fx, label, target_collection)

    parsed = _EXTERNAL_REF_PARSE_CACHE.get(abs_path)
    if parsed is None and abs_path not in _EXTERNAL_REF_PARSE_CACHE:
        try:
            from GBXBPY.io import read_gbx
            from GBXBPY.registry import dispatch
            from GBXBPY import cache as _gbx_cache
            import GBXBPY.load  # noqa: F401

            def _do_parse(p: str):
                raw = read_gbx(p, recursive=False)
                return dispatch(raw, source_path=p, resolver=resolver)

            parsed = _gbx_cache.cached_load(
                abs_path, _do_parse, label=entity.model_path,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("external entity %s: parse failed (%s)", label, e)
            parsed = None
        _EXTERNAL_REF_PARSE_CACHE[abs_path] = parsed

    if parsed is None:
        return None

    from GBXBPY.model import Item
    if isinstance(parsed, Item):
        from ._item_geometry import build_item_collection
        sub_key = id(parsed)
        cached_coll = subprefab_cache.get(sub_key)
        if cached_coll is None:
            cached_coll = bpy.data.collections.new(parsed.name or label)
            build_item_collection(parsed, context, lod_level, resolver=resolver,
                                  source_path=abs_path, target_collection=cached_coll)
            subprefab_cache[sub_key] = cached_coll
        child = bpy.data.objects.new(label, None)
        child.instance_type = "COLLECTION"
        child.instance_collection = cached_coll
        return child

    if hasattr(parsed, 'lods'):
        # Mesh.
        try:
            child = _build_blender_mesh_from_gbxbpy(
                parsed, lod_level, resolver=resolver, source_path=abs_path,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("external entity %s: mesh build failed (%s)", label, e)
            return None
        child.name = label
        from ._item_geometry import configure_part, attach_collision_parts
        from GBXBPY.model.geometry import MeshPart
        configure_part(child, MeshPart(name=label, mesh=parsed, collidable=parsed.collidable))
        attach_collision_parts(child, parsed, target_collection, lod_level=lod_level,
                               resolver=resolver, source_path=abs_path)
        return child

    if hasattr(parsed, 'entities'):
        # Prefab \u2014 wrap in a Collection and emit an instance Empty.
        sub_key = id(parsed)
        cached_coll = subprefab_cache.get(sub_key)
        if cached_coll is None:
            sub_name = getattr(parsed, 'name', '') or label
            cached_coll = bpy.data.collections.new(sub_name)
            _build_prefab_object_impl(
                parsed, context, lod_level,
                target_collection=cached_coll,
                mesh_cache=mesh_cache,
                subprefab_cache=subprefab_cache,
                report=report,
                resolver=resolver,
                source_path=abs_path,
                resolve_external_entities=True,
            )
            subprefab_cache[sub_key] = cached_coll
        child = bpy.data.objects.new(label, None)
        child.instance_type = "COLLECTION"
        child.instance_collection = cached_coll
        return child

    log.debug("external entity %s: unsupported asset type %s",
              label, type(parsed).__name__)
    return None


# Display size (metres) of vegetation markers by their ``Size`` tag.
_MARKER_SIZES = {"Small": 1.0, "Medium": 2.5, "Big": 5.0}


def _build_prefab_object_impl(
    prefab,
    context,
    lod_level: int,
    *,
    target_collection=None,
    mesh_cache: dict | None = None,
    subprefab_cache: dict | None = None,
    report=None,
    resolver=None,
    source_path: str = "",
    resolve_external_entities: bool = False,
    import_all_lods: bool = False,
    progress=None,
):
    """Module-level prefab builder used by both prefab and item importers.

    Animates entities with a ``DynaAnimDriver`` by generating a small
    keyframe set and a ``CYCLES`` F-curve modifier on the linked target
    entity (or the driver itself when no ``link.ent2`` is given).

    When ``resolver`` is given, sub-prefab entities that reference an
    external file (``model_class_id == 0x09145000`` with ``model_path``
    but no inline ``sub_prefab``) are resolved and instanced; otherwise
    they are silently skipped \u2014 mirroring the legacy importer, which
    never emitted placeholder Empties for unresolved entities.
    """
    if mesh_cache is None:
        mesh_cache = {}
    if subprefab_cache is None:
        subprefab_cache = {}
    if target_collection is None:
        target_collection = context.collection

    root_name = prefab.name or "Prefab"
    root = bpy.data.objects.new(root_name, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 1.0
    root["tm_prefab_version"] = prefab.prefab_version
    root["tm_prefab_url"] = prefab.url
    root["tm_prefab_entity_count"] = len(prefab.entities)
    _asset_panel.stamp(root, "Prefab", source_path or getattr(prefab, "source_path", "") or "", prefab.name or "")
    target_collection.objects.link(root)

    # Build all entities first; track per-index Object so anim drivers
    # can find their target via link.ent2.
    children: list = [None] * len(prefab.entities)

    for idx, entity in enumerate(prefab.entities):
        entity_progress = None
        if progress is not None:
            count = max(1, len(prefab.entities))
            progress(0.9 * idx / count, f"Building entities {idx + 1}/{count}")
            entity_progress = progress.sub(0.9 * idx / count, 0.9 * (idx + 1) / count)
        child = None
        label = entity.name or f"Ent{idx:02d}"
        light_templates: list = []
        extra_lods: list = []

        if entity.mesh is not None:
            cache_key = id(entity.mesh)
            cached_entry = mesh_cache.get(cache_key)
            try:
                if cached_entry is not None:
                    cached_data, light_templates, extra_lods = cached_entry
                    child = bpy.data.objects.new(label, cached_data)
                else:
                    child = _build_blender_mesh_from_gbxbpy(
                        entity.mesh, lod_level,
                        resolver=resolver, source_path=entity.mesh.source_path or source_path,
                        progress=entity_progress,
                    )
                    light_templates = _build_light_templates(entity.mesh)
                    if import_all_lods:
                        extra_lods = _build_extra_lod_meshes(
                            entity.mesh, lod_level,
                            resolver=resolver,
                            source_path=entity.mesh.source_path or source_path,
                        )
                    mesh_cache[cache_key] = (child.data, light_templates, extra_lods)
                    child.name = label
            except Exception as e:  # noqa: BLE001
                log.exception("Failed to build entity mesh %s", label)
                if report is not None:
                    report("WARNING", f"Entity {label}: mesh build failed ({e})")
                child = bpy.data.objects.new(label, None)
                child.empty_display_type = "CUBE"
        elif entity.sub_prefab is not None:
            sub_key = id(entity.sub_prefab)
            cached_coll = subprefab_cache.get(sub_key)
            if cached_coll is None:
                sub_name = entity.sub_prefab.name or label
                cached_coll = bpy.data.collections.new(sub_name)
                _build_prefab_object_impl(
                    entity.sub_prefab,
                    context,
                    lod_level,
                    target_collection=cached_coll,
                    mesh_cache=mesh_cache,
                    subprefab_cache=subprefab_cache,
                    report=report,
                    resolver=resolver,
                    source_path=source_path,
                    resolve_external_entities=resolve_external_entities,
                    import_all_lods=import_all_lods,
                    progress=entity_progress,
                )
                subprefab_cache[sub_key] = cached_coll

            child = bpy.data.objects.new(label, None)
            child.instance_type = "COLLECTION"
            child.instance_collection = cached_coll
        elif entity.anim_driver is not None:
            # Procedural anim driver \u2014 no geometry. Materialize as a small
            # empty so the user can see/manipulate it; keyframes will be
            # written below onto either this or the linked target.
            child = bpy.data.objects.new(label, None)
            child.empty_display_type = "ARROWS"
            child.empty_display_size = 0.4
        elif entity.fx is not None:
            # Decoded fogger/sparkler effect: Geometry Nodes particle emitters.
            from .fx_particles import build_fx_object

            child = build_fx_object(entity.fx, label, target_collection)
        elif entity.marker_name:
            # Geometry unavailable (truncated vista VegetTreeModel): a named
            # stand-in at the entity's placement.
            child = bpy.data.objects.new(entity.marker_name, None)
            child.empty_display_type = "SINGLE_ARROW"
            child.empty_display_size = _MARKER_SIZES.get(entity.marker_tags.get("Size", ""), 2.0)
            child["tm_vegetation_model"] = entity.marker_name
            if entity.marker_tags:
                child["tm_placement_tags"] = ";".join(
                    f"{key}={value}" for key, value in entity.marker_tags.items()
                )
        else:
            # No inline geometry / driver. When ``resolve_external_entities``
            # is set we try to follow ``entity.model_path`` through the
            # resolver and instance whatever it points at (Mesh, Prefab,
            # Item, ...). The legacy importer skips this branch.
            child = None
            if (
                (resolve_external_entities or entity.model_class_id == 0x0915C000)
                and resolver is not None
                and entity.model_path
            ):
                child = _build_external_entity_ref(
                    entity, label, target_collection,
                    context=context, lod_level=lod_level,
                    mesh_cache=mesh_cache, subprefab_cache=subprefab_cache,
                    report=report, resolver=resolver,
                    source_path=source_path,
                )
            if child is None:
                continue

        if entity.model_class_id:
            child["tm_prefab_model_class_id"] = entity.model_class_id
        if entity.is_dynamic:
            child["tm_prefab_dynamic"] = True
        if entity.is_kinematic:
            child["tm_prefab_kinematic"] = True

        target_collection.objects.link(child)
        if entity.mesh is not None:
            from ._item_geometry import configure_part, attach_collision_parts
            from GBXBPY.model.geometry import MeshPart
            if child.type == "MESH":
                configure_part(child, MeshPart(name=label, mesh=entity.mesh,
                                               collidable=entity.mesh.collidable))
                attach_collision_parts(child, entity.mesh, target_collection, lod_level=lod_level,
                                       resolver=resolver, source_path=source_path)

        pos = entity.transform.position
        rot = entity.transform.rotation
        child.location = (pos.x, pos.y, pos.z)
        child.rotation_mode = "QUATERNION"
        child.rotation_quaternion = (rot.w, rot.x, rot.y, rot.z)
        child.parent = root

        if light_templates:
            _attach_light_instances(child, light_templates, target_collection)
        if extra_lods:
            _attach_extra_lod_objects(
                child, entity.mesh, lod_level, extra_lods, target_collection,
            )

        children[idx] = child

    # Second pass: install procedural animations.
    if progress is not None:
        progress(0.9, "Applying animations")
    for idx, entity in enumerate(prefab.entities):
        if entity.anim_driver is None:
            continue
        target_idx = entity.link.ent2 if entity.link is not None else -1
        if 0 <= target_idx < len(children) and children[target_idx] is not None:
            target = children[target_idx]
        else:
            target = children[idx]   # fall back to driver itself
        try:
            _apply_anim_driver(target, entity.anim_driver, context.scene)
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to apply anim driver to %s", target.name)
            if report is not None:
                report("WARNING", f"Anim driver on {target.name} failed: {e}")

    if progress is not None:
        progress(1.0, "Prefab built")
    return root


def _gbx_axis_to_blender(axis: str) -> tuple[int, float]:
    """Map a GBX axis name to ``(blender_index, sign)``.

    GBX uses a left-handed Y-up frame; Blender is right-handed Z-up. The
    coord swizzle is ``(x, y, z) -> (x, -z, y)`` (see
    :func:`GBXBPY.coords.gbx_to_blender_vec3`). For an axis-aligned
    angular-velocity vector the same swizzle applies, so:

    * GBX  X →  Blender X (no flip)
    * GBX  Y →  Blender Z (no flip)
    * GBX  Z →  Blender Y, **negated**
    """
    return {
        "X": (0, 1.0),
        "Y": (2, 1.0),
        "Z": (1, -1.0),
    }.get(axis, (-1, 1.0))


def _find_fcurve(action, data_path: str, index: int):
    """Find an F-curve on either a legacy or layered (Blender 4.4+) Action."""
    # Legacy API (Blender < 4.4)
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves.find(data_path, index=index)
    # Layered API: action.layers[*].strips[*].channelbag(slot).fcurves
    layers = getattr(action, "layers", None)
    if not layers:
        return None
    for layer in layers:
        for strip in getattr(layer, "strips", []) or []:
            for cbag in getattr(strip, "channelbags", []) or []:
                for fc in getattr(cbag, "fcurves", []) or []:
                    if fc.data_path == data_path and fc.array_index == index:
                        return fc
    return None


def _apply_anim_driver(obj, drv, scene) -> None:
    """Build complete piecewise animation channels, including return segments."""
    from GBXBPY.model.kinematic import AnimSubFunc

    fps = scene.render.fps / scene.render.fps_base
    ease_families = ("QUAD", "CUBIC", "QUART", "QUINT", "SINE", "EXPO",
                     "CIRC", "BACK", "ELASTIC", "ELASTIC", "BOUNCE")
    ease_names = ("Quad", "Cubic", "Quart", "Quint", "Sine", "Exp",
                  "Circ", "Back", "Elastic", "Elastic2", "Bounce")

    def easing(value):
        if value == 0:
            return "CONSTANT", "AUTO"
        if value == 1:
            return "LINEAR", "AUTO"
        if 2 <= value <= 34 and not 29 <= value <= 31:
            return ease_families[(value - 2) // 3], (
                "EASE_IN", "EASE_OUT", "EASE_IN_OUT"
            )[(value - 2) % 3]
        raise ValueError(f"Unsupported GBX animation easing {value}")

    def channel(data_path, axis, minimum, maximum, segments, duration, ease, reverse, time_is_duration):
        if axis not in ("X", "Y", "Z"):
            return
        if not segments:
            if duration <= 0:
                return
            names = ["Constant", "Linear"] + [
                family + suffix for family in ease_names for suffix in ("In", "Out", "InOut")
            ]
            segments = [AnimSubFunc(ease=names.index(ease), reverse=reverse, duration_ms=duration)]
        if minimum == maximum == 0:
            return
        if not time_is_duration:
            raise ValueError("Animation curves with absolute-time sub-functions are unsupported")
        if any(segment.duration_ms <= 0 for segment in segments):
            raise ValueError("Animation segment duration must be positive")
        styles = [easing(segment.ease) for segment in segments]
        index, sign = _gbx_axis_to_blender(axis)
        base = getattr(obj, data_path)[index]
        frame = 1.0
        starts = []
        for segment, style in zip(segments, styles):
            start, end = (maximum, minimum) if segment.reverse else (minimum, maximum)
            starts.append((frame, style))
            getattr(obj, data_path)[index] = base + start * sign
            obj.keyframe_insert(data_path=data_path, index=index, frame=frame)
            frame += segment.duration_ms * fps / 1000.0
            getattr(obj, data_path)[index] = base + end * sign
            obj.keyframe_insert(data_path=data_path, index=index, frame=frame)
        fcurve = _find_fcurve(obj.animation_data.action, data_path, index)
        if fcurve is None:
            raise RuntimeError(f"Animation F-curve not created for {obj.name}.{data_path}[{index}]")
        for point, (_, style) in zip(fcurve.keyframe_points, starts):
            point.interpolation, point.easing = style
        if not any(modifier.type == "CYCLES" for modifier in fcurve.modifiers):
            fcurve.modifiers.new(type="CYCLES")
        scene.frame_end = max(scene.frame_end, math.ceil(frame))
        getattr(obj, data_path)[index] = fcurve.evaluate(scene.frame_current)

    if drv.rot_axis in ("X", "Y", "Z") and (drv.rot_angle_min_deg or drv.rot_angle_max_deg):
        if obj.rotation_mode == "QUATERNION":
            rest_rotation = obj.rotation_quaternion.to_euler("XYZ")
            obj.rotation_mode = "XYZ"
            obj.rotation_euler = rest_rotation
        channel("rotation_euler", drv.rot_axis,
                math.radians(drv.rot_angle_min_deg), math.radians(drv.rot_angle_max_deg),
                drv.rot_sub_funcs, drv.rot_duration_ms, drv.rot_easing, drv.rot_reverse,
                drv.rot_time_is_duration)
    channel("location", drv.trans_axis, drv.trans_min, drv.trans_max,
            drv.trans_sub_funcs, drv.trans_duration_ms, drv.trans_easing, drv.trans_reverse,
            drv.time_is_duration)


class TMBRIDGE_OT_ImportItem(bpy.types.Operator, ImportHelper):
    """Import a Trackmania .Item.Gbx (CGameItemModel) via GBXBPY."""

    bl_idname = "trackmania_blender_bridge.import_item"
    bl_label = "Trackmania Item (.Item.Gbx)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Gbx"
    filter_glob: StringProperty(
        default="*.Item.Gbx;*.item.gbx",
        options={"HIDDEN"},
        maxlen=255,
    )

    lod_level: bpy.props.IntProperty(
        name="LOD Level",
        description="Which LOD level to import for the embedded mesh (1 = highest detail)",
        default=1,
        min=1,
    )

    @_progress.tracked("Importing Item")
    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, f"File not found: {self.filepath}")
            return {"CANCELLED"}

        gamedata_dir = _get_gamedata_dir(context)
        try:
            import GBXBPY
            from GBXBPY.errors import GbxbpyError
        except ImportError as e:
            log.exception("Failed to import GBXBPY: %s", e)
            self.report({"ERROR"}, f"GBXBPY package not importable: {e}")
            return {"CANCELLED"}

        resolver = GBXBPY.Resolver(gamedata_dir) if gamedata_dir else None

        log.info("Importing item: %s", self.filepath)
        try:
            asset = GBXBPY.load(self.filepath, resolver=resolver)
        except GbxbpyError as e:
            log.error("GBXBPY load failed: %s", e)
            self.report({"ERROR"}, f"GBXBPY load failed: {e}")
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error during item import")
            self.report({"ERROR"}, f"Unexpected error: {e}")
            return {"CANCELLED"}

        # Duck-type the asset: needs the Item-shaped fields.
        if not hasattr(asset, "entity_variants") or not hasattr(asset, "default_placement"):
            self.report(
                {"ERROR"},
                f"Loaded asset is not an Item (got {type(asset).__name__})",
            )
            return {"CANCELLED"}

        log.info(
            "Loaded item '%s' type=%r ingame=%s custom=%s variants=%d "
            "parts=%d embedded_prefab=%s",
            asset.name, asset.item_type, asset.is_ingame, asset.is_custom,
            len(asset.entity_variants),
            len(asset.parts),
            getattr(asset, "embedded_prefab", None) is not None,
        )
        for v in asset.entity_variants:
            log.info(
                "  variant entity=%r tags=%s hidden=%s",
                v.entity_model_path, v.tags, v.hidden_in_manual_cycle,
            )

        # Per-colour obstacle items redirect their materials through the
        # EntityModel's MaterialModifier (ItemObstacle -> ItemObstacleLevel2).
        from . import _gbxmat_adapter as _gma
        _gma.set_active_modifier_folders(resolver, (getattr(asset, "material_modifier_path", "") or "",))
        try:
            _progress.update(0.35, "Building item")
            return self._build_item(context, asset, resolver)
        finally:
            _gma.clear_active_modifier_folders(resolver)

    def _build_item(self, context, asset, resolver, progress=None):
        from ._item_geometry import build_item_collection
        from .map_import_v4.bridge import _ID_COLLECTIONS, _snapshot
        before = _snapshot()
        try:
            collection = build_item_collection(
                asset, context, self.lod_level, resolver=resolver, source_path=self.filepath,
                progress=progress if progress is not None else _progress.Progress(0.35, 0.95),
            )
        except Exception as exc:
            bpy.data.batch_remove([block for name in _ID_COLLECTIONS for block in getattr(bpy.data, name)
                                   if block.as_pointer() not in before])
            log.exception("Failed to build item definition")
            self.report({"ERROR"}, f"Failed to build item: {exc}")
            return {"CANCELLED"}
        for obj in context.selected_objects:
            obj.select_set(False)
        meshes = [obj for obj in collection.all_objects if obj.type == "MESH"]
        for obj in meshes:
            obj.select_set(True)
        if meshes:
            context.view_layer.objects.active = meshes[0]
        self.report({"INFO"}, f"{asset.name}: {len(meshes)} mesh parts in item collection")
        return {"FINISHED"}


class TMBRIDGE_OT_ImportPrefab(bpy.types.Operator, ImportHelper):
    """Import a Trackmania .Prefab.Gbx (CPlugPrefab) via GBXBPY."""

    bl_idname = "trackmania_blender_bridge.import_prefab"
    bl_label = "Trackmania Prefab (.Prefab.Gbx)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Gbx"
    filter_glob: StringProperty(
        default="*.Prefab.Gbx;*.prefab.gbx",
        options={"HIDDEN"},
        maxlen=255,
    )

    lod_level: bpy.props.IntProperty(
        name="LOD Level",
        description="Which LOD level to import for embedded meshes (1 = highest detail)",
        default=1,
        min=1,
    )

    def _build_prefab_object(
        self,
        prefab,
        context,
        lod_level: int,
        target_collection=None,
        mesh_cache: dict | None = None,
        subprefab_cache: dict | None = None,
        resolver=None,
        progress=None,
    ):
        """Recursively materialize a Prefab as an Empty + child entities.

        Each entity becomes either a transformed mesh object (embedded
        mesh), a recursive prefab Empty (nested CPlugPrefab), or an
        unresolved-reference Empty carrying the GameData path as a
        custom property.

        Performance optimizations:

        * ``mesh_cache`` maps ``id(entity.mesh)`` to a ``bpy.data.meshes``
          datablock so repeat references share geometry instead of
          duplicating it. **MUST be scoped to a single prefab tree** \u2014
          ``id()`` values are recycled by Python after garbage
          collection, so reusing this dict across different
          ``GBXBPY.load`` calls causes one block's cached mesh to be
          handed to an unrelated entity in another block.
        * ``subprefab_cache`` maps ``id(entity.sub_prefab)`` to a
          ``bpy.types.Collection`` containing the first-built copy of
          that sub-prefab. Same scoping rule as ``mesh_cache``.
        """
        return _build_prefab_object_impl(
            prefab,
            context,
            lod_level,
            target_collection=target_collection,
            mesh_cache=mesh_cache,
            subprefab_cache=subprefab_cache,
            report=lambda lvl, msg: self.report({lvl}, msg),
            resolver=resolver,
            source_path=getattr(self, "filepath", "") or "",
            progress=progress,
        )

    @_progress.tracked("Importing Prefab")
    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, f"File not found: {self.filepath}")
            return {"CANCELLED"}

        gamedata_dir = _get_gamedata_dir(context)
        try:
            import GBXBPY
            from GBXBPY.errors import GbxbpyError
        except ImportError as e:
            log.exception("Failed to import GBXBPY: %s", e)
            self.report({"ERROR"}, f"GBXBPY package not importable: {e}")
            return {"CANCELLED"}

        resolver = GBXBPY.Resolver(gamedata_dir) if gamedata_dir else None

        log.info("Importing prefab: %s", self.filepath)
        try:
            asset = GBXBPY.load(self.filepath, resolver=resolver)
        except GbxbpyError as e:
            log.error("GBXBPY load failed: %s", e)
            self.report({"ERROR"}, f"GBXBPY load failed: {e}")
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error during prefab import")
            self.report({"ERROR"}, f"Unexpected error: {e}")
            return {"CANCELLED"}

        if not hasattr(asset, "entities") or not hasattr(asset, "prefab_version"):
            self.report(
                {"ERROR"},
                f"Loaded asset is not a Prefab (got {type(asset).__name__})",
            )
            return {"CANCELLED"}

        log.info(
            "Loaded prefab '%s' v%d entities=%d",
            asset.name, asset.prefab_version, len(asset.entities),
        )

        root = self._build_prefab_object(
            asset, context, self.lod_level, resolver=resolver,
            progress=_progress.Progress(0.35, 0.95),
        )
        context.view_layer.objects.active = root
        for o in context.view_layer.objects:
            o.select_set(False)
        root.select_set(True)

        embedded = sum(1 for e in asset.entities if e.mesh is not None)
        nested = sum(1 for e in asset.entities if e.sub_prefab is not None)
        external = sum(
            1 for e in asset.entities
            if e.mesh is None and e.sub_prefab is None and e.model_path
        )
        parametric = len(asset.entities) - embedded - nested - external
        self.report(
            {"INFO"},
            f"{asset.name}: {len(asset.entities)} entities "
            f"({embedded} mesh, {nested} nested, {external} ref, {parametric} parametric)",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  ED* (Block / Clip) import
# ---------------------------------------------------------------------------

# Block-unit size in metres (Stadium grid: 32 m XZ, 8 m vertical).
_TM_UNIT_X = 32.0
_TM_UNIT_Y = 8.0
_TM_UNIT_Z = 32.0

# Cardinal direction → yaw (radians, around Blender Z).
# TM "North" = +Z (no rotation); each step is a clockwise 90° turn in
# game-space, which is a -90° rotation about Blender's Z axis.
_DIRECTION_TO_YAW = {
    "North": 0.0,
    "East": -math.pi * 0.5,
    "South": -math.pi,
    "West": -math.pi * 1.5,
}


# Per-face placement matrices for clips referenced from a block-unit cell.
# A clip mesh is authored facing the parent block (Y-). For each face the
# matrix rotates the clip and translates it from the cell's corner-origin
# into the adjacent cell. Values mirror the legacy ``_FACE_MATRIX`` table
# (Legacy/gbxbpy/blender/gamedata/loader.py).
def _face_clip_matrix(face: str):
    """Return the per-face clip placement matrix in cell-local Blender space."""
    from mathutils import Matrix as _Matrix
    if face == "west":
        m = _Matrix.Rotation(math.pi * 0.5, 4, "Z")
        m[0][3], m[1][3], m[2][3] = -_TM_UNIT_X, -_TM_UNIT_Z, 0.0
        return m
    if face == "south":
        m = _Matrix.Identity(4)
        m[0][3], m[1][3], m[2][3] = 0.0, _TM_UNIT_Z, 0.0
        return m
    if face == "east":
        m = _Matrix.Rotation(-math.pi * 0.5, 4, "Z")
        m[0][3], m[1][3], m[2][3] = 2.0 * _TM_UNIT_X, 0.0, 0.0
        return m
    if face == "north":
        m = _Matrix.Rotation(math.pi, 4, "Z")
        m[0][3], m[1][3], m[2][3] = _TM_UNIT_X, -2.0 * _TM_UNIT_Z, 0.0
        return m
    if face == "top":
        m = _Matrix.Identity(4)
        m[0][3], m[1][3], m[2][3] = 0.0, 0.0, _TM_UNIT_Y
        return m
    if face == "bottom":
        m = _Matrix.Identity(4)
        m[0][3], m[1][3], m[2][3] = 0.0, 0.0, -_TM_UNIT_Y
        return m
    return _Matrix.Identity(4)


def _direction_corner_offset(direction: str, size_x: int, size_z: int) -> tuple[float, float]:
    """Compensate for the corner-origin shift after CW yaw rotation.

    Block meshes have their origin at the cell corner (min X, min Z in
    GBX). Rotating around (0, 0, 0) moves the visible footprint off the
    intended cell range, so we add a translation that puts the rotated
    footprint back on the original cells.

    Returned offset is in Blender XY (Z-up world).
    """
    sx = size_x * _TM_UNIT_X
    sz = size_z * _TM_UNIT_Z   # GBX-Z magnitude; in Blender this maps to -Y
    if direction == "East":
        return (sz, 0.0)
    if direction == "South":
        return (sx, -sz)
    if direction == "West":
        return (0.0, -sx)
    return (0.0, 0.0)


# Cell-occupancy helpers used by the placement-time clip-fallback culler.
# The block's "direction" rotates the block-local unit grid around the
# anchor before placing it on the map. The four formulas below derive
# the world TM cell of a unit by tracing the cell *centre* through the
# Blender placement transform (rotation around Z + corner-offset shift)
# and back into integer TM coords. Verified for direction-independent
# 1\u00d71 placement (cell does not change) and 2\u00d71 East (extends along
# +Z), matching the existing visible block layout.
def _unit_world_cell(
    b_coords: tuple[int, int, int],
    direction: str,
    unit_offset: tuple[int, int, int],
    footprint: tuple[int, int, int],
) -> tuple[int, int, int]:
    cx, cy, cz = b_coords
    ux, uy, uz = unit_offset
    sx, _sy, sz = footprint
    if direction == "East":
        return (cx + sz - uz - 1, cy + uy, cz + ux)
    if direction == "South":
        return (cx + sx - ux - 1, cy + uy, cz + sz - uz - 1)
    if direction == "West":
        return (cx + uz, cy + uy, cz + sx - ux - 1)
    return (cx + ux, cy + uy, cz + uz)


# Block-local face name -> world face name. Each direction step beyond
# "North" rotates faces one position counter-clockwise (north -> west ->
# south -> east -> north), matching the empirical behaviour of the
# placement transform's yaw=-pi/2 rotation per step.
_FACE_ROTATE_CCW = {
    "north": "west",
    "west": "south",
    "south": "east",
    "east": "north",
    "top": "top",
    "bottom": "bottom",
}

_DIRECTION_STEPS = {"North": 0, "East": 1, "South": 2, "West": 3}


def _rotate_face_to_world(face: str, direction: str) -> str:
    steps = _DIRECTION_STEPS.get(direction, 0)
    for _ in range(steps):
        face = _FACE_ROTATE_CCW.get(face, face)
    return face


# World TM cell delta to step from a unit cell into its neighbour for
# each cardinal world face.
_WORLD_FACE_DELTA_TM = {
    "north":  (0, 0, +1),
    "south":  (0, 0, -1),
    "east":   (+1, 0, 0),
    "west":   (-1, 0, 0),
    "top":    (0, +1, 0),
    "bottom": (0, -1, 0),
}


def _variant_has_mobil_geometry(variant) -> bool:
    """True if any mobil in any group carries a solid or prefab path."""
    for group in (getattr(variant, "mobils", None) or []):
        for m in group:
            if m.solid_path or m.prefab_path:
                return True
    return False


def _variant_footprint(variant) -> tuple[int, int, int]:
    """Return ``(size_x, size_y, size_z)`` cells covered by a BlockVariant."""
    units = getattr(variant, "units", None) or []
    if not units:
        return (1, 1, 1)
    mx = my = mz = 0
    for u in units:
        ox, oy, oz = u.relative_offset
        if ox > mx: mx = ox
        if oy > my: my = oy
        if oz > mz: mz = oz
    return (mx + 1, my + 1, mz + 1)


_LIBRARY_COLLECTION_NAME = "__TM_Library"


# ---------------------------------------------------------------------------
# Light placement
# ---------------------------------------------------------------------------
#
# TM meshes (CPlugSolid2Model) carry a ``lights`` array \u2014 each entry is a
# noderef whose name typically ends in ``.Light.Gbx`` plus an Iso4
# placement matrix. Parameters still use legacy defaults, with verified
# source-type overrides where a point light must not become a spotlight.
#
# Per-name Light datablocks are shared across the whole .blend
# (``bpy.data.lights`` is keyed by name), so 5000 placements of
# "MyLamp.Light.Gbx" share one Light datablock and only spawn 5000 cheap
# Light Objects.

# Default light parameters (mirrors Legacy ``GbxLight``).
_TM_LIGHT_DEFAULTS = {
    "energy": 1000.0,
    "exposure": 2.0,
    "color": (1.0, 1.0, 1.0),
    "shadow_soft_size": 1.0,
    "cutoff_distance": 20.0,
    "spot_size": 1.9199,   # ~110\u00b0 beam
    "spot_blend": 0.8,
}

_TM_LIGHT_TYPES = {
    # SpeedometerCP.Light.Gbx embeds CGxLightPoint (0x04002000), not
    # CGxLightSpot (0x0400B000). Ring prefabs use numbered references too.
    "speedometercp": "POINT",
}


def _tm_iso4_to_blender_matrix(t):
    """Convert a TM ``Iso4`` (3\u00d74 row-major) to a Blender 4\u00d74 ``Matrix``.

    The Iso4 stores a row-major 3\u00d73 rotation followed by a translation::

        [ XX XY XZ ]   row 0 \u2192 image of TM local +X in TM world
        [ YX YY YZ ]   row 1 \u2192 image of TM local +Y in TM world
        [ ZX ZY ZZ ]   row 2 \u2192 image of TM local +Z in TM world
        [ TX TY TZ ]   translation in TM world

    Wait \u2014 NICE/blendermania interprets the entries directly as a row-major
    matrix ``R[i,j]`` (no transpose), so::

        R_tm =  [[XX, XY, XZ],
                 [YX, YY, YZ],
                 [ZX, ZY, ZZ]]

    and the *columns* of ``R_tm`` are the local axes expressed in world
    coords. TM is right-handed Y-up; Blender is right-handed Z-up. The TM
    \u2192 Blender swizzle is ``S(x, y, z) = (x, -z, y)`` (matches\n    :func:`gbx_to_blender_vec3`). The basis-changed rotation is::

        R_blender = S \u00b7 R_tm \u00b7 S\u207b\u00b9

    The same swizzle is applied to the translation column.

    Finally TM spot lights point along their local **-Z**. After the basis
    change that direction lands along Blender local **+Y**, but Blender
    spot lights shine along local **-Z**. We therefore post-multiply by a
    fixed -90\u00b0 rotation about local X (mapping local -Z onto local +Y),
    so the spot ends up pointing the right way regardless of the input
    orientation.
    """
    from mathutils import Matrix
    if not t or len(t) < 12:
        return Matrix.Identity(4)
    xx, xy, xz, yx, yy, yz, zx, zy, zz, tx, ty, tz = t[:12]

    # R_tm interpreted as a row-major rotation matrix (NICE convention).
    r_tm = Matrix((
        (xx, xy, xz),
        (yx, yy, yz),
        (zx, zy, zz),
    ))
    # Swizzle S: (x, y, z) -> (x, -z, y).
    s = Matrix((
        (1.0,  0.0,  0.0),
        (0.0,  0.0, -1.0),
        (0.0,  1.0,  0.0),
    ))
    s_inv = s.transposed()
    r_blender = s @ r_tm @ s_inv

    # Spot pre-rotation: -90\u00b0 about local X. Mathematically this rotates
    # local +Y to local -Z and local -Z to local +Y, so multiplying on the
    # right makes Blender's local -Z point where TM's local -Z (now at
    # Blender +Y) was \u2014 i.e. the spot beam direction.
    spot_fix = Matrix((
        (1.0, 0.0,  0.0),
        (0.0, 0.0,  1.0),
        (0.0, -1.0, 0.0),
    ))
    r_blender = r_blender @ spot_fix

    # Translation swizzle: (TX, TY, TZ) -> (TX, -TZ, TY).
    bx, by, bz = tx, -tz, ty
    return Matrix((
        (r_blender[0][0], r_blender[0][1], r_blender[0][2], bx),
        (r_blender[1][0], r_blender[1][1], r_blender[1][2], by),
        (r_blender[2][0], r_blender[2][1], r_blender[2][2], bz),
        (0.0,             0.0,             0.0,             1.0),
    ))


def _get_or_create_light_data(name: str) -> "bpy.types.Light":
    """Return a shared ``bpy.types.Light`` datablock for ``name``.

    The same datablock is returned across calls with the same name, so
    repeated placements of one ``.Light.Gbx`` reference share their
    Light data (color/energy/falloff are all identical anyway).
    """
    import re

    clean = name
    for suffix in (".Light.Gbx", ".light.gbx", ".Gbx", ".gbx"):
        if clean.endswith(suffix):
            clean = clean[: -len(suffix)]
            break
    clean = clean or "TM_Light"

    type_name = re.sub(r"\.light\.gbx\d*$", "", name.replace("\\", "/").rsplit("/", 1)[-1],
                       flags=re.IGNORECASE).lstrip("? ").lower()
    light_type = _TM_LIGHT_TYPES.get(type_name, "SPOT")
    light = bpy.data.lights.get(clean)
    if light is not None:
        if light.type != light_type:
            light.type = light_type
        return bpy.data.lights[clean]

    light = bpy.data.lights.new(name=clean, type=light_type)
    light.energy = _TM_LIGHT_DEFAULTS["energy"]
    light.color = _TM_LIGHT_DEFAULTS["color"]
    light.shadow_soft_size = _TM_LIGHT_DEFAULTS["shadow_soft_size"]
    if hasattr(light, "exposure"):
        light.exposure = _TM_LIGHT_DEFAULTS["exposure"]
    if hasattr(light, "use_custom_distance"):
        light.use_custom_distance = True
    if hasattr(light, "cutoff_distance"):
        light.cutoff_distance = _TM_LIGHT_DEFAULTS["cutoff_distance"]
    if light_type == "SPOT":
        light.spot_size = _TM_LIGHT_DEFAULTS["spot_size"]
        light.spot_blend = _TM_LIGHT_DEFAULTS["spot_blend"]
    return light


def _build_light_templates(gbx_mesh):
    """Return a list of ``(light_data, matrix_local)`` for a mesh's lights.

    ``light_data`` is a shared ``bpy.types.Light`` datablock; ``matrix_local``
    is the Blender-space placement relative to the parent mesh object.
    Supports named ``.Light.Gbx`` references and typed embedded lights.
    """
    templates: list = []
    for la in getattr(gbx_mesh, "lights", None) or []:
        ref = la.light_path or la.name
        if la.light_type:
            light = _get_or_create_light_data(ref)
            light.type = la.light_type
            light.color = la.color
            if la.intensity is not None:
                light.energy = _TM_LIGHT_DEFAULTS["energy"] * la.intensity
            if la.distance is not None:
                light.cutoff_distance = la.distance
            templates.append((light, _tm_iso4_to_blender_matrix(la.transform)))
            continue
        if not ref:
            continue
        # Only emit lights whose noderef looks like a real Light.Gbx \u2014
        # mirrors legacy filter and avoids spawning lights for unrelated
        # named noderefs (collision shapes, etc.).
        if ".light.gbx" not in ref.lower():
            continue
        templates.append((
            _get_or_create_light_data(ref),
            _tm_iso4_to_blender_matrix(la.transform),
        ))
    return templates


def _attach_light_instances(parent_obj, templates, link_coll) -> int:
    """Spawn one Light Object per template, parented under ``parent_obj``.

    Returns the number of lights spawned.
    """
    if not templates:
        return 0
    count = 0
    for light_data, mat in templates:
        light_obj = bpy.data.objects.new(light_data.name, light_data)
        link_coll.objects.link(light_obj)
        light_obj.parent = parent_obj
        light_obj.matrix_local = mat
        count += 1
    return count


def _get_or_create_library_collection(scene) -> "bpy.types.Collection":
    """Return the hidden scene-level collection that holds dedup'd block geometry.

    All variant geometry built during a map import lives under this
    collection (one child collection per unique block variant).
    Map-scene placements are Empties with ``instance_type='COLLECTION'``
    pointing at those library collections, so each unique variant's
    mesh data exists exactly once in the .blend regardless of how many
    times the block appears on the map.

    The library is excluded from the active view layer so it does not
    clutter the outliner.
    """
    coll = bpy.data.collections.get(_LIBRARY_COLLECTION_NAME)
    if coll is None:
        coll = bpy.data.collections.new(_LIBRARY_COLLECTION_NAME)
    if _LIBRARY_COLLECTION_NAME not in scene.collection.children:
        try:
            scene.collection.children.link(coll)
        except RuntimeError:
            # Already linked under a different parent; fine.
            pass
    coll.hide_viewport = True
    coll.hide_render = True
    # Exclude from the current view layer so it doesn't render.
    try:
        layer_coll = scene.view_layers[0].layer_collection.children.get(
            _LIBRARY_COLLECTION_NAME
        )
        if layer_coll is not None:
            layer_coll.exclude = True
    except (AttributeError, IndexError):
        pass
    return coll


def _build_mobil_object(
    mobil,
    *,
    label: str,
    context,
    lod_level: int,
    resolver,
    source_path: str,
    mesh_cache: dict,
    subprefab_cache: dict,
    report,
    progress=None,
) -> "tuple[bpy.types.Object | None, list]":
    """Materialize a single ``BlockMobil`` into a Blender object.

    Tries ``solid_path`` (CPlugSolid2Model) first, then ``prefab_path``
    (CPlugPrefab) as a fallback. Returns ``(None, [])`` when the mobil
    has neither path or both fail to load. The caller is responsible
    for parenting, linking, applying ``geom_transform``, and handling
    light templates.
    """
    import GBXBPY  # local: build env may differ from top-level import
    from GBXBPY.errors import GbxbpyError

    child = None
    light_templates: list = []

    if mobil.solid_path and resolver is not None:
        abs_path = resolver.resolve(mobil.solid_path, source_path=source_path)
        if abs_path:
            cache_key = (abs_path, int(lod_level))
            cached_entry = mesh_cache.get(cache_key)
            if cached_entry is not None:
                cached_mesh, light_templates = cached_entry
                child = bpy.data.objects.new(label, cached_mesh)
            else:
                try:
                    mesh_asset = GBXBPY.load(abs_path, resolver=resolver)
                    if progress is not None:
                        progress(0.2, "Building mobil mesh")
                    child = _build_blender_mesh_from_gbxbpy(
                        mesh_asset, lod_level,
                        resolver=resolver, source_path=abs_path,
                        progress=progress.sub(0.2, 1.0) if progress is not None else None,
                    )
                    light_templates = _build_light_templates(mesh_asset)
                    mesh_cache[cache_key] = (child.data, light_templates)
                    child.name = label
                except (GbxbpyError, Exception) as e:  # noqa: BLE001
                    log.warning("Mobil solid %r failed: %s", mobil.solid_path, e)

    if child is None and mobil.prefab_path and resolver is not None:
        abs_path = resolver.resolve(mobil.prefab_path, source_path=source_path)
        if abs_path:
            sub_key = (abs_path, int(lod_level))
            cached_coll = subprefab_cache.get(sub_key)
            if cached_coll is None:
                try:
                    prefab_asset = GBXBPY.load(abs_path, resolver=resolver)
                    if hasattr(prefab_asset, "entities"):
                        cached_coll = bpy.data.collections.new(
                            prefab_asset.name or label
                        )
                        # IMPORTANT: do NOT forward the map-wide
                        # ``mesh_cache`` / ``subprefab_cache`` here.
                        # Those dicts use ``id(entity.mesh)`` /
                        # ``id(entity.sub_prefab)`` as keys inside
                        # ``_build_prefab_object_impl``. Python recycles
                        # ``id()`` values once a parsed object is
                        # garbage-collected, so sharing the dict across
                        # different ``GBXBPY.load`` calls causes block A's
                        # cached collection to be reused by block B \u2014
                        # producing geometry-in-the-wrong-place /
                        # wrong-angle bugs. The internal caches are
                        # only safe for sharing within a single prefab
                        # tree, so let them be per-tree (the function
                        # creates fresh dicts when ``None`` is passed).
                        _build_prefab_object_impl(
                            prefab_asset,
                            context,
                            lod_level,
                            target_collection=cached_coll,
                            report=report,
                            resolver=resolver,
                            source_path=abs_path,
                            progress=progress.sub(0.2, 1.0) if progress is not None else None,
                        )
                        subprefab_cache[sub_key] = cached_coll
                except (GbxbpyError, Exception) as e:  # noqa: BLE001
                    log.warning("Mobil prefab %r failed: %s", mobil.prefab_path, e)
            if cached_coll is not None:
                child = bpy.data.objects.new(label, None)
                child.instance_type = "COLLECTION"
                child.instance_collection = cached_coll

    return child, light_templates


def _build_unit_clip_meshes(
    variant,
    *,
    parent,
    link_coll,
    context,
    lod_level: int,
    resolver,
    source_path: str,
    mesh_cache: dict,
    subprefab_cache: dict,
    report,
    should_render_face=None,
) -> int:
    """Materialize clip meshes referenced from each block-unit's faces.

    ``should_render_face`` is an optional callable
    ``(unit_offset, face_name) -> bool`` that returns ``False`` to cull
    a face (e.g. when an adjacent placement provides the matching seam).
    When ``None``, every face that survives the internal-seam check is
    rendered \u2014 the right behaviour for standalone block import where
    no neighbor information is available.

    For blocks whose ``mobils`` array is empty (Stand*, DecoCliff*,
    DecoWall*, ...), the visible geometry lives in clip files referenced
    from the unit-cell faces. Each clip is parsed as a ``Clip`` carrying
    its own ``BlockVariant`` (ground/air); we walk that variant's mobils
    and place them in the parent block's local space using the unit's
    grid offset and a per-face placement matrix.

    Returns the number of clip-mobil children built.
    """
    import GBXBPY  # local: build env may differ from top-level import
    from GBXBPY.errors import GbxbpyError
    try:
        from mathutils import Matrix as _Matrix
    except Exception:  # noqa: BLE001
        return 0

    if resolver is None:
        return 0

    clip_asset_cache: dict = {}
    built = 0

    # Per-face neighbor cell delta in TM (ox, oy, oz) coords. A clip on
    # face F of unit U "belongs" to the seam between U and the cell at
    # U.offset + delta(F). When that neighbor cell is occupied by another
    # unit of THIS SAME variant, the seam is internal to the block and
    # both sides reference the same clip — we skip the face to avoid
    # rendering the clip twice.
    face_neighbor_delta = {
        "west":   (-1, 0,  0),
        "east":   (+1, 0,  0),
        "south":  ( 0, 0, -1),
        "north":  ( 0, 0, +1),
        "top":    ( 0, +1, 0),
        "bottom": ( 0, -1, 0),
    }

    occupied_cells = {tuple(u.relative_offset) for u in variant.units}

    face_lists = (
        ("north", "clips_north"),
        ("east", "clips_east"),
        ("south", "clips_south"),
        ("west", "clips_west"),
        ("top", "clips_top"),
        ("bottom", "clips_bottom"),
    )

    for ui, unit in enumerate(variant.units):
        ox, oy, oz = unit.relative_offset
        cell_x = ox * _TM_UNIT_X
        cell_y = -(oz * _TM_UNIT_Z)
        cell_z = oy * _TM_UNIT_Y

        for face_name, attr in face_lists:
            paths = getattr(unit, attr, None) or []
            if not paths:
                continue

            # Skip internal seams of multi-cell variants.
            dx, dy, dz = face_neighbor_delta[face_name]
            if (ox + dx, oy + dy, oz + dz) in occupied_cells:
                continue

            # Caller-supplied culling (e.g. neighbor block at placement
            # time provides the matching seam clip).
            if should_render_face is not None and not should_render_face(
                unit.relative_offset, face_name
            ):
                continue

            face_mat = _face_clip_matrix(face_name)

            # ``paths`` is a list of *alternates* used by the engine to
            # pick a matching neighbor at runtime. Rendering all of them
            # produces stacked overlapping geometry, so take only the
            # first non-empty entry.
            clip_ref = next((p for p in paths if p), None)
            if clip_ref:
                ci = 0
                abs_path = resolver.resolve(clip_ref, source_path=source_path)
                if not abs_path:
                    log.warning(
                        "Unit clip %r (face=%s) could not be resolved",
                        clip_ref, face_name,
                    )
                    continue

                clip_asset = clip_asset_cache.get(abs_path)
                if clip_asset is None:
                    try:
                        clip_asset = GBXBPY.load(abs_path, resolver=resolver)
                    except (GbxbpyError, Exception) as e:  # noqa: BLE001
                        log.warning("Clip %r load failed: %s", abs_path, e)
                        continue
                    clip_asset_cache[abs_path] = clip_asset

                # The Clip carries one or two BlockVariants (ground/air).
                # Use whichever is populated; prefer ground.
                clip_variant = getattr(clip_asset, "variant_ground", None) \
                    or getattr(clip_asset, "variant_air", None)
                if clip_variant is None:
                    continue

                # Walk the clip variant's mobils. Each clip-mobil group is
                # an LOD list \u2014 take the first.
                for cgi, cgroup in enumerate(clip_variant.mobils or []):
                    if not cgroup:
                        continue
                    cprimary = cgroup[0]
                    if not (cprimary.solid_path or cprimary.prefab_path):
                        continue

                    label = (
                        f"unit{ui:02d}_clip_{face_name}_{ci:02d}_m{cgi:02d}"
                    )
                    child, light_templates = _build_mobil_object(
                        cprimary,
                        label=label,
                        context=context,
                        lod_level=lod_level,
                        resolver=resolver,
                        source_path=abs_path,
                        mesh_cache=mesh_cache,
                        subprefab_cache=subprefab_cache,
                        report=report,
                    )
                    if child is None:
                        continue

                    # Compose: cell origin + face matrix + clip-mobil
                    # geom_transform (as local matrix). The face matrix is
                    # already expressed in cell-local Blender space.
                    gt = getattr(cprimary, "geom_transform", None)
                    if gt:
                        tx, ty, tz, yaw, pitch, roll = gt
                        loc = _Matrix.Translation((tx, -tz, ty))
                        rot = (
                            _Matrix.Rotation(pitch, 4, "X")
                            @ _Matrix.Rotation(-roll, 4, "Y")
                            @ _Matrix.Rotation(yaw, 4, "Z")
                        )
                        local_mat = loc @ rot
                    else:
                        local_mat = _Matrix.Identity(4)

                    cell_mat = _Matrix.Translation((cell_x, cell_y, cell_z))
                    child.matrix_local = cell_mat @ face_mat @ local_mat

                    child["tm_clip_source"] = clip_ref
                    child["tm_clip_face"] = face_name
                    child["tm_clip_unit"] = ui

                    child.parent = parent
                    if child.name not in link_coll.objects:
                        try:
                            link_coll.objects.link(child)
                        except RuntimeError:
                            pass

                    if light_templates:
                        _attach_light_instances(child, light_templates, link_coll)

                    built += 1

    return built


def _build_block_variant(
    variant,
    *,
    label: str,
    parent,
    context,
    lod_level: int,
    resolver,
    source_path: str,
    mesh_cache: dict,
    subprefab_cache: dict,
    report,
    target_collection=None,
    mobil_index: int = -1,
    include_clip_fallback: bool = True,
    progress=None,
):
    """Materialize a ``BlockVariant`` under ``parent``.

    Only the mobil group selected by ``mobil_index`` is built. ``-1``
    (or any out-of-range value) falls back to the first non-empty group
    \u2014 mirroring the legacy importer's behaviour. This is critical:
    each map placement only displays one specific mobil group at a
    time; building all of them stacks every gameplay/visual variant on
    top of each other.

    Each mobil in the chosen group becomes a child object: an instanced
    mesh (resolved ``solid_path``), an instanced sub-prefab (resolved
    ``prefab_path``), or a placeholder Empty. The mobil's
    ``geom_transform`` (TM ``GbxPose3D``) is applied as the child's
    local transform, swizzled into Blender space.
    Units are emitted as small Empty markers positioned on the Stadium
    grid so the user can see the block's footprint.
    """
    import GBXBPY  # local: build env may differ from top-level import
    from GBXBPY.errors import GbxbpyError

    var_empty = bpy.data.objects.new(label, None)
    var_empty.empty_display_type = "PLAIN_AXES"
    var_empty.empty_display_size = 0.5
    if parent is not None:
        var_empty.parent = parent

    # Variant identification
    var_empty["tm_var_kind"] = getattr(variant, "kind", "") or ""
    var_empty["tm_var_index"] = int(getattr(variant, "index", 0) or 0)
    var_empty["tm_var_name"] = getattr(variant, "name", "") or ""
    var_empty["tm_var_cardinal_dir"] = getattr(variant, "cardinal_dir", "") or ""
    var_empty["tm_var_multi_dir"] = getattr(variant, "multi_dir", "") or ""
    var_empty["tm_var_base_type"] = getattr(variant, "variant_base_type", "") or ""
    var_empty["tm_var_symmetric_idx"] = int(getattr(variant, "symmetrical_variant_index", -1) or -1)
    var_empty["tm_var_no_pillar_below"] = int(getattr(variant, "no_pillar_below_index", -1) or -1)

    # Manual symmetry flags
    var_empty["tm_var_sym_h"] = bool(getattr(variant, "has_manual_symmetry_h", False))
    var_empty["tm_var_sym_v"] = bool(getattr(variant, "has_manual_symmetry_v", False))
    var_empty["tm_var_sym_d1"] = bool(getattr(variant, "has_manual_symmetry_d1", False))
    var_empty["tm_var_sym_d2"] = bool(getattr(variant, "has_manual_symmetry_d2", False))

    # Waypoint / gameplay sub-models — only set when non-empty so they
    # don't clutter the property panel.
    for prop_name, attr in (
        ("tm_var_gate", "gate_path"),
        ("tm_var_teleporter", "teleporter_path"),
        ("tm_var_turbine", "turbine_path"),
        ("tm_var_flock", "flock_model_path"),
        ("tm_var_spawn_model", "spawn_model_path"),
        ("tm_var_probe", "probe_path"),
        ("tm_var_compound", "compound_model_path"),
        ("tm_var_screen_trigger", "screen_interaction_trigger_solid"),
        ("tm_var_wp_solid", "waypoint_trigger_solid"),
        ("tm_var_wp_shape", "waypoint_trigger_shape"),
    ):
        val = getattr(variant, attr, "") or ""
        if val:
            var_empty[prop_name] = val
    if getattr(variant, "entity_spawner_paths", None):
        var_empty["tm_var_entity_spawners"] = list(variant.entity_spawner_paths)
    if getattr(variant, "placed_pillar_paths", None):
        var_empty["tm_var_placed_pillars"] = list(variant.placed_pillar_paths)
    if getattr(variant, "replaced_pillar_paths", None):
        var_empty["tm_var_replaced_pillars"] = list(variant.replaced_pillar_paths)
    if getattr(variant, "water_volumes", None):
        var_empty["tm_var_water_volumes"] = len(variant.water_volumes)

    # Auto-terrain (ground variants only, but harmless on air)
    var_empty["tm_block_zone_ids"] = list(variant.auto_terrain_zone_ids)
    if getattr(variant, "auto_terrain_height_offset", 0):
        var_empty["tm_var_auto_terrain_height_offset"] = int(variant.auto_terrain_height_offset)
    if getattr(variant, "auto_terrain_place_type", "") or "":
        var_empty["tm_var_auto_terrain_place_type"] = variant.auto_terrain_place_type

    link_coll = target_collection if target_collection is not None else context.collection
    link_coll.objects.link(var_empty)

    # Mobils: outer = mobil group (gameplay state), inner = sub-state.
    # Select ONE group (the placement's mobil_index) instead of building
    # every group on top of itself. Falls back to first non-empty group
    # when the requested index is invalid or empty.
    groups = list(variant.mobils or [])

    def _is_nonempty(group) -> bool:
        return bool(group) and any(
            (m.solid_path or m.prefab_path) for m in group
        )

    sel_idx: int = -1
    if 0 <= int(mobil_index) < len(groups) and _is_nonempty(groups[int(mobil_index)]):
        sel_idx = int(mobil_index)
    else:
        for i, g in enumerate(groups):
            if _is_nonempty(g):
                sel_idx = i
                break

    selected_groups: list[tuple[int, list]] = (
        [(sel_idx, groups[sel_idx])] if sel_idx >= 0 else []
    )

    mobil_built_geometry = False
    for gi, group in selected_groups:
        if not group:
            continue

        # Outer index ``gi`` selects the LOD/state group; every sub-mobil
        # within the group is a parallel piece of the visual and must be
        # built. Building only ``group[0]`` would lose the secondary
        # geometry on blocks like ``DecoCliff10Straight`` (3 sub-mobils),
        # ``DecoCliff10CornerOut`` (2), ``StandStraight`` (2), etc.
        for mi, mobil in enumerate(group):
            mobil_progress = None
            if progress is not None:
                progress(0.9 * mi / len(group), f"Building mobils {mi + 1}/{len(group)}")
                mobil_progress = progress.sub(0.9 * mi / len(group), 0.9 * (mi + 1) / len(group))
            if not (mobil.solid_path or mobil.prefab_path):
                continue

            mobil_label = (
                f"mobil{gi:02d}" if len(group) == 1
                else f"mobil{gi:02d}_{mi:02d}"
            )

            child, light_templates = _build_mobil_object(
                mobil,
                label=mobil_label,
                context=context,
                lod_level=lod_level,
                resolver=resolver,
                source_path=source_path,
                mesh_cache=mesh_cache,
                subprefab_cache=subprefab_cache,
                report=report,
                progress=mobil_progress,
            )
            if child is not None:
                mobil_built_geometry = True
            else:
                child = bpy.data.objects.new(mobil_label, None)
                child.empty_display_type = "CUBE"
                child.empty_display_size = 0.5

            child["tm_mobil_solid"] = mobil.solid_path
            child["tm_mobil_prefab"] = mobil.prefab_path
            child["tm_mobil_kind"] = int(mobil.kind or 0)
            child["tm_mobil_group"] = int(gi)
            child["tm_mobil_sub_index"] = int(mi)

            # Apply mobil geom_transform (TM GbxPose3D -> Blender local).
            # TM coords (X right, Y up, Z forward) -> Blender (X, -Z, Y).
            # TM Eulers: yaw=Y, pitch=X, roll=Z -> Blender Euler
            # (pitch, -roll, yaw).
            gt = getattr(mobil, "geom_transform", None)
            if gt:
                tx, ty, tz, yaw, pitch, roll = gt
                child.location = (tx, -tz, ty)
                if yaw != 0.0 or pitch != 0.0 or roll != 0.0:
                    child.rotation_mode = "XYZ"
                    child.rotation_euler = (pitch, -roll, yaw)
                child["tm_mobil_geom_transform"] = list(gt)

            child.parent = var_empty
            # Link if not already (mesh builder doesn't link).
            if child.name not in link_coll.objects:
                try:
                    link_coll.objects.link(child)
                except RuntimeError:
                    pass

            if light_templates:
                _attach_light_instances(child, light_templates, link_coll)

    # Block-unit clip fallback. Many blocks (Stand*, DecoCliff*, DecoWall*,
    # ...) carry their visible geometry not in mobils but in clip files
    # referenced from each block-unit's face lists. When the mobil pass
    # produced no geometry, materialize those clip meshes so the block is
    # not rendered as a bare empty.
    #
    # ``include_clip_fallback`` is set to ``False`` by the map-import
    # library cache so the shared collection contains no baked-in clips
    # (preventing seam-doubling between adjacent placements). The map
    # placement loop then builds neighbor-aware per-placement clip
    # children on top of each instance.
    if include_clip_fallback and not mobil_built_geometry \
            and getattr(variant, "units", None):
        _build_unit_clip_meshes(
            variant,
            parent=var_empty,
            link_coll=link_coll,
            context=context,
            lod_level=lod_level,
            resolver=resolver,
            source_path=source_path,
            mesh_cache=mesh_cache,
            subprefab_cache=subprefab_cache,
            report=report,
        )

    # Units: small Empty per occupied cell, positioned on the Stadium grid.
    for ui, unit in enumerate(variant.units):
        ox, oy, oz = unit.relative_offset
        u_empty = bpy.data.objects.new(f"unit{ui:02d}", None)
        u_empty.empty_display_type = "CUBE"
        u_empty.empty_display_size = 0.25
        # Cell-corner origin in Blender world (Z-up):
        # GBX (gx, gy, gz) -> Blender (gx, -gz, gy).
        u_empty.location = (
            ox * _TM_UNIT_X,
            -(oz * _TM_UNIT_Z),
            oy * _TM_UNIT_Y,
        )
        u_empty["tm_unit_offset"] = list(unit.relative_offset)
        u_empty["tm_unit_clips_N"] = list(unit.clips_north)
        u_empty["tm_unit_clips_E"] = list(unit.clips_east)
        u_empty["tm_unit_clips_S"] = list(unit.clips_south)
        u_empty["tm_unit_clips_W"] = list(unit.clips_west)
        u_empty["tm_unit_clips_T"] = list(unit.clips_top)
        u_empty["tm_unit_clips_B"] = list(unit.clips_bottom)
        u_empty.parent = var_empty
        link_coll.objects.link(u_empty)

    return var_empty


class TMBRIDGE_OT_ImportED(bpy.types.Operator, ImportHelper):
    """Import a Trackmania ED* file (block info or clip info) via GBXBPY."""

    bl_idname = "trackmania_blender_bridge.import_ed"
    bl_label = "Trackmania Block/Clip (.ED*.Gbx)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".Gbx"
    filter_glob: StringProperty(
        default=(
            "*.EDClassic.Gbx;*.EDFlat.Gbx;*.EDFrontier.Gbx;*.EDTransition.Gbx;"
            "*.EDClip.Gbx;*.EDHorizontalClip.Gbx;*.EDVerticalClip.Gbx;"
            "*.edclassic.gbx;*.edflat.gbx;*.edfrontier.gbx;*.edtransition.gbx;"
            "*.edclip.gbx;*.edhorizontalclip.gbx;*.edverticalclip.gbx"
        ),
        options={"HIDDEN"},
        maxlen=255,
    )

    lod_level: bpy.props.IntProperty(
        name="LOD Level",
        description="Which LOD level to import for resolved meshes (1 = highest detail)",
        default=1,
        min=1,
    )

    import_air: bpy.props.BoolProperty(
        name="Import Air Variants",
        description="Build the air variants in addition to the ground variants",
        default=True,
    )

    variant_kind: bpy.props.EnumProperty(
        name="Variant Kind",
        description="Limit import to one kind of variant",
        items=[
            ("ALL", "All", "Import every variant in the file"),
            ("GROUND", "Ground only", "Import only ground variants"),
            ("AIR", "Air only", "Import only air variants"),
        ],
        default="ALL",
    )

    variant_index: bpy.props.IntProperty(
        name="Variant Index",
        description=(
            "Index of the variant within its kind to import. "
            "Use -1 to import every variant of the selected kind. "
            "0 = base variant, 1..N = additional variants."
        ),
        default=-1,
        min=-1,
    )

    @_progress.tracked("Importing Block/Clip")
    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, f"File not found: {self.filepath}")
            return {"CANCELLED"}

        gamedata_dir = _get_gamedata_dir(context)
        try:
            import GBXBPY
            from GBXBPY.errors import GbxbpyError
        except ImportError as e:
            log.exception("Failed to import GBXBPY: %s", e)
            self.report({"ERROR"}, f"GBXBPY package not importable: {e}")
            return {"CANCELLED"}

        resolver = GBXBPY.Resolver(gamedata_dir) if gamedata_dir else None

        log.info("Importing ED* file: %s", self.filepath)
        try:
            asset = GBXBPY.load(self.filepath, resolver=resolver)
        except GbxbpyError as e:
            log.error("GBXBPY load failed: %s", e)
            self.report({"ERROR"}, f"GBXBPY load failed: {e}")
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            log.exception("Unexpected error during ED* import")
            self.report({"ERROR"}, f"Unexpected error: {e}")
            return {"CANCELLED"}

        is_block = hasattr(asset, "ground_variants")
        is_clip = hasattr(asset, "variant_ground")
        if not (is_block or is_clip):
            self.report(
                {"ERROR"},
                f"Loaded asset is not a Block or Clip (got {type(asset).__name__})",
            )
            return {"CANCELLED"}

        root = bpy.data.objects.new(asset.name or os.path.basename(self.filepath), None)
        root.empty_display_type = "ARROWS"
        root.empty_display_size = 1.0
        root["tm_ed_class"] = f"0x{(asset.class_id or 0):08X}"
        root["tm_ed_page"] = asset.page_path
        root["tm_ed_internal"] = asset.is_internal
        root["tm_ed_advanced"] = asset.is_advanced
        _asset_panel.stamp(root, "Block" if is_block else "Clip", self.filepath, asset.name or "")
        context.collection.objects.link(root)

        mesh_cache: dict = {}
        subprefab_cache: dict = {}
        report = lambda lvl, msg: self.report({lvl}, msg)

        built = 0
        skipped = 0
        variants = list(asset.all_variants())
        _progress.update(0.35, "Building variants")

        def _want(v) -> bool:
            """Return True iff this variant matches the user's selection."""
            if self.variant_kind == "GROUND" and v.kind != "ground":
                return False
            if self.variant_kind == "AIR" and v.kind != "air":
                return False
            if not self.import_air and v.kind == "air" and self.variant_kind == "ALL":
                return False
            if self.variant_index >= 0 and v.index != self.variant_index:
                return False
            return True

        if is_block:
            root["tm_ed_kind"] = "block"
            root["tm_ed_pillar"] = asset.is_pillar
            root["tm_ed_waypoint"] = asset.waypoint_type
            root["tm_ed_symmetric"] = asset.symmetrical_block_id
            root["tm_ed_total_ground"] = len(asset.ground_variants)
            root["tm_ed_total_air"] = len(asset.air_variants)
            # Material-modifier override: redirect material lookups to
            # the block's themed/skinned remap folder (e.g. DecoHill,
            # SpecialFXTurbo) before any mesh build happens.
            modifier_paths = tuple(
                p for p in (
                    getattr(asset, "material_modifier_path", "") or "",
                    getattr(asset, "material_modifier2_path", "") or "",
                ) if p
            )
            try:
                from . import _gbxmat_adapter as _gma
                folders = _gma.set_active_modifier_folders(resolver, modifier_paths)
                if folders:
                    root["tm_block_modifier_folders"] = list(folders)
                    log.info("Block %s: modifier folders %s", asset.name, folders)
            except Exception as e:  # noqa: BLE001
                log.debug("modifier-folder setup failed: %s", e)
            try:
                for vi, v in enumerate(variants):
                    _progress.update(0.35 + 0.6 * vi / max(1, len(variants)),
                                     f"Building variants {vi + 1}/{len(variants)}")
                    if not _want(v):
                        skipped += 1
                        continue
                    _build_block_variant(
                        v, label=f"{v.kind}{v.index:02d}", parent=root,
                        context=context, lod_level=self.lod_level,
                        resolver=resolver, source_path=self.filepath,
                        mesh_cache=mesh_cache, subprefab_cache=subprefab_cache,
                        report=report,
                        progress=_progress.Progress(
                            0.35 + 0.6 * vi / max(1, len(variants)),
                            0.35 + 0.6 * (vi + 1) / max(1, len(variants)),
                        ),
                    )
                    built += 1
            finally:
                try:
                    from . import _gbxmat_adapter as _gma
                    _gma.clear_active_modifier_folders(resolver)
                except Exception:  # noqa: BLE001
                    pass
        else:
            root["tm_ed_kind"] = "clip"
            root["tm_ed_clip_type"] = asset.clip_type
            root["tm_ed_clip_group"] = asset.clip_group_id
            root["tm_ed_symmetric_clip"] = asset.symmetrical_clip_id
            root["tm_ed_full_free"] = asset.is_full_free_clip
            for vi, v in enumerate(variants):
                _progress.update(0.35 + 0.6 * vi / max(1, len(variants)),
                                 f"Building variants {vi + 1}/{len(variants)}")
                if not _want(v):
                    skipped += 1
                    continue
                _build_block_variant(
                    v, label=v.kind, parent=root,
                    context=context, lod_level=self.lod_level,
                    resolver=resolver, source_path=self.filepath,
                    mesh_cache=mesh_cache, subprefab_cache=subprefab_cache,
                    report=report,
                    progress=_progress.Progress(
                        0.35 + 0.6 * vi / max(1, len(variants)),
                        0.35 + 0.6 * (vi + 1) / max(1, len(variants)),
                    ),
                )
                built += 1

        if built == 0:
            self.report(
                {"WARNING"},
                f"No variants matched the selection (kind={self.variant_kind} "
                f"index={self.variant_index}); {skipped} skipped",
            )

        context.view_layer.objects.active = root
        for o in context.view_layer.objects:
            o.select_set(False)
        root.select_set(True)

        kind = "Block" if is_block else "Clip"
        self.report(
            {"INFO"},
            f"{asset.name}: {kind} loaded ({built} variant(s))",
        )
        return {"FINISHED"}


class TMBRIDGE_OT_UpgradeFromBuild(bpy.types.Operator):
    """Find the newest build zip and install it as an addon upgrade."""

    bl_idname = "trackmania_blender_bridge.upgrade_from_build"
    bl_label = "Upgrade from Build Directory"
    bl_options = {"REGISTER"}

    def execute(self, context):
        addon = context.preferences.addons.get(ADDON_ID)
        if addon is None:
            self.report({"ERROR"}, "Addon preferences not available")
            return {"CANCELLED"}

        build_dir = bpy.path.abspath(addon.preferences.build_dir or "")
        if not build_dir or not os.path.isdir(build_dir):
            self.report({"ERROR"}, "Build directory is not set or does not exist")
            return {"CANCELLED"}

        zips = _build_archives(build_dir)
        if not zips:
            self.report({"ERROR"}, f"No {ADDON_SHORT_NAME} or legacy build ZIP found in {build_dir}")
            return {"CANCELLED"}

        newest = max(zips, key=os.path.getmtime)
        install_addon_zip(addon, newest)
        self.report({"INFO"}, f"Upgrading to {os.path.basename(newest)}...")
        return {"FINISHED"}


def install_addon_zip(addon, zip_path: str, cleanup=None) -> None:
    """Install ``zip_path`` over the running addon and re-enable it in place.

    ``cleanup`` is called after the install attempt, e.g. to delete a
    downloaded temporary ZIP.
    """
    pkg = ADDON_ID

    # Snapshot preferences BEFORE the upgrade. Re-registering the addon
    # resets AddonPreferences PropertyGroup defaults, so we must save and
    # restore the user's settings (game data dir, build dir, etc.).
    prefs_snapshot: dict = {}
    try:
        prefs_obj = addon.preferences
        for prop in prefs_obj.bl_rna.properties:
            if prop.identifier == "rna_type" or prop.is_readonly:
                continue
            try:
                prefs_snapshot[prop.identifier] = getattr(prefs_obj, prop.identifier)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        print(f"[trackmania_blender_bridge] failed to snapshot prefs: {e}")

    # IMPORTANT: do NOT call addon_disable here. The calling operator's class
    # is owned by `pkg`, so disabling now would unregister the very class
    # whose execute() is currently running -> Blender segfault.
    # Defer the entire disable -> purge -> install -> enable sequence so
    # the operator returns first and the C-side operator wrapper is no
    # longer on the call stack.
    def _deferred_upgrade():
        try:
            try:
                bpy.ops.preferences.addon_disable(module=pkg)
            except Exception:  # noqa: BLE001
                pass

            # Purge cached modules so new code is picked up on re-enable.
            # GBXMAT must go too: a stale registry/builder set paired with
            # a new adapter yields magenta fallback materials.
            prefixes = ("gbxbpy.", "nice.", "GBXBPY.", "NICE.", "GBXMAT.", pkg + ".")
            roots = {"gbxbpy", "nice", "GBXBPY", "NICE", "GBXMAT", pkg}
            for mod_name in [
                k for k in list(sys.modules)
                if k in roots or any(k.startswith(p) for p in prefixes)
            ]:
                del sys.modules[mod_name]

            try:
                bpy.ops.preferences.addon_install(
                    filepath=zip_path, overwrite=True
                )
            except Exception as e:  # noqa: BLE001
                print(f"[trackmania_blender_bridge] install failed: {e}")
                return None

            try:
                bpy.ops.preferences.addon_enable(module=pkg)
            except Exception as e:  # noqa: BLE001
                print(f"[trackmania_blender_bridge] enable failed: {e}")

            # Restore the snapshotted preferences.
            try:
                new_addon = bpy.context.preferences.addons.get(pkg)
                if new_addon is not None and prefs_snapshot:
                    new_prefs = new_addon.preferences
                    for k, v in prefs_snapshot.items():
                        try:
                            setattr(new_prefs, k, v)
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        bpy.ops.wm.save_userpref()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001
                print(f"[trackmania_blender_bridge] failed to restore prefs: {e}")

            # Force the next preferences draw to re-check.
            global _upgrade_check_time
            _upgrade_check_time = 0.0
        except Exception as e:  # noqa: BLE001
            print(f"[trackmania_blender_bridge] upgrade failed: {e}")
        finally:
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:  # noqa: BLE001
                    pass
        return None

    bpy.app.timers.register(_deferred_upgrade, first_interval=0.1)


def menu_func_import(self, _context) -> None:
    self.layout.menu(
        _import_menu.TMBRIDGE_MT_Import.bl_idname,
        icon_value=_import_menu.get_submenu_icon_id(),
    )


class TMBRIDGE_OT_ClearAddonPyCache(bpy.types.Operator):
    """Recursively delete __pycache__ folders from the installed addon tree.

    Targets the addon's own package and its bundled NICE / GBXBPY / GBXMAT
    subpackages. Useful during development when stale .pyc files mask
    source-level changes.
    """

    bl_idname = "trackmania_blender_bridge.clear_addon_pycache"
    bl_label = "Clear Installed Addon __pycache__"
    bl_options = {"REGISTER"}

    def execute(self, context):
        addon_root = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isdir(addon_root):
            self.report({"ERROR"}, f"Addon directory not found: {addon_root}")
            return {"CANCELLED"}

        # Limit deletion to the addon root + its bundled subpackages so
        # we never touch anything outside Blender's addons folder.
        targets = [addon_root]
        for sub in ("NICE", "GBXBPY", "GBXMAT"):
            sub_path = os.path.join(addon_root, sub)
            if os.path.isdir(sub_path):
                targets.append(sub_path)

        removed = 0
        failed: list[str] = []
        seen: set[str] = set()
        for base in targets:
            for dirpath, dirnames, _files in os.walk(base):
                if "__pycache__" in dirnames:
                    pc = os.path.join(dirpath, "__pycache__")
                    if pc in seen:
                        continue
                    seen.add(pc)
                    try:
                        shutil.rmtree(pc)
                        removed += 1
                    except OSError as e:
                        failed.append(f"{pc} ({e})")
                    # Don't descend into the now-removed dir.
                    dirnames[:] = [d for d in dirnames if d != "__pycache__"]

        if failed:
            self.report(
                {"WARNING"},
                f"Removed {removed} __pycache__ dir(s); failed: {len(failed)}",
            )
        else:
            self.report({"INFO"}, f"Removed {removed} __pycache__ dir(s) from addon")
        return {"FINISHED"}


def _remove_collection_recursive(coll) -> None:
    for child in list(coll.children):
        _remove_collection_recursive(child)
    for obj in list(coll.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(coll)


class TMBRIDGE_OT_CleanBlendFile(bpy.types.Operator):
    """Remove all data from the current blend file."""

    bl_idname = "trackmania_blender_bridge.clean_blend_file"
    bl_label = "Clean Blend File"
    bl_description = "Remove all scenes, objects, meshes, materials, etc. from the current blend file"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, _context):
        for scene in bpy.data.scenes[1:]:
            bpy.data.scenes.remove(scene)
        default_scene = bpy.data.scenes[0]
        default_scene.name = "Scene"
        for key in list(default_scene.keys()):
            if key.startswith("tm_"):
                del default_scene[key]
        for obj in list(default_scene.collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for coll in list(default_scene.collection.children):
            _remove_collection_recursive(coll)

        while True:
            count = bpy.data.orphans_purge(
                do_local_ids=True, do_linked_ids=True, do_recursive=True
            )
            if count == 0:
                break

        self.report({"INFO"}, "Blend file cleaned")
        return {"FINISHED"}


def _format_active_object_stats(obj) -> str:
    """Build a human-readable, copy-friendly stats block for an object."""
    if obj is None:
        return "(no active object)"

    lines: list[str] = []
    lines.append(f"Object: {obj.name}")
    lines.append(f"  type           : {obj.type}")
    lines.append(f"  data           : {obj.data.name if obj.data else '(none)'}")
    lines.append(f"  collection(s)  : {[c.name for c in obj.users_collection]}")
    lines.append(f"  parent         : {obj.parent.name if obj.parent else '(none)'}")
    lines.append(f"  children       : {len(obj.children)}")
    loc = obj.location
    rot = obj.rotation_euler
    scl = obj.scale
    lines.append(f"  location       : ({loc.x:.6f}, {loc.y:.6f}, {loc.z:.6f})")
    lines.append(f"  rotation_euler : ({rot.x:.6f}, {rot.y:.6f}, {rot.z:.6f})")
    lines.append(f"  scale          : ({scl.x:.6f}, {scl.y:.6f}, {scl.z:.6f})")
    lines.append(f"  dimensions     : ({obj.dimensions.x:.6f}, {obj.dimensions.y:.6f}, {obj.dimensions.z:.6f})")
    lines.append(f"  visible        : {obj.visible_get()}")
    lines.append(f"  hide_viewport  : {obj.hide_viewport}")
    lines.append(f"  hide_render    : {obj.hide_render}")

    if obj.type == "MESH" and obj.data is not None:
        mesh = obj.data
        lines.append("Mesh:")
        lines.append(f"  vertices       : {len(mesh.vertices)}")
        lines.append(f"  edges          : {len(mesh.edges)}")
        lines.append(f"  polygons       : {len(mesh.polygons)}")
        lines.append(f"  loops          : {len(mesh.loops)}")
        lines.append(f"  uv_layers      : {[uv.name for uv in mesh.uv_layers]}")
        lines.append(f"  color_attrs    : {[ca.name for ca in mesh.color_attributes]}")
        lines.append(f"  vertex_groups  : {[vg.name for vg in obj.vertex_groups]}")
        lines.append(f"  shape_keys     : {len(mesh.shape_keys.key_blocks) if mesh.shape_keys else 0}")
        lines.append(f"  materials      : {[m.name if m else '(none)' for m in mesh.materials]}")
        attrs = [(a.name, a.data_type, a.domain) for a in mesh.attributes]
        lines.append(f"  attributes ({len(attrs)}):")
        for name, dtype, domain in attrs:
            lines.append(f"    - {name}  [{dtype}, {domain}]")
        modifiers = [(m.name, m.type) for m in obj.modifiers]
        lines.append(f"  modifiers ({len(modifiers)}):")
        for name, mtype in modifiers:
            lines.append(f"    - {name}  [{mtype}]")

    elif obj.type == "ARMATURE" and obj.data is not None:
        arm = obj.data
        lines.append("Armature:")
        lines.append(f"  bones          : {len(arm.bones)}")
        lines.append(f"  pose_bones     : {len(obj.pose.bones) if obj.pose else 0}")

    elif obj.type == "EMPTY":
        lines.append("Empty:")
        lines.append(f"  display_type   : {obj.empty_display_type}")
        lines.append(f"  display_size   : {obj.empty_display_size:.6f}")

    custom_props = [k for k in obj.keys() if not k.startswith("_")]
    if custom_props:
        lines.append(f"Custom properties ({len(custom_props)}):")
        for key in custom_props:
            try:
                value = obj[key]
            except Exception:  # noqa: BLE001
                value = "<unreadable>"
            value_str = repr(value)
            if len(value_str) > 200:
                value_str = value_str[:200] + "..."
            lines.append(f"  - {key} = {value_str}")

    mats = list(getattr(obj, "material_slots", []) or [])
    if mats:
        lines.append(f"Material slots ({len(mats)}):")
        for slot_idx, slot in enumerate(mats):
            mat = slot.material
            if mat is None:
                lines.append(f"  [{slot_idx}] (empty slot)")
                continue
            lines.extend(_format_material_stats(mat, slot_idx, indent="  "))

    return "\n".join(lines)


def _format_material_stats(mat, slot_idx: int = -1, indent: str = "") -> list[str]:
    """Build a copy-friendly stats block for a single material."""
    lines: list[str] = []
    head = f"[{slot_idx}] {mat.name}" if slot_idx >= 0 else mat.name
    lines.append(f"{indent}{head}")
    lines.append(f"{indent}  use_nodes      : {mat.use_nodes}")
    lines.append(f"{indent}  blend_method   : {getattr(mat, 'blend_method', '?')}")
    if hasattr(mat, "shadow_method"):
        lines.append(f"{indent}  shadow_method  : {mat.shadow_method}")
    lines.append(f"{indent}  alpha_threshold: {getattr(mat, 'alpha_threshold', '?')}")
    lines.append(f"{indent}  backface_cull  : {getattr(mat, 'use_backface_culling', '?')}")
    lines.append(f"{indent}  diffuse_color  : {tuple(round(c, 4) for c in mat.diffuse_color)}")

    custom_props = [k for k in mat.keys() if not k.startswith("_")]
    if custom_props:
        lines.append(f"{indent}  custom props ({len(custom_props)}):")
        for key in custom_props:
            try:
                value = mat[key]
            except Exception:  # noqa: BLE001
                value = "<unreadable>"
            value_str = repr(value)
            if len(value_str) > 160:
                value_str = value_str[:160] + "..."
            lines.append(f"{indent}    - {key} = {value_str}")

    if mat.use_nodes and mat.node_tree is not None:
        nt = mat.node_tree
        nodes = list(nt.nodes)
        links = list(nt.links)
        lines.append(f"{indent}  node_tree      : {len(nodes)} nodes, {len(links)} links")

        type_counts: dict[str, int] = {}
        for node in nodes:
            type_counts[node.bl_idname] = type_counts.get(node.bl_idname, 0) + 1
        if type_counts:
            summary = ", ".join(
                f"{k}={v}" for k, v in sorted(type_counts.items())
            )
            lines.append(f"{indent}    node types : {summary}")

        images = []
        for node in nodes:
            if node.bl_idname == "ShaderNodeTexImage" and node.image is not None:
                img = node.image
                src = getattr(img, "source", "?")
                fp = getattr(img, "filepath", "")
                size = tuple(img.size) if hasattr(img, "size") else ("?", "?")
                images.append(
                    f"{node.name}: {img.name} [{src}] {size[0]}x{size[1]}  {fp}"
                )
        if images:
            lines.append(f"{indent}    image textures ({len(images)}):")
            for entry in images:
                lines.append(f"{indent}      - {entry}")

        for node in nodes:
            if node.bl_idname == "ShaderNodeBsdfPrincipled":
                inputs = node.inputs

                def _val(name: str) -> str:
                    if name not in inputs:
                        return "(missing)"
                    inp = inputs[name]
                    if inp.is_linked:
                        return "<linked>"
                    try:
                        v = inp.default_value
                        if hasattr(v, "__len__"):
                            return str(tuple(round(x, 4) for x in v))
                        return f"{v:.4f}" if isinstance(v, float) else str(v)
                    except Exception:  # noqa: BLE001
                        return "?"

                lines.append(f"{indent}    Principled BSDF ('{node.name}'):")
                for prop in (
                    "Base Color", "Metallic", "Roughness", "IOR",
                    "Alpha", "Normal", "Emission Color", "Emission Strength",
                ):
                    lines.append(f"{indent}      {prop:<18}: {_val(prop)}")

        outputs = [n for n in nodes if n.bl_idname == "ShaderNodeOutputMaterial"]
        for out in outputs:
            surf = out.inputs.get("Surface")
            if surf is None:
                continue
            if surf.is_linked and surf.links:
                src = surf.links[0].from_node
                lines.append(
                    f"{indent}    Output '{out.name}' Surface <- "
                    f"{src.bl_idname} ('{src.name}')"
                )
            else:
                lines.append(f"{indent}    Output '{out.name}' Surface: (unlinked)")

    return lines


class TMBRIDGE_OT_PrintActiveObjectStats(bpy.types.Operator):
    """Print stats of the active object to the console and copy to clipboard."""

    bl_idname = "trackmania_blender_bridge.print_active_object_stats"
    bl_label = "Print Active Object Stats"
    bl_description = "Print stats of the currently active object and copy them to the system clipboard"
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"WARNING"}, "No active object")
            return {"CANCELLED"}

        text = _format_active_object_stats(obj)

        for line in text.splitlines():
            log.info(line)
        print(text)

        try:
            context.window_manager.clipboard = text
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, f"Stats printed but clipboard copy failed: {e}")
            return {"FINISHED"}

        self.report({"INFO"}, f"Stats for '{obj.name}' printed and copied to clipboard")
        return {"FINISHED"}


def _format_active_object_vertex_data(obj, max_verts: int = 2048) -> str:
    """Format mesh vertex data (positions, UVs, normals) for the active object."""
    if obj is None:
        return "(no active object)"
    if obj.type != "MESH":
        return f"Object '{obj.name}' is not a mesh (type={obj.type})"

    mesh = obj.data
    n_verts = len(mesh.vertices)
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)

    lines: list[str] = []
    lines.append(f"Mesh: {mesh.name}  (object: {obj.name})")
    lines.append(f"  vertices : {n_verts}")
    lines.append(f"  loops    : {n_loops}")
    lines.append(f"  polygons : {n_polys}")
    lines.append(f"  uv_layers: {[uv.name for uv in mesh.uv_layers]}")
    lines.append(f"  materials: {[m.name if m else '(none)' for m in mesh.materials]}")

    truncated = n_verts > max_verts
    limit = min(n_verts, max_verts)

    # Vertices: index, position, normal
    lines.append("")
    lines.append(f"Vertices (idx, position, normal){'  [truncated]' if truncated else ''}:")
    for i in range(limit):
        v = mesh.vertices[i]
        p, n = v.co, v.normal
        lines.append(
            f"  v{i:5d}  pos=({p.x: .6f}, {p.y: .6f}, {p.z: .6f})  "
            f"nrm=({n.x: .4f}, {n.y: .4f}, {n.z: .4f})"
        )
    if truncated:
        lines.append(f"  ... ({n_verts - max_verts} more vertices not shown)")

    # Per-loop data: corner index, owning poly, vertex idx, UVs from each layer
    loop_truncated = n_loops > max_verts
    loop_limit = min(n_loops, max_verts)
    # Build poly-index lookup for each loop
    loop_to_poly = [-1] * n_loops
    for pi, poly in enumerate(mesh.polygons):
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            loop_to_poly[li] = pi

    lines.append("")
    lines.append(f"Loops (idx, poly, vert, uvs){'  [truncated]' if loop_truncated else ''}:")
    uv_layers = list(mesh.uv_layers)
    for li in range(loop_limit):
        loop = mesh.loops[li]
        uv_parts = []
        for layer in uv_layers:
            uv = layer.data[li].uv
            uv_parts.append(f"{layer.name}=({uv.x: .4f}, {uv.y: .4f})")
        uv_str = ("  " + "  ".join(uv_parts)) if uv_parts else ""
        lines.append(
            f"  L{li:5d}  poly={loop_to_poly[li]:5d}  v={loop.vertex_index:5d}{uv_str}"
        )
    if loop_truncated:
        lines.append(f"  ... ({n_loops - max_verts} more loops not shown)")

    # Polygons: index, material, vert indices
    poly_truncated = n_polys > max_verts
    poly_limit = min(n_polys, max_verts)
    lines.append("")
    lines.append(f"Polygons (idx, mat, verts){'  [truncated]' if poly_truncated else ''}:")
    for pi in range(poly_limit):
        poly = mesh.polygons[pi]
        verts = list(poly.vertices)
        lines.append(f"  p{pi:5d}  mat={poly.material_index}  verts={verts}")
    if poly_truncated:
        lines.append(f"  ... ({n_polys - max_verts} more polygons not shown)")

    return "\n".join(lines)


class TMBRIDGE_OT_PrintActiveObjectVertexData(bpy.types.Operator):
    """Print mesh vertex/loop/polygon data of the active object."""

    bl_idname = "trackmania_blender_bridge.print_active_object_vertex_data"
    bl_label = "Print Vertex Data"
    bl_description = (
        "Print positions, normals, per-corner UVs and polygon vertex indices "
        "of the active mesh object; copy to clipboard"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"WARNING"}, "No active object")
            return {"CANCELLED"}
        if obj.type != "MESH":
            self.report({"WARNING"}, f"Active object is not a mesh ({obj.type})")
            return {"CANCELLED"}

        text = _format_active_object_vertex_data(obj)

        # Console only — log can be slow for large meshes.
        print(text)

        try:
            context.window_manager.clipboard = text
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, f"Printed but clipboard copy failed: {e}")
            return {"FINISHED"}

        self.report(
            {"INFO"},
            f"Vertex data for '{obj.name}' printed to console and copied to clipboard",
        )
        return {"FINISHED"}


def _format_selected_verts_stats(obj) -> str:
    """Build a human-readable report of selected vertices in edit mode."""
    import bmesh
    me = obj.data
    bm = bmesh.from_edit_mesh(me)
    bm.verts.ensure_lookup_table()
    sel = [v for v in bm.verts if v.select]
    lines = [f"Mesh: {me.name}  (object: {obj.name})  selected {len(sel)} vert(s)"]
    if not sel:
        lines.append("  (no vertices selected)")
        return "\n".join(lines)

    xs = [v.co.x for v in sel]
    ys = [v.co.y for v in sel]
    zs = [v.co.z for v in sel]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    cz = sum(zs) / len(zs)
    lines.append(
        f"  bounds: x[{min(xs):.4f}, {max(xs):.4f}] "
        f"y[{min(ys):.4f}, {max(ys):.4f}] "
        f"z[{min(zs):.4f}, {max(zs):.4f}]"
    )
    lines.append(f"  centroid: ({cx:.4f}, {cy:.4f}, {cz:.4f})")
    lines.append("")
    lines.append("Vertices (idx, position, normal):")
    for v in sorted(sel, key=lambda x: x.index):
        n = v.normal
        lines.append(
            f"  v {v.index:4d}  pos=({v.co.x: .6f}, {v.co.y: .6f}, {v.co.z: .6f})"
            f"  nrm=({n.x: .4f}, {n.y: .4f}, {n.z: .4f})"
        )

    sel_set = {v.index for v in sel}
    sel_edges = [e for e in bm.edges if e.verts[0].index in sel_set and e.verts[1].index in sel_set]
    if sel_edges:
        lines.append("")
        lines.append(f"Edges among selection ({len(sel_edges)}):")
        for e in sel_edges:
            a, b = e.verts[0].index, e.verts[1].index
            lines.append(f"  e ({a}, {b})  length={e.calc_length():.4f}")

    sel_faces = [f for f in bm.faces if all(v.index in sel_set for v in f.verts)]
    if sel_faces:
        lines.append("")
        lines.append(f"Faces fully in selection ({len(sel_faces)}):")
        for f in sel_faces:
            verts = [v.index for v in f.verts]
            lines.append(f"  f {f.index:4d}  mat={f.material_index}  verts={verts}")

    return "\n".join(lines)


class TMBRIDGE_OT_PrintSelectedVertsStats(bpy.types.Operator):
    """Print stats of the selected vertices (edit mode)."""

    bl_idname = "trackmania_blender_bridge.print_selected_verts_stats"
    bl_label = "Print Selected Verts"
    bl_description = (
        "Print position, normal and connectivity info for the selected "
        "vertices in edit mode; also lists edges and fully-selected faces. "
        "Copy to clipboard"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == "MESH"
            and obj.mode == "EDIT"
        )

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"WARNING"}, "Need an active mesh in edit mode")
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Active mesh must be in edit mode")
            return {"CANCELLED"}

        text = _format_selected_verts_stats(obj)
        print(text)

        try:
            context.window_manager.clipboard = text
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, f"Printed but clipboard copy failed: {e}")
            return {"FINISHED"}

        self.report(
            {"INFO"},
            f"Selected vert stats for '{obj.name}' printed and copied to clipboard",
        )
        return {"FINISHED"}


class TMBRIDGE_OT_DistanceFromOrigin(bpy.types.Operator):
    """Compute world-space distance from (0,0,0) to the active vertex."""

    bl_idname = "trackmania_blender_bridge.distance_from_origin"
    bl_label = "Distance From Origin"
    bl_description = (
        "In edit mode, print the world-space distance from (0,0,0) to the "
        "active selected vertex (and its X, Y, Z components). Result is "
        "also copied to the clipboard."
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == "MESH"
            and obj.mode == "EDIT"
        )

    def execute(self, context):
        import bmesh

        obj = context.active_object
        if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
            self.report({"WARNING"}, "Need an active mesh in edit mode")
            return {"CANCELLED"}

        bm = bmesh.from_edit_mesh(obj.data)
        sel = [v for v in bm.verts if v.select]
        if not sel:
            self.report({"WARNING"}, "No selected vertices")
            return {"CANCELLED"}

        mw = obj.matrix_world
        lines = [
            f"Distance from origin for {len(sel)} selected verts of '{obj.name}':",
            f"  {'idx':>5}  {'X':>10} {'Y':>10} {'Z':>10}   {'dist3D':>10}  {'distXY':>10}",
        ]
        sum_xy = 0.0
        sum_3d = 0.0
        min_xy = float("inf")
        max_xy = float("-inf")
        for v in sel:
            wco = mw @ v.co
            d3 = wco.length
            dxy = math.hypot(wco.x, wco.y)
            sum_xy += dxy
            sum_3d += d3
            if dxy < min_xy:
                min_xy = dxy
            if dxy > max_xy:
                max_xy = dxy
            lines.append(
                f"  {v.index:>5}  {wco.x:>10.4f} {wco.y:>10.4f} {wco.z:>10.4f}   "
                f"{d3:>10.4f}  {dxy:>10.4f}"
            )
        n = len(sel)
        lines.append("")
        lines.append(
            f"  XY: min={min_xy:.4f}  max={max_xy:.4f}  "
            f"avg={sum_xy / n:.4f}  spread={max_xy - min_xy:.4f}"
        )
        lines.append(f"  3D avg: {sum_3d / n:.4f}")

        text = "\n".join(lines)
        print(text)

        try:
            context.window_manager.clipboard = text
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, f"Printed but clipboard copy failed: {e}")

        self.report(
            {"INFO"},
            f"{n} verts: XY min={min_xy:.3f} max={max_xy:.3f} avg={sum_xy / n:.3f}",
        )
        return {"FINISHED"}


def _format_outliner_tree(scene) -> str:
    """Format the scene's collection/object hierarchy as an indented tree."""
    lines: list[str] = []
    lines.append(f"Scene: {scene.name}")

    def _obj_summary(obj) -> str:
        info = f"{obj.name} [{obj.type}]"
        if obj.type == "MESH" and obj.data is not None:
            info += (
                f"  v={len(obj.data.vertices)}"
                f" p={len(obj.data.polygons)}"
            )
        if obj.instance_collection is not None:
            info += f"  -> instance:{obj.instance_collection.name}"
        if obj.parent is not None:
            info += f"  (parent:{obj.parent.name})"
        return info

    visited_collections: set[int] = set()
    visited_objects: set[int] = set()

    def _walk_object(obj, depth: int) -> None:
        oid = id(obj)
        if oid in visited_objects:
            lines.append(f"{'  ' * depth}- {obj.name} [...recursive]")
            return
        visited_objects.add(oid)
        lines.append(f"{'  ' * depth}- {_obj_summary(obj)}")
        for child in obj.children:
            _walk_object(child, depth + 1)

    def _walk_collection(coll, depth: int) -> None:
        cid = id(coll)
        if cid in visited_collections:
            lines.append(f"{'  ' * depth}+ {coll.name} [...recursive]")
            return
        visited_collections.add(cid)
        lines.append(f"{'  ' * depth}+ {coll.name}/")
        # Top-level (parent-less) objects of this collection
        for obj in coll.objects:
            if obj.parent is None or obj.parent.name not in coll.objects:
                _walk_object(obj, depth + 1)
        for child_coll in coll.children:
            _walk_collection(child_coll, depth + 1)

    _walk_collection(scene.collection, 0)

    n_obj = len(visited_objects)
    n_coll = len(visited_collections)
    lines.append("")
    lines.append(f"Totals: {n_coll} collections, {n_obj} objects")
    return "\n".join(lines)


class TMBRIDGE_OT_PrintOutlinerTree(bpy.types.Operator):
    """Print the outliner (scene collection/object) tree and copy to clipboard."""

    bl_idname = "trackmania_blender_bridge.print_outliner_tree"
    bl_label = "Print Outliner Tree"
    bl_description = (
        "Print the scene's collection/object hierarchy to the console "
        "and copy it to the system clipboard"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        if scene is None:
            self.report({"WARNING"}, "No active scene")
            return {"CANCELLED"}

        text = _format_outliner_tree(scene)
        print(text)
        for line in text.splitlines():
            log.info(line)

        try:
            context.window_manager.clipboard = text
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, f"Printed but clipboard copy failed: {e}")
            return {"FINISHED"}

        self.report({"INFO"}, "Outliner tree printed and copied to clipboard")
        return {"FINISHED"}


from . import _dev_panels  # noqa: E402
from . import _gbxmat_panel  # noqa: E402
from . import _import_menu  # noqa: E402
from . import _map_panel  # noqa: E402
from . import _map_panel_preview  # noqa: E402
from . import _asset_panel  # noqa: E402
from . import _asset_panel_preview  # noqa: E402
from . import _tools_panel  # noqa: E402
from . import _batch_items  # noqa: E402
from . import _trimsheet  # noqa: E402
from . import _blendermania_items  # noqa: E402
from . import _embedded_items  # noqa: E402
from . import _replay  # noqa: E402
from . import _render_view  # noqa: E402
from . import _mediatracker  # noqa: E402
from . import _mediatracker_ui  # noqa: E402
from . import _openplanet_bridge  # noqa: E402
from . import gbx_inspector  # noqa: E402
from . import map_import_v4  # noqa: E402

# Sidebar panels registered only while the "Developer UI" preference is on.
DEVELOPER_PANELS = _dev_panels.panels(_gbxmat_panel.TMBRIDGE_PT_GBXMatPanel)


def _sync_developer_ui(enabled: bool) -> None:
    """Register or unregister the developer sidebar panels at runtime."""
    if not enabled:
        _mediatracker_ui.close_all()
    for cls in DEVELOPER_PANELS if enabled else reversed(DEVELOPER_PANELS):
        registered = getattr(cls, "is_registered", False)
        try:
            if enabled and not registered:
                bpy.utils.register_class(cls)
            elif not enabled and registered:
                bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError) as exc:
            log.warning("Developer panel %s toggle failed: %s", cls.__name__, exc)


CLASSES = (
    TMBRIDGE_OT_InstallDependencies,
    TMBRIDGE_AddonPreferences,
    TMBRIDGE_OT_ImportMaterial,
    TMBRIDGE_OT_ImportMesh,
    TMBRIDGE_OT_ImportVegetTree,
    TMBRIDGE_OT_ImportItem,
    TMBRIDGE_OT_ImportPrefab,
    TMBRIDGE_OT_ImportED,
    TMBRIDGE_OT_UpgradeFromBuild,
    TMBRIDGE_OT_ClearAddonPyCache,
    TMBRIDGE_OT_CleanBlendFile,
    TMBRIDGE_OT_PrintActiveObjectStats,
    TMBRIDGE_OT_PrintActiveObjectVertexData,
    TMBRIDGE_OT_PrintSelectedVertsStats,
    TMBRIDGE_OT_DistanceFromOrigin,
    TMBRIDGE_OT_PrintOutlinerTree,
) + _dev_panels.PUBLIC_PANELS + tuple(c for c in _gbxmat_panel.CLASSES if c not in DEVELOPER_PANELS) + map_import_v4.CLASSES + _import_menu.CLASSES + _render_view.CLASSES + _mediatracker.CLASSES + _mediatracker_ui.CLASSES + _map_panel.CLASSES + _map_panel_preview.CLASSES + _asset_panel.CLASSES + _tools_panel.CLASSES + _batch_items.CLASSES + _trimsheet.CLASSES + _blendermania_items.CLASSES + _embedded_items.CLASSES + _replay.CLASSES + _openplanet_bridge.CLASSES


CLASSES += _asset_panel_preview.CLASSES
CLASSES = tuple(
    cls for cls in CLASSES
    if not (cls in _map_panel.CLASSES + _asset_panel.CLASSES
            and issubclass(cls, bpy.types.Panel))
)


def register() -> None:
    _ensure_bundled_packages_on_path()
    from pathlib import Path
    from ._package_cleanup import remove_retired_importers

    for path in remove_retired_importers(Path(__file__).parent):
        log.info("Removed retired map importer: %s", path)
    _snapshot_running_build()

    # Defensive: strip any stale copies of our menu hook from previous
    # module instances (can happen after a hot-reload upgrade if the old
    # unregister didn't run cleanly).
    _drain_menu_hook()

    for cls in CLASSES:
        bpy.utils.register_class(cls)

    _gbxmat_panel.register_scene_props()
    _map_panel.register_scene_props()
    _map_panel_preview.register_scene_props()
    _mediatracker.register_scene_props()
    _mediatracker_ui.register()
    _asset_panel.register_scene_props()
    _asset_panel_preview.register_scene_props()
    _batch_items.register_scene_props()
    _trimsheet.register()
    _progress.register()
    gbx_inspector.register()
    try:
        _openplanet_bridge.register()
    except Exception:  # noqa: BLE001 - the addon must load without the bridge
        log.exception("Openplanet bridge failed to start")

    from . import gbx_props
    gbx_props.register()
    _blendermania_items.register()
    _embedded_items.register()
    _tools_panel.register()

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

    addon = bpy.context.preferences.addons.get(ADDON_ID)
    _sync_developer_ui(addon is not None and addon.preferences.developer_ui)
    if addon is not None:
        configure_logging(addon.preferences.log_level)
        _configure_file_logging(addon.preferences)
    else:
        configure_logging()

    log.info("%s registered", ADDON_NAME)


def _drain_menu_hook() -> None:
    """Remove every existing 'menu_func_import' callback owned by this addon.

    Iterates by callback name (``__name__``) and module prefix so it also
    catches stale function objects left over from previous module instances.
    """
    menu = getattr(bpy.types, "TOPBAR_MT_file_import", None)
    draw_funcs = getattr(menu, "_dyn_ui_initialize", lambda: None)() if menu else None
    if draw_funcs is None:
        return
    pkg_prefix = (ADDON_ID or "").split(".")[0]
    for fn in list(draw_funcs):
        if getattr(fn, "__name__", "") != "menu_func_import":
            continue
        mod = getattr(fn, "__module__", "") or ""
        if mod == __name__ or mod.startswith(pkg_prefix):
            try:
                draw_funcs.remove(fn)
            except ValueError:
                pass


def unregister() -> None:
    if _batch_items._running is not None:
        _batch_items._running.cancel(bpy.context)
    _embedded_items.unregister()
    _tools_panel.unregister()
    _blendermania_items.unregister()
    log.info("%s unregistering", ADDON_NAME)
    _file_logging.unregister()
    _drain_menu_hook()
    _import_menu.unregister_icons()
    _progress.unregister()
    _render_view.unregister()
    _mediatracker_ui.unregister()
    _mediatracker.unregister_scene_props()
    _openplanet_bridge.unregister()
    _sync_developer_ui(False)

    try:
        from . import gbx_props
        gbx_props.unregister()
    except Exception:
        pass

    gbx_inspector.unregister()
    _map_panel.unregister_scene_props()
    _map_panel_preview.unregister_scene_props()
    _asset_panel.unregister_scene_props()
    _asset_panel_preview.unregister_scene_props()
    _batch_items.unregister_scene_props()
    _trimsheet.unregister()
    _gbxmat_panel.unregister_scene_props()

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
