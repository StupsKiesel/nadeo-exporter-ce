"""User-facing Tools panel and collection-instance editing operator."""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import bpy
from mathutils import Matrix
from . import ADDON_SHORT_NAME

log = logging.getLogger("trackmania_blender_bridge.tools")
OP = "trackmania_blender_bridge."
GITHUB_REPOSITORY_URL = "https://github.com/StupsKiesel/nadeo-exporter-ce"
_RELEASE_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")
_ADDON_ZIP = re.compile(r"nadeo_exporter_ce_\d+\.\d+\.\d+\.zip")


def _is_instance(obj):
    return obj.instance_type == "COLLECTION" and obj.instance_collection is not None


def _contents(collection, matrix, ancestors=()):
    if collection in ancestors:
        raise ValueError(f"Recursive collection instance: {collection.name}")
    matrix = matrix @ Matrix.Translation(-collection.instance_offset)
    for obj in collection.all_objects:
        world = matrix @ obj.matrix_world
        if _is_instance(obj):
            yield from _contents(obj.instance_collection, world, (*ancestors, collection))
        else:
            yield obj, world


class TMBRIDGE_OT_MakeBlocksEditable(bpy.types.Operator):
    """Replace selected collection instances with independent editable mesh copies"""

    bl_idname = "trackmania_blender_bridge.make_blocks_editable"
    bl_label = "Make Blocks Editable"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        from . import _progress

        return (
            context.mode == "OBJECT" and not _progress.active()
            and any(_is_instance(obj) and obj.is_editable for obj in context.selected_objects)
        )

    def execute(self, context):
        selected = [obj for obj in context.selected_objects if _is_instance(obj) and obj.is_editable]
        # Attached clip instances belong to the selected block, even when not selected.
        targets = set(selected)
        for obj in selected:
            targets.update(child for child in obj.children_recursive if _is_instance(child))
        staged = []
        created = []
        try:
            for target in targets:
                parts = list(_contents(target.instance_collection, target.matrix_world))
                if not any(source.type == "MESH" for source, _ in parts):
                    self.report({"WARNING"}, f"{target.name}: no mesh geometry to convert")
                    continue
                copies = []
                for source, world in parts:
                    obj = source.copy()
                    created.append(obj)
                    if source.data is not None:
                        obj.data = source.data.copy()
                    obj.parent = None
                    obj.matrix_world = world
                    obj.name = f"{target.name} {source.name}"
                    copies.append(obj)
                staged.append((target, copies))
        except Exception as exc:
            log.exception("Could not make selected blocks editable")
            for obj in created:
                data = obj.data
                bpy.data.objects.remove(obj)
                if data is not None and data.users == 0:
                    bpy.data.batch_remove([data])
            self.report({"ERROR"}, f"Conversion failed: {exc}")
            return {"CANCELLED"}

        if not staged:
            self.report({"WARNING"}, "No selected blocks contain mesh geometry")
            return {"CANCELLED"}
        active = context.view_layer.objects.active
        replacement_active = None
        for target, copies in staged:
            for obj in copies:
                # Preserve the selected placement's metadata after realizing its geometry.
                for key in target.keys():
                    if key.startswith("tm_"):
                        obj[key] = target[key]
                if target.instance_collection is not None:
                    placement = target.instance_collection.get("tm_item_placement")
                    if "tm_item_placement" not in obj and placement is not None:
                        obj["tm_item_placement"] = placement
                    source = target.get("tm_source_path") or target.instance_collection.get("tm_source_path")
                    if source:
                        obj["tm_source_path"] = source
                for collection in target.users_collection:
                    collection.objects.link(obj)
                obj.hide_viewport = target.hide_viewport or obj.hide_viewport
                obj.hide_render = target.hide_render or obj.hide_render
            if target == active:
                replacement_active = next(obj for obj in copies if obj.type == "MESH")
        # Preserve other children (for example lights) when removing their parent.
        for target, _copies in staged:
            for child in list(target.children):
                world = child.matrix_world.copy()
                child.parent = target.parent
                child.matrix_world = world
        for obj in context.selected_objects:
            obj.select_set(False)
        for target, copies in staged:
            bpy.data.objects.remove(target, do_unlink=True)
            for obj in copies:
                obj.select_set(True)
        if replacement_active is None:
            replacement_active = next(obj for _, copies in staged for obj in copies if obj.type == "MESH")
        context.view_layer.objects.active = replacement_active
        self.report({"INFO"}, f"Made {len(staged)} block/clip instances editable")
        return {"FINISHED"}


