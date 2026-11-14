"""Sky clouds as plain data: the game's sprite clusters and their placement.

The engine draws clouds with ``CPlugClouds`` (``Clouds/Func/FuncClouds/
Tech3.FuncClouds.Gbx``). Its ``CPlugCloudsSolids`` lists authored
``Cloudy*.Solid.Gbx`` clusters that together fill one ``GridSizeXZ`` tile
(16000 m); the tile repeats around the viewer. Each cluster is a
``CPlugVisualSprite`` whose vertices are sprites (position, atlas cell, size)
textured from the mood's ``SkyClouds.dds`` 4x4 atlas. The mood's
``SkyCloudsParams.FuncCloudsParam.Gbx`` (``CPlugCloudsParam``) bends the
layer down with distance (``PointDists`` -> ``PointHeights``) so far clouds
sit on the horizon.

``Clouds.tga`` of the mood is not part of this: the surface shaders sample
it next to the sun shadow map, so it is the scrolling cloud shadow mask.

None of these chunks are decoded by NICE, so the parsers below read the
uncompressed body bytes directly and validate what they read.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
import re
import struct
import xml.etree.ElementTree as ET

from .. import library

log = logging.getLogger("gbxbpy.map_import_v4")

Vec3 = tuple[float, float, float]

FUNC_CLOUDS = os.path.join("Clouds", "Func", "FuncClouds", "Tech3.FuncClouds.Gbx")
CLOUD_SOLIDS = os.path.join("Clouds", "Media", "Solid", "Cloudy")
# Half width of a sprite at size 1. Not stored in the files we decode; fitted
# from the cluster bounding boxes (flat clusters give 520-580 m).
SPRITE_HALF_SIZE = 550.0

_VISUAL = 0x0900600F
_VERTICES = 0x0902C004
_ATLAS = 0x09010009
_SOLIDS_GRID = 0x09183001
_PARAM_POINTS = 0x09182001
_PARAM_MAIN = 0x09182002
_SPRITE = struct.Struct("<3fIif")  # position, packed normal, atlas cell, size


@dataclass(frozen=True, slots=True)
class CloudSprite:
    position: Vec3          # Blender axes, metres, inside one tile
    cell: int               # SkyClouds atlas cell
    size: float             # scale of SPRITE_HALF_SIZE


@dataclass(frozen=True, slots=True)
class CloudsParam:
    """``CPlugCloudsParam``: layer height over horizontal distance and wind."""

    points: tuple[tuple[float, float], ...] = ((5000.0, 2500.0), (30000.0, -1000.0))
    bottom_near: float = 4500.0
    bottom_far: float = 0.0
    speed_scale: float = 200.0


@dataclass(frozen=True, slots=True)
class CloudShading:
    """Mood XML: ``<Atmo><CloudsX2>`` shadow/lit colours and ``<Fog><Clouds>``."""

    min_color: Vec3 = (0.38, 0.38, 0.38)
    max_color: Vec3 = (0.627, 0.627, 0.627)
    fog_depth: float = 80000.0
    fog_exponent: float = 5.0
    fog_max: float = 0.55


@dataclass(frozen=True, slots=True)
class CloudLayer:
    """Everything the bridge needs to build the clouds of one mood."""

    atlas_path: str = ""
    # (u0, v0, u1, v1) per cell, image space (v down).
    cells: tuple[tuple[float, float, float, float], ...] = ()
    sprites: tuple[CloudSprite, ...] = ()
    grid_size: tuple[float, float] = (16000.0, 16000.0)
    param: CloudsParam = CloudsParam()
    shading: CloudShading = CloudShading()
    shadow_path: str = ""
    # Clouds.FuncShader UV scroll per second.
    shadow_speed: tuple[float, float] = (0.0, 0.0)


def _body(data: bytes) -> bytes:
    if data[:3] != b"GBX" or len(data) < 9:
        raise ValueError("not a GBX file")
    if data[7:8] == b"C":
        raise ValueError("compressed GBX body is not supported")
    return data


def _chunk(data: bytes, chunk_id: int) -> int:
    offset = data.find(struct.pack("<I", chunk_id))
    if offset < 0:
        raise ValueError(f"chunk {chunk_id:#010x} missing")
    return offset + 4


def parse_cloud_solid(data: bytes) -> tuple[list[CloudSprite], list[tuple[float, float, float, float]]]:
    """Sprites and atlas cells of a ``Cloudy*.Solid.Gbx`` (``CPlugVisualSprite``)."""
    data = _body(data)
    visual = _chunk(data, _VISUAL)
    count = struct.unpack_from("<I", data, visual + 12)[0]
    offset = _chunk(data, _VERTICES)
    if count <= 0 or offset + count * _SPRITE.size > len(data):
        raise ValueError(f"bad sprite count {count}")
    sprites = []
    for index in range(count):
        x, y, z, _normal, cell, size = _SPRITE.unpack_from(data, offset + index * _SPRITE.size)
        if not (0 <= cell < 64 and 0.0 < size < 100.0 and all(map(math.isfinite, (x, y, z)))):
            raise ValueError(f"implausible sprite {index}: cell {cell} size {size}")
        sprites.append(CloudSprite((x, -z, y), cell, size))
    atlas = _chunk(data, _ATLAS)
    cell_count = struct.unpack_from("<I", data, atlas)[0]
    if not 0 < cell_count <= 64:
        raise ValueError(f"bad atlas cell count {cell_count}")
    values = struct.unpack_from(f"<{cell_count * 4}f", data, atlas + 4)
    cells = [tuple(values[i:i + 4]) for i in range(0, len(values), 4)]
    if any(not 0.0 <= v <= 1.0 for v in values):
        raise ValueError("atlas cells outside 0..1")
    if max(s.cell for s in sprites) >= cell_count:
        raise ValueError("sprite cell outside atlas")
    return sprites, cells  # type: ignore[return-value]


def parse_func_clouds(data: bytes) -> tuple[list[str], tuple[float, float]]:
    """Solid file names and ``GridSizeXZ`` of ``Tech3.FuncClouds.Gbx``."""
    data = _body(data)
    names = list(dict.fromkeys(
        match.decode("ascii") for match in re.findall(rb"Cloudy\d+\.Solid\.Gbx", data)
    ))
    grid = struct.unpack_from("<2f", data, _chunk(data, _SOLIDS_GRID))
    if not names or not all(1000.0 <= g <= 1e6 for g in grid):
        raise ValueError(f"no solids or bad grid {grid}")
    return names, grid  # type: ignore[return-value]


def parse_clouds_param(data: bytes) -> CloudsParam:
    """``SkyCloudsParams.FuncCloudsParam.Gbx``.

    ``0x09182001``: count, then ``(PointDist, PointHeight)`` pairs.
    ``0x09182002``: version, HeightCenter, HeightCenterXZ, BottomNearZ,
    BottomFarZ, SpeedScale, Lighting, MaterialUseT3b.
    """
    data = _body(data)
    offset = _chunk(data, _PARAM_POINTS)
    count = struct.unpack_from("<I", data, offset)[0]
    if not 0 < count <= 16:
        raise ValueError(f"bad point count {count}")
    values = struct.unpack_from(f"<{count * 2}f", data, offset + 4)
    points = tuple(sorted(zip(values[0::2], values[1::2])))
    _version, _center, _cx, _cz, near, far, speed = struct.unpack_from(
        "<2I5f", data, _chunk(data, _PARAM_MAIN),
    )
    return CloudsParam(points=points, bottom_near=near, bottom_far=far, speed_scale=speed)


def _hex(text: str | None, scale: float) -> Vec3 | None:
    if not text or not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return tuple(int(text[i:i + 2], 16) / 255.0 * scale for i in (0, 2, 4))  # type: ignore[return-value]


def _attr(element: ET.Element | None, name: str, default: float) -> float:
    try:
        return float(element.get(name, default)) if element is not None else default
    except ValueError:
        return default


def parse_cloud_shading(text: str) -> CloudShading:
    root = ET.fromstring(text)
    x2 = root.find("Atmo/CloudsX2")
    fog = root.find("Fog/Clouds")
    default = CloudShading()

    def color(name: str, fallback: Vec3) -> Vec3:
        if x2 is None:
            return fallback
        return _hex(x2.get(f"{name}Rgb"), _attr(x2, f"{name}RgbX", 1.0)) or fallback

    return CloudShading(
        min_color=color("Min", default.min_color),
        max_color=color("Max", default.max_color),
        fog_depth=_attr(fog, "DepthMax", default.fog_depth),
        fog_exponent=_attr(fog, "Exponant", default.fog_exponent),
        fog_max=_attr(fog, "IntensMax", default.fog_max),
    )


def layer_height(points, distance: float) -> float:
    """Piecewise-linear ``PointHeights`` at ``distance``, clamped at both ends."""
    if not points:
        return 0.0
    if distance <= points[0][0]:
        return points[0][1]
    for (d0, h0), (d1, h1) in zip(points, points[1:]):
        if distance <= d1:
            return h0 + (h1 - h0) * (distance - d0) / (d1 - d0 or 1.0)
    return points[-1][1]


@dataclass(frozen=True, slots=True)
class PlacedSprite:
    position: Vec3
    cell: int
    half_size: float
    fog: float


def place_cloud_sprites(layer: CloudLayer, center: tuple[float, float]) -> list[PlacedSprite]:
    """Repeat the cluster tile around ``center`` (Blender XY) out to the last
    ``PointDist`` and bend each sprite by the height curve; ``fog`` is the
    ``<Fog><Clouds>`` share towards the fog colour."""
    points = layer.param.points
    radius = points[-1][0] if points else 30000.0
    gx, gy = layer.grid_size
    cx, cy = center
    shading = layer.shading
    placed = []
    for i in range(math.floor((cx - radius) / gx) - 1, math.ceil((cx + radius) / gx) + 2):
        for j in range(math.floor((cy - radius) / gy) - 1, math.ceil((cy + radius) / gy) + 2):
            for sprite in layer.sprites:
                x = sprite.position[0] + i * gx
                y = sprite.position[1] + j * gy
                distance = math.hypot(x - cx, y - cy)
                if distance > radius:
                    continue
                z = sprite.position[2] + layer_height(points, distance)
                depth = min(math.hypot(distance, z) / max(shading.fog_depth, 1.0), 1.0)
                placed.append(PlacedSprite(
                    (x, y, z), sprite.cell, sprite.size * SPRITE_HALF_SIZE,
                    shading.fog_max * depth ** max(shading.fog_exponent, 0.01),
                ))
    return placed


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def load_cloud_layer(
    gamedata_dir: str, find_texture, mood_xml: str = "", issue=None,
) -> CloudLayer | None:
    """Collect the cloud inputs; ``find_texture(name)`` locates a mood file.

    Returns ``None`` (after ``issue("missing_clouds", ...)``) when the
    cluster tile or the sprite atlas is unavailable.
    """
    issue = issue or (lambda *_args: None)
    func_path = os.path.join(gamedata_dir, FUNC_CLOUDS)
    atlas_path = find_texture("SkyClouds.dds")
    if not atlas_path or not library.isfile(func_path):
        issue("missing_clouds", func_path if atlas_path else "SkyClouds.dds", "Cloud sprites not found; no clouds")
        return None
    try:
        names, grid = parse_func_clouds(_read(func_path))
    except (OSError, ValueError, struct.error) as exc:
        issue("parse_failed", func_path, f"FuncClouds: {exc}")
        return None
    sprites: list[CloudSprite] = []
    cells: list = []
    for name in names:
        path = os.path.join(gamedata_dir, CLOUD_SOLIDS, name)
        if not library.isfile(path):
            issue("missing_clouds", path, "Cloud cluster not found")
            continue
        try:
            solid_sprites, solid_cells = parse_cloud_solid(_read(path))
        except (OSError, ValueError, struct.error) as exc:
            issue("parse_failed", path, f"Cloud cluster: {exc}")
            continue
        sprites.extend(solid_sprites)
        cells = cells or solid_cells
    if not sprites:
        return None
    param = CloudsParam()
    param_path = find_texture("SkyCloudsParams.FuncCloudsParam.Gbx")
    if param_path:
        try:
            param = parse_clouds_param(_read(param_path))
        except (OSError, ValueError, struct.error) as exc:
            issue("parse_failed", param_path, f"Clouds param: {exc}")
    shading = CloudShading()
    if mood_xml:
        try:
            shading = parse_cloud_shading(mood_xml)
        except ET.ParseError:
            pass
    shadow_path = find_texture("Clouds.tga")
    shadow_speed = (0.0, 0.0)
    shader_path = find_texture("Clouds.FuncShader.Gbx") if shadow_path else ""
    if shader_path:
        try:
            from .. import load

            shader = load(shader_path, recursive=False)
            shadow_speed = tuple(shader.uv_speed)  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001 - scrolling is cosmetic
            log.debug("Clouds FuncShader unreadable: %s (%s)", shader_path, exc)
    log.info("V4 clouds: %d sprites in %d clusters, grid %s, points %s", len(sprites), len(names), grid, param.points)
    return CloudLayer(
        atlas_path=atlas_path, cells=tuple(cells), sprites=tuple(sprites), grid_size=grid,
        param=param, shading=shading, shadow_path=shadow_path, shadow_speed=shadow_speed,
    )
