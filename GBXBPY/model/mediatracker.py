"""Plain domain models for editable MediaTracker clips."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _time_range(value: Any) -> tuple[float | None, float | None]:
    times: list[float] = []
    bounds: dict[str, float] = {}

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "time" and isinstance(child, (int, float)):
                    times.append(float(child))
                elif key == "times" and isinstance(child, (list, tuple)):
                    times.extend(float(time) for time in child if type(time) in (int, float))
                elif key in ("start", "end") and isinstance(child, (int, float)):
                    bounds[key] = float(child)
                elif key != "recordData":
                    # Entity recordings have their own millisecond time base.
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    if times:
        return min(times), max(times)
    return bounds.get("start"), bounds.get("end")


@dataclass
class MediaTrackerBlock:
    """One timed MediaTracker block; chunk values are GBXBPY plain data."""

    class_id: int
    chunks: dict[int, Any] = field(default_factory=dict)

    @property
    def timeline_start(self) -> float | None:
        return _time_range(self.chunks)[0]

    @property
    def timeline_end(self) -> float | None:
        return _time_range(self.chunks)[1]


@dataclass
class MediaTrackerTrack:
    """A MediaTracker track and its ordered blocks."""

    name: str
    blocks: list[MediaTrackerBlock] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] | None = None
    chunks: dict[int, Any] = field(default_factory=dict)


@dataclass
class MediaTrackerClip:
    """A standalone MediaTracker clip and its ordered tracks."""

    name: str
    tracks: list[MediaTrackerTrack] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    chunks: dict[int, Any] = field(default_factory=dict)


@dataclass
class MediaTrackerClipDocument:
    """Editable clip data plus the original bytes used for preserving export."""

    clip: MediaTrackerClip
    source_path: str
    source_bytes: bytes = field(repr=False)
    source_label: str = ""
    editable: bool = True
    warnings: tuple[str, ...] = ()


__all__ = [
    "MediaTrackerBlock",
    "MediaTrackerTrack",
    "MediaTrackerClip",
    "MediaTrackerClipDocument",
]
