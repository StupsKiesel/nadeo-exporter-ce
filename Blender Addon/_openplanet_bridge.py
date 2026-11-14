"""Openplanet bridge: user library tools, local web API and developer requests.

The server runs for opted-in V4 imports, Game Library repairs, or a manually
started developer session; the Blender Bridge Extractor Openplanet plugin polls
it for work. Map Import V4 queues its own requests through a
:class:`GBXBPY.library.LibraryProvider`; this module draws the preferences and
Game Library interfaces and provides the manual developer tools.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import bpy
from bpy.props import BoolProperty

log = logging.getLogger("trackmania_blender_bridge.openplanet_bridge")

REPORT_TEXT = "Openplanet Requests"
REDRAW_INTERVAL = 1.0
PLUGIN_CONNECT_TIMEOUT = 30.0
PLUGIN_CONNECT_POLL_INTERVAL = 0.25

server_error = ""
_developer_server = None
# Worker state of the index refresh / library check, read by draw().
task_state: dict = {"name": "", "message": "", "result": ""}
_task_thread: threading.Thread | None = None
_watched_jobs: list[str] = []


def _prefs():
    from . import ADDON_ID

    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon is not None else None


def library_dir(prefs=None) -> str:
    """The game library folder: the preference, or Openplanet's extract GameData."""
    prefs = prefs if prefs is not None else _prefs()
    path = getattr(prefs, "gamedata_dir", "") if prefs is not None else ""
    if path:
        return os.path.normpath(bpy.path.abspath(path))
    return os.path.join(openplanet_root(prefs), "Extract", "GameData")


def openplanet_root(prefs=None) -> str:
    """Configured Openplanet user root, shared by extraction and plugin tools."""
    from GBXBPY.library import openplanet_dir

    prefs = prefs if prefs is not None else _prefs()
    path = getattr(prefs, "openplanet_dir", "") if prefs is not None else ""
    return os.path.normpath(bpy.path.abspath(path)) if path else openplanet_dir()


def plugin_source_dir() -> str:
    """Plugin folder bundled with the addon, or the repository copy when run from source."""
    from GBXBPY.library import PLUGIN_ID

    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "openplanet_plugin", PLUGIN_ID),
        os.path.join(os.path.dirname(here), "Openplanet Plugin", PLUGIN_ID),
    ):
        if os.path.isfile(os.path.join(candidate, "info.toml")):
            return candidate
    return os.path.join(here, "openplanet_plugin", PLUGIN_ID)


def split_request_paths(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;\r\n]+", text or "") if p.strip() and not p.strip().startswith("#")]


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def queue():
    """The running server's job queue, or ``None``."""
    from GBXBPY.library import get_server

    server = get_server()
    return server.queue if server is not None and server.running else None


def _wait_for_plugin(q) -> bool:
    deadline = time.monotonic() + PLUGIN_CONNECT_TIMEOUT
    while not q.plugin_connected():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        task_state["message"] = "Waiting for the Openplanet plugin to connect"
        time.sleep(min(PLUGIN_CONNECT_POLL_INTERVAL, remaining))
    return True


def start_import_server():
    """Start the server for imports or repairs; surface bind failures."""
    from GBXBPY.library import get_server

    if _developer_server is not None and get_server() is _developer_server and _developer_server.running:
        return _developer_server
    return _start_server()


def _start_server():
    global server_error
    from GBXBPY.library import start_server

    prefs = _prefs()
    port = int(getattr(prefs, "bridge_port", 1337)) if prefs is not None else 1337
    try:
        server = start_server(port)
    except OSError as exc:
        server_error = f"Port {port} unavailable: {exc.strerror or exc}"
        log.error("Bridge server could not start on port %d: %s", port, exc)
        raise
    server_error = ""
    server.status_extras = lambda: {"library": library_dir()}
    return server


def stop_import_server(server) -> None:
    """Release only this import's server, never a replacement."""
    from GBXBPY.library import get_server, stop_server

    if server is not None and server is not _developer_server and get_server() is server:
        stop_server()


