"""User-facing Tools panel and collection-instance editing operator."""

import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import bpy
from mathutils import Matrix, Vector
from . import ADDON_SHORT_NAME

log = logging.getLogger("trackmania_blender_bridge.tools")
OP = "trackmania_blender_bridge."
GITHUB_REPOSITORY_URL = "https://github.com/StupsKiesel/nadeo-exporter-ce"
_RELEASE_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")
_ADDON_ZIP = re.compile(r"nadeo_exporter_ce_\d+\.\d+\.\d+\.zip")


def _is_instance(obj):
    return obj.instance_type == "COLLECTION" and obj.instance_collection is not None


_CLIP_FILE = re.compile(r"\.ED(?:Horizontal|Vertical)?Clip\.Gbx$", re.IGNORECASE)
# Grid (square, height) of the main map sections Blocks and BakedBlocks.
_GRID = (32.0, 8.0)
# Unresolved baked clips join the nearest authored block within this distance.
_NEAREST_CLIP_OWNER_M = 64.0


def _section(obj):
    return next((owner["tm_section"] for owner in obj.users_collection if "tm_section" in owner), None)


def _sections():
    """``{object: section}`` for all section members.

    ``Object.users_collection`` scans every collection in the file, which takes
    seconds when called for each placement of a large map.
    """
    sections = {}
    for collection in bpy.data.collections:
        section = collection.get("tm_section")
        if section is not None:
            for obj in collection.objects:
                sections.setdefault(obj, section)
    return sections


def _is_baked_clip(obj):
    if "tm_block_is_clip" in obj:
        return bool(obj["tm_block_is_clip"])
    return bool(_CLIP_FILE.search(str(obj.get("tm_source_path", ""))))


def _grid_cells(obj):
    """Grid cells covered by a grid placement."""
    raw = obj.get("tm_block_cells")
    if raw is not None:
        values = [int(value) for value in raw]
        return [tuple(values[i:i + 3]) for i in range(0, len(values) - 2, 3)]
    coord = tuple(int(value) for value in obj["tm_block_coord"])
    if not _is_instance(obj):
        return [coord]
    # Scenes imported before the footprint was stored: use the geometry bounds.
    try:
        corners = [world @ Vector(corner) for source, world in _contents(obj.instance_collection, obj.matrix_world)
                   if source.type == "MESH" for corner in source.bound_box]
    except ValueError:
        corners = []
    if not corners:
        return [coord]
    square, height, inset = *_GRID, 0.5
    xs = range(math.floor((min(c.x for c in corners) + inset) / square),
               math.floor((max(c.x for c in corners) - inset) / square) + 1)
    ys = range(math.floor((min(c.z for c in corners) + inset) / height),
               math.floor((max(c.z for c in corners) - inset) / height) + 1)
    zs = range(math.floor(-(max(c.y for c in corners) - inset) / square),
               math.floor(-(min(c.y for c in corners) + inset) / square) + 1)
    return [(x, y, z) for x in xs for y in ys for z in zs] or [coord]


def _nearest_block(clip, blocks):
    location = clip.matrix_world.translation
    square, height = _GRID
    best, distance = None, _NEAREST_CLIP_OWNER_M
    for block, cells in blocks:
        for x, y, z in cells:
            center = Vector(((x + 0.5) * square, -(z + 0.5) * square, (y + 0.5) * height))
            if (center - location).length < distance:
                best, distance = block, (center - location).length
    return best


def _baked_clip_owners(context, map_id, sections):
    """``{baked clip: owning authored block or None}`` for the displayed clips of one map."""
    from GBXBPY.map_import_v4.inherited_modifiers import baked_owner

    blocks, clips = [], []
    for obj in context.scene.objects:
        if (obj.parent is not None or obj.get("tm_map_root") != map_id
                or "tm_block_coord" not in obj or obj.get("tm_block_is_free")):
            continue
        section = sections.get(obj)
        if section == "Blocks" and not _is_baked_clip(obj):
            blocks.append((obj, _grid_cells(obj)))
        elif section == "BakedBlocks" and _is_baked_clip(obj) and obj.visible_get(view_layer=context.view_layer):
            clips.append(obj)
    owners = {}
    for block, cells in blocks:
        for cell in cells:
            owners.setdefault(cell, block)
    owner_of = {}
    for clip in clips:
        cell = tuple(int(value) for value in clip["tm_block_coord"])
        owner_of[clip] = (baked_owner(cell, str(clip.get("tm_block_dir", "North")), owners)
                          or _nearest_block(clip, blocks))
    return owner_of


