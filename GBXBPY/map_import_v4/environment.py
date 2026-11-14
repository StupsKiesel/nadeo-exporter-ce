"""Map decoration, mood lighting (sun, ambient, fog) and sky inputs as plain data.

The map names its decoration by id (``48x48Screen155Day``, ``Day64``), not by
file, so the id is matched against ``<collection>/GameCtnDecoration``. The
decoration links a ``DecorationMood`` (latitude, sun/moon intensity); the
light colours live in ``<collection>/Media/Moods/<Mood>/Mood.MoodSetting.xml``.

The engine's sun path is not documented. ``DayTime01`` of the four stock
moods (night 0.15, sunrise ~0.52, day ~0.6, sunset ~0.73-0.75) puts the
daylight arc roughly between :data:`SUN_RISE` and :data:`SUN_SET`; the sun
direction is an approximation of that arc, rising in the east (Blender +X)
and culminating in the south (Blender -Y).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
import re
import xml.etree.ElementTree as ET

from .. import library

from ..model.decoration import Decoration, DecorationMood
from ..resolve.block_lookup import _aliases_for
from .clouds import CloudLayer, load_cloud_layer

log = logging.getLogger("gbxbpy.map_import_v4")

MOODS = ("Sunrise", "Sunset", "Night", "Day")
SUN_RISE = 0.49
SUN_SET = 0.77
# Keeps a usable key light when a custom time of day falls outside the arc.
MIN_ELEVATION = math.radians(5.0)
# Moonlight is far below the sun; this keeps night imports readable.
MIN_MOON_STRENGTH = 0.5
DEFAULT_SUN = (3.0, 2.42, 1.69)
DEFAULT_AMBIENT = (0.246, 0.333, 0.5)
DEFAULT_INTENSITY = 1.8  # Stadium Day DecorationMood.sun_moon_intensity

Vec3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class SunHalo:
    """One ``<HdrSun><AtmoN>`` glow: ``pow(cos(angle to sun), power) * scale * color``."""

    power: float
    color: Vec3
    scale: float


@dataclass(frozen=True, slots=True)
class Sky:
    """Sky dome inputs: mood textures plus the ``<Atmo>`` sun, moon and star settings.

    ``SkyColor.dds`` is a sun-relative HDR dome (u = azimuth away from the
    sun, v = elevation); the bridge mirrors it around the sun direction.
    ``Clouds.tga`` is the scrolling cloud shadow mask (not drawn in the sky)
    and ``Moon.tga`` the moon disc (only night moods ship one, so other
    moods borrow the Night copy). ``clouds`` holds the sprite cloud layer.
    """

    sky_color_path: str = ""
    clouds_path: str = ""
    moon_path: str = ""
    enable_stars: bool = False
    # Share of the star field shown; stars only show in night moods.
    star_visibility: float = 0.0
    moon_hdr_scale: float = 0.0
    moon_direction: Vec3 = (0.0, 0.7071, 0.7071)
    sun_power: float = 80000.0
    sun_halos: tuple[SunHalo, ...] = ()
    # ``<Atmo><Clouds MaxRgb MaxRgbX>``: tint of lit clouds.
    cloud_color: Vec3 = (1.0, 1.0, 1.0)
    clouds: CloudLayer | None = None


@dataclass(frozen=True, slots=True)
class MoodSetting:
    """The lighting and sky part of ``Mood.MoodSetting.xml``."""

    day_time: float = 0.6
    latitude: float = 45.0
    sun: Vec3 = DEFAULT_SUN
    moon: Vec3 = (0.0, 0.0, 0.0)
    ambient: Vec3 = DEFAULT_AMBIENT
    fog: Vec3 | None = None
    enable_stars: bool = False
    moon_hdr_scale: float = 0.0
    sun_power: float = 80000.0
    sun_halos: tuple[SunHalo, ...] = ()
    cloud_color: Vec3 = (1.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class Environment:
    decoration: str = ""
    decoration_path: str = ""
    mood: str = "Day"
    mood_path: str = ""
    day_time: float = 0.6
    latitude: float = 45.0
    # Unit vector from the scene towards the light, Blender Z-up.
    light_direction: Vec3 = (0.0, -0.7071, 0.7071)
    light_color: Vec3 = (1.0, 0.807, 0.563)
    light_strength: float = 5.4
    is_moon: bool = False
    ambient: Vec3 = DEFAULT_AMBIENT
    fog: Vec3 | None = None
    sky: Sky | None = None


def mood_name(decoration: str) -> str:
    """The stock mood a decoration id ends with (``Day`` when none matches)."""
    folded = decoration.casefold()
    for mood in MOODS:
        if mood.casefold() in folded:
            return mood
    return "Day"


def decoration_candidates(decoration: str) -> list[str]:
    """File stems that may define ``decoration``, best first."""
    names = [f"Base{decoration}", decoration]
    match = re.fullmatch(r"(Sunrise|Sunset|Night|Day)(\d+)", decoration, re.IGNORECASE)
    if match:
        size = match.group(2)
        names.insert(0, f"Base{size}x{size}{match.group(1)}")
    return names


def collection_roots(gamedata_dir: str, collection: str) -> list[str]:
    roots = []
    for name in _aliases_for(collection) + ["Stadium"]:
        path = os.path.join(gamedata_dir, name)
        if library.isdir(path) and path not in roots:
            roots.append(path)
    return roots


def find_decoration(gamedata_dir: str, collection: str, decoration: str) -> str | None:
    mood = mood_name(decoration)
    for root in collection_roots(gamedata_dir, collection):
        folder = os.path.join(root, "GameCtnDecoration")
        if not library.isdir(folder):
            continue
        files = {
            name.casefold(): os.path.join(folder, name) for name in library.listdir(folder)
            if name.casefold().endswith(".decoration.gbx")
        }
        for stem in decoration_candidates(decoration):
            path = files.get(f"{stem}.decoration.gbx".casefold())
            if path:
                return path
        # Ids without a size (e.g. ``Day`` on RedIsland): same mood, any size.
        same_mood = sorted(
            path for name, path in files.items()
            if name.startswith("base") and name.endswith(f"{mood}.decoration.gbx".casefold())
        )
        if same_mood:
            return same_mood[0]
    return None


def _color(element: ET.Element | None, attribute: str = "HdrColor") -> Vec3 | None:
    if element is None:
        return None
    try:
        values = [float(v) for v in (element.get(attribute) or "").split()]
    except ValueError:
        return None
    return (values[0], values[1], values[2]) if len(values) == 3 else None


def _hex_color(text: str | None) -> Vec3 | None:
    if not text or not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _float(element: ET.Element | None, attribute: str, default: float) -> float:
    try:
        return float(element.get(attribute, default)) if element is not None else default
    except ValueError:
        return default


def _sun_halos(hdr_sun: ET.Element | None) -> tuple[SunHalo, ...]:
    halos = []
    for name in ("Atmo1", "Atmo2"):
        element = hdr_sun.find(name) if hdr_sun is not None else None
        color = _hex_color(element.get("Color")) if element is not None else None
        if color is not None:
            halos.append(SunHalo(_float(element, "Power", 1.0), color, _float(element, "Scale", 0.0)))
    return tuple(halos)


def parse_mood_setting(text: str) -> MoodSetting:
    root = ET.fromstring(text)
    light = root.find("Light")
    if light is None:
        raise ValueError("Mood setting has no <Light>")
    fog = root.find("Fog")
    atmo = root.find("Atmo")
    hdr_sun = atmo.find("HdrSun") if atmo is not None else None
    clouds = atmo.find("Clouds") if atmo is not None else None
    cloud_rgb = _hex_color(clouds.get("MaxRgb")) if clouds is not None else None
    cloud_scale = _float(clouds, "MaxRgbX", 1.0)
    return MoodSetting(
        day_time=float(light.get("DayTime01", 0.6)),
        latitude=float(light.get("Latitude", 45.0)),
        sun=_color(light.find("LDirSun")) or (0.0, 0.0, 0.0),
        moon=_color(light.find("LDirMoon")) or (0.0, 0.0, 0.0),
        ambient=_color(light.find("LAmbient")) or DEFAULT_AMBIENT,
        fog=_hex_color(fog.get("Color")) if fog is not None and fog.get("Enabled", "1") != "0" else None,
        enable_stars=light.get("EnableStars", "0") == "1",
        moon_hdr_scale=_float(atmo, "MoonHdrScale", 0.0),
        sun_power=_float(hdr_sun, "Power", 80000.0),
        sun_halos=_sun_halos(hdr_sun),
        cloud_color=tuple(c * cloud_scale for c in cloud_rgb or (1.0, 1.0, 1.0)),  # type: ignore[arg-type]
    )


def find_mood_setting(gamedata_dir: str, collection: str, mood: str) -> str | None:
    for root in collection_roots(gamedata_dir, collection):
        path = os.path.join(root, "Media", "Moods", mood, "Mood.MoodSetting.xml")
        if library.isfile(path):
            return path
    return None


def find_sky_texture(gamedata_dir: str, collection: str, mood: str, name: str) -> str:
    """``<collection>/Media/Moods/<mood>/<name>``; other moods' copies as fallback
    (only Night ships ``Moon.tga``)."""
    roots = collection_roots(gamedata_dir, collection)
    moods = [mood] + [m for m in ("Night", "Day", "Sunset", "Sunrise") if m != mood]
    candidates = [
        os.path.join(root, "Media", "Moods", m, name) for m in moods for root in roots
    ]
    for path in candidates[:len(roots)]:
        if library.isfile(path):
            return path
    return next((p for p in candidates[len(roots):] if library.exists(p) and library.isfile(p)), "")


def _arc_direction(day_time: float, latitude: float) -> Vec3:
    span = SUN_SET - SUN_RISE
    u = math.pi * min(max((day_time - SUN_RISE) / span, 0.0), 1.0)
    peak = math.radians(max(5.0, 90.0 - abs(latitude)))
    elevation = max(MIN_ELEVATION, peak * math.sin(u))
    # Compass azimuth (north = +Y, east = +X): east at rise, south at noon.
    azimuth = math.pi / 2 + u
    if latitude < 0:
        azimuth = math.pi / 2 - u
    return (
        math.sin(azimuth) * math.cos(elevation),
        math.cos(azimuth) * math.cos(elevation),
        math.sin(elevation),
    )


def moon_direction(day_time: float, latitude: float) -> Vec3:
    """The moon follows the sun's arc half a day later (low in the east at sunset)."""
    return _arc_direction((day_time + 0.5) % 1.0, latitude)


