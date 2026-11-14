"""Adapter: GBXBPY :class:`MaterialBinding` -> GBXMAT :class:`MaterialSpec`.

Lives in the Blender addon (not in GBXBPY, not in GBXMAT) so that GBXMAT
stays free of project-specific imports and GBXBPY stays free of bpy.

Path resolution is performed here using a :class:`GBXBPY.Resolver`; the
absolute paths are then handed to GBXMAT, which only loads files from
disk.

Linked materials (``binding.material_path`` set, no inline textures) are
loaded via :func:`GBXBPY.load`. We walk the :class:`Material` and any
``base_material_path`` parents to collect a complete texture-slot set
before constructing the :class:`MaterialSpec`. Loaded materials are
cached per absolute path so repeated bindings share work.
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import replace
from typing import Iterable, Optional

import GBXBPY
from GBXBPY import library
from GBXBPY.model.funcshader import FuncShader
from GBXBPY.resolve import MaterialResolver
from GBXMAT import MaterialSpec, TextureRef


log = logging.getLogger("trackmania_blender_bridge.gbxmat_adapter")


def _material_resolver_for(resolver) -> Optional[MaterialResolver]:
    """Return a :class:`MaterialResolver` bound to ``resolver``'s GameData.

    Lazily created and attached as ``resolver._gbxmat_material_lookup`` so
    every binding in a single import shares one filesystem index.
    """
    if resolver is None:
        return None
    cached = getattr(resolver, "_gbxmat_material_lookup", None)
    if cached is not None:
        return cached
    gd = getattr(resolver, "gamedata_dir", "") or ""
    if not gd:
        return None
    lookup = MaterialResolver(gd)
    try:
        setattr(resolver, "_gbxmat_material_lookup", lookup)
    except Exception:  # noqa: BLE001 - resolver may forbid attribute set
        pass
    return lookup


# Map TM material slot names to canonical GBXMAT roles. Anything not
# listed is forwarded as-is (role == slot name) so unknown slots still
# round-trip into the Material datablock for diagnosis.
_SLOT_TO_ROLE = {
    "Diffuse":    "Diffuse",
    "BaseColor":  "Diffuse",
    "BaseColorA": "Diffuse",     # diffuse with meaningful alpha
    "FenceA":     "Diffuse",     # vegetation billboard card
    # Tech3 DecalPaint / triplanar materials split the diffuse into
    # per-projection slots. Either ``PyBaseColor`` (top projection) or
    # ``PxzBaseColor`` (side projection) is the one painted onto the
    # decal quad — both surface as Diffuse so the builder picks
    # whichever the binding actually fills.
    "PyBaseColor":  "Diffuse",
    "PxzBaseColor": "Diffuse",
    "Normal":     "Normal",
    "NormalMap":  "Normal",
    "PyNormal":   "Normal",
    "PxzNormal":  "Normal",
    "RoughMetal": "RoughMetal",
    "Roughness":  "RoughMetal",
    "PyRoughMetal":  "RoughMetal",
    "PxzRoughMetal": "RoughMetal",
    "SelfIllum":  "Emissive",
    "Emissive":   "Emissive",
    "Specular":   "Specular",
    "Detail":     "Detail",
    # TBaseColor in Tech3 PyPxz* triplanar materials is the "third
    # projection" overlay (e.g. dark wood stripes on TrackWall). We have
    # no full triplanar pipeline yet, so we surface it as a Detail-style
    # multiply overlay over the diffuse — close enough to match the
    # darkening effect on the dominant view of these materials.
    "TBaseColor": "Detail",
    "LightMap":   "Lightmap",
    "Lightmap":   "Lightmap",
    "Occlusion":  "Occlusion",
    "AO":         "Occlusion",
    "HueMask":    "HueMask",
    "BaseColorHueMask": "HueMask",
    # Py/Pxz hue masks of Tech3 Block PyPxz* materials (TrackWall); they
    # become ``HueMask`` only when ``_projected_hue_params`` finds targets.
    "PyBaseColorHueMask":  "ProjectedHueMask",
    "PxzBaseColorHueMask": "ProjectedHueMask",
    # Decal materials store the visible-shape cutout in a separate
    # ``Mask`` slot — the Diffuse RGBA is the full logo color but the
    # actual transparency comes from the mask's alpha/luminance.
    "Mask":       "Mask",
    # Tech3 DecalPaint exposes the cutout as ``TOpacity`` (single-channel
    # opacity), and Tech3 multi-layer materials carry an
    # ``OpacityMask`` slot — both feed the same Mask role so the decal
    # builder can wire alpha correctly.
    "TOpacity":   "Mask",
    "OpacityMask": "Mask",
    "Opacity":    "Mask",
    # Tech3 ``*_DispIn`` screen materials (see :data:`_SCREEN_FAMILY`).
    "BaseColorOp": "Diffuse",
    "SI":          "Emissive",
    "MulInside":   "Inside",
    "MulInsideForeground": "InsideForeground",
    "Height":      "Height",
    # Tech3 Block WaterWall (GlassWaterWall / GlassWaterStart).
    "Transmittance": "Transmittance",
    "FoamOpacity":   "FoamOpacity",
}

# Screen slots that never carry content (always the transparent
# ``Techno3/Media/Texture/Rgba0000`` placeholder in stock materials).
_IGNORED_SLOTS = frozenset({"MulInside1", "MulInsideForeground1"})
_PLACEHOLDER_STEMS = frozenset({"rgba0000"})
_SCREEN_FAMILY = "Screen"


# Higher number = preferred when multiple slots map to the same role.
# FenceA wins over BaseColorA wins over plain Diffuse / BaseColor so a
# vegetation billboard card takes priority over the underlying ground
# diffuse declared on the same material chain.
_SLOT_PRIORITY = {
    "FenceA":     30,
    "BaseColorA": 20,
    "Diffuse":    10,
    "BaseColor":  10,
}


# Filename-suffix fallback when the slot name is unknown / numeric.
# Match is performed on the texture's basename (stripped of extension)
# in lower case, looking for ``_<suffix>`` at the end.
_SUFFIX_TO_ROLE: tuple[tuple[str, str], ...] = (
    ("_d",  "Diffuse"),
    ("_di", "Diffuse"),
    ("_bc", "Diffuse"),
    ("_n",  "Normal"),
    ("_nm", "Normal"),
    ("_r",  "RoughMetal"),
    ("_rm", "RoughMetal"),
    ("_mr", "RoughMetal"),
    ("_s",  "Specular"),
    ("_sp", "Specular"),
    ("_e",  "Emissive"),
    ("_em", "Emissive"),
    ("_si", "Emissive"),
    ("_lm", "Lightmap"),
    ("_ao", "Occlusion"),
    ("_o",  "Occlusion"),
    ("_h",  "HueMask"),
    ("_det","Detail"),
)


def _role_from_filename(path: str) -> Optional[str]:
    """Infer a canonical role from the texture filename's suffix."""
    if not path:
        return None
    base = os.path.basename(path.replace("\\", "/"))
    stem = base.split(".", 1)[0].lower()
    for suffix, role in _SUFFIX_TO_ROLE:
        if stem.endswith(suffix):
            return role
    return None


# Keyword -> family routing. First match wins; case-insensitive on the
# material/shader name. Anything that doesn't match falls through to
# "Stadium.Default".
#
# ORDER MATTERS — longer / more-specific keywords before short ones that
# are substrings of the former (e.g. "tdiffablend" before "tdiffa", and
# "lightmap" before "light").
_RENDER_MODE_RULES: tuple[tuple[str, str], ...] = (
    # ---- render-mode specific (checked before generic keywords) --------
    # Additive: TAdd*, TAddLighted, TAddShield, TAddSwitch
    ("tadd",         "Additive"),
    # Alpha-blend: TDiffABlend_*, TDiffA_* (semi-transparent, depth-sorted)
    ("tdiffablend",  "AlphaBlend"),
    ("tdiffa",       "AlphaBlend"),
)
_FAMILY_RULES: tuple[tuple[str, str], ...] = _RENDER_MODE_RULES + (
    # ---- material-type keywords ----------------------------------------
    # Tech3 GrassFence_VDepLight: "light" would otherwise make it emissive.
    ("grassfence",   "GrassFence"),
    ("decal",        "Decal"),
    ("glass",        "Glass"),
    ("water",        "Glass"),
    # lightmap MUST precede "light" (substring relationship)
    ("lightmap",     "LightmapBaked"),
    # selfillum MUST precede "light" (TSelfIllum shaders)
    ("selfillum",    "LightEmissive"),
    ("light",        "LightEmissive"),
    ("neon",         "LightEmissive"),
    ("emissive",     "LightEmissive"),
    ("triplanar",    "TriPlanar"),
    ("terrain",      "TriPlanar"),
)

# Opaque ground / shore / border materials named "Water*" (Stadium
# WaterGround, RedIsland Waterground, WhiteShore WaterBottom/WaterShore*).
_NOT_WATER_SURFACE: tuple[str, ...] = ("waterground", "waterbottom", "watershore", "waterborder")


# Cache: abs path -> loaded GBXBPY Material (or None if load failed).
_LINKED_MATERIAL_CACHE: dict[str, object] = {}


