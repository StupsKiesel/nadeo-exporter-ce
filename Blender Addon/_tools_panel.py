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


def _github_release_api_url():
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
    # The list endpoint, unlike /releases/latest, includes pre-releases.
    return f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=100"


def _parse_release_version(tag):
    if not isinstance(tag, str):
        return None
    match = _RELEASE_VERSION.fullmatch(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def _newest_release(releases):
    """Return ``(tag, version)`` of the highest published release, or None."""
    newest = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name")
        version = _parse_release_version(tag)
        if version is not None and (newest is None or version > newest[1]):
            newest = (tag, version)
    return newest


class TMBRIDGE_OT_CheckGitHubRelease(bpy.types.Operator):
    """Check GitHub for a release newer than the running addon"""

    bl_idname = OP + "check_github_release"
    bl_label = "Check for Updates"

    def execute(self, _context):
        if not GITHUB_REPOSITORY_URL:
            self.report({"WARNING"}, "GitHub repository is not configured.")
            return {"CANCELLED"}
        api_url = _github_release_api_url()
        if api_url is None:
            self.report({"ERROR"}, "The configured GitHub repository URL is invalid.")
            return {"CANCELLED"}

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
            self.report({"ERROR"}, f"GitHub release check failed (HTTP {exc.code}).")
            return {"CANCELLED"}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("GitHub release check failed: %s", exc)
            self.report({"ERROR"}, "Could not reach GitHub to check for releases.")
            return {"CANCELLED"}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("GitHub release response was not valid JSON: %s", exc)
            self.report({"ERROR"}, "GitHub returned an invalid release response.")
            return {"CANCELLED"}

        if not isinstance(releases, list):
            self.report({"ERROR"}, "GitHub returned an invalid release response.")
            return {"CANCELLED"}
        newest = _newest_release(releases)
        if newest is None:
            self.report({"WARNING"}, "No published GitHub release with a version tag was found.")
            return {"CANCELLED"}
        tag, latest_version = newest

        from . import bl_info

        current_version = tuple(bl_info["version"])
        current_tag = ".".join(str(part) for part in current_version)
        if latest_version > current_version:
            self.report(
                {"INFO"},
                f"New GitHub release {tag} is available (installed: {current_tag}).",
            )
        else:
            self.report(
                {"INFO"},
                f"No newer GitHub release found (latest: {tag}; installed: {current_tag}).",
            )
        return {"FINISHED"}


def draw_github_actions(layout, *, header=False):
    row = layout.row(align=True)
    configured = _github_release_api_url() is not None
    row.enabled = configured
    if header:
        row.operator(OP + "check_github_release", text="", icon="FILE_REFRESH")
        open_repo = row.operator("wm.url_open", text="", icon="URL")
    else:
        row.operator(OP + "check_github_release", text="Check for Updates", icon="FILE_REFRESH")
        open_repo = row.operator("wm.url_open", text="Open GitHub Repository", icon="URL")
    open_repo.url = GITHUB_REPOSITORY_URL
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


CLASSES = (TMBRIDGE_OT_CheckGitHubRelease, TMBRIDGE_OT_MakeBlocksEditable, TMBRIDGE_PT_Tools)
