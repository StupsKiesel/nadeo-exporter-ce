"""Decode replay ghosts without exposing NICE containers to Blender."""

import math
import xml.etree.ElementTree as ET

from ..coords import gbx_to_blender_quat, gbx_to_blender_vec3
from ..errors import ParseError
from ..model.replay import Replay, ReplayGhost, ReplaySample
from ..registry import register
from ._gbx_helpers import body_dict, file_stem


def _sample(time_ms, sample):
    if "position" in sample:
        pos = sample.position
        angle, heading, pitch = sample.angle, sample.axisHeading, sample.axisPitch
        sine = math.sin(angle)
        rot = gbx_to_blender_quat(
            math.cos(heading) * math.cos(pitch) * sine,
            math.sin(heading) * math.cos(pitch) * sine,
            math.sin(pitch) * sine, math.cos(angle))
        telemetry = {name: float(sample[key]) for name, key in (
            ("speed_mps", "speed"), ("side_speed_mps", "sideSpeed"),
            ("rpm_raw", "rpm"), ("steer", "steer"), ("throttle", "gas"), ("brake", "break"),
            ("wheel_fl_raw", "FLWheelRotation"), ("wheel_fr_raw", "FRWheelRotation"),
            ("wheel_rl_raw", "RLWheelRotation"), ("wheel_rr_raw", "RRWheelRotation"),
        )}
        if "FLSteerAngle" in sample:
            telemetry["wheel_fl_steer_rad"] = float(sample.FLSteerAngle)
            telemetry["front_speed_mps"] = (sample.time & 0xFFFF) / 65535.0 * 11000 - 1000
            telemetry["rpm"] = (sample.u04[0] | int(sample.rpm) << 8) / 65535.0 * 30000
            telemetry["is_turbo"] = float(bool(sample.u31[0] & 0x80))
        rest = sample.get("rest", b"")
        if len(rest) >= 23:
            state, ground, control, gear = rest[7], rest[20], rest[21], rest[22]
            air_codes = ((control >> 4) & 3, (control >> 6) & 3, gear & 3)
            if 3 in air_codes:
                raise ParseError("Replay contains a reserved reactor air-control value")
            telemetry.update(
                is_top_contact=float(bool(state & 0x20)),
                is_ground_contact=float(bool(ground & 1)),
                is_reactor_ground_mode=float(bool(ground & 2)),
                reactor_inputs_x=float(bool(ground & 4)),
                reactor_boost_type=float((ground >> 3) & 3),
                reactor_boost_level=float((ground >> 5) & 3),
                reactor_air_control_x=float(air_codes[0] - 1),
                reactor_air_control_y=float(air_codes[1] - 1),
                reactor_air_control_z=float(air_codes[2] - 1),
                gear=float((gear >> 2) & 15),
            )
        for wheel in ("FL", "FR", "RL", "RR"):
            key = f"{wheel}WheelRotationCount"
            if key in sample:
                telemetry[f"wheel_{wheel.lower()}_count_raw"] = float(sample[key])
            key = f"{wheel}DampenLen"
            if key in sample:
                telemetry[f"wheel_{wheel.lower()}_damper_m"] = float(sample[key])
            key = f"{wheel}GroundContactMaterial"
            if key in sample:
                telemetry[f"wheel_{wheel.lower()}_ground_material"] = float(sample[key])
            if f"{wheel}WheelRotationCount" in sample:
                telemetry[f"wheel_{wheel.lower()}_angle_rad"] = math.tau * (
                    sample[f"{wheel}WheelRotation"] / 255 + sample[f"{wheel}WheelRotationCount"])
    else:
        pos, rotation = sample.Position, sample.Rotation
        rot = gbx_to_blender_quat(rotation.x, rotation.y, rotation.z, rotation.w)
        telemetry = {name: float(sample[key]) for name, key in (
            ("speed_kmh", "SpeedForward"), ("rpm", "RPM"), ("steer", "Steer"),
            ("throttle", "Gas"), ("brake", "Brake"),
        )}
    values = (pos.x, pos.y, pos.z, rot.x, rot.y, rot.z, rot.w, *telemetry.values())
    if time_ms < 0 or not all(math.isfinite(value) for value in values):
        raise ParseError("Replay contains invalid time or non-finite motion")
    return ReplaySample(time_ms, gbx_to_blender_vec3(pos.x, pos.y, pos.z), rot, telemetry)


