"""Local HTTP API between Blender and the Openplanet extractor plugin.

Blender runs this server (127.0.0.1 only). The plugin long-polls it for
work, extracts the requested game files into ``OpenplanetNext/Extract``
and posts the results back. Other tools can queue requests too.

Endpoints (JSON unless noted):

``GET  /api/status``
    Server, plugin connection and queue state.
``POST /api/requests``  ``{"paths": [...], "kind": "extract"}``
    Queue an extract request; returns ``{"id": ...}`` (201).
    Explicit diagnostics also accept ``extract_hook_tree`` (one tree) or
    ``extract_hook`` (one GBX). Hook extraction can crash the game;
    PlugResource.Gbx is rejected. Imports and library checks use ``extract``.
``GET  /api/requests/<id>``
    Request state (``queued``/``active``/``done``/``failed``) and results.
``GET  /api/plugin/next?wait=<s>&game=<version>&plugin=<version>``
    Plugin: hand out the next job ``{"id", "kind", "paths"}`` or 204.
``POST /api/plugin/jobs/<id>/progress``  (text ``done<TAB>total<TAB>message``)
    Plugin: heartbeat while a job runs; keeps the job and connection alive.
``POST /api/plugin/jobs/<id>/index``  (text ``path<TAB>size`` lines)
    Plugin: one chunk of the game file index.
``POST /api/plugin/jobs/<id>/result``  (text ``STATUS<TAB>path<TAB>detail`` lines)
    Plugin: finish a job. ``STATUS`` is ``OK``, ``SIZE``, ``FAIL`` or ``NOTFOUND``.

Pure standard library; never imports ``bpy``.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("gbxbpy.library.server")

DEFAULT_PORT = 1337
API_VERSION = 1
MAX_WAIT = 25.0
# The plugin polls continuously; silence for this long means it is gone.
PLUGIN_TIMEOUT = 30.0
MAX_BODY = 64 * 1024 * 1024

QUEUED, ACTIVE, DONE, FAILED = "queued", "active", "done", "failed"


@dataclass
class FileResult:
    status: str
    path: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"


@dataclass
class Job:
    id: str
    kind: str
    paths: list[str]
    state: str = QUEUED
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    heartbeat: float = 0.0
    done: int = 0
    total: int = 0
    message: str = ""
    results: list[FileResult] = field(default_factory=list)
    index_lines: list[tuple[str, int]] = field(default_factory=list)
    game_version: str = ""
    error: str = ""
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self, *, results: bool = True) -> dict:
        data = {
            "id": self.id, "kind": self.kind, "state": self.state,
            "paths": self.paths, "created": self.created,
            "started": self.started, "finished": self.finished, "error": self.error,
            "progress": {"done": self.done, "total": self.total, "message": self.message},
        }
        if results:
            data["results"] = [r.__dict__ for r in self.results]
        if self.kind == "index":
            data["files"] = len(self.index_lines)
        return data


def game_path(path: str) -> str:
    """``GameData/...`` path with ``/`` separators; a trailing separator is kept."""
    folder = path.rstrip().endswith(("/", "\\"))
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p and p != "."]
    if parts and parts[0].lower() == "gamedata":
        parts = parts[1:]
    result = "/".join(["GameData", *parts])
    return result + "/" if folder else result


def parse_results(text: str) -> list[FileResult]:
    out = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t", 2)
        out.append(FileResult(parts[0].strip(), parts[1] if len(parts) > 1 else "",
                              parts[2] if len(parts) > 2 else ""))
    return out


class JobQueue:
    """Thread-safe FIFO of jobs plus the plugin's connection state."""

    def __init__(self, *, keep: int = 200) -> None:
        self._cond = threading.Condition()
        self._jobs: dict[str, Job] = {}
        self._pending: list[str] = []
        self._ids = itertools.count(1)
        self._keep = keep
        self.plugin_seen = 0.0
        self.plugin_version = ""
        self.game_version = ""
        self.listeners: list[Callable[[Job], None]] = []

    # -- clients ------------------------------------------------------------

    def submit(self, kind: str, paths: list[str] = ()) -> Job:
        if kind not in ("extract", "extract_hook_tree", "extract_hook", "index"):
            raise ValueError(f"unknown job kind {kind!r}")
        unique: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path and path.strip():
                norm = game_path(path)
                if norm.lower() not in seen:
                    seen.add(norm.lower())
                    unique.append(norm)
        if kind != "index" and not unique:
            raise ValueError("no paths to request")
        if kind == "extract_hook_tree" and (
                len(unique) != 1 or not unique[0].lower().endswith(".vegettreemodel.gbx")):
            raise ValueError("hook diagnostic requires exactly one VegetTreeModel.Gbx file")
        if kind == "extract_hook" and (len(unique) != 1 or not unique[0].lower().endswith(".gbx")):
            raise ValueError("hook diagnostic requires exactly one GBX file")
        if kind == "extract_hook" and unique[0].lower().endswith("/plugresource.gbx"):
            raise ValueError("PlugResource.Gbx hook extraction crashes TM2020; use normal extraction only")
        with self._cond:
            job = Job(str(next(self._ids)), kind, unique)
            self._jobs[job.id] = job
            self._pending.append(job.id)
            self._trim()
            self._cond.notify_all()
        log.info("Queued %s job %s (%d paths)", kind, job.id, len(unique))
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._cond:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._cond:
            return list(self._jobs.values())

    def wait(self, job: Job, timeout: float, *, cancelled: Callable[[], bool] = lambda: False,
             tick: float = 0.1) -> bool:
        """Wait for ``job``; fails it when the plugin disconnects. True when finished."""
        deadline = time.monotonic() + timeout
        while not job.event.wait(tick):
            if cancelled():
                self.fail(job, "cancelled")
                return False
            if time.monotonic() > deadline:
                self.fail(job, "timed out")
                return False
            if not self.plugin_connected():
                self.fail(job, "Openplanet plugin disconnected")
                return False
        return job.state == DONE

    def fail(self, job: Job, error: str) -> None:
        with self._cond:
            if job.state in (DONE, FAILED):
                return
            if job.id in self._pending:
                self._pending.remove(job.id)
            job.state, job.error, job.finished = FAILED, error, time.time()
        job.event.set()
        self._notify(job)

    def plugin_connected(self) -> bool:
        return time.time() - self.plugin_seen < PLUGIN_TIMEOUT

    def status(self) -> dict:
        with self._cond:
            active = [j.id for j in self._jobs.values() if j.state == ACTIVE]
            return {
                "api": API_VERSION,
                "plugin": {
                    "connected": self.plugin_connected(),
                    "last_seen": self.plugin_seen,
                    "version": self.plugin_version,
                    "game_version": self.game_version,
                },
                "queued": len(self._pending),
                "active": active,
            }

    # -- plugin -------------------------------------------------------------

    def touch(self, plugin_version: str = "", game_version: str = "") -> None:
        self.plugin_seen = time.time()
        if plugin_version:
            self.plugin_version = plugin_version
        if game_version:
            self.game_version = game_version

    def next(self, wait: float, *, stopped: Callable[[], bool] = lambda: False) -> Optional[Job]:
        """Pop the next job, waiting up to ``wait`` s; None on timeout or ``stopped()``."""
        deadline = time.monotonic() + max(0.0, min(wait, MAX_WAIT))
        with self._cond:
            # The plugin asking for work while a job is still active means it
            # restarted mid-job (or dropped it); hand that job out again.
            for job in self._jobs.values():
                if job.state == ACTIVE and time.time() - job.heartbeat > PLUGIN_TIMEOUT:
                    job.state = QUEUED
                    self._pending.insert(0, job.id)
            while not self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or stopped():
                    return None
                self._cond.wait(remaining)
                self.plugin_seen = time.time()
            job = self._jobs[self._pending.pop(0)]
            job.state, job.started = ACTIVE, time.time()
            job.heartbeat = job.started
            job.game_version = self.game_version
            return job

    def requeue(self, job: Job) -> None:
        """Put a job handed out on a dying connection back at the front."""
        with self._cond:
            if job.state == ACTIVE:
                job.state = QUEUED
                self._pending.insert(0, job.id)
                self._cond.notify_all()

    def progress(self, job_id: str, done: int, total: int, message: str = "") -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.state != ACTIVE:
                return False
            job.heartbeat = time.time()
            job.done, job.total, job.message = done, total, message
            return True

    def add_index(self, job_id: str, lines: list[tuple[str, int]]) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.kind != "index":
                return False
            job.heartbeat = time.time()
            job.index_lines.extend(lines)
            job.done = len(job.index_lines)
            return True

    def complete(self, job_id: str, results: list[FileResult]) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.state in (DONE, FAILED):
                return False
            if job.id in self._pending:
                self._pending.remove(job.id)
            job.results = results
            job.state, job.finished = DONE, time.time()
        job.event.set()
        ok = sum(r.ok for r in results)
        log.info("%s job %s finished: %d ok, %d failed%s", job.kind, job.id, ok,
                 len(results) - ok, f", {len(job.index_lines)} index entries" if job.kind == "index" else "")
        self._notify(job)
        return True

    def _notify(self, job: Job) -> None:
        for listener in list(self.listeners):
            try:
                listener(job)
            except Exception:  # noqa: BLE001 - listeners must not break the server
                log.exception("Job listener failed")

    def _trim(self) -> None:
        finished = [j for j in self._jobs.values() if j.state in (DONE, FAILED)]
        for job in finished[: max(0, len(self._jobs) - self._keep)]:
            del self._jobs[job.id]