class TMBRIDGE_OT_ToggleBridgeServer(bpy.types.Operator):
    """Start or stop a developer connection for library repairs and requests"""

    bl_idname = "trackmania_blender_bridge.toggle_bridge_server"
    bl_label = "Start / Stop Developer Web Server"

    @classmethod
    def poll(cls, _context):
        from .map_import_v4 import operator

        prefs = _prefs()
        if prefs is None or not prefs.developer_ui or operator._running is not None or task_running():
            return False
        q = queue()
        if q is not None:
            status = q.status()
            if status["queued"] or status["active"]:
                return False
        return True

    def execute(self, _context):
        global _developer_server, server_error
        from GBXBPY.library import get_server, stop_server

        server = get_server()
        if server is not None and server.running:
            stop_server()
            _developer_server = None
            server_error = ""
        else:
            try:
                _developer_server = _start_server()
            except OSError:
                self.report({"ERROR"}, server_error)
                return {"CANCELLED"}
        _redraw_preferences()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Background tasks (index refresh, library check)
# ---------------------------------------------------------------------------


def _run_task(name: str, work) -> bool:
    global _task_thread
    if _task_thread is not None and _task_thread.is_alive():
        return False
    task_state.update(name=name, message="Starting", result="")

    def body():
        try:
            task_state["result"] = work()
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            log.exception("%s failed", name)
            task_state["result"] = f"{name} failed: {exc}"
        task_state.update(name="", message="")

    _task_thread = threading.Thread(target=body, name=f"TM bridge {name}", daemon=True)
    _task_thread.start()
    _ensure_redraw_timer()
    return True


def task_running() -> bool:
    return _task_thread is not None and _task_thread.is_alive()


class TMBRIDGE_OT_RefreshGameIndex(bpy.types.Operator):
    """Ask Trackmania for the list of all game files; imports use it to know what can be fetched"""

    bl_idname = "trackmania_blender_bridge.refresh_game_index"
    bl_label = "Refresh Game Index"

    @classmethod
    def poll(cls, _context):
        from .map_import_v4 import operator

        return not task_running() and operator._running is None

    def execute(self, _context):
        from GBXBPY.library import LibraryProvider

        try:
            server = start_import_server()
        except OSError as exc:
            message = server_error or str(exc)
            self.report({"ERROR"}, f"Could not start the Openplanet bridge server: {message}")
            return {"CANCELLED"}
        q = server.queue
        provider = LibraryProvider(library_dir(), q)

        def work():
            try:
                if not _wait_for_plugin(q):
                    return (
                        "Game index refresh failed: Openplanet did not connect. "
                        "Start Trackmania with the Blender Bridge Extractor enabled, then try again"
                    )

                def watch():
                    while task_running() and task_state.get("name"):
                        task_state["message"] = provider.status_message or "Listing game files"
                        time.sleep(0.25)

                threading.Thread(target=watch, daemon=True).start()
                if not provider.refresh_index():
                    return "Game index refresh failed (see the log)"
                return f"Game index: {len(provider.index)} files (game {provider.index.game_version or '?'})"
            finally:
                stop_import_server(server)

        if not _run_task("Refresh Index", work):
            stop_import_server(server)
            self.report({"WARNING"}, "Another library task is already running")
            return {"CANCELLED"}
        return {"FINISHED"}


class TMBRIDGE_OT_CheckLibrary(bpy.types.Operator):
    """Check the game library, re-extract broken files and install validated replacements"""

    bl_idname = "trackmania_blender_bridge.check_library"
    bl_label = "Check Library"

    @classmethod
    def poll(cls, _context):
        from .map_import_v4 import operator

        return not task_running() and operator._running is None

    def execute(self, _context):
        from GBXBPY.library import GameIndex
        from GBXBPY.library.server import FileResult
        from GBXBPY.library.validate import check_library, copy_extracted

        root = library_dir()
        extract_root = os.path.join(openplanet_root(), "Extract", "GameData")
        if not os.path.isdir(root):
            self.report({"WARNING"}, f"Library folder does not exist yet: {root}")
            return {"CANCELLED"}
        try:
            server = start_import_server()
        except OSError as exc:
            message = server_error or str(exc)
            self.report({"ERROR"}, f"Could not start the Openplanet bridge server: {message}")
            return {"CANCELLED"}
        q = server.queue

        def work():
            try:
                index = GameIndex.load(root)
                bad = check_library(root, index, progress=lambda _f, msg: task_state.update(message=msg))
                for rel, reason in bad:
                    log.warning("Broken library file %s: %s", rel, reason)
                if not bad:
                    return "Library check: no broken files"
                if not _wait_for_plugin(q):
                    return (
                        f"Library check: {len(bad)} broken files; Openplanet did not connect. "
                        "Start Trackmania with the Blender Bridge Extractor enabled, then try again"
                    )
                job = q.submit("extract", [rel for rel, _reason in bad])
                _watch_job(job.id)

                def progress(_fraction, message):
                    task_state["message"] = message

                def cancelled():
                    task_state["message"] = f"Extracting replacements ({job.done}/{job.total})"
                    return False

                if not q.wait(job, 120.0 + 2.0 * len(bad), cancelled=cancelled):
                    raise RuntimeError(f"Replacement extraction failed: {job.error or job.state}")
                by_path = {result.path.lower(): result for result in job.results}
                results = [by_path.get(path.lower(), FileResult("FAIL", path, "plugin returned no result"))
                           for path in job.paths]
                repaired, failed = copy_extracted(root, extract_root, results, index, progress=progress)
                remaining = len(bad) - repaired
                if remaining:
                    log.warning("Library repair: %d/%d repaired, %d unresolved (%d reported failures)",
                                repaired, len(bad), remaining, len(failed))
                return f"Library check: {repaired}/{len(bad)} repaired in {root}; {remaining} unresolved"
            finally:
                stop_import_server(server)

        if not _run_task("Check Library", work):
            stop_import_server(server)
            self.report({"WARNING"}, "Another library task is already running")
            return {"CANCELLED"}
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Manual requests and plugin install
# ---------------------------------------------------------------------------


