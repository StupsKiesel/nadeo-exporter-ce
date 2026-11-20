"""Candidate trim regions, not decoded authored trim definitions.

Pixel rows use Blender's bottom-up orientation; rectangles use UV coordinates.
"""

from dataclasses import dataclass
import math
from statistics import median
from typing import Sequence


@dataclass(frozen=True)
class TrimCandidate:
    bounds: tuple[float, float, float, float]
    evidence: str


def validate_bounds(bounds: Sequence[float]) -> None:
    if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        raise ValueError("Trim bounds must contain four finite UV values")
    u0, v0, u1, v1 = bounds
    if not (0 <= u0 < u1 <= 1 and 0 <= v0 < v1 <= 1):
        raise ValueError("Trim bounds must satisfy 0 <= min < max <= 1 on both axes")


def infer_texture_regions(
    pixels: Sequence[Sequence[Sequence[float]]],
) -> list[TrimCandidate]:
    """Partition at strong, spatially coherent colour boundaries.

    This detects rectangular atlas/band candidates, not their semantics.
    Local decoration/noise must not qualify as an edge spanning the sheet.
    """
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    if width < 2 or height < 2 or any(len(row) != width for row in pixels):
        raise ValueError("Texture inference needs a rectangular image of at least 2 x 2")
    if any(len(pixel) < 3 or not all(math.isfinite(v) for v in pixel[:3])
           for row in pixels for pixel in row):
        raise ValueError("Texture samples require finite RGB values")

    # Black space is common in packed game atlases. It is a heuristic, not
    # proof that dark texels are unused. Keep each disconnected silhouette.
    foreground = {(x, y) for y, row in enumerate(pixels) for x, pixel in enumerate(row)
                  if max(pixel[:3]) > .025}
    if .1 < len(foreground) / (width * height) < .9:
        components = []
        while foreground:
            start = min(foreground, key=lambda p: (p[1], p[0]))
            foreground.remove(start)
            pending, points = [start], []
            while pending:
                x, y = pending.pop()
                points.append((x, y))
                for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if neighbor in foreground:
                        foreground.remove(neighbor)
                        pending.append(neighbor)
            x0, y0 = min(p[0] for p in points), min(p[1] for p in points)
            x1, y1 = max(p[0] for p in points) + 1, max(p[1] for p in points) + 1
            if len(points) >= max(4, width * height / 1000) and x1 - x0 >= 2 and y1 - y0 >= 2:
                components.append(TrimCandidate(
                    (x0 / width, y0 / height, x1 / width, y1 / height), "TEXTURE_SILHOUETTE"))
        if len(components) > 1:
            return components

    def nested_bands() -> list[TrimCandidate]:
        # A separator may span only one parent region; global cuts would
        # incorrectly extend it through unrelated parts of the texture.
        split_count = 0
        axes = set()

        def split(x0: int, y0: int, x1: int, y1: int):
            nonlocal split_count
            choices = []
            for horizontal, start, end, other_start, other_end, full_length in (
                (True, y0, y1, x0, x1, height),
                (False, x0, x1, y0, y1, width),
            ):
                minimum = max(4, math.ceil(full_length / 16))
                if end - start < minimum * 2 or other_end - other_start < 4:
                    continue
                scores = []
                for index in range(start + 1, end):
                    differences = []
                    for offset in range(other_start, other_end):
                        a = pixels[index - 1][offset] if horizontal else pixels[offset][index - 1]
                        b = pixels[index][offset] if horizontal else pixels[offset][index]
                        differences.append(sum(abs(a[c] - b[c]) for c in range(3)) / 3)
                    scores.append(median(differences))
                midpoint = (start + end) // 2
                threshold = max(.06, median(scores) * 4)
                center = midpoint - start - 1
                left = max(scores[max(0, center - 2):center])
                right = max(scores[center + 1:min(len(scores), center + 3)])
                strength = max(scores[center], min(left, right))
                if strength >= threshold:
                    choices.append((strength / threshold, horizontal, midpoint))
            if not choices:
                return [(x0, y0, x1, y1)]
            split_count += 1
            if split_count > 31:
                return None
            _, horizontal, midpoint = max(choices)
            axes.add(horizontal)
            if horizontal:
                first = split(x0, y0, x1, midpoint)
                second = split(x0, midpoint, x1, y1) if first is not None else None
            else:
                first = split(x0, y0, midpoint, y1)
                second = split(midpoint, y0, x1, y1) if first is not None else None
            return first + second if first is not None and second is not None else None

        regions = split(0, 0, width, height)
        if regions is None or len(axes) < 2:
            return []
        return [
            TrimCandidate((x0 / width, y0 / height, x1 / width, y1 / height), "TEXTURE_BAND")
            for x0, y0, x1, y1 in regions
        ]

    nested = nested_bands()
    if nested:
        return nested

    def cuts(horizontal: bool) -> list[int]:
        length, span = (height, width) if horizontal else (width, height)
        scores = []
        for index in range(1, length):
            differences = []
            for offset in range(span):
                a = pixels[index - 1][offset] if horizontal else pixels[offset][index - 1]
                b = pixels[index][offset] if horizontal else pixels[offset][index]
                differences.append(sum(abs(a[c] - b[c]) for c in range(3)) / 3)
            # A boundary must affect at least half the row/column.
            scores.append(median(differences))
        baseline = median(scores)
        threshold = max(.06, baseline * 4)
        separation = max(2, round(length / 32))
        chosen = []
        for index in sorted(range(1, length), key=lambda i: (-scores[i - 1], i)):
            if scores[index - 1] < threshold:
                break
            if (index >= separation and length - index >= separation and
                    all(abs(index - other) >= separation for other in chosen)):
                chosen.append(index)
                if len(chosen) == 7:
                    break
        return [0, *sorted(chosen), length]

    ys, xs = cuts(True), cuts(False)
    if len(xs) == len(ys) == 2:
        return []
    return [
        TrimCandidate((x0 / width, y0 / height, x1 / width, y1 / height), "TEXTURE_BAND")
        for y0, y1 in zip(ys, ys[1:]) for x0, x1 in zip(xs, xs[1:])
    ]


def infer_uv_regions(
    faces: Sequence[Sequence[Sequence[float]]],
) -> tuple[list[TrimCandidate], int]:
    """Bound connected UV islands; reject out-of-tile islands rather than guess wrapping."""
    if any(len(face) < 3 for face in faces):
        raise ValueError("UV faces require at least three corners")
    parent = list(range(len(faces)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    edges = {}
    for index, face in enumerate(faces):
        points = []
        for uv in face:
            if len(uv) != 2 or not all(math.isfinite(v) for v in uv):
                raise ValueError("Mesh UVs require finite coordinate pairs")
            points.append(tuple(round(v, 6) for v in uv))
        for a, b in zip(points, points[1:] + points[:1]):
            edge = tuple(sorted((a, b)))
            if a == b:
                continue
            if edge in edges:
                parent[find(index)] = find(edges[edge])
            else:
                edges[edge] = index
    islands = {}
    for index, face in enumerate(faces):
        islands.setdefault(find(index), []).extend(face)
    candidates, skipped = [], 0
    for points in islands.values():
        bounds = (min(uv[0] for uv in points), min(uv[1] for uv in points),
                  max(uv[0] for uv in points), max(uv[1] for uv in points))
        try:
            validate_bounds(bounds)
        except ValueError:
            skipped += 1
            continue
        candidate = TrimCandidate(bounds, "MESH_UV")
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates, skipped
