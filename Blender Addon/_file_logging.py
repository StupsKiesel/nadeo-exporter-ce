"""Per-import text logs embedded in the blend file, plus console formatting."""

from __future__ import annotations

import copy
import datetime
import logging
import ntpath
import queue
import re
import threading

import bpy
from bpy.app.handlers import persistent

LOGGER_NAMES = (
    "trackmania_blender_bridge", "gbxbpy", "gbxbpy_blender",
    "gbxmat", "nice", "tm2020.nice",
)
_HANDLER_TAG = "_tmbridge_text_handler"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
error = ""
active_text = ""
_enabled = False
_level = logging.INFO
_depth = 0
_handler = None


class TextHandler(logging.Handler):
    """Workers queue strings; only Blender's main thread accesses Text data."""

    def __init__(self, text):
        super().__init__()
        self.text = text
        self.messages = queue.SimpleQueue()

    def emit(self, record):
        self.messages.put(self.format(record) + "\n")

    def flush(self):
        global error
        if threading.current_thread() is not threading.main_thread():
            return
        lines = []
        while True:
            try:
                lines.append(self.messages.get_nowait())
            except queue.Empty:
                break
        if lines:
            try:
                self.text.write("".join(lines))
            except (ReferenceError, RuntimeError) as exc:
                error = f"Embedded log unavailable: {exc}"
                logging.getLogger(LOGGER_NAMES[0]).error("%s", error)


def _flush_timer():
    if _handler is None:
        return None
    _handler.flush()
    return 0.25


@persistent
def _before_save(_unused):
    if _handler is not None:
        _handler.flush()


@persistent
def _before_load(_unused):
    close()


class RepeatFilter(logging.Filter):
    """Pass the first occurrence of each NICE parser message per import."""

    PREFIXES = ("nice", "tm2020.nice")

    def __init__(self) -> None:
        super().__init__()
        self.seen: set[tuple[str, int, str]] = set()
        self.suppressed = 0
        self._generation = object()
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.seen.clear()
            self.suppressed = 0
            self._generation = object()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or not any(
            record.name == prefix or record.name.startswith(prefix + ".")
            for prefix in self.PREFIXES
        ):
            return True
        with self._lock:
            # Console and file handlers see the same record, not two warnings.
            cached = record.__dict__.get("_tmbridge_repeat_decision")
            if cached is not None and cached[0] is self._generation:
                return cached[1]
            key = (record.name, record.levelno, record.getMessage())
            allowed = key not in self.seen
            if allowed:
                self.seen.add(key)
            else:
                self.suppressed += 1
            record.__dict__["_tmbridge_repeat_decision"] = (self._generation, allowed)
            return allowed


repeat_filter = RepeatFilter()


_DISPLAY_NAMES = (
    ("trackmania_blender_bridge", "Nadeo Exporter CE"),
    ("gbxbpy_blender", "GBXBPY.Blender"),
    ("gbxbpy", "GBXBPY"),
    ("gbxmat", "GBXMAT"),
    ("tm2020.nice", "NICE"),
    ("nice", "NICE"),
)
_CONSOLE_PATH_LIMIT = 80
_ABSOLUTE_FILE = re.compile(
    r"""(?<!\w)(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])"""
    r"""[^"\r\n<>|]*?[\\/][^\\/"\r\n<>|]*?\.[A-Za-z0-9]+(?=$|[\s"',;:)])"""
)


def _shorten_path(path: str) -> str:
    if len(path) <= _CONSOLE_PATH_LIMIT:
        return path
    parent, filename = ntpath.split(path.rstrip("\\/"))
    directory_name = ntpath.basename(parent)
    separator = "\\" if "\\" in path else "/"
    tail = separator.join(part for part in (directory_name, filename) if part)
    shortened = "..." + separator + tail
    return shortened if len(shortened) < len(path) else path


def _shorten_argument(value):
    if isinstance(value, str) and ntpath.isabs(value) and not re.search(
        r"""[\r\n"'<>|;:]""", ntpath.splitdrive(value)[1],
    ):
        return _shorten_path(value)
    return value