def _watch_job(job_id: str) -> None:
    _watched_jobs.append(job_id)
    _ensure_redraw_timer()


def _write_report(job) -> None:
    lines = [f"Request {job.id}: {job.state}" + (f" ({job.error})" if job.error else "")]
    lines.extend(f"{r.status}\t{r.path}\t{r.detail}" for r in job.results)
    text = bpy.data.texts.get(REPORT_TEXT) or bpy.data.texts.new(REPORT_TEXT)
    text.from_string("\n".join(lines) + "\n")


def _tick():
    """Timer: report finished manual requests and redraw the preferences."""
    q = queue()
    for job_id in list(_watched_jobs):
        job = q.get(job_id) if q is not None else None
        if job is None or job.state in ("done", "failed"):
            _watched_jobs.remove(job_id)
            if job is not None:
                ok = sum(r.ok for r in job.results)
                log.info("Openplanet request %s %s: %d/%d ok", job.id, job.state, ok, len(job.results))
                try:
                    _write_report(job)
                except Exception:  # noqa: BLE001 - restricted context
                    log.debug("Could not write the request report", exc_info=True)
    _redraw_preferences()
    if _watched_jobs or task_running():
        return REDRAW_INTERVAL
    return None


def _ensure_redraw_timer() -> None:
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=REDRAW_INTERVAL)


def _redraw_preferences() -> None:
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", []):
        for area in window.screen.areas:
            if area.type in ("PREFERENCES", "VIEW_3D"):
                area.tag_redraw()


STATUS_INTERVAL = 2.0
_last_status: tuple | None = None


def _status_key() -> tuple:
    from GBXBPY.library import get_server

    server = get_server()
    if server is None or not server.running:
        return (server_error, False)
    q = server.queue
    status = q.status()
    return (server_error, True, q.plugin_connected(), q.plugin_version, status["queued"], len(status["active"]))


def _watch_status():
    """Timer: redraw the Openplanet panel when the server or plugin state changes."""
    global _last_status
    try:
        from . import _check_upgrade_available

        key = (*_status_key(), _check_upgrade_available())
    except Exception:  # noqa: BLE001 - never raise out of a timer
        return STATUS_INTERVAL
    if key != _last_status:
        _last_status = key
        _redraw_preferences()
    return STATUS_INTERVAL


class TMBRIDGE_OT_RequestExtract(bpy.types.Operator):
    """Ask the Openplanet plugin to extract game files into the game library"""

    bl_idname = "trackmania_blender_bridge.request_extract"
    bl_label = "Request Extract"

    clipboard: BoolProperty(
        name="From Clipboard",
        description="Request the paths on the clipboard; the text field cuts long pasted lists",
        default=False,
        options={"SKIP_SAVE"},
    )

    @classmethod
    def description(cls, _context, properties):
        if properties.clipboard:
            return "Ask the Openplanet plugin to extract the game paths on the clipboard (separated by ';' or new lines)"
        return "Ask the Openplanet plugin to extract the typed game paths"

    @classmethod
    def poll(cls, _context):
        return queue() is not None

    def execute(self, context):
        prefs = _prefs()
        text = context.window_manager.clipboard if self.clipboard else getattr(prefs, "extract_request", "")
        paths = split_request_paths(text)
        if not paths:
            self.report({"WARNING"}, "No paths to request")
            return {"CANCELLED"}
        q = queue()
        try:
            job = q.submit("extract", paths)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _watch_job(job.id)
        waiting = "" if q.plugin_connected() else "; the plugin is not connected yet, it runs when Trackmania connects"
        self.report({"INFO"}, f"Requested {len(job.paths)} paths (request {job.id}){waiting}")
        return {"FINISHED"}