def _block_group(context, target, cache):
    """``(owner, clips)``: the authored block a placement belongs to and its displayed baked clips."""
    map_id = target.get("tm_map_root")
    if map_id is None or "tm_block_coord" not in target or target.get("tm_block_is_free"):
        return target, []
    if None not in cache:
        cache[None] = _sections()
    section = cache[None].get(target)
    if section not in {"Blocks", "BakedBlocks"} or (section == "BakedBlocks" and not _is_baked_clip(target)):
        return target, []
    if map_id not in cache:
        cache[map_id] = _baked_clip_owners(context, map_id, cache[None])
    owner_of = cache[map_id]
    owner = owner_of.get(target, target)
    if owner is None:
        return target, []
    return owner, [clip for clip, clip_owner in owner_of.items() if clip_owner == owner]


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
    """Replace selected placements and all clips of their block with editable mesh copies in one collection"""

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

    @staticmethod
    def _groups(context):
        """``[(owner, targets)]``: each selected block with its attached and baked clips."""
        groups = []
        claimed = set()
        cache = {}
        for obj in context.selected_objects:
            if not (_is_instance(obj) and obj.is_editable):
                continue
            # A selected attached clip belongs to its block.
            while "tm_clip_source" in obj and obj.parent is not None and _is_instance(obj.parent):
                obj = obj.parent
            owner, baked = _block_group(context, obj, cache)
            if owner in claimed:
                continue
            claimed.add(owner)
            targets = []
            for member in (owner, *baked):
                for candidate in (member, *member.children_recursive):
                    if (_is_instance(candidate) and candidate.is_editable
                            and (candidate == owner or candidate not in claimed)):
                        claimed.add(candidate)
                        targets.append(candidate)
            if targets:
                groups.append((owner, targets))
        return groups

    def execute(self, context):
        from . import _progress

        with _progress.task("Make Editable") as progress:
            return self._make_editable(context, progress)

    def _make_editable(self, context, progress):
        progress(0.0, "Finding block clips")
        groups = self._groups(context)
        total = sum(len(targets) for _owner, targets in groups) or 1
        done = 0
        staged = []
        created = []
        try:
            for owner, targets in groups:
                converted = []
                for target in targets:
                    done += 1
                    progress(0.1 + 0.7 * done / total, f"Copying {target.name}")
                    parts = list(_contents(target.instance_collection, target.matrix_world))
                    if not any(source.type == "MESH" for source, _ in parts):
                        if target == owner:
                            self.report({"WARNING"}, f"{target.name}: no mesh geometry to convert")
                        else:
                            log.debug("%s: clip without mesh geometry stays an instance", target.name)
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
                    converted.append((target, copies))
                if converted:
                    staged.append((owner, converted))
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
        progress(0.8, "Building collections")
        active = context.view_layer.objects.active
        replacement_active = None
        for owner, converted in staged:
            bundle = bpy.data.collections.new(f"{owner.name} (Editable)")
            # The bundle carries the block's identity for the asset panels.
            for key in owner.keys():
                if key.startswith("tm_"):
                    bundle[key] = owner[key]
            if "tm_source_path" not in bundle and owner.instance_collection is not None:
                source = owner.instance_collection.get("tm_source_path")
                if source:
                    bundle["tm_source_path"] = source
            parent = owner.users_collection[0] if owner.users_collection else context.scene.collection
            parent.children.link(bundle)
            for target, copies in converted:
                for obj in copies:
                    # Preserve each placement's metadata after realizing its geometry.
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
                    bundle.objects.link(obj)
                    obj.hide_viewport = target.hide_viewport or obj.hide_viewport
                    obj.hide_render = target.hide_render or obj.hide_render
                if target == active:
                    replacement_active = next(obj for obj in copies if obj.type == "MESH")
        targets = [target for _owner, converted in staged for target, _copies in converted]
        copies = [obj for _owner, converted in staged for _target, part in converted for obj in part]
        # Preserve other children (for example lights) when removing their parent.
        for target in targets:
            for child in list(target.children):
                if child in targets:
                    continue
                parent = target.parent
                while parent is not None and parent in targets:
                    parent = parent.parent
                world = child.matrix_world.copy()
                child.parent = parent
                child.matrix_world = world
        progress(0.9, "Removing instances")
        for obj in context.selected_objects:
            obj.select_set(False)
        bpy.data.batch_remove(targets)
        progress(1.0, "Complete")
        for obj in copies:
            obj.select_set(True)
        if replacement_active is None:
            replacement_active = next(obj for obj in copies if obj.type == "MESH")
        context.view_layer.objects.active = replacement_active
        self.report({"INFO"}, f"Made {len(targets)} block/clip instances editable in {len(staged)} collection(s)")
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
_update_state = {"update": None, "status": "", "level": "", "checking": False}


def reset_update_state():
    _update_state.update(update=None, status="", level="")


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
        _update_state.update(update=update, status=message, level=level)
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
                _update_state.update(update=update, status=message if update else "",
                                     level=level if update else "")
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


def draw_github_actions(layout, *, header=False, prefs=None):
    row = layout.row(align=True)
    configured = _github_release_api_url() is not None
    row.enabled = configured and not _update_state["checking"]
    update = _update_state["update"] if configured else None
    if prefs is not None:
        row.prop(prefs, "include_prereleases", text="Pre-releases")
    if update and update["zip_url"]:
        row.alert = True
        text = "" if header else f"Install {update['tag']} ({_release_kind(update)})"
        row.operator(OP + "install_github_release", text=text, icon="IMPORT")
    elif header:
        row.operator(OP + "check_github_release", text="", icon="FILE_REFRESH")
    else:
        if _update_state["checking"]:
            text = "Checking..."
        elif update:
            text = "No addon ZIP - Check again"
        elif _update_state["level"] == "ERROR":
            text = "Check failed - Retry"
        elif _update_state["level"] == "WARNING":
            text = "No releases - Check again"
        elif _update_state["status"]:
            text = "Up to date - Check again"
        else:
            text = "Check for Updates"
        row.operator(OP + "check_github_release", text=text, icon="FILE_REFRESH")
    if header:
        open_repo = row.operator("wm.url_open", text="", icon="URL")
    elif update:
        open_repo = row.operator("wm.url_open", text="Release Notes", icon="URL")
    else:
        open_repo = row.operator("wm.url_open", text="", icon="URL")
    open_repo.url = update["notes_url"] if update else GITHUB_REPOSITORY_URL
    if not configured and not header:
        message = (
            "GitHub repository not configured"
            if not GITHUB_REPOSITORY_URL
            else "GitHub repository URL is invalid"
        )
        layout.label(text=message, icon="INFO")


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
