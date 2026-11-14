"""Loader for ``CPlugFxSystem`` (``*.FxSys.Gbx``) and its particle models.

Normal Openplanet extraction returns pack-encrypted fogger/sparkler effect
files; NICE then stops inside the body. :func:`read_particle_model` and
:func:`read_fx_system` ask the library to hook-extract such a file once
(:func:`GBXBPY.library.refetch`) and parse it again.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .. import library
from ..errors import GbxbpyError
from ..model.fx import FxEmitter, FxSystem, FxVar, FxVarUpdate
from ..model.particle import ParticleEmitterModel
from ..registry import register
from ._gbx_helpers import body_dict, external_node_paths, file_stem

log = logging.getLogger("gbxbpy.load.fx")

FX_SYSTEM_CLASS_ID = 0x0915C000
_CHUNK_SYSTEM = 0x0915C000
_FLOAT_VAR_TYPES = ("Float",)
_INT_VAR_TYPES = ("Int8", "Int16", "Int32", "Bool8", "Enum8", "Enum16", "Enum32")


def _parse_clean(path: str) -> Optional[Any]:
    """NICE container of ``path`` without parser errors, refetching once."""
    from ..io import read_gbx

    library.ensure(path)
    for attempt in range(2):
        try:
            parsed = read_gbx(path, recursive=False)
        except GbxbpyError as exc:
            parsed, error = None, str(exc)
        else:
            error = "; ".join(map(str, getattr(parsed, "_errors", None) or ()))
        if parsed is not None and not error:
            return parsed
        if attempt == 0 and library.refetch(path):
            continue
        log.warning("Effect file unreadable: %s (%s)", path, error or "parse failed")
        return None
    return None


_particle_cache: dict = {}
_fx_cache: dict = {}


def read_particle_model(path: str) -> Optional[ParticleEmitterModel]:
    key = os.path.normcase(os.path.abspath(path))
    if key not in _particle_cache:
        from .particle import load_particle_emitter_model

        parsed = _parse_clean(path)
        model = (
            load_particle_emitter_model(parsed, source_path=os.path.abspath(path))
            if parsed is not None and parsed.classId == 0x090B3000 else None
        )
        for emission in model.emissions if model is not None else ():
            emission.texture_image = _texture_image(emission.texture_path)
        _particle_cache[key] = model
    return _particle_cache[key]


def _texture_image(texture_path: str) -> str:
    from ..textures.terrain_layers import IMAGE_EXTENSIONS, wrapper_image_reference

    if not texture_path:
        return ""
    if texture_path.lower().endswith(IMAGE_EXTENSIONS):
        image = texture_path
    elif library.ensure(texture_path):
        image = wrapper_image_reference(texture_path) or ""
    else:
        return ""
    return image if image and library.ensure(image) else ""


def read_fx_system(path: str) -> Optional[FxSystem]:
    """Load an FxSystem and its emitters' particle models, or ``None``."""
    key = os.path.normcase(os.path.abspath(path))
    if key not in _fx_cache:
        parsed = _parse_clean(path)
        _fx_cache[key] = (
            load_fx_system(parsed, source_path=os.path.abspath(path))
            if parsed is not None and parsed.classId == FX_SYSTEM_CLASS_ID else None
        )
    return _fx_cache[key]


def clear_cache() -> None:
    _particle_cache.clear()
    _fx_cache.clear()


def _and(condition: str, extra: str) -> str:
    if not extra:
        return condition
    return f"({condition}) && ({extra})" if condition else extra


def _walk(node: Any, condition: str, fx: FxSystem, externals: dict, load_models: bool) -> None:
    if node is None:
        return
    kind = str(getattr(node, "type", ""))
    data = getattr(node, "node", None)
    if kind == "Parallel":
        for child in getattr(data, "Children", None) or []:
            _walk(child, condition, fx, externals, load_models)
    elif kind == "Condition":
        _walk(data.Child, _and(condition, str(data.ConditionExpr or "")), fx, externals, load_models)
    elif kind == "SubFxSystem":
        _walk(data.FxSystem, condition, fx, externals, load_models)
    elif kind == "UpdateVar":
        fx.var_updates.append(FxVarUpdate(
            var=str(data.VarName or ""), expr=str(data.UpdateVarExpr or ""),
            condition=condition, reset_if_inactive=bool(data.ResetToDefaultIfInactive),
        ))
    elif kind == "ParticleEmitter":
        path = externals.get(int(getattr(data.Model, "_index", -1)), "")
        emitter = FxEmitter(
            name=str(data.name or ""), model_path=path, condition=condition,
            local_offset=str(data.LocalOffsetExpr or ""), world_offset=str(data.WorldOffsetExpr or ""),
            spawn_freq=str(data.SpawnFreqModifierExpr or ""), scale=str(data.ScaleExpr or ""),
            opacity=str(data.OpacityExpr or ""), up=str(data.UpExpr or ""), dov=str(data.DOVExpr or ""),
            dov_and_up_local=bool(data.DOVAndUpAreLocalSpace),
            linear_hue=str(data.LinearHue01 or ""), hue_lightness=str(data.HueLightness or ""),
        )
        if path and load_models:
            emitter.model = read_particle_model(path)
        fx.emitters.append(emitter)
    elif kind == "SoundEmitter":
        fx.sound_emitter_count += 1


def _var(var: Any) -> FxVar:
    kind = str(var.Type)
    value = var.Value
    out = FxVar(name=str(var.Name or ""), type=kind)
    if kind in _FLOAT_VAR_TYPES:
        out.value, out.minimum, out.maximum = float(value.FloatVal), float(value.MinVal), float(value.MaxVal)
    elif kind in _INT_VAR_TYPES:
        out.value = float(next((v for k, v in value.items() if k.endswith("Val")), 0))
    return out


@register(FX_SYSTEM_CLASS_ID)
def load_fx_system(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
    load_models: bool = True,
) -> FxSystem:
    body = body_dict(parsed)
    fx = FxSystem(class_id=FX_SYSTEM_CLASS_ID, source_path=source_path or None, name=file_stem(source_path))
    chunk = body.get(_CHUNK_SYSTEM)
    if chunk is None:
        return fx
    externals = external_node_paths(parsed, source_path) if source_path else {}
    _walk(chunk.rootNode, "", fx, externals, load_models)
    fx.vars = [_var(v) for v in chunk.Vars or []]
    fx.context_class_id = int(chunk.ContextClassId)
    log.debug(
        "FxSystem %s: %d emitters, %d var updates, %d vars",
        fx.name, len(fx.emitters), len(fx.var_updates), len(fx.vars),
    )
    return fx


__all__ = ["FX_SYSTEM_CLASS_ID", "load_fx_system", "read_fx_system", "read_particle_model", "clear_cache"]