class _Handler(BaseHTTPRequestHandler):
    server_version = "TrackmaniaBlenderBridge"
    protocol_version = "HTTP/1.1"

    @property
    def queue(self) -> JobQueue:
        return self.server.queue  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:  # noqa: D401 - silence default stderr logging
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload=None, *, text: str | None = None) -> None:
        body = b""
        content_type = "application/json"
        if text is not None:
            body, content_type = text.encode("utf-8"), "text/plain; charset=utf-8"
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _route(self) -> tuple[list[str], dict[str, str]]:
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        return parts, query

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        parts, query = self._route()
        if parts == ["api", "status"]:
            status = self.queue.status()
            status.update(self.server.extra_status())  # type: ignore[attr-defined]
            return self._send(200, status)
        if len(parts) == 3 and parts[:2] == ["api", "requests"]:
            job = self.queue.get(parts[2])
            return self._send(200, job.as_dict()) if job else self._send(404, {"error": "unknown request"})
        if parts == ["api", "requests"]:
            return self._send(200, {"requests": [j.as_dict(results=False) for j in self.queue.jobs()]})
        if parts == ["api", "plugin", "next"]:
            self.queue.touch(query.get("plugin", ""), query.get("game", ""))
            try:
                wait = float(query.get("wait", "0"))
            except ValueError:
                wait = 0.0
            stopping = self.server.stopping  # type: ignore[attr-defined]
            job = None if stopping.is_set() else self.queue.next(wait, stopped=stopping.is_set)
            if stopping.is_set():
                if job is not None:
                    self.queue.requeue(job)
                # A plain 204 would tell the plugin Blender is still there.
                self.close_connection = True
                return self._send(503, {"error": "bridge server stopped"})
            self.queue.touch()
            if job is None:
                return self._send(204)
            return self._send(200, {"id": job.id, "kind": job.kind, "paths": job.paths})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        parts, _query = self._route()
        try:
            body = self._body()
        except ValueError as exc:
            return self._send(413, {"error": str(exc)})
        if parts == ["api", "requests"]:
            try:
                data = json.loads(body or b"{}")
                job = self.queue.submit(str(data.get("kind", "extract")), list(data.get("paths", [])))
            except (ValueError, TypeError, AttributeError) as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(201, {"id": job.id})
        if len(parts) == 5 and parts[:3] == ["api", "plugin", "jobs"]:
            self.queue.touch()
            text = body.decode("utf-8", errors="replace")
            if parts[4] == "index":
                from .index import GameIndex

                ok = self.queue.add_index(parts[3], GameIndex.parse_lines(text))
                return self._send(204) if ok else self._send(404, {"error": "unknown index job"})
            if parts[4] == "progress":
                fields = (text.splitlines() or [""])[0].split("\t", 2)
                try:
                    done, total = int(fields[0]), int(fields[1]) if len(fields) > 1 else 0
                except ValueError:
                    done = total = 0
                ok = self.queue.progress(parts[3], done, total, fields[2] if len(fields) > 2 else "")
                return self._send(204) if ok else self._send(404, {"error": "unknown or finished job"})
            if parts[4] == "result":
                ok = self.queue.complete(parts[3], parse_results(text))
                return self._send(204) if ok else self._send(404, {"error": "unknown or finished job"})
        self._send(404, {"error": "not found"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX needs reuse for TIME_WAIT after an import. Windows reuse allows duplicate listeners.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, queue: JobQueue, extra_status: Callable[[], dict]) -> None:
        super().__init__(address, _Handler)
        self.stopping = threading.Event()
        self.queue = queue
        self.extra_status = extra_status


class BridgeServer:
    """Owns the HTTP server thread and the job queue."""

    def __init__(self, port: int = DEFAULT_PORT, *, host: str = "127.0.0.1",
                 queue: Optional[JobQueue] = None) -> None:
        self.host = host
        self.port = port
        self.queue = queue or JobQueue()
        self.status_extras: Callable[[], dict] = dict
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _Server((self.host, self.port), self.queue, lambda: self.status_extras())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
            name="TM bridge server", daemon=True,
        )
        self._thread.start()
        log.info("Bridge server listening on %s", self.url)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.stopping.set()
        with self.queue._cond:  # wake long-polls; they answer 503
            self.queue._cond.notify_all()
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        log.info("Bridge server stopped")


_server: Optional[BridgeServer] = None
_server_lock = threading.Lock()


def get_server() -> Optional[BridgeServer]:
    return _server


def start_server(port: int = DEFAULT_PORT) -> BridgeServer:
    """Start the process-wide server, restarting it when the port changed."""
    global _server
    with _server_lock:
        if _server is not None and _server.running and _server.port == port:
            return _server
        old, _server = _server, None
        if old is not None:
            old.stop()
        server = BridgeServer(port, queue=old.queue if old is not None else None)
        server.start()  # OSError when the port is taken
        _server = server
        return server


def stop_server() -> None:
    global _server
    with _server_lock:
        if _server is not None:
            _server.stop()
        _server = None
