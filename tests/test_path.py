import json
import pathlib

from custom_components.tuya_vacuum_local_map.core.grid import CoverageGrid
from custom_components.tuya_vacuum_local_map.core.path import MAX_JUMP, RunAssembler
from custom_components.tuya_vacuum_local_map.core.protocol import (
    PathChunk,
    decode_dp,
    parse_path,
)
from custom_components.tuya_vacuum_local_map.core.runs import (
    IDLE_SECONDS,
    KEEP_FINISHED_IDS,
    RunTracker,
)

FIXTURES = {
    f["name"]: f
    for f in json.loads(
        (pathlib.Path(__file__).parent / "fixtures/frames.json").read_text()
    )
}


def chunk(path_id, start, points, total=None):
    n = start + len(points) if total is None else total
    return PathChunk(path_id, start, n, list(points))


def line(n, x0=0, y=0, step=50):
    return [(x0 + i * step, y) for i in range(n)]


def fixture_chunks(prefix):
    result = []
    for name, item in FIXTURES.items():
        if name.startswith(prefix):
            parsed = parse_path(decode_dp(item["dp"], item["b64"]))
            assert parsed is not None
            result.append(parsed)
    return result


# --- RunAssembler ------------------------------------------------------------------


def test_overlapping_and_repeated_chunks_index_by_position():
    run = RunAssembler()
    pts = line(10)
    run.add(chunk(1, 0, pts[:4], total=10))
    run.add(chunk(1, 2, pts[2:7], total=10))  # overlaps
    run.add(chunk(1, 2, pts[2:7], total=10))  # repeated exactly
    run.add(chunk(1, 6, pts[6:], total=10))
    assert run.segments() == [pts]
    assert len(run) == 10 and run.path_id == 1 and run.total == 10


def test_the_live_fixture_run_assembles_without_gaps():
    chunks = fixture_chunks("path_live_")
    run = RunAssembler()
    for c in chunks:
        run.add(c)
    assert run.path_id == chunks[0].path_id
    assert len(run) == run.total == 20
    assert len(run.segments()) == 1


def test_add_reports_whether_anything_was_new():
    run = RunAssembler()
    assert run.add(chunk(1, 0, line(3))) is True
    assert run.add(chunk(1, 0, line(3))) is False  # a re-send
    assert run.add(chunk(1, 1, line(2, x0=50))) is False
    assert run.add(chunk(1, 0, [(9, 9)] + line(3)[1:])) is True  # latest wins
    assert run.segments()[0][0] == (9, 9)


def test_empty_chunks_are_fine():
    run = RunAssembler()
    assert run.add(chunk(1, 0, [], total=0)) is False
    assert run.path_id == 1 and run.segments() == []


def test_a_different_path_id_starts_a_new_run():
    run = RunAssembler()
    run.add(chunk(1, 0, line(3)))
    run.add(chunk(2, 0, line(2, y=500)))
    assert run.path_id == 2
    assert run.segments() == [line(2, y=500)]


def test_missing_indexes_split_segments():
    run = RunAssembler()
    run.add(chunk(1, 0, line(3), total=10))
    run.add(chunk(1, 5, line(3, x0=250), total=10))  # 3 and 4 never arrived
    segments = run.segments()
    assert len(segments) == 2
    assert segments[0][-1] == (100, 0) and segments[1][0] == (250, 0)


def test_a_jump_splits_segments_even_with_no_gap_in_indexes():
    run = RunAssembler()
    run.add(chunk(1, 0, [(0, 0), (50, 0), (51 + MAX_JUMP, 0), (101 + MAX_JUMP, 0)]))
    assert [len(s) for s in run.segments()] == [2, 2]


def test_a_jump_of_exactly_the_limit_still_joins():
    run = RunAssembler()
    run.add(chunk(1, 0, [(0, 0), (MAX_JUMP, 0)]))
    assert len(run.segments()) == 1


def test_a_diagonal_jump_also_splits():
    run = RunAssembler()
    run.add(chunk(1, 0, [(0, 0), (0, MAX_JUMP + 1)]))
    assert len(run.segments()) == 2


def test_points_far_beyond_total_are_ignored():
    run = RunAssembler()
    run.add(chunk(1, 0, line(3), total=3))
    run.add(chunk(1, 5_000_000, line(3), total=3))
    run.add(chunk(1, 2, line(1000), total=3))  # runs past total + slack
    assert len(run) < 200


def test_a_lying_total_cannot_make_a_run_huge():
    run = RunAssembler()
    run.add(chunk(1, 0, line(5), total=10**12))
    run.add(chunk(1, 99_990, line(50), total=10**12))
    assert len(run) <= 100_000


# --- RunTracker --------------------------------------------------------------------


def tracker(**kwargs):
    return RunTracker(CoverageGrid(), **kwargs)


def test_a_run_is_counted_once_when_it_goes_quiet():
    t = tracker()
    assert t.add(chunk(100, 0, line(20)), now=0) == []
    assert t.poll(IDLE_SECONDS - 1) == []
    assert t.grid.covered_cells() == 0
    assert t.poll(IDLE_SECONDS) == [100]
    assert t.poll(IDLE_SECONDS * 5) == []
    cells = t.grid.cells()
    assert cells and set(cells.values()) == {1}
    assert t.finished_ids == [100]


