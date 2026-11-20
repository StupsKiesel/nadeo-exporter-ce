"""Game identity and Nadeo.ini directory resolution."""

from __future__ import annotations

import configparser
import ctypes
import logging
import os
from pathlib import Path
import re
import sys

log = logging.getLogger("gbxbpy.game_paths")

GAME_NAMES = {
    "trackmania": "Trackmania 2020",
    "maniaplanet": "ManiaPlanet / Trackmania 2",
    "turbo": "Trackmania Turbo",
    "forever": "Trackmania Forever",
}


def _ini(game_dir: str) -> dict[str, str]:
    if not game_dir:
        return {}
    path = Path(game_dir) / "Nadeo.ini"
    try:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="cp1252")
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(text)
    except FileNotFoundError:
        return {}
    except configparser.Error:
        log.warning("Invalid Nadeo.ini format at %s", path)
        return {}
    except (OSError, UnicodeError) as exc:
        log.warning("Cannot read Nadeo.ini at %s: %s", path, exc)
        return {}
    fields = ("userdir", "usersubdir", "commondir", "windowtitle", "distro")
    return {key: value for section in parser.sections() for key, value in parser.items(section)
            if key in fields}


def game_family(game_dir: str) -> str:
    if not game_dir:
        return ""
    root = Path(game_dir)
    if any((root / name).is_file() for name in ("ManiaPlanet.exe", "ManiaPlanet64.exe")):
        return "maniaplanet"
    if (root / "TrackmaniaTurbo.exe").is_file():
        return "turbo"
    if (root / "TmForever.exe").is_file():
        return "forever"
    if (root / "Trackmania.exe").is_file():
        return "trackmania"
    return ""


def game_label(game_dir: str) -> str:
    family = game_family(game_dir)
    title = GAME_NAMES.get(family, "Unknown game")
    distro = _ini(game_dir).get("distro", "").casefold()
    platform = {"uplay": "Ubisoft", "azuro": "Steam"}.get(distro, "")
    return f"{title} - {platform or Path(game_dir).name}"


def _windows_folder(folder_id: int) -> str:
    buffer = ctypes.create_unicode_buffer(260)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, folder_id, None, 0, buffer)
    if result:
        raise OSError(f"Cannot resolve Windows known folder (HRESULT {result})")
    return buffer.value


def _base_dirs(openplanet_dir: str) -> tuple[str, str]:
    if sys.platform == "win32":
        try:
            return _windows_folder(5), _windows_folder(35)
        except OSError as exc:
            log.warning("%s", exc)
            return "", ""
    # Openplanet's Wine user root identifies the corresponding Wine profile.
    user = Path(openplanet_dir).parent if openplanet_dir else None
    if user is not None and user.parent.name.casefold() == "users":
        drive = user.parent.parent
        prefix = drive.parent
        if drive.name.casefold() == "drive_c":
            from .linux_paths import _wine_documents
            return str(_wine_documents(prefix, user)), str(drive / "ProgramData")
    return "", ""


def nadeo_dirs(game_dir: str, openplanet_dir: str = "") -> tuple[str, str]:
    """Return user and common data roots from INI; unresolved values are empty."""
    data = _ini(game_dir)
    if not data:
        return "", ""
    docs, common = _base_dirs(openplanet_dir)

    def expand(value: str) -> str:
        if not value:
            return ""
        values = {"userdocs": docs, "commondata": common}
        tokens = re.findall(r"\{([^{}]+)\}", value)
        if any(not values.get(token.casefold()) for token in tokens):
            return ""
        for token in tokens:
            value = re.sub(r"\{" + re.escape(token) + r"\}",
                           lambda _match: values[token.casefold()], value, flags=re.I)
        if sys.platform != "win32":
            value = value.replace("\\", os.sep)
        if not os.path.isabs(value) or "{" in value or "}" in value:
            return ""
        return os.path.normpath(value)

    user = expand(data.get("userdir", ""))
    if not user and data.get("usersubdir") and docs:
        subdir = data["usersubdir"]
        if subdir not in (".", "..") and not any(c in subdir for c in "/\\:"):
            user = os.path.join(docs, subdir)
    return user, expand(data.get("commondir", ""))


def openplanet_mismatch(game_dir: str, openplanet_dir: str) -> bool:
    expected = {"trackmania": "openplanetnext", "maniaplanet": "openplanet4",
                "turbo": "openplanetturbo", "forever": "openplanetunited"}
    known = set(expected.values()) | {"openplanetnations"}
    family = game_family(game_dir)
    root = os.path.basename(os.path.normpath(openplanet_dir)).casefold() if openplanet_dir else ""
    return bool(family and root in known and root != expected[family]
                and not (family == "forever" and root == "openplanetnations"))