def _name_hint_from_binding(binding) -> str:
    raw = binding.material_path or "Material"
    base = os.path.basename(raw.replace("\\", "/")) or "Material"
    for ext in (".Material.Gbx", ".material.gbx"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base or "Material"


def _family_from_binding(
    binding, linked_material=None, resolver=None
) -> tuple[str, str]:
    """Pick a shader family and extract the shader name from the material chain.

    Walks ``binding.material_path``, the loaded ``linked_material``, and
    every ``base_material_path`` ancestor to build a list of candidate
    strings. Returns ``(family, shader_name)`` where ``shader_name`` is
    the stem of the deepest base material (the TM "Tech3_*" shader-level
    material) and ``family`` is the first keyword match against
    :data:`_FAMILY_RULES`.

    The per-mesh ``binding.shader_model`` (read from CPlugMaterialUserInst)
    is checked **first** when present — it's the game's own internal name
    for the shader (e.g. ``"TDiffABlend"``, ``"TAdd"``, ``"TSelfIllum"``)
    and is far more reliable than guessing from filenames.
    """
    candidates: list[str] = []
    # Top-priority: the game's own shader-model identifier.
    shader_model = getattr(binding, "shader_model", "") or ""
    if shader_model:
        candidates.append(shader_model)
    if binding.material_path:
        candidates.append(binding.material_path)

    # Walk the full base-material chain for keyword checking. The deepest
    # item in the chain is the "shader material" (e.g.
    # Tech3_Block_DecalGeom_TOp_PyPxzDSN_X2) whose name encodes the
    # render mode.
    chain_names: list[str] = []
    if linked_material is not None:
        for mat in _walk_material_and_parents(linked_material, resolver):
            for attr in ("source_path", "name"):
                val = getattr(mat, attr, "") or ""
                if val:
                    chain_names.append(val)

    all_candidates = candidates + chain_names

    # Extract shader name: the binding's shader_model (when set) is the
    # canonical answer; otherwise fall back to the deepest chain entry.
    shader_name = ""
    if shader_model:
        shader_name = shader_model
    elif chain_names:
        raw = chain_names[-1]
        base = os.path.basename(raw.replace("\\", "/"))
        low = base.lower()
        for ext in (".material.gbx", ".shader.gbx", ".material", ".shader"):
            if low.endswith(ext):
                base = base[: -len(ext)]
                break
        shader_name = base

    for keyword, family in _RENDER_MODE_RULES:
        if keyword in shader_name.lower():
            return family, shader_name

    # Opaque Tech3 block PBR shaders named "*Water*" (Stadium Underwater
    # pool tiles) are tiles, not a water surface.
    deepest_key = ""
    if chain_names:
        deepest_key = "".join(
            ch for ch in os.path.basename(chain_names[-1].replace("\\", "/")).lower()
            if ch.isalnum()
        )
    opaque_block_shader = deepest_key.startswith("tech3blocktdsn")

    for cand in all_candidates:
        # Folder names must not pick the family: vista copies live in
        # "...\Modifier\StadiumOnTerrain\" and are ordinary UV materials.
        low = os.path.basename(cand.replace("\\", "/")).lower() or cand.lower()
        for keyword, family in _FAMILY_RULES:
            if keyword in low:
                if keyword == "water" and (
                    opaque_block_shader
                    or any(m in low for m in _NOT_WATER_SURFACE)
                ):
                    continue
                return family, shader_name

    # Note: we deliberately do NOT promote a material to ``TriPlanar`` just
    # because it has ``Py*`` / ``Pxz*`` slot names. Tech3 PyPxz materials
    # are routinely used on regular UV-unwrapped geometry (skin overlays,
    # platform variants) — the per-projection slots are flattened to
    # canonical Diffuse/Normal/RoughMetal by ``_SLOT_TO_ROLE`` and the
    # default Stadium builder handles them correctly. Forcing TriPlanar
    # here was making the majority of those materials render incorrectly.
    # True triplanar is selected via the ``triplanar`` / ``terrain``
    # keyword rules above.

    return "Stadium.Default", shader_name


def _modifier_folders_for(resolver) -> tuple[str, ...]:
    """Return the active material-modifier remap folders, if any.

    The block-import operator stashes resolved ``RemapFolder`` strings
    on the resolver as ``_gbxmat_modifier_folders`` before kicking off
    a build. Returns an empty tuple when nothing is set or the resolver
    is missing.
    """
    if resolver is None:
        return ()
    raw = getattr(resolver, "_gbxmat_modifier_folders", None)
    if not raw:
        return ()
    return tuple(f for f in raw if f)


def _modifier_allows(material_path: str, folder: str, resolver) -> bool:
    """Whether the modifier owning ``folder`` remaps ``material_path``.

    Each modifier's GameSkin lists the stock materials it redirects; a
    folder may also contain copies the modifier must not use (the
    ``PlatformGrass`` folder's ``PlatformTech`` under ``TrackWallToDecoCliff``).
    """
    filters = getattr(resolver, "_gbxmat_modifier_materials", None) or {}
    allowed = filters.get(folder.casefold())
    if allowed is None:
        return True
    names = {n.casefold() for n in allowed}
    stem = os.path.basename(material_path.replace("\\", "/")).split(".", 1)[0].casefold()
    leaf = folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
    stripped = stem[len(leaf) + 1:] if leaf and stem.startswith(leaf + "_") else stem
    return stem in names or stripped in names


def _modifier_renamed(material_path: str, folder: str, resolver) -> str:
    """Remap file stem a modifier's GameSkin gives ``material_path``, or ``""``.

    Item modifiers store per-colour copies under new names
    (``ItemObstacle`` -> ``ItemObstacleLevel2.Material.Gbx``).
    """
    renames = (getattr(resolver, "_gbxmat_modifier_renames", None) or {}).get(folder.casefold())
    if not renames:
        return ""
    stem = os.path.basename(material_path.replace("\\", "/")).split(".", 1)[0].casefold()
    leaf = folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
    stripped = stem[len(leaf) + 1:] if leaf and stem.startswith(leaf + "_") else stem
    return renames.get(stem) or renames.get(stripped) or ""


def _modifier_material_basename(material_path: str) -> str:
    """Reduce ``material_path`` to ``<stem>.Material.Gbx`` for folder lookup."""
    if not material_path:
        return ""
    base = os.path.basename(material_path.replace("\\", "/"))
    if not base:
        return ""
    low = base.lower()
    if low.endswith(".material.gbx"):
        return base
    # Drop any other extension before re-appending the canonical one.
    dot = base.find(".")
    stem = base[:dot] if dot > 0 else base
    return stem + ".Material.Gbx"


def _resolve_in_modifier_folder(
    folder: str,
    base_filename: str,
    resolver,
) -> Optional[str]:
    """Try ``<folder>/<base_filename>`` against the resolver's roots."""
    if not folder or not base_filename or resolver is None:
        return None
    folder = folder.replace("\\", "/").strip("/")
    candidate = f"{folder}/{base_filename}" if folder else base_filename
    try:
        return resolver.resolve(candidate)
    except Exception as e:  # noqa: BLE001
        log.debug("modifier-folder resolve failed for %r: %s", candidate, e)
        return None


def _modifier_name_candidates(material_path: str, folder: str) -> list[str]:
    """Return material-name keys to try inside a modifier folder.

    Trackmania mesh bindings often store *synthesised* material names
    that aren't direct filenames inside the modifier folder. Three
    transforms recover the real key:

    1. **As-is** — ``<name>`` matches when the modifier folder ships a
       per-name override (e.g. ``PlatformDirt/PlatformTech.Material.Gbx``
       for a binding called ``PlatformTech``).
    2. **Strip folder prefix** — ``<FolderLeaf>_<Stem>`` is a common
       prefab convention (e.g. binding ``PlatformDirt_PlatformTech`` in
       a prefab under ``Stadium/Media/Prefab/PlatformDirt/`` resolves to
       ``PlatformDirt/PlatformTech.Material.Gbx``).
    3. **First PascalCase token** — generic templates like
       ``Decal.Material.Gbx`` or ``SpecialFX.Material.Gbx`` live in
       every Boost*/Turbo* folder. A binding called
       ``DecalSpecialTurbo`` collapses to ``Decal``;
       ``SpecialFXTurbo`` collapses to ``SpecialFX``.
    """
    if not material_path:
        return []
    base = os.path.basename(material_path.replace("\\", "/"))
    low = base.lower()
    for ext in (".material.gbx", ".material"):
        if low.endswith(ext):
            base = base[: -len(ext)]
            break
    seen: set[str] = set()
    out: list[str] = []

    def _add(candidate: str) -> None:
        if not candidate:
            return
        key = candidate.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(candidate + ".Material.Gbx")

    _add(base)

    # 2) strip ``<FolderLeaf>_`` prefix.
    folder_leaf = folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if folder_leaf:
        prefix = folder_leaf + "_"
        if base.lower().startswith(prefix.lower()):
            _add(base[len(prefix):])

    # 3) first PascalCase token (uppercase letter followed by lowercase
    #    run, ending before the next uppercase letter).
    if base and base[0].isupper():
        cut = 1
        while cut < len(base) and not base[cut].isupper():
            cut += 1
        token = base[:cut]
        if token and token != base:
            _add(token)

    return out


def _collection_root(path: str) -> str:
    """Return ``<GameData>\\<Collection>`` for a path below its ``Media`` dir."""
    if not path:
        return ""
    parts = os.path.normpath(path).split(os.sep)
    lowered = [p.lower() for p in parts]
    if "media" not in lowered:
        return ""
    idx = lowered.index("media")
    return os.sep.join(parts[:idx]) if idx > 0 else ""


def _source_collection_material(material_path: str, resolver, source_path: str) -> str:
    """Find a bare material stem in the source file's own collection."""
    lookup = _material_resolver_for(resolver)
    root = _collection_root(source_path)
    if lookup is None or not root or not hasattr(lookup, "_index_for"):
        return ""
    stem = os.path.basename(material_path.replace("\\", "/")).split(".", 1)[0].lower()
    try:
        return lookup._index_for(root).get(stem, "") if stem else ""
    except OSError:
        return ""


def _prefixed_modifier_material(material_path: str, resolver, source_path: str) -> str:
    """Resolve ``<Modifier>_<Stem>`` to ``<Collection>\\Media\\Modifier\\<Modifier>\\<Stem>``."""
    if resolver is None or not material_path or "\\" in material_path or "/" in material_path:
        return ""
    stem = material_path.split(".", 1)[0]
    prefix, sep, rest = stem.partition("_")
    if not sep or not prefix or not rest:
        return ""
    root = _collection_root(source_path)
    collections = [os.path.basename(root)] if root else []
    if "stadium" not in (c.casefold() for c in collections):
        collections.append("Stadium")
    for collection in collections:
        hit = _resolve_in_modifier_folder(
            f"{collection}/Media/Modifier/{prefix}", rest + ".Material.Gbx", resolver,
        )
        if hit:
            log.debug("prefixed modifier material: %r -> %s", material_path, hit)
            return hit
    return ""


# Per-environment water depth-fog colour ramps (shallow -> deep).
_VISTA_WATER_FOG_NAMES = ("WaterSea_Fog.dds", "WaterFog.dds")
_VISTA_WATER_FAMILY = "Water.Vista"
# Lake ramps (``WaterFog.dds``) colour shallow inland water over a visible
# bed; the deep sea defaults turned RedIsland lakes and shores near-black.
_VISTA_LAKE_PARAMS = {
    "depth_facing": 0.0, "depth_grazing": 0.5, "alpha_facing": 0.25, "alpha_grazing": 0.85,
}


def _vista_water_fog(material_source: str) -> str:
    """Return the fog ramp for a non-Stadium environment's water material."""
    root = _collection_root(material_source)
    if not root or os.path.basename(root).lower() == "stadium":
        return ""
    image_dir = os.path.join(root, "Media", "Texture", "Image")
    for name in _VISTA_WATER_FOG_NAMES:
        candidate = os.path.join(image_dir, name)
        if library.isfile(candidate):
            return candidate
    return ""


def _load_linked_material(material_path: str, resolver, source_path: str, *, terrain_remap: bool = False):
    """Resolve + GBXBPY.load a .Material.Gbx, with per-process caching.

    Lookup order:
        1. Each active material-modifier remap folder (when the block's
           ``materialModifier`` redirects materials to a themed copy —
           see :func:`_modifier_folders_for`). For each folder we try
           several name candidates (see :func:`_modifier_name_candidates`)
           because Trackmania prefabs frequently encode the modifier
           folder leaf as a prefix on the material name. The override
           has precedence over the default material.
        2. The standard path-based resolver against the binding's path.
        3. The source file's own collection (``<Env>\\Media\\Material``),
           so vista prefabs get their environment's copy of a shared stem.
        4. :class:`MaterialResolver`'s stem index over every collection's
           ``Media/Material`` tree (TM2020 bindings frequently store
           only a bare stem like ``"RoadTech"``).

    With ``terrain_remap`` the result then passes through the vista's
    StadiumOnTerrain remap (see :func:`set_terrain_material_remap`). Only
    binding-level loads use it: a remapped copy may inherit from the very
    material it replaces.
    """
    if not material_path or resolver is None:
        return None
    abs_path: Optional[str] = None

    # 1) Modifier-folder override (themed/skinned material copy).
    modifier_folders = _modifier_folders_for(resolver)
    if modifier_folders:
        for folder in modifier_folders:
            if not _modifier_allows(material_path, folder, resolver):
                continue
            renamed = _modifier_renamed(material_path, folder, resolver)
            names = ([renamed + ".Material.Gbx"] if renamed else []) + _modifier_name_candidates(material_path, folder)
            for fname in names:
                hit = _resolve_in_modifier_folder(folder, fname, resolver)
                if hit:
                    abs_path = hit
                    log.debug(
                        "modifier override: %r -> %s (folder=%r, key=%r)",
                        material_path, abs_path, folder, fname,
                    )
                    break
            if abs_path:
                break

    # 2) Standard path resolution.
    if not abs_path:
        try:
            abs_path = resolver.resolve(material_path, source_path=source_path)
        except Exception as e:  # noqa: BLE001
            log.debug("resolver failed for material %r: %s", material_path, e)
    # Custom items name game materials without the extension
    # ("Stadium\Media\Material_BlockCustom\CustomModOpaque").
    if not abs_path and not os.path.splitext(material_path)[1] and ("\\" in material_path or "/" in material_path):
        try:
            abs_path = resolver.resolve(material_path + ".Material.Gbx", source_path=source_path)
        except Exception as e:  # noqa: BLE001
            log.debug("resolver failed for material %r: %s", material_path, e)

    # 3) Same-collection stem lookup: a vista prefab's bare "Water" means
    # <Env>\Media\Material\Water, not the Stadium copy the global index
    # prefers.
    if not abs_path:
        abs_path = _source_collection_material(material_path, resolver, source_path)

    # 3b) "<Modifier>_<Stem>" bindings outside a modified block (e.g. the
    # RallyCastle tower's "PlatformDirt_PlatformTech") name a modifier copy.
    if not abs_path:
        abs_path = _prefixed_modifier_material(material_path, resolver, source_path)

    # 4) Stem-index fallback.
    if not abs_path:
        lookup = _material_resolver_for(resolver)
        if lookup is not None:
            try:
                abs_path = lookup.find(material_path)
            except Exception as e:  # noqa: BLE001
                log.debug("material lookup failed for %r: %s", material_path, e)
                abs_path = None
    if not abs_path:
        return None
    if terrain_remap:
        remap = getattr(resolver, "_gbxmat_terrain_remap", None) or {}
        target = remap.get(os.path.normcase(os.path.abspath(abs_path)))
        if target:
            log.debug("terrain remap: %s -> %s", abs_path, target)
            abs_path = target
    if abs_path in _LINKED_MATERIAL_CACHE:
        return _LINKED_MATERIAL_CACHE[abs_path]
    try:
        loaded = GBXBPY.load(abs_path, resolver=resolver, recursive=False)
    except Exception as e:  # noqa: BLE001
        log.debug("failed to load linked material %s: %s", abs_path, e)
        loaded = None
    _LINKED_MATERIAL_CACHE[abs_path] = loaded
    return loaded


_FUNC_SHADER_CACHE: dict[str, object] = {}


# Race-clock digit -> (seconds per cycle, frames), as in
# Stadium/Func/FuncShader/ChronoFinish-*.FuncShader.Gbx.
_CHRONO_DIGITS = {
    "00-00-001": (0.01, 10), "00-00-01": (0.1, 10), "00-00-10": (1.0, 10),
    "00-01-00": (10.0, 10), "00-10-00": (60.0, 6), "01-00-00": (600.0, 10),
    "10-00-00": (6000.0, 10),
}


def _load_func_shader(material_source_path: str, resolver):
    """Load the ``.FuncShader.Gbx`` paired with a material file, if any.

    The TM convention (see repo memory ``funcshader-files.md``) is:

    * Same-stem pairing — ``Foo.Material.Gbx`` + ``Foo.FuncShader.Gbx``
      in the same directory or in the engine-wide
      ``Stadium/Func/FuncShader/`` registry.

    Returns the loaded :class:`GBXBPY.model.FuncShader`, or ``None`` if
    no paired file is found.
    """
    if not material_source_path or resolver is None:
        return None
    base = os.path.basename(material_source_path)
    stem = base.split(".", 1)[0] if base else ""
    if not stem:
        return None
    cache_key = os.path.normcase(os.path.abspath(material_source_path))
    if cache_key in _FUNC_SHADER_CACHE:
        return _FUNC_SHADER_CACHE[cache_key]
    fs = None
    # 1) Adjacent to the material.
    candidates = [
        os.path.join(os.path.dirname(material_source_path),
                     f"{stem}.FuncShader.Gbx"),
    ]
    # 2) Engine-wide func-shader registry. Item materials drop their
    # "Item" prefix there (ItemTorchFlame -> TorchFlame).
    registry_stem = stem
    if stem == "SpecialFX":
        folder = os.path.basename(os.path.dirname(material_source_path))
        registry_stem = {
            "Turbo2": "SpecialFXTurbo2",
            "TurboRoulette": "SpecialFXTurboRoulette",
        }.get(folder, stem)
    registry_stems = [registry_stem]
    if stem.startswith("Item") and len(stem) > len("Item"):
        registry_stems.append(stem[len("Item"):])
    for name in registry_stems:
        try:
            registry_hit = resolver.resolve(
                f"Stadium/Func/FuncShader/{name}.FuncShader.Gbx",
                probe=True,
            )
        except Exception:  # noqa: BLE001
            registry_hit = None
        if registry_hit:
            candidates.append(registry_hit)
    for cand in candidates:
        if not cand or not library.isfile(cand):
            continue
        try:
            fs = GBXBPY.load(cand, resolver=resolver, recursive=False)
        except Exception as e:  # noqa: BLE001
            log.debug("FuncShader load failed for %s: %s", cand, e)
            fs = None
        if fs is not None:
            log.debug("FuncShader paired %s -> %s",
                      os.path.basename(material_source_path), cand)
            break
    digit = stem[len("ChronoCheckpoint-"):] if stem.startswith("ChronoCheckpoint-") else ""
    if fs is None and digit in _CHRONO_DIGITS:
        # Checkpoint digits have no paired FuncShaders in GameData. The
        # finish clock uses the same 8x4 atlas layout; the race clock has
        # the same timing on a 8x2 atlas. Extractions don't always contain
        # either, so fall back to the known timing table.
        def sibling(template_stem):
            return _load_func_shader(
                os.path.join(os.path.dirname(material_source_path),
                             f"{template_stem}.Material.Gbx"),
                resolver,
            )
        fs = sibling(f"ChronoFinish-{digit}")
        if fs is None:
            clock = sibling(f"Chrono-{digit}")
            if clock is not None:
                fs = replace(clock, atlas_rows=clock.atlas_rows * 2)
        if fs is None:
            period, frames = _CHRONO_DIGITS[digit]
            log.debug("Using built-in clock animation for %s", stem)
            fs = FuncShader(
                name=stem, period=period, input_val_id="RaceTime",
                layer_name="MulInside", layer_mode=3,
                atlas_frames=frames, atlas_cols=8, atlas_rows=4, atlas_flag=1,
            )
    if fs is None:
        # Stock SpecialFX material names and their shared animation names
        # differ. Modifier variants keep the same material filename, so use
        # the resolved folder too (not a stem-only cache).
        template = {
            "SpecialFXTurbo": "SpecialFX",
            "SpecialFXGateGameplay": "SpecialFXGate",
        }.get(stem)
        if template is not None:
            fs = _load_func_shader(
                os.path.join(os.path.dirname(material_source_path),
                             f"{template}.Material.Gbx"),
                resolver,
            )
            if fs is None:
                log.warning("No FX animation template %s found for %s",
                            template, material_source_path)
    _FUNC_SHADER_CACHE[cache_key] = fs
    return fs


def _flatten_func_shader(fs, out: list[tuple]) -> None:
    """Append a tuple-encoded record per leaf animation in ``fs``."""
    if fs is None:
        return
    children = list(getattr(fs, "children", None) or [])
    if children:
        for ch in children:
            _flatten_func_shader(ch, out)
        return
    kind = getattr(getattr(fs, "shader_kind", None), "value", "Unknown")
    layer = str(getattr(fs, "layer_name", "") or "")
    period = float(getattr(fs, "period", 1.0) or 1.0)
    inp = str(getattr(fs, "input_val_id", "") or "")
    cols = int(getattr(fs, "atlas_cols", 0) or 0)
    rows = int(getattr(fs, "atlas_rows", 0) or 0)
    frames = int(getattr(fs, "atlas_frames", 0) or 0)
    off = getattr(fs, "uv_offset", (0.0, 0.0)) or (0.0, 0.0)
    scl = getattr(fs, "uv_scale", (1.0, 1.0)) or (1.0, 1.0)
    spd = getattr(fs, "uv_speed", (0.0, 0.0)) or (0.0, 0.0)
    out.append((
        kind, layer, period, inp,
        cols, rows, frames,
        float(off[0]), float(off[1]),
        float(scl[0]), float(scl[1]),
        float(spd[0]), float(spd[1]),
    ))


def _func_shaders_for(linked_material, resolver) -> tuple:
    """Return the spec-tuple list of FuncShader animations for a material.

    Walks the material's parent chain so a FuncShader paired with a
    base material still applies to a derived skin (the modifier-folder
    overrides usually don't ship their own paired file).
    """
    if linked_material is None or resolver is None:
        return ()
    flat: list[tuple] = []
    seen: set[int] = set()
    cur = linked_material
    depth = 0
    while cur is not None and id(cur) not in seen and depth < 8:
        seen.add(id(cur))
        depth += 1
        src = getattr(cur, "source_path", "") or ""
        fs = _load_func_shader(src, resolver) if src else None
        if fs is not None:
            _flatten_func_shader(fs, flat)
            break  # nearest match wins
        parent_path = getattr(cur, "base_material_path", "") or ""
        if not parent_path:
            break
        cur = _load_linked_material(parent_path, resolver, src)
    return tuple(flat)


def _params_from_material(linked_material, resolver, binding=None) -> dict:
    """Build a :class:`MaterialSpec`-friendly params dict from the chain.

    Currently produces:

    * ``triplanar_scale`` — derived from the per-binding ``texture_size``
      first (preferred — that's where TM stores per-mesh tiling), then
      from the deepest non-default ``Material.texture_size`` in the
      parent chain. TM's ``textureSize`` is the world-space length (in
      metres) of one texture repetition, so the Blender Mapping
      ``Scale`` must be ``1 / texture_size`` for the texture to tile
      correctly. Without this, every triplanar surface sampled the
      texture once across the entire object.

    Skipped entirely when no useful value is found, so the spec's
    immutable ``params={}`` default is preserved for non-triplanar
    materials that don't need any params.
    """
    chain_size = 0.0
    # 1) Per-binding tiling wins — that's where TM stores per-mesh scale.
    if binding is not None:
        bsz = float(getattr(binding, "texture_size", 1.0) or 0.0)
        if bsz > 0.0 and bsz != 1.0:
            chain_size = bsz
    # 2) Material-file fallback (rare, but real for crystal materials).
    if chain_size <= 0.0 and linked_material is not None:
        for mat in _walk_material_and_parents(linked_material, resolver):
            size = float(getattr(mat, "texture_size", 0.0) or 0.0)
            if size > 0.0 and size != 1.0:
                chain_size = size
                break
    if chain_size <= 0.0:
        return {}
    return {"triplanar_scale": 1.0 / chain_size}


def _walk_material_and_parents(material, resolver) -> Iterable[object]:
    """Yield ``material`` then each ancestor reachable via base_material_path.

    Stops on missing parents, broken parses, or after a small depth cap
    to defend against accidental cycles.
    """
    seen: set[int] = set()
    cur = material
    depth = 0
    while cur is not None and id(cur) not in seen and depth < 8:
        seen.add(id(cur))
        yield cur
        parent_path = getattr(cur, "base_material_path", "") or ""
        if not parent_path:
            return
        parent_src = getattr(cur, "source_path", "") or ""
        cur = _load_linked_material(parent_path, resolver, parent_src)
        depth += 1


_TEXTURE_GBX_SUFFIXES = (
    ".texturearray.gbx",
    ".texture.gbx",
)
# Order matters: DDS first (preferred), then on-disk fallbacks Blender can
# read natively.
_IMAGE_EXTENSIONS = (".dds", ".tga", ".png", ".webp", ".jpg", ".jpeg")


def _texture_gbx_to_image(abs_path: str) -> Optional[str]:
    """Translate ``<dir>/<name>.Texture.gbx`` -> ``<dir>/Image/<name>.dds``.

    TM stores ``.Texture.gbx`` files as wrappers that point to a sibling
    ``Image/<stem>.dds``. Blender can't read the wrapper, so we resolve
    to the image file. Returns ``None`` if no image variant exists.
    """
    if not abs_path:
        return None
    low = abs_path.lower()
    stripped: Optional[str] = None
    for suffix in _TEXTURE_GBX_SUFFIXES:
        if low.endswith(suffix):
            stripped = abs_path[: -len(suffix)]
            break
    if stripped is None:
        return None
    parent = os.path.dirname(stripped)
    name = os.path.basename(stripped)
    image_dir = os.path.join(parent, "Image")
    for ext in _IMAGE_EXTENSIONS:
        candidate = os.path.join(image_dir, name + ext)
        if library.isfile(candidate):
            return candidate
    return None


# Texture references that resolved to no file during the current import;
# the map importer reports them (an incomplete library renders untextured).
_MISSING_TEXTURES: set[str] = set()


def reset_missing_textures() -> None:
    _MISSING_TEXTURES.clear()


def missing_textures() -> list[str]:
    return sorted(_MISSING_TEXTURES, key=str.casefold)


def _resolve_texture_path(
    rel_path: str, resolver, source_path: str, *, array_index: int = 0,
) -> Optional[str]:
    paths = _resolve_texture_paths(rel_path, resolver, source_path)
    return _select_texture_path(paths, rel_path, array_index)


def _select_texture_path(paths: tuple[str, ...], rel_path: str, array_index: int) -> Optional[str]:
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    if not 0 <= array_index < len(paths):
        log.warning("texture array %s: layer %d outside %d layers", rel_path, array_index, len(paths))
        _MISSING_TEXTURES.add(rel_path.replace("\\", "/"))
        return None
    return paths[array_index]


def _resolve_texture_paths(rel_path: str, resolver, source_path: str) -> tuple[str, ...]:
    """Resolve an image or all ordered CPlugBitmap array layers."""
    if not rel_path:
        return ()
    from GBXMAT.images import map_path
    replacement = map_path(rel_path)
    if replacement != rel_path and os.path.isfile(replacement):
        return (replacement,)
    abs_path: Optional[str] = None
    if resolver is not None:
        try:
            abs_path = resolver.resolve(rel_path, source_path=source_path)
        except Exception as e:  # noqa: BLE001
            log.debug("resolver failed for texture %r: %s", rel_path, e)
            abs_path = None
    if not abs_path:
        if os.path.isabs(rel_path) and library.ensure(rel_path):
            abs_path = rel_path
        else:
            if not _is_placeholder(rel_path):
                _MISSING_TEXTURES.add(rel_path.replace("\\", "/"))
            return ()
    # ``.Texture.gbx`` files are wrappers — translate to the underlying
    # ``Image/<stem>.dds`` (or .tga/.png fallback) so GBXMAT can decode
    # the bytes Blender expects.
    image_path = _texture_gbx_to_image(abs_path)
    if image_path is not None:
        return (image_path,)
    low = abs_path.lower()
    if low.endswith(_TEXTURE_GBX_SUFFIXES):
        from GBXBPY.textures.terrain_layers import read_texture_array, wrapper_image_reference
        referenced = wrapper_image_reference(abs_path)
        if referenced is not None:
            if library.ensure(referenced):
                return (referenced,)
            _MISSING_TEXTURES.add(rel_path.replace("\\", "/"))
            log.debug("texture wrapper %s references missing image %s", abs_path, referenced)
            return ()
        info = read_texture_array(abs_path)
        if info is not None and info.layers:
            if all(path for _name, path in info.layers):
                return tuple(path for _name, path in info.layers if path)
            log.warning("texture array %s has missing layer images", abs_path)
            _MISSING_TEXTURES.add(rel_path.replace("\\", "/"))
        elif info is None and not low.endswith(".texturearray.gbx"):
            # No image and no layers: a render target or runtime-generated
            # texture, not an asset missing from the library.
            log.debug("runtime texture without image data: %s", abs_path)
            return ()
        else:
            _MISSING_TEXTURES.add(rel_path.replace("\\", "/"))
        log.debug("no image sibling or complete array for texture wrapper %s", abs_path)
        return ()
    return (abs_path,)


_TEXTURE_UV_CHUNK = (0x09011025).to_bytes(4, "little")
_TEXTURE_UV_CACHE: dict[str, Optional[tuple[float, float, float, float, float]]] = {}


def _texture_uv_transform(image_path: str) -> Optional[tuple[float, float, float, float]]:
    """Return ``(scale_u, scale_v, offset_u, offset_v)`` from a texture wrapper."""
    full = _texture_uv_transform_full(image_path)
    return full[:4] if full is not None else None


def _texture_uv_transform_full(image_path: str) -> Optional[tuple[float, float, float, float, float]]:
    """Return ``(scale_u, scale_v, offset_u, offset_v, rotation_deg)`` from a texture wrapper.

    ``image_path`` is the decoded ``<dir>/Image/<stem>.dds`` sibling; the
    wrapper lives at ``<dir>/<stem>.Texture.Gbx``. Chunk ``0x09011025`` stores
    five floats: UV scale, UV offset and rotation (degrees).
    """
    if not image_path:
        return None
    key = os.path.normcase(image_path)
    if key in _TEXTURE_UV_CACHE:
        return _TEXTURE_UV_CACHE[key]
    result = None
    stem = os.path.splitext(os.path.basename(image_path))[0]
    folder = os.path.dirname(os.path.dirname(image_path))
    for suffix in (".Texture.Gbx", ".Texture.gbx"):
        wrapper = os.path.join(folder, stem + suffix)
        if not library.isfile(wrapper):
            continue
        try:
            with open(wrapper, "rb") as fh:
                raw = fh.read()
            index = raw.find(_TEXTURE_UV_CHUNK)
            if index >= 0 and index + 24 <= len(raw):
                values = struct.unpack_from("<5f", raw, index + 4)
                if values[0] > 0.0 and values[1] > 0.0:
                    result = tuple(float(v) for v in values)
        except OSError as e:
            log.debug("texture wrapper %s unreadable: %s", wrapper, e)
        break
    _TEXTURE_UV_CACHE[key] = result
    return result


def _is_placeholder(path: str) -> bool:
    stem = os.path.basename(path.replace("\\", "/")).split(".", 1)[0]
    return stem.lower() in _PLACEHOLDER_STEMS


def _floats(value) -> list[float]:
    """Flatten a ShaderParam value (nested ListContainers) to floats."""
    if isinstance(value, (int, float)):
        return [float(value)]
    out: list[float] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_floats(item))
    return out