def test_a_new_run_finishes_the_previous_one():
    t = tracker()
    t.add(chunk(100, 0, line(20)), now=0)
    assert t.add(chunk(200, 0, line(5, y=900)), now=1) == [100]
    assert t.finished_ids == [100]
    assert t.flush() == [200]
    assert t.finished_ids == [100, 200]


def test_a_resent_run_is_not_counted_twice():
    t = tracker()
    t.add(chunk(100, 0, line(20)), now=0)
    t.flush()
    before = t.grid.cells()
    # the app's map screen re-sends the previous run, whole, later on
    assert t.add(chunk(100, 0, line(20)), now=5000) == []
    assert t.poll(10_000) == [] and t.flush() == []
    assert t.grid.cells() == before


def test_a_resent_run_after_a_restart_is_not_counted_twice():
    first = tracker()
    first.add(chunk(100, 0, line(20)), now=0)
    first.flush()
    grid = CoverageGrid.from_bytes(first.grid.to_bytes())
    second = RunTracker(grid, finished_ids=first.finished_ids)
    second.add(chunk(100, 0, line(20)), now=0)
    assert second.flush() == []
    assert grid.cells() == first.grid.cells()


def test_a_whole_run_we_never_saw_is_recorded_as_a_run_of_its_own():
    t = tracker()
    t.add(chunk(100, 0, line(20)), now=0)
    t.flush()
    t.add(chunk(50, 0, line(20, y=2000)), now=10)  # unknown ID, sent whole
    assert t.flush() == [50]
    assert t.finished_ids == [100, 50]
    assert t.grid.count(0, 2000) == 1


def test_the_resent_fixture_run_is_recorded_only_if_unknown():
    resent = fixture_chunks("path_resend")[0]
    unknown = tracker()
    unknown.add(resent, now=0)
    assert unknown.flush() == [resent.path_id]
    known = tracker(finished_ids=[resent.path_id])
    known.add(resent, now=0)
    assert known.flush() == [] and known.grid.covered_cells() == 0


def test_chunks_resuming_after_an_idle_finish_reopen_the_run_without_double_counting():
    t = tracker()
    t.add(chunk(100, 0, line(20), total=40), now=0)
    assert t.poll(IDLE_SECONDS) == [100]
    cells_before = t.grid.cells()
    t.add(chunk(100, 20, line(20, x0=1000), total=40), now=500)
    assert t.poll(500 + IDLE_SECONDS) == [100]
    after = t.grid.cells()
    assert set(after) > set(cells_before)
    assert set(after.values()) == {1}  # nothing was counted twice
    assert t.finished_ids == [100]


def test_a_run_interrupted_by_a_resent_old_run_still_counts_once():
    t = tracker()
    t.add(chunk(100, 0, line(10), total=30), now=0)
    t.add(chunk(7, 0, line(10, y=3000)), now=1)  # the app re-sends an unknown run
    t.add(chunk(100, 10, line(10, x0=500), total=30), now=2)
    t.flush()
    assert set(t.grid.cells().values()) == {1}
    assert t.grid.count(0, 0) == 1 and t.grid.count(0, 3000) == 1
    assert set(t.finished_ids) == {100, 7}


def test_idle_time_counts_from_the_last_new_point_not_from_a_resend():
    t = tracker()
    t.add(chunk(100, 0, line(5)), now=0)
    t.add(chunk(100, 0, line(5)), now=100)  # nothing new
    assert t.poll(IDLE_SECONDS) == [100]


def test_an_empty_run_is_never_recorded():
    t = tracker()
    t.add(chunk(100, 0, [], total=0), now=0)
    assert t.flush() == [] and t.finished_ids == []
    t.add(chunk(100, 0, line(3)), now=1)
    assert t.flush() == [100]


def test_last_run_follows_the_latest_new_points():
    t = tracker()
    assert t.last_run == [] and t.path_id is None
    t.add(chunk(100, 0, line(3)), now=0)
    assert t.last_run == [line(3)] and t.path_id == 100
    t.add(chunk(200, 0, line(2, y=900)), now=1)
    assert t.last_run == [line(2, y=900)] and t.path_id == 200
    t.add(chunk(100, 0, line(3)), now=2)  # a re-send isn't news
    assert t.path_id == 200


def test_only_the_last_fifty_run_ids_are_remembered():
    t = tracker()
    for i in range(KEEP_FINISHED_IDS + 5):
        t.add(chunk(1000 + i, 0, line(3, y=i * 700)), now=i)
        t.flush()
    ids = t.finished_ids
    assert len(ids) == KEEP_FINISHED_IDS and ids[-1] == 1000 + KEEP_FINISHED_IDS + 4


def test_many_runs_open_at_once_do_not_pile_up():
    t = tracker()
    for i in range(10):
        t.add(chunk(i + 1, 0, line(3, y=i * 700)), now=i)
    assert len(t._runs) <= 3
    t.flush()
    assert sorted(t.finished_ids) == list(range(1, 11))


def test_revision_and_point_count_track_new_points_only():
    t = tracker()
    assert t.revision == 0 and t.point_count == 0
    t.add(chunk(100, 0, line(5)), now=0)
    assert (t.revision, t.point_count) == (1, 5)
    t.add(chunk(100, 0, line(5)), now=1)  # a re-send
    assert t.revision == 1
    t.add(chunk(100, 3, line(5, x0=150)), now=2)
    assert (t.revision, t.point_count) == (2, 8)
    t.add(chunk(100, 0, [], total=0), now=3)  # an empty chunk
    assert t.revision == 2
