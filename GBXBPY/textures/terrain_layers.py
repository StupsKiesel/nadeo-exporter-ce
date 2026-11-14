"""Vista terrain texture-array layers.

Vista terrain materials (``Tech3 Block PyPxz_Ids*``) bind one
``*.TextureArray.Gbx`` per channel (``Terrain_D``, ``_N``, ``_R``, ``_M``).
Each texture array lists its layer images, in layer order, through chunk
``0x09011034.layerNodeIndices`` (external-node indices of the ``.dds``
files). External node 2 is the shared ``*.ImageArray.Gbx`` that carries
the per-layer world tile sizes (``u01`` = top/Py, ``u03`` = side/Pxz, in
metres). Mesh vertices select a per-prefab *material slot* through
``BlendIndices`` bytes 0/1; :func:`resolve_terrain_slots` maps slots to
layers via collision-shape votes (:mod:`GBXBPY.load.terrain_slots`) and
the vista material definitions (:func:`vista_material_layers`).

The ``TerrainLayers_DNRM`` image arrays are pack-encrypted after byte 256 when
extracted with plain ``Fids::Extract``; :func:`read_image_array` asks the
library provider for one hook re-extraction, which returns the plain file.
Until then NICE decodes and marks the leading complete layer records, and
:func:`read_image_array` also has a raw fallback for other malformed files.
Layers whose tile size cannot be recovered use :data:`FALLBACK_TILE`.
"""

from __future__ import annotations

import logging
import math
import os
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from .. import library

log = logging.getLogger("gbxbpy.textures.terrain_layers")

IMAGE_ARRAY_CHUNK = 0x0914C000
TEXTURE_ARRAY_CHUNK = 0x09011034
MATERIAL_CUSTOM_CHUNK = 0x09079007
MATERIAL_LAYERS_CHUNK = 0x0903A015

# Tile size (metres) for layers whose ImageArray entry is unreadable.
FALLBACK_TILE: Tuple[float, float] = (64.0, 64.0)


@dataclass(frozen=True)
class ImageArrayLayer:
    """One ``CPlugImageArray`` layer. ``None`` tiles were unreadable."""

    name: str
    tile_py: Optional[Tuple[float, float]]
    tile_pxz: Optional[Tuple[float, float]]


@dataclass(frozen=True)
class TextureArrayInfo:
    """Layer images of one ``*.TextureArray.Gbx`` in layer order."""

    channel_suffix: str
    layers: Tuple[Tuple[str, Optional[str]], ...]  # (layer name, abs image path)
    image_array: Optional[str]                      # abs path of node 2


@dataclass(frozen=True)
class TerrainLayer:
    """Everything a material needs to render one terrain layer."""

    index: int
    name: str
    diffuse: Optional[str] = None
    normal: Optional[str] = None
    roughmetal: Optional[str] = None
    mask: Optional[str] = None
    tile_py: Tuple[float, float] = FALLBACK_TILE
    tile_pxz: Tuple[float, float] = FALLBACK_TILE
    tile_known: bool = False


@dataclass(frozen=True)
class VistaMaterialLayers:
    """Texture-array layer names of one vista terrain material.

    From ``CPlugMaterialCustom`` chunk ``0x0903A015`` of
    ``<env>/Media/Material/<name>.Material.Gbx``: ``pxz`` is projected on
    sides, ``py`` on top; ``x2`` / ``h2`` are macro layers (unused).
    """

    pxz: str
    py: str
    x2: str = ""
    h2: str = ""


def env_material_dir(texture_array_path: str) -> str:
    """``<env>/Media/Material`` for ``<env>/Media/Texture Array/X.TextureArray.Gbx``."""
    return os.path.join(os.path.dirname(os.path.dirname(texture_array_path)), "Material")


