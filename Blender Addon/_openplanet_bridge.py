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
from bpy.props import BoolProperty, StringProperty

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
_task_cancel = threading.Event()
_watched_jobs: list[str] = []
# Manual requests whose results are moved into a custom library when they finish.
_install_jobs: set[str] = set()


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
    return extract_dir(prefs)


def extract_dir(prefs=None) -> str:
    """Openplanet's ``Extract/GameData``, where the plugin writes; files are moved from here into the library."""
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
    _task_cancel.clear()
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


class TMBRIDGE_OT_CancelLibraryTask(bpy.types.Operator):
    """Stop the running library task after the current request"""

    bl_idname = "trackmania_blender_bridge.cancel_library_task"
    bl_label = "Cancel"

    @classmethod
    def poll(cls, _context):
        return task_running() and not _task_cancel.is_set()

    def execute(self, _context):
        _task_cancel.set()
        task_state["message"] = "Cancelling"
        return {"FINISHED"}


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
        from GBXBPY.library.validate import check_library

        root = library_dir()
        extract_root = extract_dir()
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
                repaired, _failed = _extract_and_install(
                    q, root, extract_root, index, [rel for rel, _reason in bad], "Repairing")
                remaining = len(bad) - repaired
                if remaining:
                    log.warning("Library repair: %d/%d repaired, %d unresolved", repaired, len(bad), remaining)
                cancelled = " (cancelled)" if _task_cancel.is_set() else ""
                return f"Library check: {repaired}/{len(bad)} repaired in {root}; {remaining} unresolved{cancelled}"
            finally:
                stop_import_server(server)

        if not _run_task("Check Library", work):
            stop_import_server(server)
            self.report({"WARNING"}, "Another library task is already running")
            return {"CANCELLED"}
        return {"FINISHED"}


FILL_BATCH = 200
HOOK_TIMEOUT = 120.0


def _request(q, kind, rels, label, offset, total):
    """Submit one extraction job and wait for it; ``(job, results in request order)``."""
    from GBXBPY.library.server import FileResult

    job = q.submit(kind, ["GameData/" + rel for rel in rels])
    _watch_job(job.id)

    def cancelled():
        task_state["message"] = f"{label} {offset + job.done}/{total} (extracting)"
        return _task_cancel.is_set()

    q.wait(job, 120.0 + 2.0 * len(rels), cancelled=cancelled)
    by_path = {result.path.lower(): result for result in job.results}
    results = [by_path.get(path.lower(), FileResult("FAIL", path, job.error or "plugin returned no result"))
               for path in job.paths]
    return job, results


def _install(root, extract_root, index, results, label, offset, total) -> tuple[int, dict[str, str]]:
    """Install validated results; rejected ones as ``{rel: reason}``."""
    from GBXBPY.library.index import normalize
    from GBXBPY.library.validate import install_extracted

    def progress(_fraction, _message):
        task_state["message"] = f"{label} {offset + len(results)}/{total} (installing)"

    count, rejected = install_extracted(root, extract_root, results, index, progress=progress)
    return count, {normalize(path): reason for path, reason in rejected.items()}


def _hook_candidate(rel: str, reason: str) -> bool:
    key = rel.lower()
    return (key.endswith(".gbx") and not key.endswith("plugresource.gbx")
            and not reason.startswith("NOTFOUND"))