class PlainFormatter(logging.Formatter):
    """Consistent display labels without modifying shared logging records."""

    def format(self, record: logging.LogRecord) -> str:
        display_record = copy.copy(record)
        for root, label in _DISPLAY_NAMES:
            if record.name == root or record.name.startswith(root + "."):
                display_record.name = label + record.name[len(root):]
                break
        text = re.sub(r"\x1b\[[0-9;]*m", "", super().format(display_record))
        if self.usesTime():
            timestamp = self.formatTime(display_record, self.datefmt)
            text = text.replace("\n", "\n" + timestamp + " ")
        return text


class ConsoleFormatter(PlainFormatter):
    """Keep long paths readable; embedded logs retain the full originals."""

    def format(self, record: logging.LogRecord) -> str:
        console_record = copy.copy(record)
        if isinstance(record.args, dict):
            console_record.args = {key: _shorten_argument(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            console_record.args = tuple(_shorten_argument(value) for value in record.args)
        text = super().format(console_record)
        return _ABSOLUTE_FILE.sub(lambda match: _shorten_path(match.group()), text)


def close() -> None:
    """Detach only our handlers, leaving host/application handlers alone."""
    global _depth, _handler
    if repeat_filter.suppressed:
        logging.getLogger(LOGGER_NAMES[0]).info(
            "%d repeated parser messages suppressed", repeat_filter.suppressed,
        )
        repeat_filter.suppressed = 0
    owned: set[logging.Handler] = set()
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_TAG, False):
                logger.removeHandler(handler)
                owned.add(handler)
    for handler in owned:
        handler.flush()
        handler.close()
    _handler = None
    if bpy.app.timers.is_registered(_flush_timer):
        bpy.app.timers.unregister(_flush_timer)
    _depth = 0


def configure(*, enabled: bool, level: int) -> None:
    """Enable one internal Text datablock per import; never write disk logs."""
    global error, _enabled, _level
    close()
    error = ""
    _enabled = enabled
    _level = level
    if not enabled:
        return
    for handlers, callback in (
        (bpy.app.handlers.save_pre, _before_save),
        (bpy.app.handlers.load_pre, _before_load),
    ):
        if callback not in handlers:
            handlers.append(callback)
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False


def log_name(source: str, now: datetime.datetime | None = None) -> str:
    """Name for an internal import log; Blender resolves name collisions."""
    now = now or datetime.datetime.now()
    name = ntpath.basename(source.rstrip("\\/")) or "import"
    if name.lower().endswith(".gbx"):
        name = name[:-4]
    name = _INVALID.sub("_", name).strip(" .") or "import"
    return f"{now:%Y-%m-%d %H-%M-%S} {name}.log"


def begin(source: str) -> bool:
    """Create an internal log for an import; nested imports share its handler."""
    global _depth, active_text, error, _handler
    _depth += 1
    if _depth == 1:
        repeat_filter.reset()
    if _depth > 1 or not _enabled:
        return False
    try:
        text = bpy.data.texts.new(log_name(source))
        text.use_fake_user = True
        handler = TextHandler(text)
    except RuntimeError as exc:
        error = str(exc)
        logging.getLogger(LOGGER_NAMES[0]).error("Cannot create embedded import log: %s", exc)
        return False
    setattr(handler, _HANDLER_TAG, True)
    handler.setLevel(_level)
    handler.addFilter(repeat_filter)
    handler.setFormatter(PlainFormatter(
        "%(asctime)s [%(name)s %(threadName)s] %(levelname)s: %(message)s",
    ))
    for name in LOGGER_NAMES:
        logging.getLogger(name).addHandler(handler)
    _handler = handler
    active_text = text.name
    bpy.app.timers.register(_flush_timer, first_interval=0.25)
    logging.getLogger(LOGGER_NAMES[0]).info("Import log for %s", source)
    return True


def end(owner: bool) -> None:
    global _depth
    _depth = max(0, _depth - 1)
    if owner:
        close()


def unregister():
    close()
    for handlers, callback in (
        (bpy.app.handlers.save_pre, _before_save),
        (bpy.app.handlers.load_pre, _before_load),
    ):
        if callback in handlers:
            handlers.remove(callback)
