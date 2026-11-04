import random
import time
import tracemalloc

import pytest

from custom_components.tuya_vacuum_local_map.core.grid import LIMIT, CoverageGrid


def serpentine(width=8000, height=6000, step=50, lane=250):
    points, y, forward = [], 0, True
    while y <= height:
        xs = range(0, width + 1, step)
        points += [(x, y) for x in (xs if forward else reversed(xs))]
        forward = not forward
        y += lane
    return points


def test_empty_grid():
    grid = CoverageGrid()
    assert grid.bounds() is None and grid.covered_cells() == 0
    assert grid.add_run([]) == frozenset()
    assert grid.add_run([[]]) == frozenset()


def test_a_single_point_stamps_a_disc_of_the_brush_diameter():
    grid = CoverageGrid(cell=50, brush=300)
    grid.add_run([[(0, 0)]])
    min_x, min_y, max_x, max_y = grid.bounds()
    # a 6-cell diameter at 50 units, centred on the cell holding the point
    assert (max_x - min_x, max_y - min_y) == (350, 350)
    assert 25 <= grid.covered_cells() <= 37
    assert grid.count(0, 0) == 1 and grid.count(1000, 1000) == 0


def test_a_line_is_stamped_without_gaps():
    grid = CoverageGrid(cell=50, brush=100)
    grid.add_run([[(0, 0), (2000, 0)]])  # two points far apart: still solid
    for x in range(0, 2000, 10):
        assert grid.count(x, 0) == 1, x


def test_a_diagonal_line_is_stamped_without_gaps():
    grid = CoverageGrid(cell=50, brush=100)
    grid.add_run([[(-1000, 1000), (1000, -1000)]])
    for i in range(0, 2000, 10):
        assert grid.count(-1000 + i, 1000 - i) == 1


def test_negative_coordinates_land_in_the_right_cells():
    grid = CoverageGrid(cell=50, brush=50)
    grid.add_run([[(-1, -1)]])
    assert grid.count(-1, -1) == 1 and grid.count(0, 0) == 0
    assert grid.bounds() == (-50, -50, 0, 0)


def test_cells_count_runs_and_overlap_within_a_run_counts_once():
    grid = CoverageGrid()
    path = serpentine(2000, 1000)
    grid.add_run([path])
    assert set(grid.cells().values()) == {1}  # lanes overlap, still 1
    grid.add_run([path])
    assert set(grid.cells().values()) == {2}


def test_counts_saturate_at_255():
    grid = CoverageGrid()
    for _ in range(300):
        grid.add_run([[(0, 0)]])
    assert set(grid.cells().values()) == {255}


def test_add_run_returns_the_cells_and_skips_those_already_counted():
    grid = CoverageGrid()
    first = grid.add_run([[(0, 0), (500, 0)]])
    again = grid.add_run([[(0, 0), (1000, 0)]], already=first)
    assert first < again
    assert set(grid.cells().values()) == {1}


def test_stamp_does_not_touch_the_grid():
    grid = CoverageGrid()
    assert grid.stamp([[(0, 0), (500, 0)]])
    assert grid.covered_cells() == 0


def test_round_trip_keeps_everything():
    grid = CoverageGrid(cell=40, brush=240)
    grid.add_run([serpentine(3000, 2000)])
    grid.add_run([[(-5000, -300), (-4000, 800)]])
    grid.add_run([serpentine(1000, 500)])
    restored = CoverageGrid.from_bytes(grid.to_bytes())
    assert restored.cell == 40 and restored.brush == 240
    assert restored.cells() == grid.cells()
    assert restored.bounds() == grid.bounds()
    assert restored.to_bytes() == grid.to_bytes()


def test_empty_grid_round_trips():
    restored = CoverageGrid.from_bytes(CoverageGrid().to_bytes())
    assert restored.covered_cells() == 0


def test_round_trip_of_a_long_row_of_equal_cells():
    grid = CoverageGrid(cell=1, brush=1)
    grid.add_run([[(0, 0), (70_000, 0)]])  # more than one 16-bit run length
    assert CoverageGrid.from_bytes(grid.to_bytes()).cells() == grid.cells()


def test_storage_starts_with_a_version_byte_and_is_compact():
    grid = CoverageGrid()
    grid.add_run([serpentine(20_000, 8_000)])  # about 20 x 8 m
    data = grid.to_bytes()
    assert data[0] == 1
    assert grid.covered_cells() > 50_000
    assert len(data) < 20_000


def test_noisy_grid_still_stores_smaller_than_one_byte_per_nine_cells():
    rng = random.Random(1)
    grid = CoverageGrid()
    for _ in range(5):
        grid.add_run(
            [[(rng.randint(-9000, 9000), rng.randint(-9000, 9000)) for _ in range(300)]]
        )
    assert len(grid.to_bytes()) < grid.covered_cells() * 3


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00", b"\x02abc", b"\x01", b"\x01not zlib", b"\x01" + b"x" * 100],
)
def test_corrupt_storage_raises_value_error(data):
    with pytest.raises(ValueError):
        CoverageGrid.from_bytes(data)


def test_truncated_or_tampered_storage_raises_value_error():
    data = CoverageGrid()
    data.add_run([serpentine(1000, 1000)])
    blob = data.to_bytes()
    for bad in (blob[:-5], blob[: len(blob) // 2], blob[:1] + blob[2:]):
        with pytest.raises(ValueError):
            CoverageGrid.from_bytes(bad)


def test_one_absurd_point_does_not_blow_up_memory():
    grid = CoverageGrid()
    tracemalloc.start()
    grid.add_run(
        [[(0, 0), (10**15, 10**15), (50, 0)], [(-(10**9), 5)], [(0, 0), (60, 0)]]
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 5_000_000
    assert grid.covered_cells() < 200
    assert grid.bounds()[2] < 1000  # nothing far away


def test_points_just_inside_the_limit_are_kept_and_just_outside_dropped():
    grid = CoverageGrid()
    grid.add_run([[(LIMIT, LIMIT)], [(LIMIT + 1, 0)], [(0, -LIMIT - 1)]])
    assert grid.count(LIMIT, LIMIT) == 1
    assert grid.count(LIMIT + 1, 0) == 0 and grid.count(0, -LIMIT - 1) == 0


def test_a_dropped_point_never_joins_its_neighbours():
    grid = CoverageGrid(cell=50, brush=50)
    grid.add_run([[(0, 0), (10**9, 0), (2000, 0)]])
    assert grid.count(0, 0) == 1 and grid.count(2000, 0) == 1
    assert grid.count(1000, 0) == 0  # no line across the gap


def test_bad_parameters_are_rejected():
    for cell, brush in [(0, 300), (50, 0), (-1, 5)]:
        with pytest.raises(ValueError):
            CoverageGrid(cell, brush)


def test_stamping_a_3000_point_run_is_fast():
    path = serpentine(8000, 6000)[:3000]
    assert len(path) == 3000
    grid = CoverageGrid()
    start = time.perf_counter()
    grid.add_run([path])
    elapsed = time.perf_counter() - start
    print(f"3000-point run stamped in {elapsed * 1000:.1f} ms")
    assert elapsed < 1.0  # the budget for a Raspberry Pi class CPU
    assert grid.covered_cells() > 3000