# Shader param name -> spec param names for its components.
_SCREEN_PARAMS = {
    "TcScale_BRNH":    ("tc_scale_u", "tc_scale_v"),
    "Meters_Depth":    ("meters_depth",),
    "RefractIndex":    ("refract_index",),
    "DoubleRefract":   ("double_refract",),
    "Meters_UV":       ("meters_uv_u", "meters_uv_v"),
    "Meters_UV_Parallax": ("meters_uv_parallax",),
    "SelfIllum_Scale": ("self_illum_scale",),
    "Meters_Depth_Parallax": tuple(f"inside_layer{i}_depth" for i in range(8)),
}

_WATER_WALL_PARAMS = {
    "Y0_Speed":       ("water_y0_speed",),
    "Y1_Speed":       ("water_y1_speed",),
    "X0_Period":      ("water_x0_period",),
    "X0_Scale":       ("water_x0_scale",),
    "Normal1_Weight": ("water_normal1_weight",),
}


def _screen_params(linked_material, resolver, table=None) -> dict:
    """Read shader params named in ``table``, child materials overriding parents."""
    table = _SCREEN_PARAMS if table is None else table
    params: dict[str, float] = {}
    for mat in _walk_material_and_parents(linked_material, resolver):
        for param in getattr(mat, "shader_params", None) or ():
            names = table.get(getattr(param, "name", ""))
            if not names or names[0] in params:
                continue
            values = _floats(getattr(param, "value", None))
            for name, value in zip(names, values):
                params[name] = value
    return params