def _github_repository():
    """``(owner, repository)`` of the configured GitHub URL, or None."""
    if not GITHUB_REPOSITORY_URL:
        return None
    try:
        parsed = urllib.parse.urlparse(GITHUB_REPOSITORY_URL)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repository
    ):
        return None
    return owner, repository


def _github_release_api_url():
    repo = _github_repository()
    if repo is None:
        return None
    # The list endpoint, unlike /releases/latest, includes pre-releases.
    return f"https://api.github.com/repos/{repo[0]}/{repo[1]}/releases?per_page=100"


def _parse_release_version(tag):
    if not isinstance(tag, str):
        return None
    match = _RELEASE_VERSION.fullmatch(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def _release_zip(release, repo):
    """``(name, url)`` of the release's addon ZIP asset hosted by ``repo``, or None."""
    prefix = f"https://github.com/{repo[0]}/{repo[1]}/releases/download/".lower()
    for asset in release.get("assets") or ():
        if not isinstance(asset, dict):
            continue
        name, url = asset.get("name"), asset.get("browser_download_url")
        if (
            isinstance(name, str) and _ADDON_ZIP.fullmatch(name)
            and isinstance(url, str) and url.lower().startswith(prefix)
        ):
            return name, url
    return None


def _newest_release(releases, include_prereleases=True, repo=None):
    """Return the highest published release as a dict, or None.

    Drafts are never offered; pre-releases only with ``include_prereleases``.
    """
    repo = repo or _github_repository() or ("", "")
    newest = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        prerelease = bool(release.get("prerelease"))
        if prerelease and not include_prereleases:
            continue
        tag = release.get("tag_name")
        version = _parse_release_version(tag)
        if version is None or (newest is not None and version <= newest["version"]):
            continue
        notes = release.get("html_url")
        if not (isinstance(notes, str) and notes.lower().startswith(
                f"https://github.com/{repo[0]}/{repo[1]}/releases/".lower())):
            notes = f"https://github.com/{repo[0]}/{repo[1]}/releases"
        asset = _release_zip(release, repo)
        newest = {
            "tag": tag,
            "version": version,
            "prerelease": prerelease,
            "notes_url": notes,
            "zip_name": asset[0] if asset else None,
            "zip_url": asset[1] if asset else None,
        }
    return newest


def _release_kind(release):
    return "Pre-release" if release["prerelease"] else "Release"


# Result of the last update check, shared by the operators, the UI and the
# background startup check.
_update_state = {"update": None, "status": "", "checking": False}


def reset_update_state():
    _update_state.update(update=None, status="")


def _installed_version():
    from . import bl_info

    return tuple(bl_info["version"])


def _check_releases(include_prereleases):
    """Query GitHub; return ``(level, message, update)``. Safe off the main thread."""
    api_url = _github_release_api_url()
    if api_url is None:
        return "ERROR", "The configured GitHub repository URL is invalid.", None
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Nadeo-Exporter-CE",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            releases = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("GitHub release check returned HTTP %s", exc.code)
        return "ERROR", f"GitHub release check failed (HTTP {exc.code}).", None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("GitHub release check failed: %s", exc)
        return "ERROR", "Could not reach GitHub to check for releases.", None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("GitHub release response was not valid JSON: %s", exc)
        return "ERROR", "GitHub returned an invalid release response.", None

    if not isinstance(releases, list):
        return "ERROR", "GitHub returned an invalid release response.", None
    newest = _newest_release(releases, include_prereleases)
    if newest is None:
        kind = "GitHub release" if include_prereleases else "full GitHub release (pre-releases are off)"
        return "WARNING", f"No published {kind} with a version tag was found.", None

    current = ".".join(str(part) for part in _installed_version())
    if newest["version"] > _installed_version():
        message = f"{_release_kind(newest)} {newest['tag']} is available (installed: {current})."
        if not newest["zip_url"]:
            message += " It has no addon ZIP to install; see the release page."
        return "INFO", message, newest
    return "INFO", f"Up to date (latest: {newest['tag']}; installed: {current}).", None


def _include_prereleases(context):
    from . import ADDON_ID

    addon = context.preferences.addons.get(ADDON_ID)
    return bool(addon is not None and addon.preferences.include_prereleases)


def _redraw_ui():
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()


class TMBRIDGE_OT_CheckGitHubRelease(bpy.types.Operator):
    """Check GitHub for a release newer than the running addon"""

    bl_idname = OP + "check_github_release"
    bl_label = "Check for Updates"

    def execute(self, context):
        if not GITHUB_REPOSITORY_URL:
            self.report({"WARNING"}, "GitHub repository is not configured.")
            return {"CANCELLED"}
        level, message, update = _check_releases(_include_prereleases(context))
        _update_state.update(update=update, status=message)
        self.report({level}, message)
        return {"CANCELLED"} if level == "ERROR" or (level == "WARNING" and update is None) else {"FINISHED"}


_MAX_DOWNLOAD = 200 * 1024 * 1024


class TMBRIDGE_OT_InstallGitHubRelease(bpy.types.Operator):
    """Download the newer GitHub release and install it over this addon"""

    bl_idname = OP + "install_github_release"
    bl_label = "Install Update"

    @classmethod
    def poll(cls, _context):
        update = _update_state["update"]
        return bool(update and update["zip_url"])

    def invoke(self, context, _event):
        try:
            return context.window_manager.invoke_props_dialog(self, width=380, confirm_text="Install")
        except TypeError:  # Blender < 4.1 has no confirm_text
            return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, _context):
        update = _update_state["update"]
        layout = self.layout
        if update is None:
            layout.label(text="No update available.")
            return
        current = ".".join(str(part) for part in _installed_version())
        layout.label(text=f"Install {_release_kind(update)} {update['tag']}?", icon="IMPORT")
        layout.label(text=f"Installed version: {current}")
        if update["prerelease"]:
            layout.label(text="Pre-releases are previews and may contain bugs.", icon="ERROR")
        layout.label(text="The addon reloads in place; restart Blender afterwards.", icon="INFO")
        layout.operator("wm.url_open", text="Release Notes", icon="URL").url = update["notes_url"]

    def execute(self, context):
        import shutil
        import tempfile
        import zipfile
        from . import ADDON_ID, install_addon_zip

        update = _update_state["update"]
        addon = context.preferences.addons.get(ADDON_ID)
        if not update or not update["zip_url"] or addon is None:
            self.report({"ERROR"}, "No installable update is available; check for updates first.")
            return {"CANCELLED"}

        folder = tempfile.mkdtemp(prefix="nadeo_exporter_ce_update_")
        path = os.path.join(folder, update["zip_name"])
        window = context.window
        if window is not None:
            window.cursor_set("WAIT")
        try:
            request = urllib.request.Request(
                update["zip_url"], headers={"User-Agent": "Nadeo-Exporter-CE"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(_MAX_DOWNLOAD + 1)
            if len(data) > _MAX_DOWNLOAD:
                raise ValueError("download is unexpectedly large")
            with open(path, "wb") as handle:
                handle.write(data)
            with zipfile.ZipFile(path) as archive:
                if f"{ADDON_ID}/__init__.py" not in archive.namelist():
                    raise ValueError("the ZIP does not contain this addon")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, zipfile.BadZipFile) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            log.warning("Downloading %s failed: %s", update["zip_url"], exc)
            self.report({"ERROR"}, f"Download of {update['tag']} failed: {exc}")
            return {"CANCELLED"}
        finally:
            if window is not None:
                window.cursor_set("DEFAULT")

        log.info("Installing %s from %s", update["tag"], update["zip_url"])
        install_addon_zip(addon, path, cleanup=lambda: shutil.rmtree(folder, ignore_errors=True))
        reset_update_state()
        self.report(
            {"INFO"},
            f"Installing {update['tag']}... Restart Blender afterwards to finish the update.",
        )
        return {"FINISHED"}


_startup_thread = None


def _poll_startup_check():
    if _startup_thread is not None and _startup_thread.is_alive():
        return 0.5
    _redraw_ui()
    return None


def _startup_check():
    """Check once for updates after Blender starts, without blocking the UI."""
    import threading

    global _startup_thread
    if bpy.app.background or _github_release_api_url() is None:
        return None
    include = _include_prereleases(bpy.context)

    def run():
        _update_state["checking"] = True
        try:
            level, message, update = _check_releases(include)
            # Network failures at startup stay silent; the user can retry.
            if level != "ERROR":
                _update_state.update(update=update, status=message if update else "")
        finally:
            _update_state["checking"] = False

    _startup_thread = threading.Thread(target=run, name="nadeo-exporter-ce-update-check", daemon=True)
    _startup_thread.start()
    bpy.app.timers.register(_poll_startup_check, first_interval=0.5)
    return None


def register():
    if not bpy.app.timers.is_registered(_startup_check):
        bpy.app.timers.register(_startup_check, first_interval=5.0, persistent=True)


def unregister():
    for timer in (_startup_check, _poll_startup_check):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)


