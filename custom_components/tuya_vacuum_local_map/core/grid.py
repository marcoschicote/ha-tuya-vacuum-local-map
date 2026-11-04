"""A sparse coverage grid: how many runs cleaned each cell.

Units are the device's own. `cell` and `brush` are in those units (50 and 300 are 5 cm
and 30 cm if the units are mm, which is still a guess, see PROTOCOL).
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Iterable, Iterator

# Points beyond this are dropped, so one bad point can't make the grid enormous.
LIMIT = 100_000

FORMAT_VERSION = 1
_MAX_SAVED_BYTES = 64 * 1024 * 1024  # decompressed; a bomb guard, real data is tiny
_MAX_SAVED_CELLS = 4_000_000  # about 10 km² at the default cell size
_HEADER = struct.Struct("<II")  # cell, brush
_COUNT = struct.Struct("<I")
_RUN = struct.Struct("<iiHB")  # y, x start, length, count

Cell = tuple[int, int]
Point = tuple[int, int]


class CoverageGrid:
    def __init__(self, cell: int = 50, brush: int = 300) -> None:
        if cell < 1 or brush < 1:
            raise ValueError("cell and brush must be positive")
        self.cell = cell
        self._cells: dict[Cell, int] = {}
        self.set_brush(brush)

    def set_brush(self, brush: int) -> None:
        """Change the brush width for runs added from now on. Cells already counted
        keep what they have: the old paths are gone, so they can't be re-stamped."""
        if brush < 1:
            raise ValueError("cell and brush must be positive")
        self.brush = brush
        radius = brush / (2 * self.cell)
        reach = math.ceil(radius)
        # The disc's cell offsets, computed once.
        self._disc = tuple(
            (dx, dy)
            for dx in range(-reach, reach + 1)
            for dy in range(-reach, reach + 1)
            if dx * dx + dy * dy <= radius * radius
        ) or ((0, 0),)

    def copy(self) -> CoverageGrid:
        """An independent copy, cheap enough to hand to another thread to draw."""
        other = CoverageGrid(self.cell, self.brush)
        other._cells = dict(self._cells)
        return other

    # --- stamping ---------------------------------------------------------------

    def _centres(self, segments: Iterable[Iterable[Point]]) -> Iterator[Cell]:
        """Cells along the segments, stamped no further apart than one cell."""
        cell = self.cell
        for segment in segments:
            previous: Point | None = None
            for x, y in segment:
                if not (-LIMIT <= x <= LIMIT and -LIMIT <= y <= LIMIT):
                    previous = None  # never join across a dropped point
                    continue
                if previous is None:
                    yield x // cell, y // cell
                else:
                    px, py = previous
                    steps = max(abs(x - px), abs(y - py)) // cell + 1
                    for i in range(1, steps + 1):
                        yield (
                            (px + (x - px) * i // steps) // cell,
                            (py + (y - py) * i // steps) // cell,
                        )
                previous = (x, y)

    def stamp(self, segments: Iterable[Iterable[Point]]) -> frozenset[Cell]:
        """The cells a run would cover, without touching the grid."""
        disc = self._disc
        covered: set[Cell] = set()
        last: Cell | None = None
        for centre in self._centres(segments):
            if centre == last:
                continue
            last = centre
            cx, cy = centre
            covered.update([(cx + dx, cy + dy) for dx, dy in disc])
        return frozenset(covered)

    def add_run(
        self,
        segments: Iterable[Iterable[Point]],
        already: frozenset[Cell] = frozenset(),
    ) -> frozenset[Cell]:
        """Count one run: each cell it covers goes up by one, saturating at 255.

        Returns the cells covered. If the same run is added again after it grew, pass
        that return value as `already`: the cells in it are not counted a second time.
        """
        covered = self.stamp(segments)
        cells = self._cells
        for key in covered - already:
            cells[key] = min(cells.get(key, 0) + 1, 255)
        return covered | already

    # --- queries ----------------------------------------------------------------

    def covered_cells(self) -> int:
        return len(self._cells)

    def count(self, x: int, y: int) -> int:
        """Runs that cleaned the cell holding the device point (x, y)."""
        return self._cells.get((x // self.cell, y // self.cell), 0)

    def cells(self) -> dict[Cell, int]:
        """A copy of the cell counts, keyed by cell index (device unit // cell)."""
        return dict(self._cells)

    def bounds(self) -> tuple[int, int, int, int] | None:
        """(min_x, min_y, max_x, max_y) of the covered cells, in device units."""
        if not self._cells:
            return None
        xs = [x for x, _ in self._cells]
        ys = [y for _, y in self._cells]
        cell = self.cell
        return (
            min(xs) * cell,
            min(ys) * cell,
            (max(xs) + 1) * cell,
            (max(ys) + 1) * cell,
        )

    # --- storage ----------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """A version byte, then zlib over runs of equal counts along each row."""
        rows: dict[int, list[tuple[int, int]]] = {}
        for (x, y), count in self._cells.items():
            rows.setdefault(y, []).append((x, count))
        runs = bytearray()
        run_count = 0
        for y in sorted(rows):
            start = length = count = 0
            for x, c in sorted(rows[y]):
                if length and x == start + length and c == count and length < 65535:
                    length += 1
                    continue
                if length:
                    runs += _RUN.pack(y, start, length, count)
                    run_count += 1
                start, length, count = x, 1, c
            runs += _RUN.pack(y, start, length, count)
            run_count += 1
        body = _HEADER.pack(self.cell, self.brush) + _COUNT.pack(run_count) + runs
        return bytes([FORMAT_VERSION]) + zlib.compress(body, 9)

    @classmethod
    def from_bytes(cls, data: bytes) -> CoverageGrid:
        """Restore a grid. Raises ValueError on corrupt data (it is our own file)."""
        if not data or data[0] != FORMAT_VERSION:
            raise ValueError("unknown coverage grid version")
        try:
            decompressor = zlib.decompressobj()
            body = decompressor.decompress(data[1:], _MAX_SAVED_BYTES)
            if decompressor.unconsumed_tail:
                raise ValueError("coverage grid too large")
            if not decompressor.eof or decompressor.unused_data:
                raise ValueError("truncated coverage grid")
            cell, brush = _HEADER.unpack_from(body, 0)
            (run_count,) = _COUNT.unpack_from(body, _HEADER.size)
            offset = _HEADER.size + _COUNT.size
            if len(body) != offset + run_count * _RUN.size:
                raise ValueError("coverage grid length mismatch")
            grid = cls(cell, brush)
            total = 0
            for y, start, length, count in _RUN.iter_unpack(body[offset:]):
                if not 1 <= count <= 255 or length < 1:
                    raise ValueError("bad coverage grid run")
                total += length
                if total > _MAX_SAVED_CELLS:
                    raise ValueError("coverage grid too large")
                for x in range(start, start + length):
                    grid._cells[(x, y)] = count
        except (zlib.error, struct.error) as err:
            raise ValueError("corrupt coverage grid") from err
        return grid
