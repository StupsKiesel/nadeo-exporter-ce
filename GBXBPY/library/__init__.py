"""Game file library: Openplanet extract folder, local bridge API, on-demand fetching.

* :mod:`.server` – local HTTP API the Openplanet plugin polls for work.
* :mod:`.index` – the game's file listing with sizes.
* :mod:`.provider` – fetch-on-demand file access used during imports.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from .index import GameIndex
from .provider import (
    LibraryProvider, activate, active, current, ensure, exists, first_available, isdir, isfile, listdir,
    refetch, walk,
)
from .server import DEFAULT_PORT, BridgeServer, JobQueue, game_path, get_server, start_server, stop_server

PLUGIN_ID = "BlenderBridgeExtractor"


def openplanet_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "OpenplanetNext")


def default_library_dir() -> str:
    """``%USERPROFILE%/OpenplanetNext/Extract/GameData``, where the plugin extracts to."""
    return os.path.join(openplanet_dir(), "Extract", "GameData")


def installed_plugin_dir(openplanet: Optional[str] = None) -> str:
    return os.path.join(openplanet or openplanet_dir(), "Plugins", PLUGIN_ID)


def install_plugin(source_dir: str, openplanet: Optional[str] = None) -> str:
    """Copy the plugin folder into ``OpenplanetNext/Plugins``; returns the target."""
    if not os.path.isfile(os.path.join(source_dir, "info.toml")):
        raise FileNotFoundError(f"Plugin source not found: {source_dir}")
    target = installed_plugin_dir(openplanet)
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(source_dir, target)
    return target


__all__ = [
    "BridgeServer", "DEFAULT_PORT", "GameIndex", "JobQueue", "LibraryProvider", "PLUGIN_ID",
    "activate", "active", "current", "default_library_dir", "ensure", "exists", "first_available", "game_path",
    "get_server", "install_plugin", "installed_plugin_dir", "isdir", "isfile", "listdir",
    "openplanet_dir", "refetch", "start_server", "stop_server", "walk",
]