def draw_github_actions(layout, *, header=False):
    row = layout.row(align=True)
    configured = _github_release_api_url() is not None
    row.enabled = configured and not _update_state["checking"]
    update = _update_state["update"] if configured else None
    if update and update["zip_url"]:
        row.alert = True
        text = "" if header else f"Install {update['tag']} ({_release_kind(update)})"
        row.operator(OP + "install_github_release", text=text, icon="IMPORT")
    elif header:
        row.operator(OP + "check_github_release", text="", icon="FILE_REFRESH")
    else:
        text = "Checking..." if _update_state["checking"] else "Check for Updates"
        row.operator(OP + "check_github_release", text=text, icon="FILE_REFRESH")
    if header:
        open_repo = row.operator("wm.url_open", text="", icon="URL")
    elif update:
        open_repo = row.operator("wm.url_open", text="Release Notes", icon="URL")
    else:
        open_repo = row.operator("wm.url_open", text="Open GitHub Repository", icon="URL")
    open_repo.url = update["notes_url"] if update else GITHUB_REPOSITORY_URL
    if not configured and not header:
        message = (
            "GitHub repository not configured"
            if not GITHUB_REPOSITORY_URL
            else "GitHub repository URL is invalid"
        )
        layout.label(text=message, icon="INFO")


