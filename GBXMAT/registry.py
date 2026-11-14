"""Family-builder registry, dispatch, and dedup cache.

A *family builder* is a callable ``(MaterialSpec) -> bpy.types.Material``
that constructs the node graph for one shader family (e.g. Stadium
default, decal, light-emissive, terrain blend).

Builders register themselves under a string family name. Callers select
a family by setting :attr:`MaterialSpec.family`. Unknown families fall
back to :func:`gbxmat.builders.fallback.build` which produces a clearly
diagnostic placeholder material so imports never crash on unknown
shaders.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable, Dict, Optional

try:
    import bpy
except Exception:  # pragma: no cover
    bpy = None  # type: ignore[assignment]

from .spec import MaterialSpec


log = logging.getLogger("gbxmat.registry")


Builder = Callable[[MaterialSpec], "bpy.types.Material"]

_BUILDERS: Dict[str, Builder] = {}
_WARNED_FAMILIES: set[str] = set()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_family(name: str, builder: Builder) -> None:
    """Register (or replace) a shader-family builder.

    The same name registered twice silently replaces the previous
    builder — useful for downstream projects that want to override a
    bundled family without forking GBXMAT.
    """
    _BUILDERS[name] = builder


def list_supported_families() -> list[str]:
    return sorted(_BUILDERS.keys())


# ---------------------------------------------------------------------------
# Dedup + dispatch
# ---------------------------------------------------------------------------

def _spec_signature(spec: MaterialSpec) -> str:
    """Stable short hash of a spec — used as the dedup name suffix.

    Two specs that produce visually-identical materials must hash the
    same. Two specs that should produce *different* materials must hash
    differently. The fields included below are the minimum to satisfy
    both invariants.
    """
    h = hashlib.blake2s(digest_size=6)
    h.update(spec.family.encode("utf-8"))
    h.update(repr(spec.color).encode("utf-8"))
    # Opacity modes change the graph; also avoid reusing pre-cutout materials.
    if spec.alpha_cutout:
        h.update(b"\x00alpha-cutout-v1")
    if spec.opacity_from_diffuse_alpha:
        h.update(b"\x00diffuse-opacity")
    from .images import map_path

    for tex in spec.textures:
        h.update(tex.role.encode("utf-8"))
        h.update(b"\x00")
        # Hash the file actually loaded so a texture mod gets its own material.
        h.update(map_path(tex.path).encode("utf-8", errors="replace"))
        h.update(b"\x01")
    for k in sorted(spec.params):
        h.update(k.encode("utf-8"))
        h.update(repr(spec.params[k]).encode("utf-8"))
    h.update(str(spec.physics_id).encode("ascii"))
    h.update(str(spec.gameplay_id).encode("ascii"))
    h.update(b"1" if spec.flip_normal_green else b"0")
    for fs in spec.func_shaders:
        h.update(repr(fs).encode("utf-8"))
        h.update(b"\x02")
    return h.hexdigest()


def _material_name(spec: MaterialSpec) -> str:
    return f"{spec.name_hint}__{_spec_signature(spec)}"


def get_or_create_material(spec: MaterialSpec):
    """Return a :class:`bpy.types.Material` for ``spec``.

    Cached on ``bpy.data.materials`` keyed by a deterministic name
    derived from ``spec``, so identical specs always return the same
    datablock with no rebuild cost.
    """
    if bpy is None:
        raise RuntimeError("gbxmat.get_or_create_material requires Blender (bpy)")

    name = _material_name(spec)
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing

    builder = _BUILDERS.get(spec.family)
    if builder is None:
        from .builders.fallback import build as builder  # type: ignore[assignment]
        if spec.family not in _WARNED_FAMILIES:
            _WARNED_FAMILIES.add(spec.family)
            log.warning(
                "no builder for family %r (registered: %s); using magenta fallback",
                spec.family, ", ".join(sorted(_BUILDERS)),
            )

    mat = builder(spec)
    if mat.name != name:
        mat.name = name
    # Tag for downstream diagnostics; callers may inspect these.
    mat["gbxmat_family"] = spec.family
    mat["gbxmat_signature"] = name.rsplit("__", 1)[-1]
    return mat


__all__ = [
    "register_family",
    "list_supported_families",
    "get_or_create_material",
]


# ---------------------------------------------------------------------------
# Auto-register bundled families on import
# ---------------------------------------------------------------------------
# Done at module-bottom so the registry dict exists before builders
# (which import :mod:`registry` indirectly) try to register themselves.

def _bootstrap_builtin_families() -> None:
    from .builders import (  # noqa: F401
        stadium_default,
        fallback,
        decal,
        glass,
        light_emissive,
        lightmap_baked,
        triplanar,
        additive,
        alpha_blend,
        screen,
        grass,
        terrain_layers,
        water,
        water_vista,
        grass_fence,
    )

    register_family("Stadium.Default", stadium_default.build)
    register_family("Decal", decal.build)
    register_family("Glass", glass.build)
    register_family("LightEmissive", light_emissive.build)
    register_family("LightmapBaked", lightmap_baked.build)
    register_family("TriPlanar", triplanar.build)
    register_family("Additive", additive.build)
    register_family("AlphaBlend", alpha_blend.build)
    register_family("Screen", screen.build)
    register_family("Grass.PyPxzT", grass.build)
    register_family("Dirt.PyPxzT", grass.build)
    register_family("Terrain.PyPxzT", grass.build)
    register_family("Terrain.PyPxz", grass.build)
    register_family("Custom.PyPxzHue", grass.build)
    register_family("Terrain.Layers", terrain_layers.build)
    register_family("Water.Vista", water_vista.build)
    register_family("Water.Surface", water.build_surface)
    register_family("Water.Wall", water.build_wall)
    register_family("GrassFence", grass_fence.build)
    # Fallback is invoked implicitly by ``get_or_create_material`` when
    # the requested family is unknown; we *also* expose it under an
    # explicit name so callers can opt-in.
    register_family("Fallback", fallback.build)


_bootstrap_builtin_families()
