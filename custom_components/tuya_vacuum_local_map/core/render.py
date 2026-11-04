"""Draw the coverage grid and the latest run as an SVG.

Device y grows upwards and SVG y grows downwards. `_Frame` is the one place that
flips it: `sx = x - min_x`, `sy = max_y - y`, in device units.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .grid import LIMIT, CoverageGrid
from .protocol import Room

MAX_BYTES = 500_000  # budget for a whole-home map

COVERAGE = "#4a90d9"
TRAIL = "#e8590c"
# (lowest count, highest count, opacity): one <path> each. Translucent, so the same
# colours read on light and dark dashboards.
BANDS = ((1, 1, 0.25), (2, 2, 0.4), (3, 4, 0.55), (5, 255, 0.7))

_MAX_NAMES = 200
_MAX_NAME_LENGTH = 64
# Anything XML 1.0 can't carry (control characters, lone surrogates, U+FFFE/F).
_NOT_XML = re.compile("[^\t\n\r -퟿-�\U00010000-\U0010ffff]")

Point = tuple[int, int]


@dataclass(frozen=True)
class RenderInput:
    grid: CoverageGrid
    last_run: list[list[Point]]
    rooms: list[Room]
    hidden_rooms: frozenset[int]
    position: tuple[int, int] | None


@dataclass(frozen=True)
class _Frame:
    """Maps device coordinates to SVG ones. `left` and `top` are the device x and y
    shown at the view box's top-left corner, so a point (x, y) lands at
    (x - left, top - y)."""

    left: int
    top: int
    width: int
    height: int

    def x(self, x: int) -> int:
        return x - self.left

    def y(self, y: int) -> int:
        return self.top - y


def _in_range(point: Point) -> bool:
    return -LIMIT <= point[0] <= LIMIT and -LIMIT <= point[1] <= LIMIT


def _frame(bounds: tuple[int, int, int, int], margin: int, unit: int) -> _Frame:
    """A frame around `bounds` plus margin, with edges on multiples of `unit`."""
    min_x, min_y, max_x, max_y = bounds
    left = (min_x - margin) // unit * unit
    bottom = (min_y - margin) // unit * unit
    right = -((-(max_x + margin)) // unit) * unit
    top = -((-(max_y + margin)) // unit) * unit
    return _Frame(left=left, top=top, width=right - left, height=top - bottom)


def _clean(text: str) -> str:
    text = _NOT_XML.sub("", text).replace("\n", " ").replace("\r", " ")
    return escape(text[:_MAX_NAME_LENGTH])


def _svg(view: str, body: str) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'viewBox="{view}">{body}</svg>'
    ).encode()


def _placeholder() -> bytes:
    return _svg(
        "0 0 400 120",
        "<title>Coverage map</title>"
        '<text x="200" y="66" text-anchor="middle" font-family="sans-serif" '
        'font-size="18" fill="#888">No cleaning runs recorded yet</text>',
    )


def _band_paths(grid: CoverageGrid, frame: _Frame, factor: int) -> list[str]:
    """One path per band. Blocks of `factor` x `factor` cells take their highest
    count, and each row's neighbouring blocks merge into one rectangle."""
    unit = grid.cell * factor
    origin_x = frame.left // unit
    origin_top = frame.top // unit
    if factor == 1:
        blocks = grid.cells()
    else:
        blocks: dict[tuple[int, int], int] = {}
        for (x, y), count in grid.cells().items():
            key = (x // factor, y // factor)
            if blocks.get(key, 0) < count:
                blocks[key] = count
    rows: list[defaultdict[int, list[int]]] = [defaultdict(list) for _ in BANDS]
    for (x, y), count in blocks.items():
        for band, (low, high, _) in enumerate(BANDS):
            if low <= count <= high:
                rows[band][y].append(x)
                break
    paths = []
    for band, (_, _, opacity) in enumerate(BANDS):
        d = []
        for y in sorted(rows[band]):
            xs = sorted(rows[band][y])
            top = origin_top - (y + 1)  # the y flip, in blocks
            start = previous = xs[0]
            for x in [*xs[1:], None]:
                if x is not None and x == previous + 1:
                    previous = x
                    continue
                width = previous - start + 1
                d.append(f"M{start - origin_x} {top}h{width}v1h-{width}z")
                if x is not None:
                    start = previous = x
        if d:
            paths.append(
                f'<path fill="{COVERAGE}" fill-opacity="{opacity}" d="{"".join(d)}"/>'
            )
    return paths


def _trail(segments: list[list[Point]], frame: _Frame) -> str:
    lines = []
    for segment in segments:
        run: list[str] = []
        for point in [*segment, None]:
            if point is not None and _in_range(point):
                run.append(f"{frame.x(point[0])},{frame.y(point[1])}")
                continue
            if len(run) > 1:  # a lone point has no line to draw
                lines.append(
                    '<polyline vector-effect="non-scaling-stroke" '
                    f'points="{" ".join(run)}"/>'
                )
            run = []
    if not lines:
        return ""
    return (
        f'<g fill="none" stroke="{TRAIL}" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round">{"".join(lines)}'
        "</g>"
    )


def _description(data: RenderInput) -> str:
    names = [
        _clean(room.name) for room in data.rooms if room.id not in data.hidden_rooms
    ][:_MAX_NAMES]
    names = [name for name in names if name]
    if not names:
        return ""
    # Room positions aren't known yet (PLAN, phase 5 adds labels), so the names go in
    # the description for now. They come from the network, hence the escaping.
    return f"<desc>Rooms: {', '.join(names)}</desc>"


def _build(
    data: RenderInput, bounds: tuple[int, int, int, int], factor: int
) -> bytes | None:
    cell = data.grid.cell
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    frame = _frame(bounds, max(cell * 4, span // 20), cell * factor)
    scale = cell * factor
    body = ["<title>Coverage map</title>", _description(data)]
    paths = _band_paths(data.grid, frame, factor)
    if paths:
        body.append(f'<g transform="scale({scale})">{"".join(paths)}</g>')
    trail = _trail(data.last_run, frame)
    body.append(trail)
    drawn = bool(paths or trail)
    if data.position is not None and _in_range(data.position):
        radius = max(cell * 2, span // 80)
        body.append(
            f'<circle cx="{frame.x(data.position[0])}" '
            f'cy="{frame.y(data.position[1])}" r="{radius}" fill="{TRAIL}" '
            'stroke="#fff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>'
        )
        drawn = True
    if not drawn:
        return None
    return _svg(f"0 0 {frame.width} {frame.height}", "".join(body))


def render_svg(data: RenderInput) -> bytes:
    """The map as SVG bytes. A valid small image if there is nothing to draw."""
    points = [p for segment in data.last_run for p in segment if _in_range(p)]
    if data.position is not None and _in_range(data.position):
        points.append(data.position)
    bounds = data.grid.bounds()
    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if bounds is None:
            bounds = (min(xs), min(ys), max(xs), max(ys))
        else:
            bounds = (
                min(bounds[0], min(xs)),
                min(bounds[1], min(ys)),
                max(bounds[2], max(xs)),
                max(bounds[3], max(ys)),
            )
    if bounds is None:
        return _placeholder()
    # Over budget: draw coarser blocks until it fits.
    for factor in (1, 2, 4, 8):
        svg = _build(data, bounds, factor)
        if svg is None:
            return _placeholder()  # nothing in range, or only lone points
        if len(svg) <= MAX_BYTES:
            break
    return svg
