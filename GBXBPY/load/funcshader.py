"""Loader for ``CFuncShaderLayerUV`` (``0x05015000``) and
``CFuncShaderArray`` (``0x05014000``).

A ``.FuncShader.Gbx`` file describes how a material slot is animated
(UV scroll, atlas frame cycle, parametric transform). See the repo
memory ``funcshader-files.md`` for the chunk-level reference.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..model import FuncShader, FuncShaderKind
from ..registry import register

log = logging.getLogger("gbxbpy.load.funcshader")

CLASS_LAYER_UV = 0x05015000
CLASS_ARRAY = 0x05014000

# CFuncPlug header
_CHUNK_PLUG = 0x0500B005

# CFuncShaderArray
_CHUNK_ARRAY = 0x05014000

# CFuncShaderLayerUV
_CHUNK_LAYER_BASE = 0x05015005
_CHUNK_UV_SCROLL = 0x05015009          # u01=scale vec2, u02=speed vec2
_CHUNK_UV_OFFSET_SCROLL = 0x0501500D   # u01=offset, u02=scale, u03=speed
_CHUNK_UV_VARIANT_13 = 0x05015013      # 3x vec2 (offset/scale/speed variant)
_CHUNK_UV_VARIANT_14 = 0x05015014      # vec2 + 2 floats
_CHUNK_ATLAS = 0x05015015              # frames, cols, rows, flag


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
    # Strip .FuncShader.Gbx (or any double-extension)
    return base.split(".", 1)[0] if base else ""


def _vec2(node: Any) -> tuple[float, float]:
    if node is None:
        return (0.0, 0.0)
    x = getattr(node, "x", None)
    y = getattr(node, "y", None)
    if x is None or y is None:
        # Some construct outputs are list-like (Container ListContainer)
        try:
            return (float(node[0]), float(node[1]))
        except Exception:  # noqa: BLE001
            return (0.0, 0.0)
    return (float(x), float(y))


def _populate_layer(fs: FuncShader, body: dict) -> None:
    """Fill chunk-derived fields from a ``CFuncShaderLayerUV`` body."""
    plug = body.get(_CHUNK_PLUG)
    if plug is not None:
        fs.period = float(getattr(plug, "period", 1.0) or 1.0)
        fs.phase = float(getattr(plug, "phase", 0.0) or 0.0)
        fs.randomize_phase = bool(getattr(plug, "randomizePhase", False))
        fs.auto_create_motion = bool(getattr(plug, "autoCreateMotion", True))
        fs.input_val_id = str(getattr(plug, "inputValId", "") or "")

    base = body.get(_CHUNK_LAYER_BASE)
    if base is not None:
        fs.layer_name = str(getattr(base, "layerName", "") or "")
        fs.layer_mode = int(getattr(base, "u01", 0) or 0)

    scroll = body.get(_CHUNK_UV_SCROLL)
    if scroll is not None:
        fs.uv_scale = _vec2(getattr(scroll, "u01", None))
        fs.uv_speed = _vec2(getattr(scroll, "u02", None))

    offset_scroll = body.get(_CHUNK_UV_OFFSET_SCROLL)
    if offset_scroll is not None:
        fs.uv_offset = _vec2(getattr(offset_scroll, "u01", None))
        fs.uv_scale = _vec2(getattr(offset_scroll, "u02", None))
        fs.uv_speed = _vec2(getattr(offset_scroll, "u03", None))

    variant_13 = body.get(_CHUNK_UV_VARIANT_13)
    if variant_13 is not None:
        fs.uv_offset = _vec2(getattr(variant_13, "u01", None))
        fs.uv_scale = _vec2(getattr(variant_13, "u02", None))
        fs.uv_speed = _vec2(getattr(variant_13, "u03", None))

    atlas = body.get(_CHUNK_ATLAS)
    if atlas is not None:
        fs.atlas_frames = int(getattr(atlas, "u01", 0) or 0)
        fs.atlas_cols = int(getattr(atlas, "u02", 0) or 0)
        fs.atlas_rows = int(getattr(atlas, "u03", 0) or 0)
        fs.atlas_flag = int(getattr(atlas, "u04", 0) or 0)


def _load_inline(node: Any) -> FuncShader | None:
    """Build a :class:`FuncShader` from an inline NodeRef container."""
    cid = getattr(node, "classId", None)
    body = _body_dict(node)
    if cid == CLASS_LAYER_UV:
        fs = FuncShader(class_id=cid, raw=node)
        _populate_layer(fs, body)
        fs.refresh_kind()
        return fs
    if cid == CLASS_ARRAY:
        fs = FuncShader(class_id=cid, raw=node, shader_kind=FuncShaderKind.COMPOSITE)
        plug = body.get(_CHUNK_PLUG)
        if plug is not None:
            fs.period = float(getattr(plug, "period", 1.0) or 1.0)
            fs.phase = float(getattr(plug, "phase", 0.0) or 0.0)
            fs.input_val_id = str(getattr(plug, "inputValId", "") or "")
        arr = body.get(_CHUNK_ARRAY)
        for child_ref in getattr(arr, "funcShaders", None) or []:
            child = _load_inline(child_ref)
            if child is not None:
                fs.children.append(child)
        return fs
    return None


@register(CLASS_LAYER_UV)
def load_funcshader_layer(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> FuncShader:
    body = _body_dict(parsed)
    fs = FuncShader(
        class_id=CLASS_LAYER_UV,
        source_path=source_path or None,
        name=_file_stem(source_path),
        raw=parsed,
    )
    _populate_layer(fs, body)
    fs.refresh_kind()
    log.debug(
        "FuncShaderLayerUV %s: kind=%s layer=%r period=%.3f atlas=%dx%d/%d",
        fs.name, fs.shader_kind.value, fs.layer_name, fs.period,
        fs.atlas_cols, fs.atlas_rows, fs.atlas_frames,
    )
    return fs


@register(CLASS_ARRAY)
def load_funcshader_array(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> FuncShader:
    body = _body_dict(parsed)
    fs = FuncShader(
        class_id=CLASS_ARRAY,
        source_path=source_path or None,
        name=_file_stem(source_path),
        raw=parsed,
        shader_kind=FuncShaderKind.COMPOSITE,
    )
    plug = body.get(_CHUNK_PLUG)
    if plug is not None:
        fs.period = float(getattr(plug, "period", 1.0) or 1.0)
        fs.phase = float(getattr(plug, "phase", 0.0) or 0.0)
        fs.input_val_id = str(getattr(plug, "inputValId", "") or "")
    arr = body.get(_CHUNK_ARRAY)
    for child_ref in getattr(arr, "funcShaders", None) or []:
        child = _load_inline(child_ref)
        if child is not None:
            fs.children.append(child)
    log.debug(
        "FuncShaderArray %s: %d children, period=%.3f",
        fs.name, len(fs.children), fs.period,
    )
    return fs


__all__ = ["load_funcshader_layer", "load_funcshader_array",
           "CLASS_LAYER_UV", "CLASS_ARRAY"]