_WATER_SURFACE_FAMILY = "Water.Surface"
_WATER_WALL_FAMILY = "Water.Wall"
_WATER_TRANSMITTANCE = os.path.join("Media", "Texture", "Image", "WaterTransmittance.ImageGen.Gbx")


def _decompress_lzo(data: bytes, size: int) -> bytes:
    import gbx.lzo  # gbx-py; also used by the NICE parser
    return gbx.lzo.decompress(data, False, size)


def parse_water_transmittance(data: bytes) -> Optional[tuple[tuple[float, float, float], float]]:
    """``(linear rgb, distance m)`` from a ``WaterTransmittance.ImageGen.Gbx``.

    The body is a small struct: a flag at byte 20 (1 = an RGBA colour
    follows), then a u32 kind and the transmittance colour reached after
    ``distance`` metres. Stadium stores (0.84, 0.95, 0.97) at 50 m.
    """
    from GBXBPY.library.validate import gbx_body
    import struct
    try:
        body = gbx_body(data, _decompress_lzo)
    except Exception as exc:  # noqa: BLE001
        log.debug("WaterTransmittance unreadable: %s", exc)
        return None
    if not body or len(body) < 28:
        return None
    pos = 24 + (16 if struct.unpack_from("<I", body, 20)[0] == 1 else 0)
    if len(body) < pos + 20:
        return None
    r, g, b, distance = struct.unpack_from("<4f", body, pos + 4)
    if not all(0.0 <= c <= 1.0 for c in (r, g, b)) or distance <= 0.0:
        return None
    return (r, g, b), distance


