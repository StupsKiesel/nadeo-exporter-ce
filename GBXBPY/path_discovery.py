"""On-demand suggestions for addon directory preferences."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


def _windows_steam_games() -> list[Path]:
    from .linux_paths import _vdf, _steam_game, GAME_APP_IDS

    roots = []
    program_files = os.environ.get("PROGRAMFILES(X86)")
    if program_files:
        roots.append(Path(program_files) / "Steam")
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                root, _kind = winreg.QueryValueEx(key, "SteamPath")
                if isinstance(root, str) and root:
                    roots.append(Path(root))
        except FileNotFoundError:
            pass
        except OSError as exc:
            import logging
            logging.getLogger("gbxbpy.path_discovery").warning("Cannot read Steam registry: %s", exc)
    libraries = list(roots)
    for root in roots:
        folders = _vdf(root / "steamapps" / "libraryfolders.vdf").get("libraryfolders", {})
        if isinstance(folders, dict):
            for entry in folders.values():
                value = entry.get("path") if isinstance(entry, dict) else entry
                if isinstance(value, str) and os.path.isabs(value):
                    libraries.append(Path(value))
    games = []
    for library in libraries:
        steamapps = library / "steamapps"
        for app_id in GAME_APP_IDS:
            game = _steam_game(steamapps, app_id)
            if game is not None:
                games.append(game)
    return games


@dataclass(frozen=True)
class Candidate:
    path: str
    suggested: bool = False


def candidates(name: str, configured: dict[str, str]) -> list[Candidate]:
    """Find existing folders or explicitly marked locations that can be created."""
    home = Path.home()
    games: list[Path] = []
    openplanets: list[Path] = []
    users: list[Path] = []
    if sys.platform.startswith("linux"):
        from .linux_paths import discover

        discover.cache_clear()
        found = discover(include_legacy=True)
        games = [Path(path) for path in found.games]
        openplanets = [Path(path) for path in found.openplanet]
        users = [Path(path) for path in found.users]
    else:
        from .linux_paths import LEGACY_GAME_FOLDERS, OPENPLANET_FOLDERS
        from .resolve.skins import default_packs_dirs, default_user_dirs

        games = [Path(path).parent for path in default_packs_dirs()]
        if sys.platform == "win32":
            games.extend(_windows_steam_games())
            drive = os.environ.get("SystemDrive")
            if drive:
                games.extend(Path(drive + os.sep) / name for name in LEGACY_GAME_FOLDERS)
            for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
                base = os.environ.get(variable)
                if base:
                    base_path = Path(base)
                    for game in LEGACY_GAME_FOLDERS:
                        games.extend((base_path / game,
                                      base_path / "Ubisoft" / "Ubisoft Game Launcher" / "games" / game))
        openplanets = [home / folder for folder in OPENPLANET_FOLDERS]
        users = [Path(path) for path in default_user_dirs()]

    def selected(key: str, fallback: list[Path]) -> list[Path]:
        value = configured.get(key, "")
        return [Path(value)] if value else fallback

    paths: list[Path]
    allow_suggestion = False
    if name == "trackmania_dir":
        from .linux_paths import _looks_like_game

        paths = [path for path in games if _looks_like_game(path)]
    elif name == "openplanet_dir":
        paths = openplanets
    elif name == "user_skins_dir":
        paths = users
    elif name == "gamedata_dir":
        paths = [root / "Extract" / "GameData"
                 for root in selected("openplanet_dir", openplanets)]
        allow_suggestion = True
    elif name == "packs_dir":
        paths = [root / "Packs" for root in selected("trackmania_dir", games)]
    elif name in ("parse_cache_dir", "skin_cache_dir"):
        from .resolve.skins import default_cache_dir

        cache = Path(default_cache_dir())
        paths = [cache if name == "skin_cache_dir" else cache.parent / "ParseCache"]
        allow_suggestion = True
    elif name == "build_dir":
        source_root = Path(__file__).resolve().parent.parent
        paths = [source_root / "build"] if (source_root / "build.py").is_file() else []
    else:
        raise ValueError(f"Unknown path preference: {name}")

    result: list[Candidate] = []
    seen: set[str] = set()
    for path in paths:
        exists = path.is_dir()
        if not exists and not allow_suggestion:
            continue
        key = os.path.normcase(os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            result.append(Candidate(str(path), suggested=not exists))
    return result
