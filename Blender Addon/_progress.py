"""Task progress bar in the central gap of the 3D Viewport Tool Header.

Imports run synchronously inside ``Operator.execute``, so Blender does not
redraw on its own until they finish. The Tool Header draws a native
``UILayout.progress`` widget when available, or a text percentage otherwise.
:class:`Progress`
forces a rate-limited window redraw on each update so the bar advances while
the import is still running.

Usage::

    with _progress.task("Importing Map") as progress:
        plan = plan_map(..., progress=progress.sub(0.0, 0.5))
        progress(0.5, "Building scene")

Nested ``task`` blocks share the outermost bar. Modal operators use :func:`begin` /
:func:`end` with ``modal=True`` and let the event loop redraw.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
import time

import bpy

from . import _file_logging

log = logging.getLogger("trackmania_blender_bridge.progress")

REDRAW_INTERVAL = 0.25

_state = {"title": "", "message": "", "fraction": 0.0, "cancel": ""}
_depth = 0
_modal = False
_log_owner = [False]
_last_redraw = 0.0
_header_class = None
_original_mode_settings = None
_shown_headers = []


class Progress:
    """Callable reporter mapping a local 0..1 range onto a slice of the bar."""

    def __init__(self, start: float = 0.0, end: float = 1.0) -> None:
        self.start = start
        self.end = end

    def __call__(self, fraction: float, message: str | None = None) -> None:
        fraction = min(max(float(fraction), 0.0), 1.0)
        update(self.start + (self.end - self.start) * fraction, message)

    def sub(self, start: float, end: float) -> "Progress":
        span = self.end - self.start
        return Progress(self.start + span * start, self.start + span * end)


def active() -> bool:
    return _depth > 0


def snapshot() -> dict:
    return dict(_state)


def update(fraction: float, message: str | None = None) -> None:
    if _depth == 0:
        return
    # Never move backwards: sub-ranges may report slightly out of order.
    _state["fraction"] = max(_state["fraction"], min(max(fraction, 0.0), 1.0))
    if message is not None:
        _state["message"] = message
    _redraw()


def begin(title: str, *, modal: bool = False, cancel_operator: str = "", source: str = "") -> bool:
    """Start showing the bar; returns True when this call owns it.

    ``modal=True`` is for imports that return to Blender's event loop between
    steps (see the V4 modal import): updates then only tag the panel for
    redraw instead of forcing a synchronous window swap. ``cancel_operator``
    (an operator idname) adds an X button after the bar. The outermost call
    also opens the per-import log file named after ``source`` (or ``title``).
    """
    global _depth, _modal
    outer = _depth == 0
    _depth += 1
    if outer:
        _log_owner[0] = _file_logging.begin(source or title)
        _modal = modal
        _state.update(title=title, message="", fraction=0.0, cancel=cancel_operator)
        _show_tool_headers()
        wm = getattr(bpy.context, "window_manager", None)
        if wm is not None:
            wm.progress_begin(0, 100)
        _redraw(force=True)
    return outer


def end(owner: bool) -> None:
    global _depth, _modal
    _depth = max(0, _depth - 1)
    if owner:
        _depth = 0
        wm = getattr(bpy.context, "window_manager", None)
        if wm is not None:
            wm.progress_end()
        _redraw(force=True)
        _modal = False
        _state["cancel"] = ""
        _file_logging.end(_log_owner[0])
        _log_owner[0] = False
        _restore_tool_headers()


@contextlib.contextmanager
def task(title: str, source: str = ""):
    """Show the progress overlay for the duration of the ``with`` block."""
    owner = begin(title, source=source)
    try:
        yield Progress()
    finally:
        end(owner)


def tracked(title: str):
    """Decorator running an operator ``execute`` inside :func:`task`."""

    def decorate(execute):
        def wrapper(self, context):
            with task(title, source=getattr(self, "filepath", "") or ""):
                update(0.02, "Reading GBX")
                result = execute(self, context)
                if "FINISHED" in result:
                    update(1.0, "Complete")
                    _redraw(force=True)
                return result

        wrapper.__name__ = execute.__name__
        wrapper.__doc__ = execute.__doc__
        return wrapper

    return decorate


def draw(layout) -> None:
    """Draw the active step, percentage and optional cancel button."""
    if _depth == 0:
        return
    label = _state["message"]
    row = layout.row(align=True)
    text = f"{label} ({_state['fraction'] * 100:.0f}%)" if label else f"{_state['fraction'] * 100:.0f}%"
    if hasattr(row, "progress"):
        row.progress(factor=_state["fraction"], type="BAR", text=text)
    else:
        row.label(text=text)
    if _state["cancel"]:
        row.operator(_state["cancel"], text="", icon="X")


def _draw_mode_settings(self, context):
    if active():
        # Blender puts its expanding spacer before the mode settings.
        # A second spacer keeps our native widget between the standard tools.
        row = self.layout.row(align=True)
        row.ui_units_x = 28
        draw(row)
        self.layout.separator_spacer()
    _original_mode_settings(self, context)


def register() -> None:
    global _header_class, _original_mode_settings
    from bl_ui.space_view3d import VIEW3D_HT_tool_header

    if _header_class is not None:
        return
    _header_class = VIEW3D_HT_tool_header
    _original_mode_settings = _header_class.draw_mode_settings
    _header_class.draw_mode_settings = _draw_mode_settings


def _show_tool_headers() -> None:
    if bpy.app.background:
        return
    wm = getattr(bpy.context, "window_manager", None)
    seen = set()
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                if space.as_pointer() in seen:
                    continue
                seen.add(space.as_pointer())
                _shown_headers.append((space, space.show_region_tool_header))
                space.show_region_tool_header = True


def _restore_tool_headers() -> None:
    for space, visible in _shown_headers:
        try:
            space.show_region_tool_header = visible
        except ReferenceError:
            log.debug("Viewport closed while import progress was visible")
    _shown_headers.clear()


def unregister() -> None:
    global _depth, _modal, _header_class
    _depth = 0
    _modal = False
    _restore_tool_headers()
    if _header_class is not None:
        if _header_class.draw_mode_settings is _draw_mode_settings:
            _header_class.draw_mode_settings = _original_mode_settings
        _header_class = None


_peek = None


def _keep_responsive() -> None:
    """Stop Windows from replacing Blender with a frozen "Not Responding" ghost.

    Windows treats a GUI thread that has not called ``PeekMessage`` for ~5 s as
    hung and shows a cached ghost image instead of the real window, so every
    panel redraw after that is invisible. Peeking with ``PM_NOREMOVE`` resets
    that timer without consuming any of Blender's queued input events.
    """
    global _peek
    if sys.platform != "win32":
        return
    if _peek is None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                wintypes.UINT, wintypes.UINT, wintypes.UINT,
            )
            msg = wintypes.MSG()
            _peek = lambda: user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0000)  # PM_NOREMOVE
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("PeekMessage unavailable: %s", exc)
            _peek = lambda: None
    _peek()


def _redraw(force: bool = False) -> None:
    global _last_redraw
    wm = getattr(bpy.context, "window_manager", None)
    if wm is not None and _depth:
        wm.progress_update(int(_state["fraction"] * 100))
    if bpy.app.background:
        return
    _keep_responsive()
    now = time.monotonic()
    if not force and now - _last_redraw < REDRAW_INTERVAL:
        return
    _last_redraw = now
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    if _modal or getattr(bpy.context, "window", None) is None:
        return
    try:
        # The only synchronous redraw available to Python. Callers may run
        # under temp_override(scene=..., view_layer=...) for a scene that is
        # not shown in the window (V4 builds the map scene that way); drawing
        # with that mismatched context crashes Blender, so pass the window's
        # own scene and view layer explicitly.
        win = bpy.context.window
        with bpy.context.temp_override(
            window=win, screen=win.screen, area=None, region=None,
            scene=win.scene, view_layer=win.view_layer,
        ), contextlib.redirect_stdout(io.StringIO()):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
    except Exception as exc:  # noqa: BLE001 - redraw is best effort
        log.debug("progress redraw failed: %s", exc)