class TMBRIDGE_OT_InstallExtractorPlugin(bpy.types.Operator):
    """Copy the Blender Bridge Extractor plugin into OpenplanetNext/Plugins. Openplanet loads unsigned plugins only in developer mode"""

    bl_idname = "trackmania_blender_bridge.install_extractor_plugin"
    bl_label = "Install Openplanet Plugin"

    def execute(self, _context):
        from GBXBPY.library import install_plugin

        try:
            target = install_plugin(plugin_source_dir(), openplanet_root())
        except OSError as exc:
            self.report({"ERROR"}, f"Plugin install failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Installed to {target}. Reload it in Openplanet (Developer > Reload plugin)")
        return {"FINISHED"}


class TMBRIDGE_OT_OpenLibraryFolder(bpy.types.Operator):
    """Open the game library folder in the file browser"""

    bl_idname = "trackmania_blender_bridge.open_library_folder"
    bl_label = "Open Library Folder"

    def execute(self, _context):
        root = library_dir()
        os.makedirs(root, exist_ok=True)
        bpy.ops.wm.path_open(filepath=root)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Preferences UI
# ---------------------------------------------------------------------------


def _plugin_version() -> str:
    from GBXBPY.library import installed_plugin_dir

    path = os.path.join(installed_plugin_dir(openplanet_root()), "info.toml")
    try:
        with open(path, encoding="utf-8") as handle:
            match = re.search(r'^\s*version\s*=\s*"([^"]+)"', handle.read(), re.M)
    except OSError:
        return ""
    return match.group(1) if match else "?"


def _bundled_plugin_version() -> str:
    try:
        with open(os.path.join(plugin_source_dir(), "info.toml"), encoding="utf-8") as handle:
            match = re.search(r'^\s*version\s*=\s*"([^"]+)"', handle.read(), re.M)
    except OSError:
        return ""
    return match.group(1) if match else ""


_index_info: dict = {"key": None, "text": ""}


def _index_label(root: str) -> str:
    """Index summary for the UI, reloaded only when the index file changes."""
    from GBXBPY.library import GameIndex

    path = GameIndex.path_for(root)
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return ""
    if _index_info["key"] != key:
        index = GameIndex.load(root)
        _index_info["key"] = key
        _index_info["text"] = f"Game index: {len(index)} files (game {index.game_version or '?'})" if index else ""
    return _index_info["text"]


def draw_server(layout, prefs) -> None:
    """Compact connection state for preferences and developer tools."""
    from GBXBPY.library import get_server

    col = layout.column(align=True)
    server = get_server()
    if server_error:
        r = col.row()
        r.alert = True
        r.label(text=server_error, icon="ERROR")
    elif server is not None and server.running:
        q = server.queue
        if q.plugin_connected():
            col.label(text="Connected to Trackmania", icon="CHECKMARK")
        else:
            col.label(text="Waiting for Trackmania", icon="TIME")
    else:
        col.label(text="Idle", icon="INFO")


def draw_plugin(layout) -> None:
    """Installed vs bundled extractor plugin version and the install button."""
    installed = _plugin_version()
    bundled = _bundled_plugin_version()
    row = layout.row(align=True)
    if not installed:
        row.label(text="Not installed", icon="ERROR")
    elif bundled and installed != bundled:
        row.alert = True
        row.label(text=f"Installed v{installed}, bundled v{bundled}", icon="ERROR")
    else:
        row.label(text=f"Installed v{installed}", icon="CHECKMARK")
    row.operator(TMBRIDGE_OT_InstallExtractorPlugin.bl_idname,
                 text="Update Plugin" if installed else "Install Plugin", icon="IMPORT")


