"""Normalize CPlugLight settings without depending on Blender or rendering effects."""

from __future__ import annotations

import logging
import os
from typing import Any

from .. import library
from ..errors import ParseError
from ..library.validate import check_parsed_light
from ..model.mesh import LightAttachment
from ..resolve import Resolver

log = logging.getLogger("gbxbpy.load.light")


def _body(node: Any) -> dict:
    body = getattr(node, "body", None)
    return body if isinstance(body, dict) else {}


def _chunk(node: Any, chunk_id: int) -> Any:
    chunk = _body(node).get(chunk_id)
    return chunk if chunk is not None and "_chunkParseFailed" not in chunk else None


def _reference(light: LightAttachment, name: str, node: Any) -> None:
    if node is None:
        return
    light.metadata[name + "_index"] = int(getattr(node, "_invalid_index", getattr(node, "_index", -1)))
    if hasattr(node, "_invalid_index"):
        light.metadata[name + "_missing"] = True
    class_id = getattr(node, "classId", None)
    if class_id is not None:
        light.metadata[name + "_class_id"] = f"0x{int(class_id):08X}"
    path = getattr(node, "_relativeFilePath", "") or getattr(node, "filepath", "")
    if path:
        light.metadata[name + "_path"] = str(path)


def _warning(light: LightAttachment, message: str) -> None:
    light.metadata["data_warning"] = message
    log.warning("%s", message)


def light_from_user_model(model: Any, *, source_path: str) -> LightAttachment | None:
    """Normalize a user light model shared by compiled meshes and Crystal layers."""
    light_type = {0: "POINT", 1: "SPOT"}.get(getattr(model, "lightType", None))
    if light_type is None:
        log.warning("Unsupported user light type %s in %s",
                    getattr(model, "lightType", None), source_path)
        return None
    light = LightAttachment(
        light_type=light_type,
        color=(float(model.color_r), float(model.color_g), float(model.color_b)),
        intensity=float(model.intensity), distance=float(model.distance),
        spot_inner_angle=getattr(model, "spotInnerAngle", None),
        spot_outer_angle=getattr(model, "spotOuterAngle", None),
        night_only=bool(getattr(model, "nightOnly", False)),
    )
    if light_type == "POINT" and hasattr(model, "pointEmissionRadius"):
        light.emitting_radius = float(model.pointEmissionRadius)
    for name, field in (
        ("point_emission_length", "pointEmissionLength"),
        ("spot_emission_size_x", "spotEmissionSizeX"),
        ("spot_emission_size_y", "spotEmissionSizeY"),
    ):
        if hasattr(model, field):
            light.metadata[name] = float(getattr(model, field))
    return light


