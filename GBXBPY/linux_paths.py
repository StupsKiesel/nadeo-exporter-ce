"""Bounded discovery of Trackmania installations and Wine user folders on Linux."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
import os
from pathlib import Path, PureWindowsPath
import re

log = logging.getLogger("gbxbpy.linux_paths")
APP_ID = "2225070"
GAME_APP_IDS = (APP_ID, "228760", "232910", "243360", "600720", "375900", "7200", "11020")
OPENPLANET_FOLDERS = ("OpenplanetNext", "Openplanet4", "OpenplanetTurbo", "OpenplanetUnited", "OpenplanetNations")
GAME_EXECUTABLES = ("Trackmania.exe", "ManiaPlanet.exe", "ManiaPlanet64.exe",
                    "TrackmaniaTurbo.exe", "TmForever.exe")
LEGACY_GAME_FOLDERS = ("ManiaPlanet", "Trackmania Turbo", "TrackMania Nations Forever",
                       "TrackMania United Forever", "TmNationsForever", "TmUnitedForever")


class AmbiguousPathError(OSError):
    pass


@dataclass(frozen=True)
class Paths:
    games: tuple[str, ...]
    openplanet: tuple[str, ...]
    users: tuple[str, ...]


def unique(paths: tuple[str, ...], label: str) -> str:
    if len(paths) > 1:
        raise AmbiguousPathError(f"Multiple {label} folders found; select one in addon preferences")
    return paths[0] if paths else ""


def _vdf(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        log.warning("Cannot read Steam metadata %s: %s", path, exc)
        return {}
    try:
        tokens = re.findall(r'"(?:\\.|[^"\\])*"|[{}]|//[^\n]*|\S+', text)
        tokens = [token for token in tokens if not token.startswith("//")]
        position = 0

        def parse(nested=False):
            nonlocal position
            result = {}
            while position < len(tokens):
                token = tokens[position]
                position += 1
                if token == "}":
                    if not nested:
                        raise ValueError("unexpected closing brace")
                    return result
                if not token.startswith('"') or not token.endswith('"'):
                    raise ValueError("expected quoted key")
                key = re.sub(r'\\([\\"])', r'\1', token[1:-1])
                if position >= len(tokens):
                    raise ValueError("missing value")
                value = tokens[position]
                position += 1
                if value == "{":
                    result[key] = parse(True)
                elif value.startswith('"') and value.endswith('"'):
                    result[key] = re.sub(r'\\([\\"])', r'\1', value[1:-1])
                else:
                    raise ValueError("expected quoted value or object")
            if nested:
                raise ValueError("missing closing brace")
            return result

        return parse()
    except (ValueError, RecursionError) as exc:
        log.warning("Invalid Steam metadata %s: %s", path, exc)
        return {}


def _children(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda item: item.name.casefold())
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("Cannot inspect %s: %s", path, exc)
        return []


def _steam_libraries(home: Path) -> list[Path]:
    roots = (
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    )
    libraries = []
    seen = set()
    for root in roots:
        steamapps = root / "steamapps"
        if not steamapps.is_dir():
            continue
        candidates = [root]
        folders = _vdf(steamapps / "libraryfolders.vdf").get("libraryfolders", {})
        if isinstance(folders, dict):
            for entry in folders.values():
                value = entry.get("path") if isinstance(entry, dict) else entry
                if isinstance(value, str) and os.path.isabs(value):
                    candidates.append(Path(value))
        for candidate in candidates:
            key = os.path.realpath(candidate)
            if key not in seen:
                seen.add(key)
                libraries.append(candidate)
    return libraries


def _wine_documents(prefix: Path, user: Path) -> Path:
    """Resolve Wine's Personal folder for the single user in a prefix."""
    registry = prefix / "user.reg"
    try:
        text = registry.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return user / "Documents"
    except (OSError, UnicodeError) as exc:
        log.warning("Cannot read Wine user registry %s: %s", registry, exc)
        return user / "Documents"
    section = False
    for line in text.splitlines():
        if line.startswith("["):
            key = line.split("]", 1)[0].replace("\\\\", "\\").casefold()
            section = key == "[software\\microsoft\\windows\\currentversion\\explorer\\user shell folders"
        elif section:
            match = re.match(r'^"Personal"=(?:str\(2\):)?"((?:\\.|[^"\\])*)"', line, re.I)
            if not match:
                continue
            value = re.sub(r'\\([\\"])', r'\1', match.group(1))
            value = re.sub(r"%USERPROFILE%", lambda _: f"C:\\users\\{user.name}", value, flags=re.I)
            path = PureWindowsPath(value)
            if not path.is_absolute() or not re.fullmatch(r"[a-zA-Z]:", path.drive) or ".." in path.parts:
                log.warning("Cannot resolve Wine Documents path %r in %s", value, registry)
                return user / "Documents"
            drive = _wine_drive(prefix, path.drive)
            return drive.joinpath(*path.parts[1:])
    return user / "Documents"


