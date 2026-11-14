"""ParticleEmitterModel domain model.

``CPlugParticleEmitterModel`` (``0x090B3000``) describes a particle-effect
emitter asset used by the game engine for visual effects such as torches,
sparklers, and foggers.  Files are named ``*.ParticleModel.Gbx``.

Stock show effects (foggers, sparklers, torch smoke) are GPU particles: each
sub-model's chunk ``0x090B203A`` holds a ``CPlugParticleGpuSpawn`` and a
``CPlugParticleGpuModel``. :class:`ParticleEmission` exposes the fields an
approximate re-creation needs. Their meaning is inferred, not documented by
Nadeo: the 8 m/16 m variants of each effect differ in exactly these values,
and ``speed / drag * (1 - exp(-drag * lifetime))`` reproduces the reach in the
item names (Fogger16M: 8 m/s, drag 0.5, 5 s -> 14.7 m). The texture is a
flipbook whose grid is ``0x090C6002`` u05 x u06 (FoggerSmoke 4x4, Sparkler_D
4x2, SparklerEnd_D 4x4 - matching the images).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .asset import Asset, AssetKind


@dataclass
class ParticleEmission:
    """Inferred GPU particle parameters of one emitter sub-model."""

    rate: float = 0.0              # particles per second (GpuSpawn u05)
    speed: float = 0.0             # m/s along the emitter axis (GpuSpawn u10)
    speed_variation: float = 0.0   # (GpuSpawn u11)
    lifetime: float = 0.0          # s (GpuModel 0x090C6000 u01[0])
    lifetime_variation: float = 0.0  # s (u01[1])
    size_start: float = 0.0        # m (u01[3])
    size_end: float = 0.0          # m (u01[2])
    gravity: float = 0.0           # m/s^2 along world up (0x090C6001 u01, a float)
    drag: float = 0.0              # 1/s linear velocity damping (0x090C6001 u02)
    max_particles: int = 0         # sub-model 0x090B202D u04
    texture_path: str = ""         # *.Texture.Gbx (0x090C6002 texture)
    texture_image: str = ""        # image the wrapper references (set by read_particle_model)
    atlas_columns: int = 1         # 0x090C6002 u05: flipbook columns
    atlas_rows: int = 1            # 0x090C6002 u06: flipbook rows
    use_hue_shift: bool = False
    use_emitter_hue: bool = True
    hue: float = 0.0               # model hue when the emitter supplies a negative hue
    is_fog_emitter: bool = False
    fog_emission_rate: float = 0.0


@dataclass
class ParticleEmitterModel(Asset):
    """A ``CPlugParticleEmitterModel`` asset (``*.ParticleModel.Gbx``)."""

    # The in-file emitter name (chunk 0x090B3001)
    emitter_name: str = ""

    # Number of sub-model NodeRefs (chunk 0x090B3000)
    sub_model_count: int = 0

    # Approximate life-span in seconds, if available (chunk 0x090B3002 u03)
    life_span: float = 0.0

    # One entry per GPU sub-model (CPU sub-models are not decoded)
    emissions: List[ParticleEmission] = field(default_factory=list)

    @property
    def emission(self) -> Optional[ParticleEmission]:
        return self.emissions[0] if self.emissions else None

    def __post_init__(self) -> None:
        if self.kind is AssetKind.UNKNOWN:
            self.kind = AssetKind.PARTICLE


__all__ = ["ParticleEmission", "ParticleEmitterModel"]