@lru_cache(maxsize=512)
def vista_material_layers(material_dir: str, name: str) -> Optional[VistaMaterialLayers]:
    """Layer names of vista material ``name``, or ``None`` (missing / no layers)."""
    from ..io import read_gbx

    path = os.path.join(material_dir, f"{name}.Material.Gbx")
    if not name or not library.ensure(path):
        return None
    try:
        parsed = read_gbx(path, recursive=False)
        custom = parsed.body[MATERIAL_CUSTOM_CHUNK].custom_material
        chunk = custom.body[MATERIAL_LAYERS_CHUNK]
        names = [str(getattr(chunk, f"u0{i}", "") or "") for i in (2, 3, 4, 5)]
    except Exception as exc:  # noqa: BLE001 - corrupt or non-terrain material
        log.debug("vista material without layers: %s (%s)", path, exc)
        return None
    if not names[0] and not names[1]:
        return None
    return VistaMaterialLayers(names[0] or names[1], names[1] or names[0], names[2], names[3])


def is_texture_array(path: str) -> bool:
    return bool(path) and path.lower().endswith(".texturearray.gbx")


def _layer_name(image_ref: str, suffix: str) -> str:
    stem = os.path.basename(image_ref).split(".", 1)[0]
    if suffix and stem.lower().endswith(suffix.lower()):
        stem = stem[: -len(suffix)]
    return stem


IMAGE_EXTENSIONS = (".dds", ".tga", ".png", ".webp", ".jpg", ".jpeg")


def _external_nodes(parsed, path: str) -> Dict[int, str]:
    folder = os.path.dirname(path)
    nodes: Dict[int, str] = {}
    table = getattr(parsed, "referenceTable", None)
    for node in getattr(table, "externalNodes", None) or []:
        rel = getattr(node, "_computedRelativePath", "") or getattr(node, "ref", "") or ""
        nodes[int(node.nodeIndex)] = os.path.normpath(os.path.join(folder, rel))
    return nodes


@lru_cache(maxsize=256)
def wrapper_image_reference(path: str) -> Optional[str]:
    """Image file a non-array ``.Texture.Gbx`` references, whatever its name.

    Most wrappers sit next to ``Image/<same stem>``; some reference another
    image (``BlueBay\\..\\WaterSxSySzPC3`` -> ``Techno\\..\\SeaHeightPC3.tga``).
    Returns ``None`` for arrays, unreadable files and wrappers without an
    image (render targets, runtime-generated textures). The image may be
    absent from the library; callers check availability.
    """
    from ..io import read_gbx

    try:
        parsed = read_gbx(path, recursive=False)
    except Exception as exc:  # noqa: BLE001 - corrupt files must not abort imports
        log.debug("texture wrapper unreadable: %s (%s)", path, exc)
        return None
    body = getattr(parsed, "body", None) or {}
    chunk = body.get(TEXTURE_ARRAY_CHUNK) if hasattr(body, "get") else None
    if chunk is not None and getattr(chunk, "layerNodeIndices", None):
        return None
    images = [p for p in _external_nodes(parsed, path).values()
              if p.lower().endswith(IMAGE_EXTENSIONS)]
    if len(images) != 1:
        return None
    return images[0]


@lru_cache(maxsize=64)
def read_texture_array(path: str) -> Optional[TextureArrayInfo]:
    """Return ordered CPlugBitmap layers, including arrays named ``.Texture.Gbx``."""
    from ..io import read_gbx

    try:
        parsed = read_gbx(path, recursive=False)
    except Exception as exc:  # noqa: BLE001 - corrupt files must not abort imports
        log.warning("texture array unreadable: %s (%s)", path, exc)
        return None
    body = getattr(parsed, "body", None) or {}
    chunk = body.get(TEXTURE_ARRAY_CHUNK) if hasattr(body, "get") else None
    indices = list(getattr(chunk, "layerNodeIndices", None) or []) if chunk else []
    if not indices:
        if is_texture_array(path):
            log.warning("texture array has no layer list: %s", path)
        else:
            # Plain CPlugBitmap wrappers without layers: render targets and
            # runtime-generated textures (SeaRender*, PostFx*, ACosSmooth).
            log.debug("texture wrapper has no layer list: %s", path)
        return None
    suffix = str(getattr(chunk, "channelSuffix", "") or "")
    nodes = _external_nodes(parsed, path)

    def existing(p: Optional[str]) -> Optional[str]:
        return p if p and library.ensure(p) else None

    layers = []
    for index in indices:
        image = nodes.get(int(index))
        layers.append((_layer_name(image or "", suffix), existing(image)))
    return TextureArrayInfo(suffix, tuple(layers), existing(nodes.get(2)))