def _wine_drive(prefix: Path, letter: str) -> Path:
    mapped = prefix / "dosdevices" / letter.lower()
    if mapped.is_dir() or letter.casefold() != "c:":
        return mapped
    return prefix / "drive_c"


def _steam_game(steamapps: Path, app_id: str) -> Path | None:
    manifest = _vdf(steamapps / f"appmanifest_{app_id}.acf").get("AppState", {})
    if not isinstance(manifest, dict) or manifest.get("appid") != app_id:
        return None
    name = manifest.get("installdir", "")
    if (isinstance(name, str) and name not in ("", ".", "..")
            and "/" not in name and "\\" not in name and ":" not in name):
        return steamapps / "common" / name
    log.warning("Invalid Trackmania install directory in %s", steamapps)
    return None


def _looks_like_game(path: Path) -> bool:
    return path.is_dir() and ((path / "Packs").is_dir()
                              or any((path / name).is_file() for name in GAME_EXECUTABLES))


@lru_cache(maxsize=2)
def discover(include_legacy: bool = False) -> Paths:
    home = Path(os.path.expanduser("~"))
    games, openplanet, users = [], [], []
    seen = [set(), set(), set()]

    def add(target, path):
        if not path.is_dir():
            return
        key = os.path.normcase(os.path.realpath(path))
        index = 0 if target is games else 1 if target is openplanet else 2
        if key not in seen[index]:
            seen[index].add(key)
            target.append(str(path))

    for folder in OPENPLANET_FOLDERS if include_legacy else ("OpenplanetNext",):
        add(openplanet, home / folder)
    add(users, home / "Documents" / "Trackmania")
    prefixes = []
    for library in _steam_libraries(home):
        steamapps = library / "steamapps"
        for app_id in GAME_APP_IDS if include_legacy else (APP_ID,):
            game = _steam_game(steamapps, app_id)
            if game is not None and (_looks_like_game(game) if include_legacy else (game / "Packs").is_dir()):
                add(games, game)
            prefixes.append(steamapps / "compatdata" / app_id / "pfx")

    if os.environ.get("WINEPREFIX"):
        prefixes.append(Path(os.path.expanduser(os.environ["WINEPREFIX"])))
    prefixes.append(home / ".wine")
    prefixes.extend(_children(home / "Games"))
    prefixes.extend(_children(home / ".local" / "share" / "bottles" / "bottles"))
    prefixes.extend(_children(home / ".var" / "app" / "com.usebottles.bottles" / "data" / "bottles" / "bottles"))
    visited = set()
    for prefix in prefixes:
        drive = _wine_drive(prefix, "c:")
        if not drive.is_dir():
            continue
        key = os.path.realpath(prefix)
        if key in visited:
            continue
        visited.add(key)
        drives = [drive]
        drives.extend(path for path in _children(prefix / "dosdevices")
                      if re.fullmatch(r"[a-zA-Z]:", path.name) and path.is_dir())
        for root in drives:
            for path in (
                root / "Program Files (x86)" / "Ubisoft" / "Ubisoft Game Launcher" / "games" / "Trackmania",
                root / "Program Files" / "Ubisoft" / "Ubisoft Game Launcher" / "games" / "Trackmania",
                root / "Program Files" / "Epic Games" / "TrackmaniaNext",
                root / "Ubisoft Game Launcher" / "games" / "Trackmania",
                root / "Trackmania",
            ):
                if (path / "Packs").is_dir():
                    add(games, path)
            if include_legacy:
                for name in LEGACY_GAME_FOLDERS:
                    path = root / name
                    if _looks_like_game(path):
                        add(games, path)
                for program in ("Program Files (x86)", "Program Files"):
                    base = root / program
                    for name in LEGACY_GAME_FOLDERS:
                        for path in (base / name, base / "Ubisoft" / "Ubisoft Game Launcher" / "games" / name):
                            if _looks_like_game(path):
                                add(games, path)
        profiles = [user for user in _children(drive / "users")
                    if user.is_dir() and user.name.casefold() not in
                    {"public", "default", "default user", "all users"}]
        for user in profiles:
            for folder in OPENPLANET_FOLDERS if include_legacy else ("OpenplanetNext",):
                add(openplanet, user / folder)
            documents = _wine_documents(prefix, user) if len(profiles) == 1 else user / "Documents"
            add(users, documents / "Trackmania")
    return Paths(tuple(games), tuple(openplanet), tuple(users))
