"""Assemble path chunks into a run.

Chunks overlap and repeat, and can go missing if the connection drops, so points are
stored by their index in the run and only joined into lines where that is safe.
"""

from __future__ import annotations

from .protocol import PathChunk

# Consecutive points further apart than this (device units) are not joined. Steps in
# the one capture were 256 at most, so this leaves room, but never draws a line
# through a wall.
MAX_JUMP = 1500

# Indexes this far past the highest `totalPoints` seen are ignored as nonsense, and
# no run is allowed more than MAX_POINTS points.
INDEX_SLACK = 100
MAX_POINTS = 100_000

Point = tuple[int, int]


class RunAssembler:
    def __init__(self) -> None:
        self._path_id: int | None = None
        self._points: dict[int, Point] = {}
        self._total = 0

    @property
    def path_id(self) -> int | None:
        """The run's start time in Unix seconds, once a chunk has arrived."""
        return self._path_id

    @property
    def total(self) -> int:
        """The highest `totalPoints` seen in this run."""
        return self._total

    def __len__(self) -> int:
        return len(self._points)

    def add(self, chunk: PathChunk) -> bool:
        """Add a chunk, and return whether it brought anything new.

        A chunk with a different `path_id` starts a new run: the caller finishes the
        old one first. Chunks repeat points they sent before; the latest wins.
        Re-sent identical points are not news, which is how callers tell a re-send
        from progress.
        """
        if chunk.path_id != self._path_id:
            self._path_id = chunk.path_id
            self._points = {}
            self._total = 0
        self._total = max(self._total, chunk.total)
        limit = min(self._total + INDEX_SLACK, MAX_POINTS)
        changed = False
        for offset, point in enumerate(chunk.points):
            index = chunk.start + offset
            if index >= limit:
                break
            if self._points.get(index) != point:
                self._points[index] = point
                changed = True
        return changed

    def segments(self) -> list[list[Point]]:
        """Runs of points in order, split at missing indexes and at jumps."""
        segments: list[list[Point]] = []
        current: list[Point] = []
        previous_index = -2
        for index in sorted(self._points):
            point = self._points[index]
            if current and (
                index != previous_index + 1 or _jump(current[-1], point) > MAX_JUMP
            ):
                segments.append(current)
                current = []
            current.append(point)
            previous_index = index
        if current:
            segments.append(current)
        return segments


def _jump(a: Point, b: Point) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))