def _plausible(value: float) -> bool:
    return math.isfinite(value) and (value == 0.0 or 0.5 <= abs(value) <= 1.0e4)


def _tile(x: float, y: float) -> Optional[Tuple[float, float]]:
    if x and y and _plausible(x) and _plausible(y):
        return (float(x), float(y))
    return None


def salvage_image_array(data: bytes) -> List[ImageArrayLayer]:
    """Decode the leading plausible layers of a raw (uncompressed) image array.

    Stops at the first layer whose name or fields look corrupt; incomplete
    records are not returned.
    """
    marker = IMAGE_ARRAY_CHUNK.to_bytes(4, "little")
    pos = data.find(marker)
    while pos >= 0 and data[pos + 4:pos + 8] != (7).to_bytes(4, "little"):
        pos = data.find(marker, pos + 1)
    if pos < 0:
        return []
    pos += 8

    def read_string() -> Optional[str]:
        nonlocal pos
        if pos + 4 > len(data):
            return None
        (length,) = struct.unpack_from("<I", data, pos)
        if length > 256 or pos + 4 + length > len(data):
            return None
        raw = data[pos + 4:pos + 4 + length]
        if not all(32 <= b < 127 for b in raw):
            return None
        pos += 4 + length
        return raw.decode("ascii")

    if read_string() is None or pos + 4 > len(data):
        return []
    (count,) = struct.unpack_from("<I", data, pos)
    pos += 4
    layers: List[ImageArrayLayer] = []
    for _ in range(min(count, 64)):
        name = read_string()
        if not name or pos + 60 > len(data):
            break
        floats = struct.unpack_from("<13f", data, pos)
        ints = struct.unpack_from("<2I", data, pos + 52)
        pos += 60
        if not (all(_plausible(f) for f in floats) and all(i < 1 << 16 for i in ints)):
            break
        layers.append(ImageArrayLayer(
            name, _tile(floats[0], floats[1]), _tile(floats[4], floats[5]),
        ))
    return layers


@lru_cache(maxsize=64)
def read_image_array(path: str) -> Tuple[ImageArrayLayer, ...]:
    """Return image-array layers, salvaging corrupt files where possible."""
    from ..io import read_gbx

    try:
        parsed = read_gbx(path, recursive=False)
        should_retry = False
        if getattr(parsed, "_errors", None):
            try:
                should_retry = library.refetch(path)
            except Exception as exc:  # noqa: BLE001 - retain usable old layers
                log.warning("image array refetch failed: %s (%s)", path, exc)
        if should_retry:
            try:
                refreshed = read_gbx(path, recursive=False)
                previous = getattr(parsed, "body", {}).get(IMAGE_ARRAY_CHUNK)
                current = getattr(refreshed, "body", {}).get(IMAGE_ARRAY_CHUNK)
                if (not getattr(refreshed, "_errors", None)
                        or len(getattr(current, "layers", None) or [])
                        >= len(getattr(previous, "layers", None) or [])):
                    parsed = refreshed
            except Exception as exc:  # noqa: BLE001 - retain usable old layers
                log.warning("refetched image array unreadable: %s (%s)", path, exc)
        body = getattr(parsed, "body", None) or {}
        chunk = body.get(IMAGE_ARRAY_CHUNK) if hasattr(body, "get") else None
        raw_layers = getattr(chunk, "layers", None) if chunk else None
    except Exception:  # noqa: BLE001 - fall through to the salvage decoder
        raw_layers = None
    if raw_layers:
        layers = tuple(
            ImageArrayLayer(
                str(layer.name),
                _tile(layer.u01.x, layer.u01.y),
                _tile(layer.u03.x, layer.u03.y),
            )
            for layer in raw_layers
        )
        if getattr(chunk, "_partial", False):
            log.warning(
                "image array %s is incomplete (pack-encrypted extract?); salvaged %d layer(s): %s. "
                "Connect Trackmania with the Openplanet plugin to re-extract it",
                os.path.basename(path), len(layers), ", ".join(layer.name for layer in layers) or "-",
            )
        return layers
    try:
        with open(path, "rb") as handle:
            layers = salvage_image_array(handle.read())
    except OSError as exc:
        log.warning("image array unreadable: %s (%s)", path, exc)
        return ()
    log.warning(
        "image array %s is incomplete or unsupported; salvaged %d layer(s): %s",
        os.path.basename(path), len(layers), ", ".join(l.name for l in layers) or "-",
    )
    return tuple(layers)


