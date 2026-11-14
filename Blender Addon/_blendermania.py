"""Optional BlenderMania stock-material metadata; never rebuild render nodes."""

from __future__ import annotations

from dataclasses import replace
import logging
import os
import sys

import bpy

log = logging.getLogger("trackmania_blender_bridge.blendermania")
_warned: set[str] = set()
_catalog_source = None
_catalog_paths: dict[str, tuple[str, dict]] = {}
_PROPERTIES = (
    "gameType", "baseTexture", "link", "physicsId", "usePhysicsId",
    "gameplayId", "useGameplayId", "environment",
)


def _warning(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        log.warning("%s", message)


def _enabled_catalog():
    for addon in bpy.context.preferences.addons:
        module = sys.modules.get(addon.module)
        info = getattr(module, "bl_info", {})
        if str(info.get("name", "")).casefold() != "blendermania":
            continue
        if not all(name in bpy.types.Material.bl_rna.properties for name in _PROPERTIES):
            _warning("enabled addon has an unsupported material property schema")
            return None
        props = bpy.types.Material.bl_rna.properties
        if ("Trackmania2020" not in props["gameType"].enum_items
                or "Stadium" not in props["environment"].enum_items):
            _warning("enabled addon does not support Trackmania2020/Stadium materials")
            return None
        constants = sys.modules.get(addon.module + ".utils.Constants")
        catalog = getattr(constants, "MATERIALS_MAP_TM2020", None)
        if not isinstance(catalog, dict) or not catalog:
            _warning("enabled addon has no Trackmania 2020 stock material catalog")
            return None
        return catalog
    return None


def _key(path: str) -> str:
    path = path.replace("\\", "/").strip("/").casefold()
    for suffix in (".material.gbx", ".gbx"):
        if path.endswith(suffix):
            return path[:-len(suffix)]
    return path


def _stock_paths(catalog) -> dict:
    global _catalog_source, _catalog_paths
    if catalog is not _catalog_source:
        _catalog_source = catalog
        paths = {}
        for link, entry in catalog.items():
            if not isinstance(link, str) or not isinstance(entry, dict):
                _warning("stock catalog contains invalid entries")
                continue
            params = entry.get("MatLibParams") or {}
            if not isinstance(params, dict):
                _warning(f"stock catalog has invalid MatLibParams for {link}")
                continue
            library = params.get("DLibrary", "")
            path = params.get("DLinkFull", "")
            if library == "Stadium" and isinstance(path, str) and path:
                paths[_key(library + "/" + path)] = (link, params)
        _catalog_paths = paths
    return _catalog_paths


def _stock_link(spec, binding, resolver, source_path, catalog):
    if binding is None:
        return None, "No game material binding (manual material)"
    if spec.family in {"Terrain.Layers", "Terrain.PyPxz", "Water.Vista", "Screen"}:
        return None, f"{spec.family} needs custom export support"
    if getattr(binding, "inline_textures", None) or getattr(binding, "inline_material", None):
        return None, "Embedded/custom material needs custom export support"
    if getattr(resolver, "_gbxmat_skin_context", None):
        return None, "Map sign/skin textures cannot be exported as a stock link"
    from GBXMAT.images import map_path

    if any(map_path(tex.path) != tex.path for tex in spec.textures):
        return None, "Texture-mod overrides cannot be exported as a stock link"
    if tuple(spec.color) != (1.0, 1.0, 1.0, 1.0):
        return None, "Per-instance tint cannot be exported as a stock link"
    if getattr(resolver, "_gbxmat_color_palette", None) not in (None, "", "Classic"):
        return None, "Map palette overrides cannot be exported as a stock link"

    from ._gbxmat_adapter import _load_linked_material

    linked = _load_linked_material(
        binding.material_path, resolver, source_path, terrain_remap=True,
    )
    effective = getattr(linked, "source_path", "") or binding.material_path
    if os.path.isabs(effective):
        root = getattr(resolver, "gamedata_dir", "")
        if not root:
            return None, "GameData root unavailable for stock-link verification"
        effective = os.path.relpath(effective, root)
    hit = _stock_paths(catalog).get(_key(effective))
    if hit is None:
        return None, f"No verified stock link for {effective or spec.name_hint}"
    return hit, ""


def get_or_create_material(spec, *, binding=None, resolver=None, source_path=""):
    """Use GBXMAT unchanged when absent; annotate verified links when enabled."""
    import GBXMAT

    catalog = _enabled_catalog()
    if catalog is None:
        return GBXMAT.get_or_create_material(spec)
    hit, reason = _stock_link(spec, binding, resolver, source_path, catalog)
    # BlenderMania's FBX metadata writer only accepts TM_/MP_ names.
    # Separate compatibility variants also keep different export links apart.
    link = hit[0] if hit else ""
    hint = f"TM_{spec.name_hint}__BM_{link or 'unsupported'}"
    mat = GBXMAT.get_or_create_material(replace(spec, name_hint=hint))
    if hit and mat.use_nodes and not any(
        name in mat.node_tree.nodes for name in ("Principled BSDF", "cus_color")
    ):
        hit, link = None, ""
        reason = "Render graph has no color node understood by BlenderMania's exporter"
    if mat.get("tm_blendermania_status") == "linked" and mat.link == link:
        return mat
    if mat.get("tm_blendermania_reason") == reason and not hit:
        return mat
    mat.gameType = "Trackmania2020"
    mat.environment = "Stadium"
    mat.baseTexture = ""
    mat.link = link
    mat.physicsId = spec.physics_id or ""
    mat.usePhysicsId = bool(spec.physics_id)
    mat.gameplayId = spec.gameplay_id or ""
    mat.useGameplayId = bool(spec.gameplay_id)
    mat["tm_blendermania_status"] = "linked" if hit else "unsupported"
    mat["tm_blendermania_reason"] = reason
    if reason:
        _warning(f"{spec.name_hint}: {reason}")
    return mat
