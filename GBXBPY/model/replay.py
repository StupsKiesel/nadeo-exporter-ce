"""Replay motion in Blender axes; sample times are milliseconds."""

from dataclasses import dataclass, field

from .asset import Asset, AssetKind
from .common import Quat, Vec3


@dataclass
class ReplaySample:
    time_ms: int
    position: Vec3
    rotation: Quat
    telemetry: dict[str, float] = field(default_factory=dict)


@dataclass
class ReplayGhost:
    nickname: str = ""
    player_model: str = ""
    skins: list[str] = field(default_factory=list)
    skin_urls: list[str] = field(default_factory=list)
    samples: list[ReplaySample] = field(default_factory=list)
    record_start_ms: int = 0
    record_end_ms: int = 0
    entity_class_ids: list[int] = field(default_factory=list)


@dataclass
class Replay(Asset):
    kind: AssetKind = AssetKind.REPLAY
    ghosts: list[ReplayGhost] = field(default_factory=list)
    map_uid: str = ""
    map_name: str = ""
    embedded_map: bytes = field(default=b"", repr=False)
    decoration_height_m: float | None = None
    diagnostics: list[str] = field(default_factory=list)
