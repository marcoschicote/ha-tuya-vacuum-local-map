"""Write docs/sample-map.svg from a simulated run in a made-up L-shaped home.

Nothing here comes from a real vacuum or a real home. The "robot" mows lanes through
two invented rooms, goes around an invented sofa, and a second, shorter run
re-covers one room so the darker band shows. Run it from the repo root:

    python tools/make_sample_map.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.tuya_vacuum_local_map.core.grid import CoverageGrid  # noqa: E402
from custom_components.tuya_vacuum_local_map.core.protocol import (  # noqa: E402
    PathChunk,
    Room,
)
from custom_components.tuya_vacuum_local_map.core.render import (  # noqa: E402
    RenderInput,
    render_svg,
)
from custom_components.tuya_vacuum_local_map.core.runs import RunTracker  # noqa: E402

STEP = 55
LANE = 260  # a little under the 300 brush, so lanes overlap


def lanes(x0, y0, x1, y1, blocked=()):
    """Points mowing back and forth over a rectangle, skipping blocked rectangles."""
    points = []
    y, forward = y0, True
    while y <= y1:
        xs = list(range(x0, x1 + 1, STEP))
        for x in xs if forward else reversed(xs):
            if not any(
                bx0 <= x <= bx1 and by0 <= y <= by1 for bx0, by0, bx1, by1 in blocked
            ):
                points.append((x, y))
        forward = not forward
        y += LANE
    return points


SOFA = (2400, 1500, 4300, 2500)
RUN_1 = lanes(300, 300, 6700, 4200, [SOFA]) + lanes(300, 4460, 2700, 7800)
RUN_2 = lanes(300, 300, 6700, 2000, [SOFA])


def feed(tracker, path_id, points, now):
    """Send the points as the vacuum would: small overlapping chunks."""
    total = len(points)
    chunk_start = 0
    while chunk_start < total:
        start = max(0, chunk_start - 3)  # chunks overlap a little
        end = min(total, chunk_start + 5)
        tracker.add(PathChunk(path_id, start, end, points[start:end]), now)
        chunk_start = end
        now += 1
    return now


def main():
    grid = CoverageGrid()
    tracker = RunTracker(grid)
    now = feed(tracker, 1_700_000_000, RUN_1, 0.0)
    tracker.flush()
    feed(tracker, 1_700_090_000, RUN_2, now)
    tracker.flush()
    svg = render_svg(
        RenderInput(
            grid=grid,
            last_run=tracker.last_run,
            rooms=[Room(1, "Living room"), Room(2, "Study")],
            hidden_rooms=frozenset(),
            position=RUN_2[-1],
        )
    )
    out = ROOT / "docs" / "sample-map.svg"
    out.write_bytes(svg)
    print(f"{out.relative_to(ROOT)}: {len(svg)} bytes, {grid.covered_cells()} cells")


if __name__ == "__main__":
    main()