def _hook_install(q, root, extract_root, index, rel) -> str:
    """Extract ``rel`` in hook mode and install it; ``""`` on success, else the reason."""
    from GBXBPY.library.server import FileResult
    from GBXBPY.library.validate import recover_hook_extract

    source = os.path.join(extract_root, *rel.split("/"))

    def read() -> bytes:
        try:
            with open(source, "rb") as handle:
                return handle.read()
        except OSError:
            return b""

    # The plugin deletes the output before extracting; keep the truncated
    # normal extraction, whose header data the hook output may lack.
    normal = read()
    try:
        job = q.submit("extract_hook", ["GameData/" + rel])
    except ValueError as exc:
        return str(exc)
    _watch_job(job.id)
    q.wait(job, HOOK_TIMEOUT, cancelled=_task_cancel.is_set)
    hooked = read()
    try:
        data = recover_hook_extract(rel, hooked, normal, index.size(rel) if index is not None else None)
    except ValueError as exc:
        reason = f"hook extraction: {exc}"
        data = None
    if data is not None:
        try:
            with open(source, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            return f"hook extraction: cannot write {source}: {exc}"
        count, rejected = _install(root, extract_root, index, [FileResult("OK", "GameData/" + rel)],
                                   "Hook", 0, 1)
        if count:
            log.info("Recovered %s with hook extraction%s", rel,
                     "" if data is hooked else " and the normal extraction's header data")
            return ""
        reason = f"hook extraction: {next(iter(rejected.values()), 'not installed')}"
    if normal:
        try:
            with open(source, "wb") as handle:
                handle.write(normal)
        except OSError as exc:
            log.warning("Cannot restore the normal extraction of %s: %s", rel, exc)
    return reason


def _retry_failed(q, root, extract_root, index, failures: dict[str, str], label) -> int:
    """Retry failed files with normal extraction, then GBX files in hook mode; updates ``failures``."""
    retry = [rel for rel, reason in failures.items() if not reason.startswith("NOTFOUND")]
    if not retry or _task_cancel.is_set():
        return 0
    log.info("%s: retrying %d failed files", label, len(retry))
    job, results = _request(q, "extract", retry, f"{label} retry", 0, len(retry))
    installed, rejected = _install(root, extract_root, index, results, f"{label} retry", 0, len(retry))
    for rel in retry:
        if rel in rejected:
            failures[rel] = rejected[rel]
        else:
            failures.pop(rel, None)
    if job.state != "done":
        return installed
    hook = [rel for rel in retry if rel in failures and _hook_candidate(rel, failures[rel])]
    for number, rel in enumerate(hook):
        if _task_cancel.is_set() or not q.plugin_connected():
            break
        task_state["message"] = f"{label}: hook extraction {number + 1}/{len(hook)}: {rel}"
        reason = _hook_install(q, root, extract_root, index, rel)
        if reason:
            failures[rel] = reason
            log.warning("Hook extraction did not recover %s: %s", rel, reason)
        else:
            failures.pop(rel, None)
            installed += 1
    return installed


def _extract_and_install(q, root, extract_root, index, rels, label) -> tuple[int, dict[str, str]]:
    """Request ``rels`` in batches and install the validated files; ``(installed, {rel: reason})``.

    Failed files are retried once normally and, for GBX files, in hook mode.
    Stops between batches when the task is cancelled.
    """
    installed = 0
    failures: dict[str, str] = {}
    total = len(rels)
    complete = True
    for start in range(0, total, FILL_BATCH):
        if _task_cancel.is_set():
            break
        batch = rels[start:start + FILL_BATCH]
        job, results = _request(q, "extract", batch, label, start, total)
        count, rejected = _install(root, extract_root, index, results, label, start, total)
        installed += count
        failures.update(rejected)
        if job.state != "done" and not _task_cancel.is_set():
            log.warning("Library request stopped: %s", job.error or job.state)
            complete = False
            break
    if complete:
        installed += _retry_failed(q, root, extract_root, index, failures, label)
    return installed, failures


def _adopt_extracted(root, extract_root, index, files) -> int:
    """Move valid files already waiting in Openplanet's extract folder into the library."""
    from GBXBPY.library.server import FileResult
    from GBXBPY.library.validate import install_extracted, same_folder

    if same_folder(root, extract_root):
        return 0
    waiting = [FileResult("OK", "GameData/" + rel, str(size)) for rel, size in files
               if os.path.isfile(os.path.join(extract_root, *rel.split("/")))]
    if not waiting:
        return 0
    count, _failures = install_extracted(
        root, extract_root, waiting, index,
        progress=lambda _f, msg: task_state.update(message=f"Moving extracted files: {msg}"))
    return count


def _fill_plan(root: str, include_media: bool):
    """``(index, [(rel, size)] missing, unavailable count)`` from the saved index, or ``(None, [], 0)``.

    Files the game reported as not extractable for this game version are left out.
    """
    from GBXBPY.library import GameIndex
    from GBXBPY.library.index import load_unavailable
    from GBXBPY.library.provider import IMPORT_UNUSED_SUFFIXES
    from GBXBPY.library.validate import missing_files

    index = GameIndex.load(root)
    if index is None:
        return None, [], 0
    skip = () if include_media else IMPORT_UNUSED_SUFFIXES
    unavailable = load_unavailable(root, index.game_version)
    missing = missing_files(root, index, skip_suffixes=skip)
    available = [(rel, size) for rel, size in missing if rel.lower() not in unavailable]
    return index, available, len(missing) - len(available)


def _record_unavailable(root, index, failures: dict[str, str]) -> int:
    from GBXBPY.library.index import save_unavailable

    gone = [rel for rel, reason in failures.items() if reason.startswith("NOTFOUND")]
    if gone and index is not None:
        try:
            save_unavailable(root, index.game_version, gone)
        except OSError as exc:
            log.warning("Cannot save the unavailable file list: %s", exc)
    return len(gone)


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


class TMBRIDGE_OT_FillLibrary(bpy.types.Operator):
    """Extract every game file the library does not have yet, so imports work without the game"""

    bl_idname = "trackmania_blender_bridge.fill_library"
    bl_label = "Fill Missing Files"

    include_media: BoolProperty(
        name="Include Audio, Video and Shaders",
        description="Also extract sounds, videos and shader sources; imports never use them",
        default=False,
    )
    summary_needed: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    summary_all: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, _context):
        from .map_import_v4 import operator

        return not task_running() and operator._running is None

    def invoke(self, context, _event):
        from GBXBPY.library.provider import IMPORT_UNUSED_SUFFIXES

        index, missing, unavailable = _fill_plan(library_dir(), True)
        if index is None:
            summaries = ("No game index yet; it is fetched from Trackmania first",) * 2
        else:
            needed = [(rel, size) for rel, size in missing if not rel.lower().endswith(IMPORT_UNUSED_SUFFIXES)]
            skipped = f"; {unavailable} unavailable skipped" if unavailable else ""
            summaries = tuple(
                f"{len(files)} of {len(index)} game files missing "
                f"({_format_size(sum(s for _r, s in files))}){skipped}"
                for files in (needed, missing)
            )
        self.summary_needed, self.summary_all = summaries
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, _context):
        from GBXBPY.library.validate import same_folder

        layout = self.layout
        layout.label(text=self.summary_all if self.include_media else self.summary_needed, icon="FILE")
        layout.label(text=f"Library: {library_dir()}", icon="FILE_FOLDER")
        if not same_folder(library_dir(), extract_dir()):
            layout.label(text="Files are moved there from Openplanet's Extract folder", icon="INFO")
        layout.prop(self, "include_media")
        layout.label(text="Trackmania must be running with the extractor plugin. You can cancel anytime.",
                     icon="INFO")

    def execute(self, _context):
        root = library_dir()
        extract_root = extract_dir()
        include_media = self.include_media
        try:
            os.makedirs(root, exist_ok=True)
            server = start_import_server()
        except OSError as exc:
            message = server_error or str(exc)
            self.report({"ERROR"}, f"Could not start the Openplanet bridge server: {message}")
            return {"CANCELLED"}
        q = server.queue

        def work():
            from GBXBPY.library import LibraryProvider
            from GBXBPY.library.validate import remove_alias_copies, same_folder

            try:
                if not _wait_for_plugin(q):
                    return ("Fill library: Openplanet did not connect. "
                            "Start Trackmania with the Blender Bridge Extractor enabled, then try again")
                index, _missing, _skipped = _fill_plan(root, include_media)
                if index is None or (q.game_version and index.game_version != q.game_version):
                    task_state["message"] = "Listing game files in Trackmania"
                    provider = LibraryProvider(root, q, extract_dir=extract_root)
                    if not provider.refresh_index():
                        return "Fill library: the game index could not be fetched (see the log)"
                index, missing, _skipped = _fill_plan(root, include_media)
                if not same_folder(root, extract_root) and os.path.isdir(extract_root):
                    removed = remove_alias_copies(extract_root, index)
                    if removed:
                        log.info("Removed %d unused alias copies from %s", removed, extract_root)
                if not missing:
                    return f"Fill library: no missing files in {root}"
                adopted = _adopt_extracted(root, extract_root, index, missing)
                if adopted:
                    index, missing, _skipped = _fill_plan(root, include_media)
                installed, failures = _extract_and_install(
                    q, root, extract_root, index, [rel for rel, _size in missing], "Filling")
                unavailable = _record_unavailable(root, index, failures)
                left = len(missing) - installed - unavailable
                cancelled = " (cancelled)" if _task_cancel.is_set() else ""
                moved = f", {adopted} moved from the Extract folder" if adopted else ""
                gone = f", {unavailable} not available in this game version" if unavailable else ""
                return (f"Fill library: {installed} added{moved}, {len(failures) - unavailable} failed"
                        f"{gone}, {left} still missing{cancelled}")
            finally:
                stop_import_server(server)

        if not _run_task("Fill Library", work):
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