def draw(layout, prefs, *, boxed=True) -> None:
    box = layout.box() if boxed else layout
    row = box.row(align=True)
    if boxed:
        row.label(text="Openplanet", icon="LINKED")

    draw_server(row, prefs)
    row = box.row(align=True)
    draw_plugin(row)
    row = box.row(align=True)
    row.prop(prefs, "bridge_port", text="Port", slider=True)
    row.operator(TMBRIDGE_OT_OpenLibraryFolder.bl_idname, icon="FILE_FOLDER")


def draw_game_library(layout, prefs) -> None:
    if prefs is None:
        layout.label(text="Addon preferences not available", icon="ERROR")
        return

    root = library_dir(prefs)
    row = layout.row(align=True)
    row.label(text=root, icon="FILE_FOLDER")
    row.operator(
        TMBRIDGE_OT_OpenLibraryFolder.bl_idname,
        text="Open Folder",
        icon="FILE_FOLDER",
    )

    row = layout.row(align=True)
    row.operator(
        TMBRIDGE_OT_CheckLibrary.bl_idname, text="Check / Repair", icon="CHECKMARK"
    )
    row.operator(
        TMBRIDGE_OT_RefreshGameIndex.bl_idname, text="Refresh Index", icon="FILE_REFRESH"
    )

    label = _index_label(root)
    if label:
        layout.label(text=label, icon="FILE")
    else:
        layout.label(text="No game index yet; fetched on the first import", icon="INFO")
    if task_state.get("name"):
        layout.label(
            text=f"{task_state['name']}: {task_state.get('message', '')}", icon="SORTTIME"
        )
    elif task_state.get("result"):
        layout.label(text=task_state["result"], icon="INFO")


def draw_tools(box, prefs) -> None:
    """Developer connection details and manual extraction tools."""
    from GBXBPY.library import get_server

    box.prop(prefs, "bridge_port", text="Port")
    server = get_server()
    row = box.row(align=True)
    row.alert = bool(server_error)
    row.operator(
        TMBRIDGE_OT_ToggleBridgeServer.bl_idname,
        text="Stop Web Server" if server is not None and server.running else "Start Web Server",
        icon="PAUSE" if server is not None and server.running else "PLAY",
    )
    if server is not None and server.running:
        box.label(text=server.url, icon="URL")
        q = server.queue
        if q.plugin_connected():
            box.label(text=f"Plugin v{q.plugin_version or '?'}, game {q.game_version or '?'}")
    root = library_dir(prefs)
    box.label(text=root, icon="FILE_FOLDER")
    label = _index_label(root)
    if label:
        box.label(text=label, icon="FILE")
    else:
        box.label(text="No game index yet; it is fetched on the first import", icon="FILE")

    row = box.row(align=True)
    row.operator(TMBRIDGE_OT_RefreshGameIndex.bl_idname, icon="FILE_REFRESH")
    row.operator(TMBRIDGE_OT_CheckLibrary.bl_idname, icon="VIEWZOOM")
    if task_state.get("name"):
        box.label(text=f"{task_state['name']}: {task_state.get('message', '')}", icon="SORTTIME")
    elif task_state.get("result"):
        box.label(text=task_state["result"], icon="INFO")

    col = box.column(align=True)
    col.prop(prefs, "extract_request")
    row = col.row(align=True)
    row.operator(TMBRIDGE_OT_RequestExtract.bl_idname, text="Request Paths", icon="EXPORT").clipboard = False
    row.operator(TMBRIDGE_OT_RequestExtract.bl_idname, text="Request Clipboard", icon="PASTEDOWN").clipboard = True


CLASSES = (
    TMBRIDGE_OT_ToggleBridgeServer,
    TMBRIDGE_OT_RefreshGameIndex,
    TMBRIDGE_OT_CheckLibrary,
    TMBRIDGE_OT_RequestExtract,
    TMBRIDGE_OT_InstallExtractorPlugin,
    TMBRIDGE_OT_OpenLibraryFolder,
)


def register() -> None:
    from GBXBPY import library
    from GBXMAT import images

    images.set_file_check(library.ensure)
    if not bpy.app.timers.is_registered(_watch_status):
        bpy.app.timers.register(_watch_status, first_interval=STATUS_INTERVAL, persistent=True)


def unregister() -> None:
    global _developer_server
    from GBXBPY.library import stop_server
    from .map_import_v4 import operator

    if operator._running is not None:
        operator._running.cancel(bpy.context)

    for timer in (_tick, _watch_status):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    try:
        from GBXMAT import images

        images.set_file_check(None)
    except ImportError:
        pass
    stop_server()
    _developer_server = None