def _water_transmittance_params(material_path: str) -> dict:
    root = _collection_root(material_path)
    path = os.path.join(root, _WATER_TRANSMITTANCE) if root else ""
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "rb") as fh:
        parsed = parse_water_transmittance(fh.read())
    if parsed is None:
        log.warning("could not parse %s", path)
        return {}
    (r, g, b), distance = parsed
    return {
        "water_transmittance_r": r, "water_transmittance_g": g,
        "water_transmittance_b": b, "water_transmittance_distance": distance,
    }


def _skin_context_for(resolver):
    """Return ``(skin, foreground)`` set by the map builder, or ``(None, None)``."""
    raw = getattr(resolver, "_gbxmat_skin_context", None) if resolver is not None else None
    if not raw:
        return None, None
    return raw


def _apply_screen_skin(textures: list, params: dict, resolver) -> list:
    """Swap the default inside image for the placement's skin layers."""
    skin, foreground = _skin_context_for(resolver)
    params["inside_parallax"] = (
        1.0 if skin is not None and skin.layers and skin.class_id.casefold() == "parallax" else 0.0
    )
    if skin is not None and skin.layers:
        textures = [t for t in textures if t.role != "Inside" and not t.role.startswith("InsideLayer")]
        for index, layer in enumerate(skin.layers):
            textures.append(TextureRef(role=f"InsideLayer{index}", path=layer.path))
            params[f"inside_layer{index}_depth"] = float(layer.depth)
            params[f"inside_layer{index}_vtile"] = 1.0 if layer.vertical_tiling else 0.0
            _store_layer_blend(params, f"inside_layer{index}", layer.path)
    if foreground is not None and foreground.layers:
        textures = [t for t in textures if not t.role.startswith("InsideForeground")]
        for index, layer in enumerate(foreground.layers):
            textures.append(TextureRef(role=f"InsideForeground{index}", path=layer.path))
            params[f"inside_fg{index}_depth"] = float(layer.depth)
            _store_layer_blend(params, f"inside_fg{index}", layer.path)
    return textures


def _store_layer_blend(params: dict, prefix: str, path: str) -> None:
    """Record the ``+RGBA``/``+RGBY`` file-name blend mode of a skin layer."""
    from GBXBPY.resolve.skins import layer_blend

    fill, from_luma = layer_blend(path)
    params[f"{prefix}_luma"] = 1.0 if from_luma else 0.0
    if fill is not None:
        for channel, value in zip("rgb", fill):
            params[f"{prefix}_fill_{channel}"] = value


def _collect_textures_from_linked(
    linked_material, resolver, source_path: str, *, preserve_projections: bool = False,
    array_index: int = 0,
) -> list[TextureRef]:
    """Walk the linked material + parents and collect resolved TextureRefs.

    Child slots win over parent slots with the same role (TM materials
    inherit then override). For roles where multiple distinct slot names
    can populate them (e.g. Diffuse from FenceA, BaseColorA, or plain
    Diffuse), :data:`_SLOT_PRIORITY` decides the winner — even when the
    higher-priority slot lives on a parent. Slots without a resolvable
    path are skipped.
    """
    if linked_material is None:
        return []
    # role -> (priority, depth_index, TextureRef). Higher priority wins;
    # on ties the deeper-in-chain (child) wins via lower depth index
    # *negated*.
    by_role: dict[str, tuple[int, int, TextureRef]] = {}
    inside_key = None
    chain = list(_walk_material_and_parents(linked_material, resolver))
    # depth: 0 = leaf/child, increasing toward parents.
    for depth, mat in enumerate(chain):
        for slot in getattr(mat, "texture_slots", None) or []:
            slot_name = getattr(slot, "name", "") or ""
            tex_rel = getattr(slot, "texture_path", "") or ""
            if (
                not tex_rel or slot_name in _IGNORED_SLOTS
                or (slot_name.startswith("MulInside") and _is_placeholder(tex_rel))
            ):
                continue
            role = (
                _PROJECTED_SLOT_ALIASES.get(slot_name, slot_name)
                if preserve_projections and slot_name in _TERRAIN_PROJECTION_SLOTS
                else _SLOT_TO_ROLE.get(slot_name) if slot_name else None
            )
            if role is None:
                role = _role_from_filename(tex_rel) or slot_name or "Diffuse"
            mat_src = getattr(mat, "source_path", "") or source_path
            paths = _resolve_texture_paths(tex_rel, resolver, mat_src)
            priority = _SLOT_PRIORITY.get(slot_name, 0)
            candidate_key = (priority, -depth)
            if role == "Inside" and paths:
                if inside_key is not None and candidate_key <= inside_key:
                    continue
                inside_key = candidate_key
                by_role = {r: entry for r, entry in by_role.items()
                           if r != "Inside" and not r.startswith("InsideLayer")}
            if role == "Inside" and len(paths) > 1:
                # Keep the whole stock display stack; its depth parameters
                # order alpha compositing but do not enable view offsets.
                for index, path in enumerate(paths):
                    layer_role = f"InsideLayer{index}"
                    by_role[layer_role] = (priority, depth, TextureRef(role=layer_role, path=path))
                continue
            abs_path = _select_texture_path(paths, tex_rel, array_index)
            if not abs_path:
                log.debug("linked-material slot %s: %r unresolved", slot_name, tex_rel)
                continue
            existing = by_role.get(role)
            # Prefer higher priority, then closer-to-leaf (smaller depth).
            if existing is None or candidate_key > (existing[0], -existing[1]):
                by_role[role] = (priority, depth, TextureRef(role=role, path=abs_path))
    return [entry[2] for entry in by_role.values()]


def _linked_slot_texture(linked_material, slot_name: str, resolver, source_path: str) -> str:
    """Resolved path of the nearest ``slot_name`` texture in the material chain."""
    for mat in _walk_material_and_parents(linked_material, resolver):
        for slot in getattr(mat, "texture_slots", None) or []:
            tex_rel = getattr(slot, "texture_path", "") or ""
            if getattr(slot, "name", "") == slot_name and tex_rel:
                mat_src = getattr(mat, "source_path", "") or source_path
                return _resolve_texture_path(tex_rel, resolver, mat_src) or ""
    return ""


def _grass_x2(textures: list[TextureRef], params: dict) -> list[TextureRef]:
    """Keep the ``GrassX2`` overlay only with its world projection from the wrapper.

    Tech3 GrassX2 shaders multiply the diffuse by ``2 * GrassX2`` sampled at
    world-position texture coordinates (``Grass_X2``: mowing stripes and the
    Stadium logo). The wrapper's scale/offset/rotation go to ``params``.
    """
    ref = next((t for t in textures if t.role == "GrassX2"), None)
    if ref is None:
        return textures
    transform = _texture_uv_transform_full(ref.path)
    if transform is None:
        log.debug("GrassX2 %s has no texture transform; overlay skipped", ref.path)
        return [t for t in textures if t.role != "GrassX2"]
    for key, value in zip(("scale_u", "scale_v", "offset_u", "offset_v", "rotation"), transform):
        params[f"grass_x2_{key}"] = value
    return textures


_PROJECTED_SLOT_ALIASES = {"PyDiffuse": "PyBaseColor", "PxzDiffuse": "PxzBaseColor"}
_TERRAIN_PROJECTION_SLOTS = frozenset({
    "PyBaseColor", "PyRoughMetal", "PyNormal", "PyX2",
    "PxzBaseColor", "PxzRoughMetal", "PxzNormal",
    "PyBaseColorHueMask", "PxzBaseColorHueMask",
    "TBaseColor", "TRoughMetal", "TNormal",
}) | _PROJECTED_SLOT_ALIASES.keys()
_GRASS_DIFFUSE_STEMS = frozenset({"grass_d", "platformgrass_d", "decohillpy_d"})
# PlatformDirt / PlatformIce DecoHill remaps: same PyPxzT shader as stock
# DecoHill, with sand ("Dirt") and snow ("Ice") projected textures.
_DECOHILL_VARIANT_DIFFUSE_STEMS = frozenset({"decohilldirtpy_d", "decohillicepy_d"})
_DECOHILL_VARIANT_FAMILY = "Terrain.PyPxzT"
# Vista RiverSideToLakeBottom: projected LakeBottom bed + RiverShore T strip
# whose texture alpha (not vertex alpha) carries the shore fade.
_LAKE_SHORE_DIFFUSE_STEMS = frozenset({"lakebottom_d"})
_PROJECTED_TERRAIN_FAMILY = "Terrain.PyPxz"
_PROJECTED_PYPXZ_SHADERS = frozenset({
    "tech3blockpypxzdiffspecnorm",
    "tech3blockpypxzdiffspecnormlm1",
    "tech3warppypxzdiff",
})
_PROJECTED_SAMPLED_SLOTS = frozenset({
    "PyBaseColor", "PyRoughMetal", "PyNormal",
    "PxzBaseColor", "PxzRoughMetal", "PxzNormal",
})
_CUSTOM_HUE_SHADER = "tech3blockpypxzhue"
_CUSTOM_HUE_FAMILY = "Custom.PyPxzHue"