def light_direction(day_time: float, latitude: float) -> tuple[Vec3, bool]:
    """Direction towards the sun, or the moon when ``day_time`` is night."""
    if SUN_RISE <= day_time <= SUN_SET:
        return _arc_direction(day_time, latitude), False
    return moon_direction(day_time, latitude), True


def build_environment(
    setting: MoodSetting, *, decoration: str = "", decoration_path: str = "",
    mood: str = "Day", mood_path: str = "", day_time: float | None = None,
    intensity: float = 1.0, latitude: float | None = None,
    sky: bool = True, sky_color_path: str = "", clouds_path: str = "", moon_path: str = "",
    clouds: CloudLayer | None = None,
) -> Environment:
    time = setting.day_time if day_time is None else day_time
    latitude = setting.latitude if latitude is None else latitude
    direction, is_moon = light_direction(time, latitude)
    color = setting.sun
    if is_moon or max(color) <= 1e-6:
        color, is_moon = setting.moon, True
    peak = max(color)
    if peak <= 1e-6:
        color, peak = (0.6, 0.7, 1.0), 1.0
    strength = peak * (intensity if intensity > 0 else 1.0)
    if is_moon:
        strength = max(strength, MIN_MOON_STRENGTH)
    sky_setting = Sky(
        sky_color_path=sky_color_path, clouds_path=clouds_path, moon_path=moon_path,
        enable_stars=setting.enable_stars,
        star_visibility=1.0 if setting.enable_stars and is_moon else 0.0,
        moon_hdr_scale=setting.moon_hdr_scale,
        moon_direction=moon_direction(time, latitude),
        sun_power=setting.sun_power, sun_halos=setting.sun_halos,
        cloud_color=setting.cloud_color, clouds=clouds,
    ) if sky else None
    return Environment(
        decoration=decoration, decoration_path=decoration_path, mood=mood,
        mood_path=mood_path, day_time=time, latitude=latitude,
        light_direction=direction,
        light_color=tuple(c / peak for c in color),  # type: ignore[arg-type]
        light_strength=strength,
        is_moon=is_moon, ambient=setting.ambient, fog=setting.fog, sky=sky_setting,
    )


