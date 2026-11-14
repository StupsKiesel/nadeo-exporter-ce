"""Structural editing of decoded MediaTracker blocks."""

from copy import deepcopy
import math

from .model.mediatracker import MediaTrackerBlock


def _interpolate(left, right, factor):
    if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
        return {key: _interpolate(value, right[key], factor) for key, value in left.items()}
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [_interpolate(a, b, factor) for a, b in zip(left, right)]
    if type(left) in (int, float) and type(right) in (int, float) and (
        type(left) is float or type(right) is float
    ):
        return left + (right - left) * factor
    return deepcopy(left)


def _split_keys(times, values, time):
    if not times or len(times) != len(values) or any(
        type(value) not in (int, float) or not math.isfinite(value) for value in times
    ) or any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError("Cannot cut a block with invalid or unordered keys")
    if time in times:
        boundary = deepcopy(values[times.index(time)])
    elif time < times[0]:
        boundary = deepcopy(values[0])
    elif time > times[-1]:
        boundary = deepcopy(values[-1])
    else:
        index = next(i for i in range(len(times) - 1) if times[i] < time < times[i + 1])
        boundary = _interpolate(values[index], values[index + 1],
                                (time - times[index]) / (times[index + 1] - times[index]))
    left = [(t, deepcopy(value)) for t, value in zip(times, values) if t < time]
    right = [(t, deepcopy(value)) for t, value in zip(times, values) if t > time]
    return [*left, (time, deepcopy(boundary))], [(time, boundary), *right]


def split_media_block(block: MediaTrackerBlock, time: float):
    """Cut at an interior time; new boundary values use linear float sampling.

    Discrete fields hold their preceding value. Native easing/tangent evaluation
    is not implemented; embedded recording data is never traversed.
    """
    start, end = block.timeline_start, block.timeline_end
    if type(time) not in (int, float) or not math.isfinite(time) or (
        start is None or end is None or not start < time < end
    ):
        raise ValueError("The playhead must be strictly inside the selected block")

    def split(value):
        if isinstance(value, dict):
            left, right = deepcopy(value), deepcopy(value)
            if "times" in value:
                times = value["times"]
                positions = value.get("verticesPositions")
                if positions is None:
                    raise ValueError("Cannot cut an unsupported time-array block")
                a, b = _split_keys(times, positions, time)
                for result, keys in ((left, a), (right, b)):
                    result["times"] = [t for t, _position in keys]
                    result["verticesPositions"] = [position for _t, position in keys]
                    result["numKeys"] = len(keys)
            for name, child in value.items():
                if name in {"recordData", "times", "verticesPositions", "numKeys"}:
                    continue
                if name in {"start", "end"} and type(child) in (int, float):
                    left[name], right[name] = min(child, time), max(child, time)
                else:
                    left[name], right[name] = split(child)
            return left, right
        if isinstance(value, list):
            if value and all(isinstance(key, dict) and "time" in key for key in value):
                a, b = _split_keys([key["time"] for key in value], value, time)
                return ([dict(key, time=t) for t, key in a],
                        [dict(key, time=t) for t, key in b])
            pairs = [split(child) for child in value]
            return [a for a, _b in pairs], [b for _a, b in pairs]
        return deepcopy(value), deepcopy(value)

    left, right = split(block.chunks)
    return MediaTrackerBlock(block.class_id, left), MediaTrackerBlock(block.class_id, right)