def draw_update_status(layout):
    """Show the result of the last update check, if any."""
    status = _update_state["status"]
    if status:
        layout.label(text=status, icon="IMPORT" if _update_state["update"] else "INFO")


def draw_build_upgrade_button(layout):
    from . import ADDON_ID, _check_upgrade_available

    addon = bpy.context.preferences.addons.get(ADDON_ID)
    prefs = addon.preferences if addon is not None else None
    build_dir = bpy.path.abspath(prefs.build_dir) if prefs is not None and prefs.build_dir else ""
    available = _check_upgrade_available()
    row = layout.row(align=True)
    row.alert = available
    row.enabled = bool(build_dir) and os.path.isdir(build_dir)
    row.operator(
        OP + "upgrade_from_build",
        text="",
        icon="ERROR" if available else "FILE_REFRESH",
        emboss=True,
    )


def _tool_section(layout, key, title, icon, *, default_closed):
    panel = getattr(layout, "panel", None)
    if panel is not None:
        header, body = panel(f"tmbridge_tools_{key}", default_closed=default_closed)
        header.label(text=title, icon=icon)
        return body
    body = layout.box()
    body.label(text=title, icon=icon)
    return body


class TMBRIDGE_PT_Tools(bpy.types.Panel):
    bl_idname = "TMBRIDGE_PT_tools"
    bl_label = "Tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_order = 0
    bl_ui_units_x = 22

    def draw_header_preset(self, _context):
        draw_github_actions(self.layout, header=True)

    def draw(self, context):
        from . import ADDON_ID, _openplanet_bridge

        addon = context.preferences.addons.get(ADDON_ID)
        prefs = addon.preferences if addon is not None else None

        body = _tool_section(
            self.layout, "game_library", "Game Library", "FILE_FOLDER", default_closed=False
        )
        if body is not None:
            _openplanet_bridge.draw_game_library(body, prefs)

        body = _tool_section(
            self.layout, "batch_processor", "Batch Processor", "MODIFIER", default_closed=True
        )
        if body is not None:
            body.enabled = False
            body.label(text="Disabled until GBX writing is supported.", icon="INFO")

        body = _tool_section(
            self.layout, "viewport_options", "Viewport Options", "OVERLAY", default_closed=True
        )
        if body is not None:
            body.prop(
                context.space_data.overlay,
                "show_extras",
                text="Show Empty Axes & Light Handles",
            )
            body.label(text="Also affects cameras and other object guides.", icon="INFO")

        self.layout.operator(
            OP + "inspector_open", text="Open GBX Inspector", icon="VIEWZOOM"
        )


CLASSES = (
    TMBRIDGE_OT_CheckGitHubRelease,
    TMBRIDGE_OT_InstallGitHubRelease,
    TMBRIDGE_OT_MakeBlocksEditable,
    TMBRIDGE_PT_Tools,
)
