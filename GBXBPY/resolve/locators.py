"""Download cache for locator URLs stored in maps.

Maps name custom files (sign images, texture mods) by a pack path such as
``Skins\\Any\\Advertisement6x1\\_download-host_x.jpg`` plus the URL the game
downloads it from (``PackDesc.LocatorUrl``). When the file is not on disk,
:class:`LocatorCache` downloads it once into its cache folder.

Only ``http``/``https`` URLs are fetched, with a timeout and a size limit;
failures are remembered per cache so an import asks each URL at most once.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("gbxbpy.resolve.locators")

DEFAULT_TIMEOUT = 30.0
MAX_BYTES = 512 * 1024 * 1024
_USER_AGENT = "TrackmaniaBlenderBridge (locator download)"
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_file_name(name: str, fallback: str = "download") -> str:
    """A file name with no path separators or reserved characters."""
    name = _UNSAFE.sub("_", os.path.basename(name.replace("\\", "/"))).strip(" .")
    return name[:120] or fallback


class LocatorCache:
    """Download locator URLs into ``<cache_dir>/<hash>/<name>``."""

    def __init__(self, cache_dir: str, *, timeout: float = DEFAULT_TIMEOUT,
                 max_bytes: int = MAX_BYTES, opener=None) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.problems: dict[str, str] = {}
        self.downloaded: list[str] = []
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()

    def target(self, url: str, name: str = "") -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        base = name or urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        return os.path.join(self.cache_dir, digest, safe_file_name(base))

    def fetch(self, url: str, name: str = "") -> Optional[str]:
        """Return the local path of ``url``'s file, downloading it if needed."""
        url = (url or "").strip()
        if not url:
            return None
        if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            self.problems[url] = "only http(s) locator URLs are downloaded"
            return None
        target = self.target(url, name)
        with self._lock:
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                return target
            if url in self.problems:
                return None
            try:
                self._download(url, target)
            except Exception as exc:  # noqa: BLE001 - any network/disk failure
                self.problems[url] = f"download failed: {exc}"
                log.warning("Locator download failed: %s (%s)", url, exc)
                return None
            self.downloaded.append(url)
            log.info("Downloaded locator %s -> %s", url, target)
            return target

    def _download(self, url: str, target: str) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        os.makedirs(os.path.dirname(target), exist_ok=True)
        partial = target + ".part"
        size = 0
        try:
            with self._open(request, timeout=self.timeout) as response, open(partial, "wb") as out:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise OSError(f"HTTP {status}")
                while True:
                    block = response.read(1 << 16)
                    if not block:
                        break
                    size += len(block)
                    if size > self.max_bytes:
                        raise OSError(f"larger than {self.max_bytes} bytes")
                    out.write(block)
            if size == 0:
                raise OSError("empty response")
            os.replace(partial, target)
        finally:
            if os.path.exists(partial):
                os.remove(partial)


__all__ = ["LocatorCache", "safe_file_name", "DEFAULT_TIMEOUT", "MAX_BYTES"]