@lru_cache(maxsize=32)
def terrain_layers(
    base_color: str,
    normal: str = "",
    roughmetal: str = "",
    mask: str = "",
) -> Tuple[TerrainLayer, ...]:
    """Combine D/N/R/M texture arrays and their image array into layers."""
    diffuse_info = read_texture_array(base_color)
    if diffuse_info is None:
        return ()
    channels = {
        "normal": read_texture_array(normal) if normal else None,
        "roughmetal": read_texture_array(roughmetal) if roughmetal else None,
        "mask": read_texture_array(mask) if mask else None,
    }
    tiles: Dict[str, ImageArrayLayer] = {}
    ordered: Tuple[ImageArrayLayer, ...] = ()
    if diffuse_info.image_array:
        ordered = read_image_array(diffuse_info.image_array)
        tiles = {layer.name.lower(): layer for layer in ordered}

    names = {name.lower() for name, _ in diffuse_info.layers}
    result = []
    fallback_names = []
    for index, (name, image) in enumerate(diffuse_info.layers):
        extra = {}
        for key, info in channels.items():
            if info is not None and index < len(info.layers):
                extra[key] = info.layers[index][1]
        entry = tiles.get(name.lower())
        if entry is None and index < len(ordered) and ordered[index].name.lower() not in names:
            # Unmatched names: both lists share the canonical layer order.
            entry = ordered[index]
        py = entry.tile_py if entry else None
        pxz = (entry.tile_pxz if entry else None) or py
        if py is None:
            fallback_names.append(name)
        result.append(TerrainLayer(
            index=index,
            name=name,
            diffuse=image,
            tile_py=py or FALLBACK_TILE,
            tile_pxz=pxz or FALLBACK_TILE,
            tile_known=py is not None,
            **extra,
        ))
    if fallback_names:
        log.warning(
            "terrain layers without tile size (using %gm): %s",
            FALLBACK_TILE[0], ", ".join(fallback_names),
        )
    return tuple(result)


@dataclass(frozen=True)
class TerrainSlot:
    """Texture-array layers one vertex material slot renders with."""

    slot: int
    name: str
    py: TerrainLayer
    pxz: TerrainLayer


def resolve_terrain_slots(
    layers: Tuple[TerrainLayer, ...],
    slots,
    slot_votes: Optional[Dict[int, Dict[str, int]]] = None,
    material_dir: str = "",
) -> Dict[int, TerrainSlot]:
    """Map vertex material slots to (Py, Pxz) texture-array layers.

    Each slot takes its most-voted collision material that has layer names
    (see :mod:`GBXBPY.load.terrain_slots`). Slots without usable votes fall
    back to the texture-array layer with the same index.
    """
    by_index = {layer.index: layer for layer in layers}
    by_name = {layer.name.lower(): layer for layer in layers}
    result: Dict[int, TerrainSlot] = {}
    for slot in sorted(set(slots)):
        votes = (slot_votes or {}).get(slot) or {}
        for name, _count in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0])):
            defs = vista_material_layers(material_dir, name) if material_dir else None
            if defs is None:
                continue
            py, pxz = by_name.get(defs.py.lower()), by_name.get(defs.pxz.lower())
            if py is None and pxz is None:
                continue
            result[slot] = TerrainSlot(slot, name, py or pxz, pxz or py)
            break
        else:
            layer = by_index.get(slot)
            if layer is not None:
                result[slot] = TerrainSlot(slot, layer.name, layer, layer)
    return result


__all__ = [
    "FALLBACK_TILE",
    "ImageArrayLayer",
    "TextureArrayInfo",
    "TerrainLayer",
    "TerrainSlot",
    "VistaMaterialLayers",
    "env_material_dir",
    "is_texture_array",
    "read_texture_array",
    "wrapper_image_reference",
    "read_image_array",
    "resolve_terrain_slots",
    "salvage_image_array",
    "terrain_layers",
    "vista_material_layers",
]
