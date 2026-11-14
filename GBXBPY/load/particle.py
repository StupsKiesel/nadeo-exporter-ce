"""Loader for ``CPlugParticleEmitterModel`` (``ParticleModel.Gbx``)."""

from __future__ import annotations

import logging
import struct
from typing import Any, Optional

from ..model.particle import ParticleEmission, ParticleEmitterModel
from ..registry import register
from ._gbx_helpers import body_dict, external_node_paths, file_stem

log = logging.getLogger("gbxbpy.load.particle")

# Body chunks
_CHUNK_SUB_MODELS = 0x090B3000  # listVersion + ParticleEmitterSubModels[]
_CHUNK_NAME       = 0x090B3001  # emitter name (lookback string)
_CHUNK_PARAMS     = 0x090B3002  # version, u01 (int), u03 (float = life span)
# CPlugParticleEmitterSubModel / GPU particle chunks (see model.particle)
_CHUNK_SUB_HEADER     = 0x090B202D
_CHUNK_SUB_GPU        = 0x090B203A
_CHUNK_GPU_SPAWN      = 0x090C5000
_CHUNK_GPU_LIFE_SIZE  = 0x090C6000
_CHUNK_GPU_MOTION     = 0x090C6001
_CHUNK_GPU_RENDER     = 0x090C6002


@register(0x090B3000)
def load_particle_emitter_model(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> ParticleEmitterModel:
    body = body_dict(parsed)

    model = ParticleEmitterModel(
        class_id=0x090B3000,
        source_path=source_path or None,
        name=file_stem(source_path),
    )

    # Chunk 001 — emitter name
    name_chunk = body.get(_CHUNK_NAME)
    if name_chunk is not None:
        raw_name = getattr(name_chunk, "name", None)
        if raw_name:
            model.emitter_name = str(raw_name)

    # Chunk 000 — sub-model list
    sub_chunk = body.get(_CHUNK_SUB_MODELS)
    if sub_chunk is not None:
        sub_models = getattr(sub_chunk, "ParticleEmitterSubModels", None) or []
        model.sub_model_count = len(sub_models)

    # Chunk 002 — params (life span is u03)
    params_chunk = body.get(_CHUNK_PARAMS)
    if params_chunk is not None:
        life_span = getattr(params_chunk, "u03", None)
        if life_span is not None:
            model.life_span = float(life_span)

    externals = external_node_paths(parsed, source_path) if source_path else {}
    for sub in (getattr(sub_chunk, "ParticleEmitterSubModels", None) or []) if sub_chunk is not None else []:
        emission = _gpu_emission(sub, externals)
        if emission is not None:
            model.emissions.append(emission)

    log.debug(
        "ParticleEmitterModel %s: emitter=%r sub_models=%d gpu=%d life_span=%.3f",
        model.name, model.emitter_name, model.sub_model_count, len(model.emissions), model.life_span,
    )
    return model


def _as_float(value: Any) -> float:
    """NICE reads some float fields as Int32; reinterpret their bits."""
    if isinstance(value, int):
        return struct.unpack("<f", struct.pack("<i", value))[0]
    return float(value or 0.0)


def _gpu_emission(sub: Any, externals: dict) -> Optional[ParticleEmission]:
    sub_body = body_dict(sub)
    gpu = sub_body.get(_CHUNK_SUB_GPU)
    if gpu is None:
        return None
    spawn_chunk = body_dict(getattr(gpu, "particleGpuSpawn", None)).get(_CHUNK_GPU_SPAWN)
    gpu_model = body_dict(getattr(gpu, "particleGpuModel", None))
    spawn = getattr(spawn_chunk, "spawn", None)
    life_size = gpu_model.get(_CHUNK_GPU_LIFE_SIZE)
    motion = gpu_model.get(_CHUNK_GPU_MOTION)
    render = gpu_model.get(_CHUNK_GPU_RENDER)
    if spawn is None or life_size is None or motion is None:
        return None
    life = list(getattr(life_size, "u01", None) or [0.0] * 4)
    emission = ParticleEmission(
        rate=float(spawn.u05 or 0.0),
        speed=float(spawn.u10 or 0.0),
        speed_variation=float(spawn.u11 or 0.0),
        lifetime=float(life[0]),
        lifetime_variation=float(life[1]),
        size_end=float(life[2]),
        size_start=float(life[3]),
        gravity=_as_float(motion.u01),
        drag=float(motion.u02 or 0.0),
    )
    header = sub_body.get(_CHUNK_SUB_HEADER)
    if header is not None:
        emission.max_particles = int(getattr(header, "u04", 0) or 0)
    if render is not None:
        texture = getattr(render, "texture", None)
        if getattr(render, "isTextureFid", False):
            emission.texture_path = str(texture or "")
        else:
            emission.texture_path = externals.get(int(getattr(texture, "_index", -1)), "")
        emission.is_fog_emitter = bool(getattr(render, "IsFogEmitter", False))
        emission.fog_emission_rate = float(getattr(render, "FogEmissionRate", 0.0) or 0.0)
        emission.atlas_columns = max(1, int(getattr(render, "u05", 1) or 1))
        emission.atlas_rows = max(1, int(getattr(render, "u06", 1) or 1))
        emission.use_hue_shift = bool(getattr(render, "useHueShift", False))
        emission.use_emitter_hue = bool(getattr(render, "useEmitterHue", True))
        emission.hue = float(getattr(render, "hue", 0.0))
    return emission