def _load_mood(decoration_path: str, resolver) -> DecorationMood | None:
    """The decoration's ``DecorationMood``; only a few scalar fields are used,
    so neither file is loaded recursively and unrelated parser noise (audio,
    scene references) does not mark the import partial."""
    from .. import load
    from ..errors import GbxbpyError

    try:
        decoration = load(decoration_path, resolver=resolver, recursive=False)
        if not isinstance(decoration, Decoration) or not decoration.deco_mood_path:
            return None
        path = resolver.resolve(decoration.deco_mood_path, source_path=decoration_path)
        mood = load(path, resolver=resolver, recursive=False) if path else None
    except (GbxbpyError, OSError) as exc:
        log.warning("Decoration mood of %s unreadable: %s", decoration_path, exc)
        return None
    return mood if isinstance(mood, DecorationMood) else None


def mood_decoration(decoration: str, mood: str) -> str:
    """``decoration`` with its mood swapped (``48x48Screen155Day`` -> ``...Night``)."""
    current = mood_name(decoration)
    match = re.search(current, decoration, re.IGNORECASE)
    if match is None:
        return decoration + mood
    return decoration[:match.start()] + mood + decoration[match.end():]


def resolve_environment(
    resolver, collection: str, decoration: str, *, mood: str | None = None,
    day_time: float | None = None, import_sky: bool = True, import_clouds: bool = False, issue=None,
) -> Environment:
    """Decoration mood lighting and sky; ``mood`` overrides the decoration's stock mood.

    ``issue(code, source, message)`` receives missing or unreadable files;
    they fall back to a default day sun or a flat sky colour.
    """
    issue = issue or (lambda *_args: None)
    gamedata = resolver.gamedata_dir
    collection = collection or "Stadium"
    if mood:
        decoration = mood_decoration(decoration or "Day", mood)
    mood = mood_name(decoration)
    decoration_path = find_decoration(gamedata, collection, decoration) if decoration else None
    intensity, latitude = DEFAULT_INTENSITY, None
    if decoration_path is None:
        if decoration:
            issue("missing_decoration", decoration, f"No {collection} decoration file")
    else:
        deco_mood = _load_mood(decoration_path, resolver)
        if deco_mood is not None:
            intensity = deco_mood.sun_moon_intensity or DEFAULT_INTENSITY
            latitude = deco_mood.latitude
    # Maps without a decoration id (synthetic/test maps) silently get the default sun.
    mood_path = find_mood_setting(gamedata, collection, mood) if decoration else None
    setting = MoodSetting()
    mood_xml = ""
    if mood_path is None:
        if decoration:
            issue("missing_mood", f"{collection}/{mood}", "Mood.MoodSetting.xml not found; default sun")
    else:
        try:
            with open(mood_path, encoding="utf-8", errors="replace") as handle:
                mood_xml = handle.read()
            setting = parse_mood_setting(mood_xml)
        except (OSError, ET.ParseError, ValueError) as exc:
            issue("parse_failed", mood_path, f"Mood setting: {exc}")
            mood_path, mood_xml = None, ""
    textures = {"sky_color_path": "", "clouds_path": "", "moon_path": ""}
    clouds = None
    if import_sky and decoration:
        textures["sky_color_path"] = find_sky_texture(gamedata, collection, mood, "SkyColor.dds")
        if setting.moon_hdr_scale > 0:
            textures["moon_path"] = find_sky_texture(gamedata, collection, mood, "Moon.tga")
        if not textures["sky_color_path"]:
            issue("missing_sky", f"{collection}/{mood}", "SkyColor.dds not found; flat sky colour")
        if import_clouds:
            textures["clouds_path"] = find_sky_texture(gamedata, collection, mood, "Clouds.tga")
            clouds = load_cloud_layer(
                gamedata, lambda name: find_sky_texture(gamedata, collection, mood, name),
                mood_xml, issue,
            )
    environment = build_environment(
        setting, decoration=decoration, decoration_path=decoration_path or "",
        mood=mood, mood_path=mood_path or "", day_time=day_time,
        intensity=intensity, latitude=latitude, sky=import_sky, clouds=clouds, **textures,
    )
    log.info(
        "V4 environment: decoration=%s mood=%s day_time=%.3f %s dir=(%.2f, %.2f, %.2f) strength=%.2f",
        decoration, mood, environment.day_time, "moon" if environment.is_moon else "sun",
        *environment.light_direction, environment.light_strength,
    )
    return environment


def plan_environment(map_asset, dependencies) -> Environment:
    """Resolve the map's decoration and mood; missing files fall back to a day sun."""
    plan = dependencies.plan
    return resolve_environment(
        dependencies.resolver, map_asset.decoration_collection or "Stadium",
        map_asset.decoration or "", day_time=getattr(map_asset, "day_time", None),
        import_sky=getattr(plan.options, "import_sky", True), issue=plan.issue,
        import_clouds=plan.options.import_clouds,
    )