def _srgb_to_linear(value: float) -> float:
    value = min(max(float(value), 0.0), 1.0)
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _custom_target_params(binding, linked_material, resolver) -> dict:
    """Linear hue target for ``Tech3 Block PyPxz_Hue`` custom materials.

    The item's own colour (already linear) wins; otherwise the material's
    ``TargetColor`` default, authored as sRGB bytes, is linearised.
    """
    rgb = tuple(getattr(binding, "target_color", ()) or ())
    if len(rgb) < 3 and linked_material is not None:
        for mat in _walk_material_and_parents(linked_material, resolver):
            value = next((
                getattr(p, "value", None)
                for p in (getattr(mat, "shader_params", None) or ())
                if getattr(p, "name", "") == "TargetColor"
            ), None)
            while isinstance(value, (list, tuple)) and len(value) == 1:
                value = value[0]
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                rgb = tuple(_srgb_to_linear(c) for c in value[:3])
                break
    if len(rgb) < 3:
        return {}
    return {f"custom_target_{c}": float(v) for c, v in zip("rgb", rgb[:3])}


def _grass_surface(textures) -> bool:
    return any(
        tex.role == "Diffuse"
        and os.path.basename(tex.path.replace("\\", "/")).split(".", 1)[0].lower()
        in _GRASS_DIFFUSE_STEMS
        for tex in textures
    )


def _dirt_surface(textures) -> bool:
    return any(
        tex.role == "Diffuse"
        and os.path.basename(tex.path.replace("\\", "/")).split(".", 1)[0].lower()
        == "dirtpy_d"
        for tex in textures
    )


def _decohill_variant_surface(textures) -> bool:
    return any(
        tex.role == "Diffuse"
        and os.path.basename(tex.path.replace("\\", "/")).split(".", 1)[0].lower()
        in _DECOHILL_VARIANT_DIFFUSE_STEMS
        for tex in textures
    )


def _lake_shore_surface(textures) -> bool:
    return any(
        tex.role == "Diffuse"
        and os.path.basename(tex.path.replace("\\", "/")).split(".", 1)[0].lower()
        in _LAKE_SHORE_DIFFUSE_STEMS
        for tex in textures
    )


def _store_projected_uv_transforms(projected, params: dict) -> None:
    """Record each sampled Py/Pxz texture wrapper's UV transform in ``params``."""
    for tex in projected:
        if tex.role not in _PROJECTED_SAMPLED_SLOTS:
            continue
        transform = _texture_uv_transform(tex.path)
        if transform is None:
            continue
        params[f"uv_scale_u:{tex.role}"] = transform[0]
        params[f"uv_scale_v:{tex.role}"] = transform[1]
        params[f"uv_offset_u:{tex.role}"] = transform[2]
        params[f"uv_offset_v:{tex.role}"] = transform[3]


def _has_fence_slot(linked_material, resolver) -> bool:
    """Return True if any material in the chain declares a FenceA slot."""
    if linked_material is None:
        return False
    for mat in _walk_material_and_parents(linked_material, resolver):
        for slot in getattr(mat, "texture_slots", None) or []:
            name = getattr(slot, "name", "") or ""
            if name == "FenceA" and getattr(slot, "texture_path", ""):
                return True
    return False


TERRAIN_LAYERS_FAMILY = "Terrain.Layers"
# Unique images per layered terrain material. EEVEE counts one sampler per
# unique image; above its limit the shader fails to compile (pink).
TERRAIN_IMAGE_BUDGET = 20
_TERRAIN_T_SLOTS = (("TBaseColorOp", "TBaseColor"), ("TBaseColor", "TBaseColor"),
                    ("TNormal", "TNormal"), ("TRoughMetal", "TRoughMetal"))


def _inline_texture_array(binding, slot: str, resolver, source_path: str) -> str:
    from GBXBPY.textures.terrain_layers import is_texture_array

    path = (binding.inline_textures or {}).get(slot) or ""
    if not is_texture_array(path):
        return ""
    if os.path.isabs(path) and library.ensure(path):
        return path
    if resolver is not None:
        return resolver.resolve(path, source_path=source_path) or ""
    return ""


def is_layered_terrain_binding(binding) -> bool:
    """True for vista ``PyPxz_Ids`` bindings whose BaseColor is a texture array."""
    from GBXBPY.textures.terrain_layers import is_texture_array

    return is_texture_array((binding.inline_textures or {}).get("BaseColor") or "")


def _layered_terrain_slots(binding, layers_used, *, resolver=None, source_path="", slot_votes=None):
    """(channel texture arrays, vertex slot -> ``TerrainSlot``) of a vista terrain binding."""
    from GBXBPY.textures.terrain_layers import (
        env_material_dir, resolve_terrain_slots, terrain_layers,
    )

    channels = {
        slot: _inline_texture_array(binding, slot, resolver, source_path)
        for slot in ("BaseColor", "Normal", "RoughMetal", "BlendMask")
    }
    if not channels["BaseColor"]:
        return channels, {}
    layers = terrain_layers(
        channels["BaseColor"], channels["Normal"], channels["RoughMetal"],
        channels["BlendMask"],
    )
    slots = resolve_terrain_slots(
        layers, layers_used, slot_votes, env_material_dir(channels["BaseColor"]),
    )
    return channels, slots


def layered_terrain_top_layers(
    binding, layers_used, *, resolver=None, source_path: str = "", slot_votes=None,
) -> dict:
    """Vertex slot -> ``{"layer", "tile", "media"}`` of the top (Py) layer.

    ``media`` is the environment's ``Media`` folder; terrain decals
    (:mod:`GBXBPY.textures.terrain_decals`) are looked up from it.
    """
    channels, slots = _layered_terrain_slots(
        binding, layers_used, resolver=resolver, source_path=source_path, slot_votes=slot_votes,
    )
    if not slots:
        return {}
    media = os.path.dirname(os.path.dirname(channels["BaseColor"]))
    return {
        slot: {"layer": entry.py.name, "tile": list(entry.py.tile_py), "media": media}
        for slot, entry in slots.items()
    }


def layered_terrain_spec(
    binding,
    layers_used,
    *,
    resolver=None,
    source_path: str = "",
    slot_votes=None,
) -> Optional[MaterialSpec]:
    """Spec for a vista terrain binding restricted to the slots ``layers_used``.

    ``layers_used`` are vertex material slots (``BlendIndices``); mesh
    builders write one ``TMLayer<slot>`` corner attribute per slot. Each
    slot resolves (via ``slot_votes``, see
    :func:`GBXBPY.textures.terrain_layers.resolve_terrain_slots`) to a top
    (Py) layer, emitted as ``Layer<slot>.<Role>``, and a side (Pxz) layer,
    emitted as ``Layer<slot>.Pxz<Role>`` when it differs. Images beyond
    :data:`TERRAIN_IMAGE_BUDGET` are dropped (diffuse first kept, masks
    first dropped).
    """
    channels, slots = _layered_terrain_slots(
        binding, layers_used, resolver=resolver, source_path=source_path, slot_votes=slot_votes,
    )
    if not channels["BaseColor"]:
        return None
    used = [slots[s] for s in sorted(slots)]
    if not used:
        log.warning("no terrain layers %s in %s", sorted(set(layers_used)), channels["BaseColor"])
        return None

    t_refs: dict[str, TextureRef] = {}
    for slot, role in _TERRAIN_T_SLOTS:
        if role in t_refs:
            continue
        path = _resolve_texture_path(
            (binding.inline_textures or {}).get(slot) or "", resolver, source_path,
        )
        if path:
            t_refs[role] = TextureRef(role=role, path=path)

    candidates: list[TextureRef] = []
    for attr, role in (("diffuse", "Diffuse"), ("normal", "Normal"),
                       ("roughmetal", "RoughMetal"), ("mask", "Mask")):
        for entry in used:
            path = getattr(entry.py, attr)
            if path:
                candidates.append(TextureRef(role=f"Layer{entry.slot}.{role}", path=path))
            pxz_path = getattr(entry.pxz, attr)
            if pxz_path and pxz_path != path:
                candidates.append(TextureRef(role=f"Layer{entry.slot}.Pxz{role}", path=pxz_path))
        t_ref = t_refs.get({"Diffuse": "TBaseColor", "Normal": "TNormal",
                            "RoughMetal": "TRoughMetal"}.get(role, ""))
        if t_ref is not None:
            candidates.append(t_ref)
    textures: list[TextureRef] = []
    seen: set[str] = set()
    dropped: list[str] = []
    for ref in candidates:
        if ref.path in seen:
            textures.append(ref)
        elif len(seen) < TERRAIN_IMAGE_BUDGET:
            seen.add(ref.path)
            textures.append(ref)
        else:
            dropped.append(ref.role)
    if dropped:
        log.warning(
            "terrain material over %d-image budget; dropped %s",
            TERRAIN_IMAGE_BUDGET, ", ".join(dropped),
        )

    params: dict[str, float] = {"blend_mask": 1.0 if channels["BlendMask"] else 0.0}
    for entry in used:
        params[f"tile_py_u:{entry.slot}"], params[f"tile_py_v:{entry.slot}"] = entry.py.tile_py
        params[f"tile_pxz_u:{entry.slot}"], params[f"tile_pxz_v:{entry.slot}"] = entry.pxz.tile_pxz
    env = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(channels["BaseColor"]))))
    name = f"{env} Terrain {'+'.join(dict.fromkeys(entry.py.name for entry in used))}"
    if "TBaseColor" in t_refs:
        name += " T"
    return MaterialSpec(
        family=TERRAIN_LAYERS_FAMILY,
        name_hint=name,
        textures=tuple(textures),
        params=params,
        shader_name=_name_hint_from_binding(binding),
    )


def _fixed_terrain_spec(binding, linked_material, resolver) -> Optional[MaterialSpec]:
    """Linked array materials such as SeaFloor select a named layer, not BlendIndices."""
    from GBXBPY.textures.terrain_layers import is_texture_array

    arrays = {}
    for mat in _walk_material_and_parents(linked_material, resolver):
        for slot in getattr(mat, "texture_slots", None) or []:
            if is_texture_array(slot.texture_path):
                arrays.setdefault(slot.name, slot.texture_path)
    if "BaseColor" not in arrays:
        return None
    source = linked_material.source_path or ""
    name = linked_material.name or ""
    fixed_binding = replace(binding, inline_textures=arrays)
    _channels, slots = _layered_terrain_slots(
        fixed_binding, (0,), resolver=resolver, source_path=source, slot_votes={0: {name: 1}},
    )
    entry = slots.get(0)
    if entry is None or entry.name != name:
        log.warning("fixed terrain layer %s unavailable in %s", name, source)
        return None
    textures = []
    params = _params_from_material(linked_material, resolver, binding)
    for prefix, layer, tile in (("Py", entry.py, entry.py.tile_py),
                                ("Pxz", entry.pxz, entry.pxz.tile_pxz)):
        for attr, role in (("diffuse", "BaseColor"), ("normal", "Normal"),
                           ("roughmetal", "RoughMetal")):
            path = getattr(layer, attr)
            if not path:
                continue
            slot = prefix + role
            textures.append(TextureRef(role=slot, path=path))
            params[f"uv_scale_u:{slot}"] = 1.0 / tile[0]
            params[f"uv_scale_v:{slot}"] = 1.0 / tile[1]
    return MaterialSpec(
        family=_PROJECTED_TERRAIN_FAMILY, name_hint=_name_hint_from_binding(binding),
        textures=tuple(textures), params=params, shader_name="Tech3 Block PyPxz_Ids",
    )