def _install_request_results(job) -> None:
    """Move a manual request's validated files into the custom library, off the main thread."""
    from GBXBPY.library import GameIndex
    from GBXBPY.library.validate import install_extracted

    root, extract_root = library_dir(), extract_dir()
    results = [r for r in job.results if r.ok]
    if not results:
        return

    def work():
        count, failed = install_extracted(root, extract_root, results, GameIndex.load(root))
        log.info("Request %s: moved %d/%d files into %s (%d rejected)",
                 job.id, count, len(results), root, len(failed))

    threading.Thread(target=work, name=f"TM bridge request {job.id} install", daemon=True).start()


def _tick():
    """Timer: report finished manual requests and redraw the preferences."""
    q = queue()
    for job_id in list(_watched_jobs):
        job = q.get(job_id) if q is not None else None
        if job is None or job.state in ("done", "failed"):
            _watched_jobs.remove(job_id)
            if job is not None and job_id in _install_jobs:
                _install_jobs.discard(job_id)
                _install_request_results(job)
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
        from GBXBPY.library.validate import same_folder

        if not same_folder(library_dir(prefs), extract_dir(prefs)):
            _install_jobs.add(job.id)
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


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _running_plugin_warning() -> str:
    """Message when Trackmania still runs a plugin older than the bundled one."""
    from GBXBPY.library import get_server

    server = get_server()
    if server is None or not server.running or not server.queue.plugin_connected():
        return ""
    running = server.queue.plugin_version or ""
    bundled = _bundled_plugin_version()
    if not running or not bundled or _version_tuple(running) >= _version_tuple(bundled):
        return ""
    return f"Trackmania runs plugin v{running}; update and reload it (bundled v{bundled})"