@register(0x03093000)
def load_replay(parsed, *, source_path="", resolver=None):
    from NICE.src.gbx_structs import body_chunks

    body = body_dict(parsed)
    replay = Replay(source_path=source_path, class_id=0x03093000,
                    name=file_stem(source_path), raw=parsed,
                    embedded_map=body.get(0x03093002, {}).get("challengeData", b""),
                    diagnostics=list(parsed.get("_errors", [])))
    if replay.embedded_map:
        from .. import load_bytes
        from ..model.map import Map

        map_asset = load_bytes(replay.embedded_map, source_path=source_path + ".embedded.Map.Gbx",
                               resolver=resolver, recursive=False)
        if not isinstance(map_asset, Map):
            raise ParseError("Replay's embedded challenge is not a map")
        from ..errors import GbxbpyError
        from ..resolve import Resolver
        from ..map_import_v4.decoration_map import resolve_absolute_height

        try:
            replay.decoration_height_m = resolve_absolute_height(map_asset, resolver or Resolver())
        except (GbxbpyError, OSError) as exc:
            replay.diagnostics.append(f"Decoration height unavailable: {exc}")
        # This temporary map is used only for metadata, not subsequent export.
        from .map import _CHUNK_EMBEDDED
        archive = body_dict(map_asset.raw).get(_CHUNK_EMBEDDED, {}).get("embeddedData", {}).get("zip")
        if archive is not None:
            archive.close()
    for blob in parsed.get("header", {}).get("data", []):
        if isinstance(blob, bytes) and b"<header" in blob:
            try:
                header = ET.fromstring(blob[blob.index(b"<header"):].decode("utf-8"))
            except (ET.ParseError, UnicodeDecodeError) as exc:
                replay.diagnostics.append(f"Replay header metadata could not be read: {exc}")
                continue
            map_info = header.find("map")
            if map_info is not None:
                replay.map_uid = map_info.get("uid", "")
                replay.map_name = map_info.get("name", "")
    for index, node in enumerate(body.get(0x03093014, {}).get("Ghosts", [])):
        chunks = body_dict(node)
        appearance = chunks.get(0x03092000, {})
        ghost = ReplayGhost(nickname=appearance.get("GhostNickname", f"Ghost {index + 1}"),
                            player_model=appearance.get("PlayerModel", {}).get("id", ""))
        for skin in appearance.get("SkinPackDescs", []):
            ghost.skins.append(skin.get("FilePath", skin.get("filePath", "")) or "")
            ghost.skin_urls.append(str(skin.get("LocatorUrl", "") or ""))
        record = appearance.get("RecordData")
        data = body_dict(record).get(0x0911F000, {}).get("data", {}).get("content", {}) if record else {}
        if data:
            ghost.record_start_ms, ghost.record_end_ms = data.start, data.end
            ghost.entity_class_ids = [desc.classId for desc in data.entRecordDescs]
            vehicles = [entity for entity in data.get("entList", []) or []
                        if data.entRecordDescs[entity.entRecordDescIndex].classId == 0x0A018000]
            if len(vehicles) != 1:
                replay.diagnostics.append(f"Ghost {index + 1}: expected one vehicle stream, found {len(vehicles)}")
            else:
                for raw in vehicles[0].samples1:
                    if not raw.get("hasNextElem", True):
                        continue
                    try:
                        sample = body_chunks[0x0A018000].parse(raw.data) if isinstance(raw.data, bytes) else raw.data
                        ghost.samples.append(_sample(raw.time, sample))
                    except Exception as exc:
                        raise ParseError(f"Ghost {index + 1}, sample {raw.time}ms: {exc}") from exc
        else:
            old = next((chunks[key].get("data", {}).get("content", {})
                        for key in (0x0303F005, 0x0303F006) if key in chunks), {})
            samples = old.get("samples", [])
            if isinstance(samples, list):
                for sample in samples:
                    ghost.samples.append(_sample(sample["_Time"], sample))
            elif samples:
                replay.diagnostics.append(f"Ghost {index + 1}: unsupported legacy sample layout")
        if any(b.time_ms <= a.time_ms for a, b in zip(ghost.samples, ghost.samples[1:])):
            raise ParseError(f"Ghost {index + 1}: sample times must increase")
        if not ghost.samples:
            replay.diagnostics.append(f"Ghost {index + 1}: no supported vehicle motion samples")
        replay.ghosts.append(ghost)
    if not any(ghost.samples for ghost in replay.ghosts):
        raise ParseError("Replay has no supported vehicle motion; its recording layout could not be decoded")
    # The revolution byte wraps after 256 turns; preserve continuous angles
    # instead of choosing a shortest arc for each individual revolution.
    for ghost in replay.ghosts:
        for wheel in ("fl", "fr", "rl", "rr"):
            key = f"wheel_{wheel}_angle_rad"
            previous = None
            turns = 0.0
            for sample in ghost.samples:
                if key not in sample.telemetry:
                    continue
                raw = sample.telemetry[key]
                if previous is not None:
                    difference = raw - previous
                    if difference < -128 * math.tau:
                        turns += 256 * math.tau
                    elif difference > 128 * math.tau:
                        turns -= 256 * math.tau
                sample.telemetry[key] = raw + turns
                previous = raw
    return replay
