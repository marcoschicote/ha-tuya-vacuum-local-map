"""Decide when a run is finished, and count each run in the grid exactly once.

This sits between RunAssembler (one run's points) and CoverageGrid (all runs). It
holds no clock: callers pass `now` in seconds from any steady clock.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from .grid import Cell, CoverageGrid
from .path import Point, RunAssembler
from .protocol import PathChunk

IDLE_SECONDS = 120.0
KEEP_FINISHED_IDS = 50  # run IDs remembered, so a re-sent run isn't counted twice
KEEP_RUNS = 3  # runs whose points are kept in memory, so they can still grow


@dataclass
class _Run:
    path_id: int
    assembler: RunAssembler
    activity: float
    dirty: bool = False
    cells: frozenset[Cell] = field(default_factory=frozenset)


class RunTracker:
    """Feed it chunks; it adds each finished run to the grid.

    A run ends when a chunk of another, unknown run arrives, or when no new point
    has arrived for `idle_seconds`. Counting a run is safe to repeat: if chunks of
    the same run resume later, only the cells it newly covers are counted. A chunk
    of a run we already finished, with nothing new in it, changes nothing: that's
    the app re-sending the previous run when its map screen opens. A run that
    arrives whole and wasn't recorded before is a run we missed, and is recorded.

    Every method returns the IDs of the runs counted by that call.
    """

    def __init__(
        self,
        grid: CoverageGrid,
        finished_ids: Iterable[int] = (),
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self.grid = grid
        self.idle_seconds = idle_seconds
        self._finished: deque[int] = deque(finished_ids, maxlen=KEEP_FINISHED_IDS)
        self._runs: dict[int, _Run] = {}
        self._latest: int | None = None
        self.revision = 0  # goes up whenever any run receives a new point

    @property
    def finished_ids(self) -> list[int]:
        """Run IDs counted so far, oldest first. Persist these."""
        return list(self._finished)

    @property
    def path_id(self) -> int | None:
        """The run that last received a new point."""
        return self._latest

    @property
    def point_count(self) -> int:
        """Points held for the run that last received a new point."""
        run = self._runs.get(self._latest) if self._latest is not None else None
        return len(run.assembler) if run else 0

    def run_cells(self, path_id: int) -> frozenset[Cell] | None:
        """The cells a run covers in the grid, if its points are still in memory."""
        run = self._runs.get(path_id)
        return run.cells if run else None

    def live_point(self) -> Point | None:
        """The newest point of a run that hasn't gone quiet yet, else None."""
        run = self._runs.get(self._latest) if self._latest is not None else None
        if run is None or not run.dirty:
            return None
        return run.assembler.last_point()

    @property
    def last_run(self) -> list[list[Point]]:
        """Segments of the run that last received a new point, for drawing."""
        run = self._runs.get(self._latest) if self._latest is not None else None
        return run.assembler.segments() if run else []

    def add(self, chunk: PathChunk, now: float) -> list[int]:
        done: list[int] = []
        run = self._runs.get(chunk.path_id)
        if run is None:
            if chunk.path_id in self._finished:
                return done  # a re-send of a run whose points we no longer hold
            for other in list(self._runs.values()):
                done += self._finish(other)
            while len(self._runs) >= KEEP_RUNS:
                oldest = min(self._runs.values(), key=lambda r: r.activity)
                done += self._finish(oldest)
                del self._runs[oldest.path_id]
            run = self._runs[chunk.path_id] = _Run(chunk.path_id, RunAssembler(), now)
        if run.assembler.add(chunk):
            run.dirty = True
            run.activity = now
            self._latest = chunk.path_id
            self.revision += 1
        return done

    def poll(self, now: float) -> list[int]:
        """Count runs that have gone quiet. Call about once a second."""
        done: list[int] = []
        for run in self._runs.values():
            if run.dirty and now - run.activity >= self.idle_seconds:
                done += self._finish(run)
        return done

    def flush(self) -> list[int]:
        """Count everything pending, such as on unload."""
        done: list[int] = []
        for run in self._runs.values():
            done += self._finish(run)
        return done

    def _finish(self, run: _Run) -> list[int]:
        if not run.dirty:
            return []
        run.dirty = False
        segments = run.assembler.segments()
        if not segments:
            return []
        run.cells = self.grid.add_run(segments, run.cells)
        if run.path_id not in self._finished:
            self._finished.append(run.path_id)
        return [run.path_id]