def draw_plugin(layout) -> None:
    """Installed vs bundled extractor plugin version and the install button."""
    installed = _plugin_version()
    bundled = _bundled_plugin_version()
    col = layout.column(align=True)
    row = col.row(align=True)
    if not installed:
        row.label(text="Not installed", icon="ERROR")
    elif bundled and installed != bundled:
        row.alert = True
        row.label(text=f"Installed v{installed}, bundled v{bundled}", icon="ERROR")
    else:
        row.label(text=f"Installed v{installed}", icon="CHECKMARK")
    row.operator(TMBRIDGE_OT_InstallExtractorPlugin.bl_idname,
                 text="Update Plugin" if installed else "Install Plugin", icon="IMPORT")
    warning = _running_plugin_warning()
    if warning:
        row = col.row()
        row.alert = True
        row.label(text=warning, icon="ERROR")


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
    warning = _running_plugin_warning()
    if warning:
        row = layout.row(align=True)
        row.alert = True
        row.label(text=warning, icon="ERROR")
        row.operator(TMBRIDGE_OT_InstallExtractorPlugin.bl_idname, text="Update Plugin", icon="IMPORT")
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
    row.operator(TMBRIDGE_OT_FillLibrary.bl_idname, text="Fill Missing", icon="IMPORT")
    row.operator(
        TMBRIDGE_OT_RefreshGameIndex.bl_idname, text="Refresh Index", icon="FILE_REFRESH"
    )

    label = _index_label(root)
    if label:
        layout.label(text=label, icon="FILE")
    else:
        layout.label(text="No game index yet; fetched on the first import", icon="INFO")
    _draw_task(layout)


def _draw_task(layout) -> None:
    if task_state.get("name"):
        row = layout.row(align=True)
        row.label(text=f"{task_state['name']}: {task_state.get('message', '')}", icon="SORTTIME")
        row.operator(TMBRIDGE_OT_CancelLibraryTask.bl_idname, text="", icon="CANCEL")
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
    row.operator(TMBRIDGE_OT_FillLibrary.bl_idname, icon="IMPORT")
    _draw_task(box)

    col = box.column(align=True)
    col.prop(prefs, "extract_request")
    row = col.row(align=True)
    row.operator(TMBRIDGE_OT_RequestExtract.bl_idname, text="Request Paths", icon="EXPORT").clipboard = False
    row.operator(TMBRIDGE_OT_RequestExtract.bl_idname, text="Request Clipboard", icon="PASTEDOWN").clipboard = True


CLASSES = (
    TMBRIDGE_OT_ToggleBridgeServer,
    TMBRIDGE_OT_RefreshGameIndex,
    TMBRIDGE_OT_CheckLibrary,
    TMBRIDGE_OT_FillLibrary,
    TMBRIDGE_OT_CancelLibraryTask,
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
