"""On-demand game files: the library folder backed by the Openplanet plugin.

The library is the folder the plugin extracts into
(``OpenplanetNext/Extract/GameData``) or a custom folder; fetched files are
then moved from the extract folder into it. While a :class:`LibraryProvider` is
active (see :func:`activate`), the module-level helpers below replace
``os.path.isfile``/``isdir``/``listdir``/``os.walk`` for library paths:

* listings merge the disk with the game index, so lookups see files that
  were never extracted;
* :func:`ensure` (and :func:`isfile` for indexed files) fetches a missing
  file, or one whose size differs from the game's, from the plugin and
  waits for it. GBX files also fetch their external references, a whole
  tree level per request, before anything parses them.

Without an active provider every helper falls back to plain ``os`` calls.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from .index import GameIndex, save_unavailable
from .refs import external_references
from .server import DONE, FileResult, JobQueue, hook_block_reason

log = logging.getLogger("gbxbpy.library")

# Files no import needs; skipping them keeps reference trees small.
# Never needed for an import: audio, video and shader sources.
_SKIP_SUFFIXES = (".sound.gbx", ".ogg", ".wav", ".webm", ".mux", ".hlsl", ".phlsl.txt", ".vhlsl.txt")
IMPORT_UNUSED_SUFFIXES = _SKIP_SUFFIXES
_TREE_MODEL_SUFFIX = ".vegettreemodel.gbx"
INDEX_TIMEOUT = 600.0
TREE_DEPTH = 12


def _fold(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


class LibraryProvider:
    """Files under ``library_dir``, fetched from Trackmania when needed.

    Args:
        library_dir: The GameData folder files are read from.
        queue: The bridge server's job queue; ``None`` reads the disk only.
        fetch: Request missing or broken files from the plugin.
        request_timeout: Seconds to wait per request (plus a little per file).
        extract_dir: The GameData folder the plugin extracts into
            (``OpenplanetNext/Extract/GameData``). When it differs from
            ``library_dir``, fetched files are moved into the library.
    """

    def __init__(self, library_dir: str, queue: Optional[JobQueue] = None, *, fetch: bool = True,
                 request_timeout: float = 120.0, extract_dir: Optional[str] = None) -> None:
        self.library_dir = os.path.normpath(os.path.abspath(library_dir))
        self._prefix = os.path.normcase(self.library_dir) + os.sep
        self.extract_dir = (
            os.path.normpath(os.path.abspath(extract_dir))
            if extract_dir and _fold(extract_dir) != _fold(self.library_dir) else None
        )
        self.queue = queue
        self.fetch_enabled = fetch and queue is not None
        self.request_timeout = request_timeout
        self.index: Optional[GameIndex] = GameIndex.load(self.library_dir)
        self.status_message = ""
        self.fetched: list[str] = []
        self.failed: dict[str, str] = {}
        self._prepared = False
        self._warned_offline = False
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._verified: set[str] = set()
        self._refetched: set[str] = set()
        self._tree_done: set[str] = set()

    # -- paths --------------------------------------------------------------

    def relative(self, path: str) -> Optional[str]:
        """GameData-relative path with ``/`` separators, or ``None`` outside the library."""
        folded = _fold(path)
        if folded + os.sep == self._prefix:
            return ""
        if not folded.startswith(self._prefix):
            return None
        tail = os.path.normpath(os.path.abspath(path))[len(self._prefix):]
        return tail.replace(os.sep, "/")

    def absolute(self, rel: str) -> str:
        return os.path.join(self.library_dir, *rel.split("/"))

    # -- connection and index -----------------------------------------------

    def connected(self) -> bool:
        return self.queue is not None and self.queue.plugin_connected()

    def can_fetch(self) -> bool:
        return self.fetch_enabled and not self._cancel.is_set() and self.connected()

    def cancel(self) -> None:
        self._cancel.set()

    def prepare(self) -> None:
        """Load or refresh the game index once per import."""
        with self._lock:
            if self._prepared:
                return
            self._prepared = True
            if not self.fetch_enabled:
                return
            if not self.connected():
                if not self._warned_offline:
                    self._warned_offline = True
                    log.warning("Openplanet plugin not connected; using files already in %s", self.library_dir)
                return
            game = self.queue.game_version
            if self.index is None or (game and self.index.game_version != game):
                self.refresh_index()

    def refresh_index(self) -> bool:
        if self.queue is None or not self.connected():
            return False
        self._set_status("Listing game files in Trackmania")
        started = time.perf_counter()
        job = self.queue.submit("index")

        def cancelled() -> bool:
            if job.done:
                self._set_status(f"Listing game files in Trackmania ({job.done})")
            return self._cancel.is_set()

        finished = self.queue.wait(job, INDEX_TIMEOUT, cancelled=cancelled)
        self._set_status("")
        if not finished or not job.index_lines:
            log.warning("Game index request failed: %s", job.error or "plugin sent no files")
            return False
        index = GameIndex(job.index_lines, game_version=job.game_version)
        try:
            index.save(self.library_dir)
            if job.index_unavailable:
                save_unavailable(self.library_dir, index.game_version, job.index_unavailable)
        except OSError as exc:
            log.warning("Cannot save the game index: %s", exc)
        self.index = index
        log.info("Game index: %d files (game %s) in %.1fs", len(index), index.game_version or "?",
                 time.perf_counter() - started)
        return True

    # -- file state -----------------------------------------------------------

    def listed(self, rel: str) -> bool:
        return self.index is not None and self.index.has_file(rel)

    def state(self, rel: str) -> str:
        """``ok``, ``missing`` or ``broken`` (size differs from the game's)."""
        try:
            size = os.stat(self.absolute(rel)).st_size
        except OSError:
            return "missing"
        expected = self.index.size(rel) if self.index is not None else None
        if expected is not None and expected > 0 and size != expected:
            return "broken"
        return "ok"

    def _may_fetch(self, rel: str, *, probe: bool) -> bool:
        if rel.lower() in self.failed or not self.can_fetch():
            return False
        name = rel.rsplit("/", 1)[-1]
        if "." not in name or ":" in rel or rel.lower().endswith(_SKIP_SUFFIXES):
            return False
        return not probe or self.listed(rel)

    def fetch(self, rels: list[str], *, force: bool = False, kind: str = "extract") -> int:
        """Request ``rels`` from the plugin and wait; returns the number extracted."""
        rels = [r for r in dict.fromkeys(rels) if force or r.lower() not in self.failed]
        if not rels or not self.can_fetch():
            return 0
        label = f"Fetching {len(rels)} file{'s' if len(rels) != 1 else ''} from Trackmania"
        self._set_status(label)
        started = time.perf_counter()
        job = self.queue.submit(kind, ["GameData/" + r for r in rels])

        def cancelled() -> bool:
            if job.total:
                self._set_status(f"{label} ({job.done}/{job.total})")
            return self._cancel.is_set()

        # The plugin's heartbeat keeps the job alive; this only bounds a hang.
        self.queue.wait(job, self.request_timeout + 2.0 * len(rels), cancelled=cancelled)
        self._set_status("")
        moved_failures = self._move_into_library(job.results)
        by_path = {r.path.lower(): r for r in job.results}
        ok = 0
        for rel in rels:
            result = by_path.get(("GameData/" + rel).lower())
            moved_failure = moved_failures.get(("GameData/" + rel).lower())
            if job.state == DONE and result is not None and result.ok and moved_failure is None:
                ok += 1
                self.fetched.append(rel)
                self.failed.pop(rel.lower(), None)
            else:
                if moved_failure is not None:
                    reason = f"not moved into the library: {moved_failure}"
                elif result is not None:
                    reason = f"{result.status}: {result.detail}"
                else:
                    reason = job.error or "no result"
                self.failed[rel.lower()] = reason
                log.warning("Fetch failed: %s (%s)", rel, reason)
        log.info("Fetched %d/%d files from Trackmania in %.2fs", ok, len(rels), time.perf_counter() - started)
        return ok

    def _move_into_library(self, results) -> dict[str, str]:
        """Move written files from the extract folder into the library; ``{lower path: reason}``."""
        if self.extract_dir is None:
            return {}
        from .validate import install_extracted

        # Only complete files go into the library; a SIZE result stays in the
        # extract folder instead of replacing or adding a truncated library file.
        written = [r for r in results if r.status == "OK"]
        if not written:
            return {}
        _count, failed = install_extracted(self.library_dir, self.extract_dir, written, verify=False)
        return {path.lower(): reason for path, reason in failed.items()}

    def ensure(self, path: str, *, probe: bool = False, tree: bool = True) -> bool:
        """True when ``path`` is on disk afterwards; fetches it when missing or broken.

        ``probe`` only fetches files the game index lists, for lookups that
        try several candidate paths.
        """
        rel = self.relative(path)
        if rel is None:
            return os.path.isfile(path)
        key = rel.lower()
        with self._lock:
            if key in self._verified:
                return True
            self.prepare()
            state = self.state(rel)
            if state != "ok" and self._may_fetch(rel, probe=probe):
                self.fetch([rel])
                state = self.state(rel)
                if state != "ok":
                    self._retry_hook_if_needed(rel)
                    state = self.state(rel)
            if state == "missing":
                return False
            self._verified.add(key)
            if tree and key.endswith(".gbx"):
                self._fetch_tree(self.absolute(rel))
            return True

    def first_available(self, paths: list[str]) -> Optional[str]:
        """First of ``paths`` available after one batched request for the missing ones."""
        with self._lock:
            self.prepare()
            for path in paths:
                if self.exists(path) and self.ensure(path):
                    return path
            rels = [rel for rel in (self.relative(p) for p in paths) if rel is not None]
            # Candidates are guesses; when the index knows one, skip the rest.
            probe = any(self.listed(rel) for rel in rels)
            missing = [rel for rel in rels if self._may_fetch(rel, probe=probe)]
            if missing:
                self.fetch(missing)
            for path in paths:
                if self.ensure(path):
                    return path
            return None

    def refetch(self, path: str) -> bool:
        """Extract ``path`` again after it failed to parse; once per file and import."""
        rel = self.relative(path)
        if rel is None:
            return False
        with self._lock:
            if rel.lower() in self._refetched or not self.can_fetch():
                return False
            if self.failed.get(rel.lower(), "").lower().startswith("notfound:"):
                return False
            self._refetched.add(rel.lower())
            if rel.lower().endswith(".gbx"):
                kind = "extract_hook_tree" if rel.lower().endswith(_TREE_MODEL_SUFFIX) else "extract_hook"
                return self._hook_refetch(rel, kind=kind)
            log.info("Re-requesting %s after a parse failure", rel)
            return self.fetch([rel], force=True) > 0

    def _retry_hook_if_needed(self, rel: str) -> bool:
        failure = self.failed.get(rel.lower(), "").lower()
        if (
            not rel.lower().endswith(".gbx")
            or self.state(rel) == "ok"
            or failure.startswith("notfound:")
        ):
            return False
        return self.refetch(self.absolute(rel))

    def _hook_refetch(self, rel: str, *, kind: str = "extract_hook") -> bool:
        """One hook extraction of ``rel``; restores the previous bytes unless it succeeds."""
        reason = hook_block_reason(rel)
        if reason:
            self.failed[rel.lower()] = reason
            log.warning("Not hook-extracting %s: %s", rel, reason)
            return False
        target = self.absolute(rel)
        try:
            with open(target, "rb") as handle:
                original: Optional[bytes] = handle.read()
        except FileNotFoundError:
            original = None
        except OSError as exc:
            self.failed[rel.lower()] = f"Cannot back up before hook extraction: {exc}"
            log.warning("Cannot back up %s before hook extraction: %s", rel, exc)
            return False
        log.info("Re-requesting %s with %s after normal extraction failed", rel, kind)
        ok = False
        fetched_before = len(self.fetched)
        try:
            fetched = self.fetch([rel], force=True, kind=kind) > 0 and self.state(rel) == "ok"
            if not fetched and self.failed.get(rel.lower(), "").startswith("SIZE:"):
                from .validate import recover_hook_extract

                source = os.path.join(self.extract_dir or self.library_dir, *rel.split("/"))
                expected = self.index.size(rel) if self.index is not None else None
                if expected is None and original is not None:
                    expected = len(original)
                try:
                    # The old body may still be encrypted; only reuse its header.
                    header_end = 17 + int.from_bytes((original or b"")[13:17], "little")
                    prefix = (original or b"")[:header_end]
                    with open(source, "rb") as handle:
                        recovered = recover_hook_extract(rel, handle.read(), prefix, expected)
                    with open(source, "wb") as handle:
                        handle.write(recovered)
                    failures = self._move_into_library([
                        FileResult("OK", "GameData/" + rel, str(len(recovered))),
                    ])
                    if failures:
                        self.failed[rel.lower()] = next(iter(failures.values()))
                    else:
                        fetched = self.state(rel) == "ok"
                        if fetched:
                            self.failed.pop(rel.lower(), None)
                            self.fetched.append(rel)
                            log.info("Restored omitted hook header for %s", rel)
                except (OSError, ValueError) as exc:
                    self.failed[rel.lower()] = f"Hook header recovery failed: {exc}"
                    log.warning("Cannot restore hook header for %s: %s", rel, exc)
            if fetched:
                from .validate import check_file, check_light

                try:
                    if rel.lower().endswith(".light.gbx"):
                        with open(target, "rb") as handle:
                            check = check_light(handle.read())
                    else:
                        check = check_file(target)
                except OSError as exc:
                    self.failed[rel.lower()] = f"Hook validation could not read output: {exc}"
                else:
                    ok = check.ok
                    if not ok:
                        self.failed[rel.lower()] = f"Hook validation failed: {check.reason}"
                if not ok:
                    log.warning("Rejected hook output for %s: %s", rel, self.failed[rel.lower()])
            else:
                ok = fetched
        finally:
            if not ok:
                del self.fetched[fetched_before:]
                try:
                    if original is None:
                        if os.path.isfile(target):
                            os.remove(target)
                    else:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with open(target, "wb") as handle:
                            handle.write(original)
                except OSError as exc:
                    log.warning("Cannot restore %s after a failed hook extraction: %s", rel, exc)
        return ok

    def _fetch_tree(self, root: str) -> None:
        frontier = [root]
        for _depth in range(TREE_DEPTH):
            references: list[str] = []
            for path in frontier:
                folded = _fold(path)
                if folded in self._tree_done:
                    continue
                self._tree_done.add(folded)
                references.extend(external_references(path))
            needed: list[str] = []
            candidates: list[str] = []
            for ref in dict.fromkeys(references):
                rel = self.relative(ref)
                if rel is None or rel.lower().endswith(_SKIP_SUFFIXES):
                    continue
                candidates.append(rel)
                if rel.lower() not in self._verified and self.state(rel) != "ok" and self._may_fetch(rel, probe=False):
                    needed.append(rel)
            if needed:
                self.fetch(needed)
                for rel in needed:
                    self._retry_hook_if_needed(rel)
            frontier = []
            for rel in candidates:
                if self.state(rel) == "missing":
                    continue
                self._verified.add(rel.lower())
                if rel.lower().endswith(".gbx"):
                    frontier.append(self.absolute(rel))
            if not frontier:
                return

    # -- listings -------------------------------------------------------------

    def exists(self, path: str) -> bool:
        rel = self.relative(path)
        if rel is None:
            return os.path.isfile(path)
        return os.path.isfile(path) or self.listed(rel)

    def isdir(self, path: str) -> bool:
        if os.path.isdir(path):
            return True
        rel = self.relative(path)
        if rel is None:
            return False
        return self.index is not None and (rel == "" or self.index.has_folder(rel))

    def _entries(self, path: str) -> tuple[list[str], list[str]]:
        dirs: dict[str, str] = {}
        files: dict[str, str] = {}
        try:
            with os.scandir(path) as it:
                for entry in it:
                    target = dirs if entry.is_dir() else files
                    target[entry.name.lower()] = entry.name
        except OSError:
            pass
        rel = self.relative(path)
        if rel is not None and self.index is not None:
            index_dirs, index_files = self.index.children(rel)
            for key, name in index_dirs.items():
                dirs.setdefault(key, name)
            for key, name in index_files.items():
                files.setdefault(key, name)
        if rel == "":
            dirs.pop(".bridge", None)
        return list(dirs.values()), list(files.values())

    def listdir(self, path: str) -> list[str]:
        dirs, files = self._entries(path)
        if not dirs and not files and not self.isdir(path):
            raise FileNotFoundError(path)
        return dirs + files

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        dirs, files = self._entries(top)
        if not dirs and not files and not self.isdir(top):
            return
        yield top, dirs, files
        for name in dirs:
            yield from self.walk(os.path.join(top, name))

    def _set_status(self, message: str) -> None:
        self.status_message = message

    def summary(self) -> str:
        parts = [f"{len(self.fetched)} files fetched from Trackmania"]
        if self.failed:
            parts.append(f"{len(self.failed)} unavailable")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Active provider and os-like helpers
# ---------------------------------------------------------------------------

_active: Optional[LibraryProvider] = None


def _nice_hook(path: str) -> None:
    provider = _active
    if provider is not None:
        provider.ensure(path)


def _set_nice_hook(hook: Optional[Callable[[str], None]]) -> None:
    try:
        from NICE.src import parser
    except ImportError:
        return
    setter = getattr(parser, "set_file_hook", None)
    if setter is not None:
        setter(hook)
    parser.set_parse_failure_hook(refetch if hook is not None else None)


def activate(provider: Optional[LibraryProvider]) -> None:
    """Route the helpers below (and NICE's file reads) through ``provider``."""
    global _active
    _active = provider
    _set_nice_hook(_nice_hook if provider is not None else None)


def current() -> Optional[LibraryProvider]:
    return _active


@contextmanager
def active(provider: Optional[LibraryProvider]) -> Iterator[Optional[LibraryProvider]]:
    previous = _active
    activate(provider)
    try:
        yield provider
    finally:
        activate(previous)


def ensure(path: str) -> bool:
    """``os.path.isfile`` that fetches ``path`` (and its references) when needed."""
    provider = _active
    return provider.ensure(path) if provider is not None else os.path.isfile(path)


def isfile(path: str) -> bool:
    """``os.path.isfile`` for candidate lookups; fetches only files the game lists."""
    provider = _active
    return provider.ensure(path, probe=True) if provider is not None else os.path.isfile(path)


def exists(path: str) -> bool:
    """True when the file is on disk or in the game index; never fetches."""
    provider = _active
    return provider.exists(path) if provider is not None else os.path.isfile(path)


def isdir(path: str) -> bool:
    provider = _active
    return provider.isdir(path) if provider is not None else os.path.isdir(path)


def listdir(path: str) -> list[str]:
    provider = _active
    return provider.listdir(path) if provider is not None else os.listdir(path)


def walk(top: str) -> Iterator[tuple[str, list[str], list[str]]]:
    provider = _active
    if provider is None:
        yield from os.walk(top)
    else:
        yield from provider.walk(top)


def refetch(path: str) -> bool:
    provider = _active
    return provider.refetch(path) if provider is not None else False


def first_available(paths: list[str]) -> Optional[str]:
    """First existing path; with a provider, missing ones are requested in one batch."""
    provider = _active
    if provider is not None:
        return provider.first_available(paths)
    return next((p for p in paths if os.path.isfile(p)), None)
