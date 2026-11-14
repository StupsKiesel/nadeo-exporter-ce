"""Loader for ``CPlugMaterial`` (class id ``0x09079000``).

Reads the material's chunks out of the parsed NICE container and produces
a :class:`~GBXBPY.model.material.Material`. Inheritance resolution and
shader-family inference are intentionally **not** done here — those are
post-load passes in :mod:`GBXBPY.normalize.materials`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

from ..model import Material, ShaderParam, TextureSlot
from ..registry import register

log = logging.getLogger("gbxbpy.load.material")

CLASS_ID = 0x09079000

# CPlugMaterial body chunks
_CHUNK_SCALE = 0x09079010
_CHUNK_FEATURES = 0x09079011
_CHUNK_BASE_PATH = 0x09079012
_CHUNK_BASE_LINK = 0x09079015
_CHUNK_CUSTOM = 0x09079007

# CPlugMaterialCustom inner chunks
_CHUNK_SHADER_PARAMS = 0x0903A00A
_CHUNK_BOOL_FLAGS = 0x0903A00C
_CHUNK_TEXTURES = 0x0903A013


def _body_dict(parsed: Any) -> dict:
    body = getattr(parsed, "body", None)
    if body is None:
        return {}
    try:
        return dict(body)
    except (TypeError, ValueError):
        return {}


def _file_stem(path: str) -> str:
    if not path:
        return ""
    base = os.path.basename(path)
    return base.split(".", 1)[0] if base else ""


def _texture_path_from_node(node: Any) -> str:
    """Pull the texture's GameData-relative path out of a NodeRef stub."""
    if node is None:
        return ""
    for attr in ("gamedata_path", "filePath", "filepath"):
        value = getattr(node, attr, "")
        if value:
            return str(value)
    rel = getattr(node, "_relativeFilePath", "") or ""
    if rel:
        parts = rel.replace("\\", "/").split("/")
        while parts and parts[0] in ("..", ""):
            parts.pop(0)
        return "/".join(parts)
    return ""


def _extract_base_path(body: dict) -> str:
    chunk = body.get(_CHUNK_BASE_PATH)
    if chunk is not None:
        raw = getattr(chunk, "baseMaterialPath", None) or getattr(chunk, "u01", None)
        if raw:
            return str(raw)
    chunk = body.get(_CHUNK_BASE_LINK)
    if chunk is not None:
        bm = getattr(chunk, "baseMaterial", None)
        if bm is not None and getattr(bm, "_index", None) != -1:
            return bm if isinstance(bm, str) else _texture_path_from_node(bm)
    return ""


def _extract_features(body: dict) -> list[str]:
    chunk = body.get(_CHUNK_FEATURES)
    if chunk is None:
        return []
    raw = getattr(chunk, "features", None) or getattr(chunk, "u01", None) or []
    return [str(f) for f in raw]


def _extract_scale(body: dict) -> float:
    chunk = body.get(_CHUNK_SCALE)
    if chunk is None:
        return 1.0
    return float(getattr(chunk, "scale", getattr(chunk, "u01", 1.0)) or 1.0)


def _custom_body(body: dict) -> dict:
    """Return the inner chunk dict of CPlugMaterialCustom, or ``{}``."""
    chunk = body.get(_CHUNK_CUSTOM)
    if chunk is None:
        return {}
    cm_ref = getattr(chunk, "custom_material", None)
    if cm_ref is not None and hasattr(cm_ref, "body"):
        inner = cm_ref.body
    else:
        inner = chunk
    if isinstance(inner, dict):
        return inner
    if hasattr(inner, "body") and isinstance(inner.body, dict):
        return inner.body
    return {}


def _extract_shader_params(custom: dict) -> list[ShaderParam]:
    chunk = custom.get(_CHUNK_SHADER_PARAMS)
    if chunk is None:
        return []
    out: list[ShaderParam] = []
    for group_attr in ("u01", "u02"):
        for entry in getattr(chunk, group_attr, None) or []:
            name = str(getattr(entry, "u01", "") or "")
            if not name:
                continue
            out.append(ShaderParam(name=name, value=getattr(entry, "u03", None)))
    return out


def _extract_flags(custom: dict) -> dict[str, bool]:
    chunk = custom.get(_CHUNK_BOOL_FLAGS)
    if chunk is None:
        return {}
    out: dict[str, bool] = {}
    for entry in getattr(chunk, "u01", None) or []:
        name = str(getattr(entry, "name", "") or "")
        value = getattr(entry, "u01", None)
        if name and isinstance(value, bool):
            out[name] = value
    return out


def _extract_texture_slots(custom: dict) -> list[TextureSlot]:
    chunk = custom.get(_CHUNK_TEXTURES)
    if chunk is None:
        return []
    raw = getattr(chunk, "textures", None)
    if raw is None:
        raw = chunk if isinstance(chunk, (list, tuple)) else []
    slots: list[TextureSlot] = []
    for entry in raw:
        slots.append(TextureSlot(
            name=str(getattr(entry, "name", "") or ""),
            texture_path=_texture_path_from_node(getattr(entry, "textureNod", None)),
        ))
    return slots


def _extract_color_table(parsed: Any) -> str:
    """Return the referenced colour-target table path, if any."""
    table = getattr(parsed, "referenceTable", None)
    for node in getattr(table, "externalNodes", None) or ():
        path = str(getattr(node, "_computedRelativePath", "") or "")
        if path.lower().endswith(".colortable.gbx.json"):
            return path
    return ""


@register(CLASS_ID)
def load_material(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> Material:
    """Build a :class:`Material` from a parsed CPlugMaterial container."""
    body = _body_dict(parsed)
    custom = _custom_body(body)

    mat = Material(
        source_path=source_path or None,
        class_id=CLASS_ID,
        name=_file_stem(source_path),
        raw=parsed,
        base_material_path=_extract_base_path(body),
        texture_size=_extract_scale(body),
        features=_extract_features(body),
        flags=_extract_flags(custom),
        shader_params=_extract_shader_params(custom),
        texture_slots=_extract_texture_slots(custom),
        color_table_path=_extract_color_table(parsed),
    )

    log.debug(
        "Loaded material %s base=%r slots=%d params=%d flags=%d features=%d",
        mat.name, mat.base_material_path,
        len(mat.texture_slots), len(mat.shader_params),
        len(mat.flags), len(mat.features),
    )
    return mat


__all__ = ["load_material", "CLASS_ID"]
