"""Strict partial reader for the verified Stadium shared visual-model parameter.

Unknown shared-model bytes are retained, not interpreted as replay telemetry.
Only the verified root/chunk versions and linked stock dependency are supported.
"""

from dataclasses import dataclass
import math
import os
from pathlib import Path
import struct

from .errors import ParseError


@dataclass(frozen=True)
class SpoilerParameter:
    value: float
    source_path: str


@dataclass(frozen=True)
class SpoilerParameters:
    spoiler_open_normed: float
    unknown_prefix: bytes
    unknown_suffix: bytes
    chunk_offset: int
    model_path: str = ""
    shared_path: str = ""


def decode_spoiler_parameters(body: bytes) -> SpoilerParameters:
    """Read a unique validated 090E8020/v2 block in an otherwise opaque body."""
    if len(body) < 8 or struct.unpack_from("<2I", body) != (0x090E8009, 18):
        raise ParseError("Unsupported spoiler shared-model leading chunk/version")
    marker = struct.pack("<I", 0x090E8020)
    offset = body.find(marker, 8)
    if offset < 0:
        raise ParseError("Missing spoiler parameter chunk 0x090E8020")
    if body.find(marker, offset + 1) >= 0:
        raise ParseError("Ambiguous spoiler parameter chunk 0x090E8020")
    if len(body) - offset < 36:
        raise ParseError("Truncated spoiler parameter block")
    version, *values, following, following_version = struct.unpack_from("<I5f2I", body, offset + 4)
    if version != 2:
        raise ParseError(f"Unsupported spoiler parameter version: {version}")
    if (following, following_version) != (0x090E8021, 5):
        raise ParseError("Unsupported spoiler parameter following boundary")
    if not body.endswith(struct.pack("<I", 0xFACADE01)):
        raise ParseError("Truncated spoiler shared-model terminal boundary")
    if not all(math.isfinite(value) for value in values) or not 0 <= values[-1] <= 1:
        raise ParseError("Invalid authored spoiler parameter values")
    return SpoilerParameters(values[-1], body[:offset], body[offset + 28:], offset)


def _read_envelope(path: Path):
    from NICE.src.gbx_structs import GbxStructWithoutBodyParsed

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ParseError(f"Spoiler visual model unavailable: {path}") from exc
    try:
        parsed = GbxStructWithoutBodyParsed.parse(
            raw, gbx_data={}, nodes=[], filename=str(path), errors=[], warns=[],
            load_external_file=lambda _relative: None)
    except Exception as exc:
        raise ParseError(f"Invalid spoiler visual-model envelope: {path}: {exc}") from exc
    if parsed.rest:
        raise ParseError(f"Unsupported trailing spoiler visual-model data: {path}")
    return parsed


def load_replay_spoiler(gamedata_dir: str, *, player_model: str = "CarSport",
                       variant: str = "Stadium") -> SpoilerParameters:
    """Resolve the actual stock Sport model reference; never default to one."""
    if (player_model, variant) != ("CarSport", "Stadium"):
        raise ParseError(f"Unsupported spoiler vehicle variant: {player_model}/{variant}")
    model_path = Path(gamedata_dir) / "Vehicles" / "Cars" / "CarSport" / "VisModelSport.VehicleVisModel.Gbx"
    model = _read_envelope(model_path)
    if model.classId != 0x090E7000:
        raise ParseError("Unsupported spoiler visual-model root class")
    if len(model.body) < 12:
        raise ParseError("Truncated spoiler visual-model root chunk")
    chunk, version, node_index = struct.unpack_from("<3I", model.body)
    if (chunk, version) != (0x090E7000, 18):
        raise ParseError("Unsupported spoiler visual-model root chunk/version")
    references = [ref for ref in model.referenceTable.externalNodes
                  if ref.nodeIndex == node_index]
    if len(references) != 1:
        raise ParseError("Missing or ambiguous spoiler shared-model reference")
    reference = references[0]
    if reference.flags.isRefResourceIndex:
        raise ParseError("Unsupported spoiler shared-model resource reference")
    shared_path = Path(os.path.abspath(os.path.join(
        model_path.parent, reference._computedRelativePath)))
    expected = Path(gamedata_dir) / "Vehicles" / "Cars" / "CommonMedia" / "Common.VehicleVisModel.Gbx"
    if os.path.normcase(str(shared_path)) != os.path.normcase(os.path.abspath(expected)):
        raise ParseError("Unsupported spoiler shared-model dependency")
    shared = _read_envelope(shared_path)
    if shared.classId != 0x090E8000:
        raise ParseError("Unsupported spoiler shared-model root class")
    result = decode_spoiler_parameters(shared.body)
    return SpoilerParameters(result.spoiler_open_normed, result.unknown_prefix,
                             result.unknown_suffix, result.chunk_offset,
                             str(model_path), str(shared_path))


def load_spoiler_parameter(resolver) -> SpoilerParameter:
    """Load the verified Stadium parameter with its authored source provenance."""
    result = load_replay_spoiler(resolver.gamedata_dir)
    return SpoilerParameter(result.spoiler_open_normed, result.shared_path)