def apply_light_source(light: LightAttachment, node: Any, *, source_path: str,
                       resolver: Resolver | None, cache: dict[str, Any]) -> None:
    """Read embedded or library light data; keep generic lights on explicit failure."""
    data_path = source_path
    if not _body(node):
        reference = str(getattr(node, "_relativeFilePath", "") or light.light_path)
        if not reference:
            return
        if resolver is None:
            _warning(light, f"Cannot resolve light {reference} without a GameData resolver")
            return
        lookup = reference
        if "/" not in lookup and "\\" not in lookup:
            lookup = os.path.join("Stadium", "Media", "Light", lookup.lstrip("? "))
        data_path = resolver.resolve(lookup, source_path=source_path)
        if not data_path:
            _warning(light, f"Cannot resolve light {reference} from {source_path}")
            return
        if data_path not in cache:
            from ..io import read_gbx

            for attempt in range(2):
                try:
                    parsed = read_gbx(data_path, recursive=False)
                except ParseError as exc:
                    parsed = str(exc)
                if attempt == 0:
                    cache[data_path] = parsed
                valid = not isinstance(parsed, str) and check_parsed_light(parsed).ok
                if valid:
                    cache[data_path] = parsed
                    break
                if attempt or not library.refetch(data_path):
                    break
        node = cache[data_path]
        if isinstance(node, str):
            _warning(light, node)
            return
        check = check_parsed_light(node)
        if not check.ok:
            _warning(light, f"Light source parse validation failed in {data_path}: {check.reason}")

    light.metadata["source_path"] = data_path
    if getattr(node, "classId", None) != 0x0901D000:
        _warning(light, f"Unsupported light source class in {data_path}")
        return
    errors = getattr(node, "_errors", ()) or ()
    if errors:
        _warning(light, f"Light source parse errors in {data_path}: {'; '.join(errors)}")
    animation = _chunk(node, 0x0901D003)
    if animation is not None:
        _reference(light, "image_anim", getattr(animation, "imageAnim", None))
        light.metadata["anim_period_min"] = float(animation.animPeriodMin)
        light.metadata["anim_period_max"] = float(animation.animPeriodMax)
        light.metadata["anim_timer_name"] = str(getattr(animation, "animTimerName", "") or "")
    settings = _chunk(node, 0x0901D004)
    if settings is None:
        _warning(light, f"Cannot decode CPlugLight settings in {data_path}; keeping generic light settings")
        return
    flags = getattr(settings, "flags", None)
    if flags is not None:
        light.metadata["flags"] = f"0x{int(flags):08X}"
        light.metadata["flag_names"] = ", ".join(getattr(settings, "flagNames", ()) or ())
        light.night_only = bool(flags & 1)
    for name, field in (
        ("gx_light_model", "gxLightModel"), ("func_light", "funcLight"),
        ("bitmap_flare", "bitmapFlare"), ("bitmap_projector", "bitmapProjector"),
        ("color_target_table", "colorTargetTable"),
    ):
        _reference(light, name, getattr(settings, field, None))
    gx = getattr(settings, "gxLightModel", None)
    light.light_type = {0x04002000: "POINT", 0x0400B000: "SPOT"}.get(
        getattr(gx, "classId", None), "",
    )
    if not light.light_type:
        _warning(light, f"Unsupported Gx light type {getattr(gx, 'classId', None)} in {data_path}")
        return
    base = _chunk(gx, 0x0400100A)
    if base is not None:
        light.color = (float(base.color.x), float(base.color.y), float(base.color.z))
        light.intensity = float(base.intensity)
        light.use_shadow = bool(base.flags & 4)
        light.metadata.update(
            gx_flags=f"0x{int(base.flags):08X}", gx_intensity=light.intensity,
            gx_diffuse_intensity=float(base.diffuseIntensity),
            gx_shadow_intensity=float(base.shadowIntensity),
            gx_flare_intensity=float(base.flareIntensity),
            gx_shadow_color=(float(base.shadowRGB.x), float(base.shadowRGB.y), float(base.shadowRGB.z)),
        )
    ball = _chunk(gx, 0x04002008)
    if ball is not None:
        light.distance = float(ball.radius)
        light.emitting_radius = float(ball.emittingRadius)
        light.metadata["gx_ball_flags"] = f"0x{int(ball.flags):08X}"
        for name, field in (
            ("radius", "radius"), ("radius_specular", "radiusSpecular"),
            ("radius_shadow", "radiusShadow"), ("radius_flare", "radiusFlare"),
            ("emitting_radius", "emittingRadius"), ("emitting_cylinder_len_z", "emittingCylinderLenZ"),
            ("att_htnlr", "attHTnLR"), ("att_htnlr2", "attHTnLR2"),
            ("att_hyper2_deriv_at0", "attHyper2DerivAt0"), ("att_hyper2_tension", "attHyper2Tension"),
        ):
            light.metadata["gx_" + name] = float(getattr(ball, field))
        light.metadata["gx_ambient_color"] = (
            float(ball.ambientRGB.x), float(ball.ambientRGB.y), float(ball.ambientRGB.z),
        )
    spot = _chunk(gx, 0x0400B003)
    if light.light_type == "SPOT" and spot is not None:
        light.spot_inner_angle = float(spot.angleInner)
        light.spot_outer_angle = float(spot.angleOuter)
        light.metadata["gx_spot_flags"] = f"0x{int(spot.flags):08X}"
        for name, field in (
            ("angle_inner", "angleInner"), ("angle_outer", "angleOuter"),
            ("angle_flare", "angleFlare"), ("angle_inner_shadow", "angleInnerShadow"),
            ("angle_outer_shadow", "angleOuterShadow"), ("falloff_exponent", "falloffExponent"),
        ):
            light.metadata["gx_" + name] = float(getattr(spot, field))
        if spot.version >= 1:
            light.metadata["gx_sub_light_count_x"] = int(spot.subLightCountX)
            light.metadata["gx_sub_light_count_y"] = int(spot.subLightCountY)
        else:
            light.metadata["gx_legacy_spot_value"] = int(spot.legacyValue)