def spec_from_binding(
    binding,
    *,
    resolver=None,
    source_path: str = "",
    terrain_layers=None,
    terrain_slot_votes=None,
) -> MaterialSpec:
    """Translate one :class:`MaterialBinding` into a :class:`MaterialSpec`.

    Resolution order for textures:
      1. Inline textures on the binding (when present, used as-is).
      2. Otherwise, follow ``binding.material_path`` through GBXBPY,
         walking parent materials, and collect their texture slots.

    ``terrain_layers`` (vertex material slots used by the mesh) selects the
    layered vista-terrain spec for texture-array bindings;
    ``terrain_slot_votes`` is :attr:`Mesh.terrain_slot_votes`.
    """
    if terrain_layers and is_layered_terrain_binding(binding):
        layered = layered_terrain_spec(
            binding, terrain_layers, resolver=resolver, source_path=source_path,
            slot_votes=terrain_slot_votes,
        )
        if layered is not None:
            return layered
    textures: list[TextureRef] = []

    # 1) Inline textures (when the mesh stored them on the slot itself).
    for slot, rel_path in (binding.inline_textures or {}).items():
        if slot in _IGNORED_SLOTS or (slot.startswith("MulInside") and _is_placeholder(rel_path)):
            continue
        role = _SLOT_TO_ROLE.get(slot) or _role_from_filename(rel_path) or slot
        paths = _resolve_texture_paths(rel_path, resolver, source_path)
        if role == "Inside" and len(paths) > 1:
            textures.extend(TextureRef(role=f"InsideLayer{i}", path=path) for i, path in enumerate(paths))
            continue
        abs_path = _select_texture_path(paths, rel_path, getattr(binding, "texture_array_index", 0))
        if not abs_path:
            log.debug("inline texture not resolved (slot=%s, path=%r)", slot, rel_path)
            continue
        textures.append(TextureRef(role=role, path=abs_path))

    # 2) Linked .Material.Gbx (only if no inline textures available).
    linked_material = None
    if not textures and binding.material_path:
        linked_material = _load_linked_material(
            binding.material_path, resolver, source_path, terrain_remap=True
        )
        if linked_material is not None and not terrain_layers:
            fixed = _fixed_terrain_spec(binding, linked_material, resolver)
            if fixed is not None:
                return fixed
        textures = _collect_textures_from_linked(
            linked_material, resolver, source_path,
            array_index=getattr(binding, "texture_array_index", 0),
        )

    family, shader_name = _family_from_binding(binding, linked_material, resolver=resolver)
    params = _params_from_material(linked_material, resolver, binding)
    grass_surface = _grass_surface(textures)
    dirt_surface = _dirt_surface(textures)
    shader_key = shader_name.lower().replace("_", "").replace(" ", "")

    if family == "Glass" and shader_key.startswith("tech3water") and linked_material is not None:
        source = getattr(linked_material, "source_path", "") or ""
        fog = _vista_water_fog(source)
        if fog:
            # The game's sea shader (screen-space refraction + depth fog) is
            # unavailable; tint by the environment's fog ramp instead of
            # rendering clear Stadium glass.
            family = _VISTA_WATER_FAMILY
            textures = [TextureRef(role="WaterFog", path=fog)]
            if os.path.basename(fog).lower() == "waterfog.dds":
                params.update(_VISTA_LAKE_PARAMS)
        else:
            family = _WATER_SURFACE_FAMILY
            params.update(_water_transmittance_params(source))
    elif family == "Glass" and shader_key == "tech3blockwaterwall":
        family = _WATER_WALL_FAMILY
        params.update(_screen_params(linked_material, resolver, _WATER_WALL_PARAMS))

    def projected_textures() -> list[TextureRef]:
        if linked_material is not None:
            return _collect_textures_from_linked(
                linked_material, resolver, source_path, preserve_projections=True,
            )
        return [
            TextureRef(
                role=_PROJECTED_SLOT_ALIASES.get(slot, slot) if slot in _TERRAIN_PROJECTION_SLOTS else (
                    _SLOT_TO_ROLE.get(slot) or _role_from_filename(path) or slot
                ),
                path=resolved,
            )
            for slot, path in (binding.inline_textures or {}).items()
            if slot not in _IGNORED_SLOTS
            and not (slot.startswith("MulInside") and _is_placeholder(path))
            if (resolved := _resolve_texture_path(path, resolver, source_path))
        ]

    if family == "GrassFence" and linked_material is not None:
        ground = _linked_slot_texture(linked_material, "Diffuse", resolver, source_path)
        if ground:
            textures.append(TextureRef(role="GrassGround", path=ground))
    textures = _grass_x2(textures, params)
    lake_shore = (
        family == "Stadium.Default"
        and "pypxztdiffspecnormpyx2hx2" in shader_key
        and _lake_shore_surface(textures)
    )
    decohill_variant = lake_shore or (
        family == "Stadium.Default"
        and "pypxztdiffspecnormpyx2hx2" in shader_key
        and _decohill_variant_surface(textures)
    )
    if decohill_variant:
        # Without this route the default builder multiplies the T layer
        # (DecoHillDirt_D) over the projected sand as Detail -> orange.
        projected = projected_textures()
        missing = {"PyBaseColor", "PxzBaseColor", "TBaseColor"} - {tex.role for tex in projected}
        if missing:
            log.warning(
                "terrain projection slots missing for %s: %s; retaining default shader",
                binding.material_path, ", ".join(sorted(missing)),
            )
        else:
            textures = projected
            family = _DECOHILL_VARIANT_FAMILY
            params["grass_projection_scale"] = 1.0 / 32.0
            # DecoHillDirtPy R/N and DecoHillIcePy N repeat per metre.
            _store_projected_uv_transforms(projected, params)
            if lake_shore:
                params["t_alpha_mask"] = 1.0
    elif (
        family == "Stadium.Default"
        and (grass_surface or dirt_surface)
        and shader_key not in _PROJECTED_PYPXZ_SHADERS
    ):
        if grass_surface:
            # Keep the dielectric override scoped to verified grass textures.
            params["grass_pbr"] = 1.0
        if "pypxztdiffspecnormpyx2hx2" in shader_key:
            projected = projected_textures()
            required = {"PyBaseColor", "PxzBaseColor", "TBaseColor"}
            missing = required - {tex.role for tex in projected}
            if missing:
                log.warning(
                    "terrain projection slots missing for %s: %s; retaining default shader",
                    binding.material_path, ", ".join(sorted(missing)),
                )
            else:
                textures = projected
                family = "Dirt.PyPxzT" if dirt_surface else "Grass.PyPxzT"
                # DecoHillPy D/R/N and DirtPy D/N store a UV scale of 1/32.
                # Keep it separate from the per-binding texture-size scale.
                params["grass_projection_scale"] = 1.0 / 32.0
                if dirt_surface:
                    # DirtPy_R has its own 1/m scale, unlike DirtPy D/N.
                    params["projected_roughness_scale"] = 1.0
    elif family == "Stadium.Default" and shader_key in _PROJECTED_PYPXZ_SHADERS:
        # World-projected Py/Pxz shader without a T layer (DecoCliff, its
        # modifier variants, Canopy, WaterGround, ThemeRallyCastleWall).
        # The _LM1 variant (modifier TrackWall -> DecoCliff remaps) shares
        # the Block_PyPxz_X2H2_p pixel shader; only the lightmap UV differs.
        # PlatformDirt\PlatformTech (DirtPy_*) also uses _LM1.
        if grass_surface:
            params["grass_pbr"] = 1.0
        projected = projected_textures()
        roles = {tex.role for tex in projected}
        if {"PyBaseColor", "PxzBaseColor"} <= roles:
            textures = projected
            family = _PROJECTED_TERRAIN_FAMILY
            _store_projected_uv_transforms(projected, params)
        else:
            log.warning(
                "projection slots missing for %s: %s; retaining default shader",
                binding.material_path,
                ", ".join(sorted({"PyBaseColor", "PxzBaseColor"} - roles)),
            )
    elif family == "Stadium.Default" and shader_key == _CUSTOM_HUE_SHADER:
        # Item-editor CustomBricks/Grass/Metal...: green-authored Py/Pxz
        # textures hue-shifted to the item's TargetColor.
        projected = projected_textures()
        roles = {tex.role for tex in projected}
        if {"PyBaseColor", "PxzBaseColor"} <= roles:
            textures = projected
            family = _CUSTOM_HUE_FAMILY
            params["grass_projection_scale"] = 1.0
            _store_projected_uv_transforms(projected, params)
            params.update(_custom_target_params(binding, linked_material, resolver))
        else:
            log.warning(
                "projection slots missing for %s: %s; retaining default shader",
                binding.material_path,
                ", ".join(sorted({"PyBaseColor", "PxzBaseColor"} - roles)),
            )
    if any(t.role == "Inside" or t.role.startswith("InsideLayer") for t in textures):
        # A real MulInside image marks a Tech3 *_DispIn screen; its SI slot
        # is the LED-cell mask, not a standalone emissive texture.
        family = _SCREEN_FAMILY
        textures = [
            TextureRef(role="SelfIllumMask", path=t.path) if t.role == "Emissive" else t
            for t in textures
        ]
        params.update(_screen_params(linked_material, resolver))
        textures = _apply_screen_skin(textures, params, resolver)
    if family == _PROJECTED_TERRAIN_FAMILY and any(
        t.role in {"PyBaseColorHueMask", "PxzBaseColorHueMask"} for t in textures
    ):
        hue = _hue_params(linked_material, resolver)
        if hue:
            params.update(hue)
        else:
            log.warning("No palette targets for projected hue material %s", binding.material_path)
    if any(t.role == "HueMask" for t in textures):
        hue_source = linked_material
        if hue_source is None and binding.material_path:
            hue_source = _load_linked_material(binding.material_path, resolver, source_path, terrain_remap=True)
        hue = _hue_params(hue_source, resolver)
        if hue:
            params.update(hue)
        else:
            textures = [t for t in textures if t.role != "HueMask"]
    if any(t.role == "ProjectedHueMask" for t in textures):
        hue = {}
        if family == "Stadium.Default" and not any(t.role == "HueMask" for t in textures):
            hue_source = linked_material
            if hue_source is None and binding.material_path:
                hue_source = _load_linked_material(binding.material_path, resolver, source_path, terrain_remap=True)
            hue = _projected_hue_params(hue_source, resolver)
        if hue:
            params.update(hue)
            textures = [
                TextureRef(role="HueMask", path=t.path) if t.role == "ProjectedHueMask" else t
                for t in textures
            ]
        else:
            textures = [t for t in textures if t.role != "ProjectedHueMask"]
    if log.isEnabledFor(logging.DEBUG):
        slot_count = (
            len(getattr(linked_material, "texture_slots", []) or [])
            if linked_material is not None else 0
        )
        log.debug(
            "spec %s family=%s shader_model=%r textures=%d "
            "(inline=%d, linked=%s, linked_slots=%d, source=%r)",
            _name_hint_from_binding(binding),
            family,
            getattr(binding, "shader_model", "") or "",
            len(textures),
            len(binding.inline_textures or {}),
            "ok" if linked_material is not None else (
                "<no path>" if not binding.material_path else "load-failed"
            ),
            slot_count,
            binding.material_path,
        )

    # Walk parent chain too — flag may live on a base material.
    opacity_alpha = False
    cur = linked_material
    seen_ids: set[int] = set()
    while cur is not None and id(cur) not in seen_ids:
        seen_ids.add(id(cur))
        flags = getattr(cur, "flags", None) or {}
        if flags.get("OpacityIsDiffuseAlpha"):
            opacity_alpha = True
            break
        parent_path = getattr(cur, "base_material_path", "") or ""
        if not parent_path:
            break
        parent_src = getattr(cur, "source_path", "") or ""
        cur = _load_linked_material(parent_path, resolver, parent_src)

    # Vegetation billboard / fence card: alpha is a binary cutout mask.
    alpha_cutout = "FenceA" in (binding.inline_textures or {}) or _has_fence_slot(linked_material, resolver)
    if alpha_cutout:
        opacity_alpha = True

    # Per-instance RGB tint from CPlugMaterialUserInst (game-set value).
    # Falls back to (1,1,1,1) when the binding doesn't carry one.
    color_tint = getattr(binding, "color_tint", ()) or ()
    if len(color_tint) >= 4:
        color = (float(color_tint[0]), float(color_tint[1]),
                 float(color_tint[2]), float(color_tint[3]))
    elif len(color_tint) == 3:
        color = (float(color_tint[0]), float(color_tint[1]),
                 float(color_tint[2]), 1.0)
    else:
        color = (1.0, 1.0, 1.0, 1.0)

    return MaterialSpec(
        family=family,
        name_hint=_name_hint_from_binding(binding),
        color=color,
        textures=tuple(textures),
        params=params,
        physics_id=binding.surface_physic_id or "",
        gameplay_id=binding.surface_gameplay_id or "",
        opacity_from_diffuse_alpha=opacity_alpha,
        alpha_cutout=alpha_cutout,
        alpha_blend=(family == "AlphaBlend"),
        additive=(family == "Additive"),
        shader_name=shader_name,
        func_shaders=_func_shaders_for(linked_material, resolver),
    )


__all__ = [
    "spec_from_binding",
    "set_active_modifier_folders",
    "clear_active_modifier_folders",
    "set_material_context",
    "clear_material_context",
    "set_color_palette",
]


def set_active_modifier_folders(resolver, modifier_paths) -> tuple[str, ...]:
    """Stash resolved modifier ``RemapFolder`` strings on ``resolver``.

    Reads each ``modifier_paths`` entry (typically a block's
    ``material_modifier_path`` / ``material_modifier2_path``) via
    :func:`GBXBPY.resolve.resolve_modifier_folder` and records the
    resulting folders on ``resolver._gbxmat_modifier_folders`` so that
    subsequent :func:`spec_from_binding` calls consult them. Returns
    the folder tuple (may be empty).

    Call :func:`clear_active_modifier_folders` once the block import
    has finished so the override doesn't leak into the next import.
    """
    if resolver is None:
        return ()
    try:
        from GBXBPY.resolve import (
            resolve_modifier_folder, resolve_modifier_materials, resolve_modifier_renames,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("resolve_modifier_folder unavailable: %s", e)
        return ()
    folders: list[str] = []
    filters: dict = {}
    renames: dict = {}
    for path in modifier_paths or ():
        if not path:
            continue
        folder = resolve_modifier_folder(path, resolver)
        if not folder:
            continue
        materials = resolve_modifier_materials(path, resolver)
        key = folder.casefold()
        pairs = {original.casefold(): name for original, name in resolve_modifier_renames(path, resolver)}
        if pairs:
            renames.setdefault(key, {}).update(pairs)
        if key in filters:
            previous = filters[key]
            filters[key] = None if previous is None or materials is None else previous + materials
            continue
        filters[key] = materials
        folders.append(folder)
    folders_tuple = tuple(folders)
    try:
        setattr(resolver, "_gbxmat_modifier_folders", folders_tuple)
        setattr(resolver, "_gbxmat_modifier_materials", filters)
        setattr(resolver, "_gbxmat_modifier_renames", renames)
    except Exception:  # noqa: BLE001
        pass
    return folders_tuple


def clear_active_modifier_folders(resolver) -> None:
    """Remove modifier-folder override previously set on ``resolver``."""
    if resolver is None:
        return
    try:
        setattr(resolver, "_gbxmat_modifier_folders", ())
        setattr(resolver, "_gbxmat_modifier_materials", {})
        setattr(resolver, "_gbxmat_modifier_renames", {})
    except Exception:  # noqa: BLE001
        pass


def set_material_context(resolver, context) -> None:
    """Apply a V4 ``MaterialContext`` (modifier folders + skins) to ``resolver``.

    Every :func:`spec_from_binding` call until
    :func:`clear_material_context` sees the context's remap folders and
    replaces screen inside images with its skin layers.
    """
    if resolver is None:
        return
    folders = tuple(getattr(context, "modifier_folders", ()) or ())
    materials = tuple(getattr(context, "modifier_materials", ()) or ())
    renames = tuple(getattr(context, "modifier_renames", ()) or ())
    skin = getattr(context, "skin", None)
    foreground = getattr(context, "foreground", None)
    setattr(resolver, "_gbxmat_modifier_folders", folders)
    setattr(resolver, "_gbxmat_modifier_materials", {
        folder.casefold(): allowed for folder, allowed in zip(folders, materials)
    })
    setattr(resolver, "_gbxmat_modifier_renames", {
        folder.casefold(): {original.casefold(): name for original, name in pairs}
        for folder, pairs in zip(folders, renames) if pairs
    })
    setattr(resolver, "_gbxmat_skin_context", (skin, foreground) if (skin or foreground) else None)


def clear_material_context(resolver) -> None:
    if resolver is None:
        return
    setattr(resolver, "_gbxmat_modifier_folders", ())
    setattr(resolver, "_gbxmat_modifier_materials", {})
    setattr(resolver, "_gbxmat_modifier_renames", {})
    setattr(resolver, "_gbxmat_skin_context", None)


def set_terrain_material_remap(resolver, remap) -> None:
    """Set (or clear with ``None``) a vista's StadiumOnTerrain material remap.

    ``remap`` maps normcased absolute original material paths to the
    absolute path of their vista copy (see
    ``GBXBPY.resolve.modifier_lookup.resolve_terrain_material_remap``).
    """
    if resolver is not None:
        setattr(resolver, "_gbxmat_terrain_remap", dict(remap or {}))


def set_color_palette(resolver, palette) -> None:
    """Set (or clear with ``None``) the map palette used for hue targets."""
    if resolver is not None:
        setattr(resolver, "_gbxmat_color_palette", palette or None)


_HUE_TARGET_PARAMS = frozenset({"BaseColorTarget", "BaseColorTarget_sRGB"})


def _hue_params(linked_material, resolver) -> dict:
    """Return the five palette targets for a placement-coloured material.

    The game recolours only materials with a ``BaseColorTarget`` param;
    ``Material.color_table_path`` picks the table (``Default`` otherwise).
    """
    if linked_material is None:
        return {}
    chain = list(_walk_material_and_parents(linked_material, resolver))
    if not any(
        getattr(p, "name", "") in _HUE_TARGET_PARAMS
        for mat in chain for p in (getattr(mat, "shader_params", None) or ())
    ):
        return {}
    owner = next((m for m in chain if getattr(m, "color_table_path", "")), None)
    try:
        from GBXBPY.color_tables import palette_targets
    except Exception as e:  # noqa: BLE001
        log.debug("color tables unavailable: %s", e)
        return {}
    targets = palette_targets(
        resolver,
        getattr(owner, "color_table_path", "") if owner else "",
        getattr(resolver, "_gbxmat_color_palette", None) or "Classic",
        source_path=(getattr(owner, "source_path", "") or "") if owner else "",
    )
    if not targets:
        return {}
    return {
        f"hue_target{index}_{channel}": float(value)
        for index, rgb in enumerate(targets, start=1)
        for channel, value in zip("rgb", rgb)
    }


def _projected_hue_params(linked_material, resolver) -> dict:
    """Targets for Py/Pxz hue materials (``BaseColorTarget_PyPxz``).

    Only element colours 1..5 are recoloured; ``Default`` keeps the
    original texture (TrackWall's natural wood). See
    :func:`GBXBPY.color_tables.projected_palette_targets`.
    """
    if linked_material is None:
        return {}
    owner = None
    target_id = 0
    for mat in _walk_material_and_parents(linked_material, resolver):
        params = {
            getattr(p, "name", ""): getattr(p, "value", None)
            for p in (getattr(mat, "shader_params", None) or ())
        }
        if "BaseColorTarget_PyPxz" in params:
            owner = mat
            target_id = _first_number(params.get("BaseColorTargetId"))
            break
    if owner is None:
        return {}
    try:
        from GBXBPY.color_tables import projected_palette_targets
    except Exception as e:  # noqa: BLE001
        log.debug("color tables unavailable: %s", e)
        return {}
    targets = projected_palette_targets(
        resolver,
        getattr(owner, "source_path", "") or "",
        getattr(resolver, "_gbxmat_color_palette", None) or "Classic",
        int(target_id),
    )
    if not targets:
        return {}
    return {
        f"hue_target{index}_{channel}": float(value)
        for index, rgb in enumerate(targets)
        if index > 0
        for channel, value in zip("rgb", rgb)
    }


def _first_number(value) -> float:
    """First scalar in a (possibly nested) shader-param value, else 0."""
    while isinstance(value, (list, tuple)) and value:
        value = value[0]
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
