import re
import xml.etree.ElementTree as ET

from custom_components.tuya_vacuum_local_map.core.grid import CoverageGrid
from custom_components.tuya_vacuum_local_map.core.protocol import Room
from custom_components.tuya_vacuum_local_map.core.render import (
    BANDS,
    MAX_BYTES,
    RenderInput,
    _frame,
    render_svg,
)

NS = "{http://www.w3.org/2000/svg}"


def make(grid=None, last_run=(), rooms=(), hidden=(), position=None):
    return RenderInput(
        grid=grid or CoverageGrid(),
        last_run=[list(s) for s in last_run],
        rooms=list(rooms),
        hidden_rooms=frozenset(hidden),
        position=position,
    )


def parse(svg: bytes) -> ET.Element:
    return ET.fromstring(svg)


def serpentine(width=8000, height=6000, step=50, lane=250):
    points, y, forward = [], 0, True
    while y <= height:
        xs = range(0, width + 1, step)
        points += [(x, y) for x in (xs if forward else reversed(xs))]
        forward = not forward
        y += lane
    return points


def view_box(root):
    return [int(v) for v in root.get("viewBox").split()]


def test_nothing_to_draw_gives_a_small_valid_svg_saying_so():
    svg = render_svg(make())
    root = parse(svg)
    assert root.tag == NS + "svg"
    assert "No cleaning runs recorded yet" in "".join(root.itertext())
    assert len(svg) < 1000


def test_one_absurd_last_run_point_is_ignored():
    svg = render_svg(make(last_run=[[(0, 0), (10**15, 0)]], position=(10**12, 5)))
    assert "No cleaning runs" in svg.decode()


def test_the_y_axis_is_flipped_so_top_right_stays_top_right():
    grid = CoverageGrid()
    grid.add_run([[(0, 0), (2000, 0), (2000, 1000)]])
    top_right, bottom_left = (2000, 1000), (0, 0)
    run = [[bottom_left, top_right]]
    a = parse(render_svg(make(grid, last_run=run, position=top_right)))
    b = parse(render_svg(make(grid, last_run=run, position=bottom_left)))
    dot_tr = a.find(f".//{NS}circle")
    dot_bl = b.find(f".//{NS}circle")
    assert float(dot_tr.get("cx")) > float(dot_bl.get("cx"))
    assert float(dot_tr.get("cy")) < float(dot_bl.get("cy"))  # SVG y runs down
    first, last = (
        tuple(map(int, p.split(",")))
        for p in (a.find(f".//{NS}polyline").get("points").split()[i] for i in (0, -1))
    )
    assert first[0] < last[0] and first[1] > last[1]
    # and the point lands inside the view box
    _, _, width, height = view_box(a)
    for x, y in (first, last):
        assert 0 < x < width and 0 < y < height


def test_frame_maps_device_points_to_svg_exactly():
    frame = _frame((0, 0, 1000, 500), margin=100, unit=50)
    assert (frame.width, frame.height) == (1200, 700)
    assert (frame.x(-100), frame.y(600)) == (0, 0)  # top-left corner of the view
    assert (frame.x(1100), frame.y(-100)) == (1200, 700)  # bottom-right
    assert (frame.x(1000), frame.y(500)) == (1100, 100)  # the grid's top-right


def test_the_frame_stays_aligned_to_whole_cells_for_odd_bounds():
    frame = _frame((-1234, -777, 4321, 999), margin=213, unit=50)
    assert frame.left % 50 == 0 and frame.top % 50 == 0
    assert frame.width % 50 == 0 and frame.height % 50 == 0
    assert frame.left <= -1234 - 213 and frame.top >= 999 + 213


def test_cells_are_drawn_as_one_path_per_band():
    grid = CoverageGrid()
    run = [serpentine(3000, 1500)]
    for _ in range(5):
        grid.add_run(run)
    grid.add_run([[(6000, 0), (7000, 0)]])  # only one run here
    root = parse(render_svg(make(grid)))
    paths = root.findall(f".//{NS}path")
    assert len(paths) == 2  # counts of 5 and of 1
    assert {p.get("fill-opacity") for p in paths} == {"0.25", "0.7"}


def test_cells_land_where_the_grid_says():
    grid = CoverageGrid(cell=50, brush=50)
    grid.add_run([[(0, 0)]])  # the cell [0, 50) x [0, 50)
    grid.add_run([[(500, 400)]])
    svg = render_svg(make(grid))
    root = parse(svg)
    scale = int(re.search(r"scale\((\d+)\)", svg.decode()).group(1))
    assert scale == 50
    d = root.find(f".//{NS}path").get("d")
    corners = [tuple(map(int, m)) for m in re.findall(r"M(-?\d+) (-?\d+)", d)]
    (x1, y1), (x2, y2) = sorted(corners, key=lambda c: c[1], reverse=True)
    assert x2 - x1 == 10 and y1 - y2 == 8  # (500, 400): 10 cells right, 8 up


def test_each_row_merges_into_runs():
    grid = CoverageGrid(cell=50, brush=50)
    grid.add_run([[(0, 0), (950, 0)]])  # one row of 20 cells
    d = parse(render_svg(make(grid))).find(f".//{NS}path").get("d")
    assert d.count("M") == 1 and "h20v1h-20z" in d


def test_a_hostile_room_name_comes_out_escaped():
    grid = CoverageGrid()
    grid.add_run([[(0, 0), (500, 0)]])
    rooms = [Room(1, "<script>alert(1)</script>"), Room(2, 'a & "b" </desc><x/>')]
    svg = render_svg(make(grid, rooms=rooms))
    text = svg.decode()
    assert "<script" not in text and "<x/>" not in text
    assert "&lt;script&gt;" in text and "&amp;" in text
    root = parse(svg)  # and it still parses
    desc = root.find(f"{NS}desc").text
    assert "<script>alert(1)</script>" in desc and 'a & "b" </desc><x/>' in desc


def test_names_with_characters_xml_cannot_carry_still_give_a_valid_svg():
    grid = CoverageGrid()
    grid.add_run([[(0, 0)]])
    rooms = [Room(1, "a\x00b\x1fc\x0bd￾\n e"), Room(2, "ñ日本語😀")]
    root = parse(render_svg(make(grid, rooms=rooms)))
    desc = root.find(f"{NS}desc").text
    assert "abcd  e" in desc and "ñ日本語😀" in desc


def test_hidden_rooms_are_left_out_and_long_lists_are_capped():
    grid = CoverageGrid()
    grid.add_run([[(0, 0)]])
    rooms = [Room(1, "Hall"), Room(2, "Balcony")]
    root = parse(render_svg(make(grid, rooms=rooms, hidden={2})))
    assert "Hall" in root.find(f"{NS}desc").text
    assert "Balcony" not in root.find(f"{NS}desc").text
    many = [Room(i, "n" * 500) for i in range(1000)]
    assert len(render_svg(make(grid, rooms=many))) < 20_000


def test_no_desc_when_there_are_no_visible_rooms():
    grid = CoverageGrid()
    grid.add_run([[(0, 0)]])
    root = parse(render_svg(make(grid, rooms=[Room(1, "x")], hidden={1})))
    assert root.find(f"{NS}desc") is None


def test_the_last_run_is_a_polyline_per_segment_and_lone_points_are_skipped():
    segments = [[(0, 0), (100, 0), (200, 100)], [(5000, 0)], [(0, 500), (100, 500)]]
    root = parse(render_svg(make(last_run=segments)))
    assert len(root.findall(f".//{NS}polyline")) == 2


def test_a_dropped_point_splits_the_polyline():
    root = parse(
        render_svg(make(last_run=[[(0, 0), (50, 0), (10**12, 0), (100, 0), (150, 0)]]))
    )
    assert len(root.findall(f".//{NS}polyline")) == 2


def test_trail_and_position_alone_are_enough_to_draw():
    root = parse(render_svg(make(last_run=[[(0, 0), (500, 500)]], position=(500, 500))))
    assert root.find(f".//{NS}circle") is not None
    assert root.find(f".//{NS}path") is None


def test_output_parses_and_has_a_usable_view_box():
    grid = CoverageGrid()
    grid.add_run([serpentine()])
    root = parse(render_svg(make(grid, last_run=[serpentine()])))
    x, y, w, h = view_box(root)
    assert (x, y) == (0, 0) and w > 8000 and h > 6000


def test_a_simulated_3000_point_run_stays_within_the_size_budget():
    path = serpentine()[:3000]
    grid = CoverageGrid()
    grid.add_run([path])
    svg = render_svg(make(grid, last_run=[path], position=path[-1]))
    print(f"3000-point map: {len(svg)} bytes")
    assert len(svg) < MAX_BYTES
    parse(svg)


def test_a_dense_whole_home_map_is_squeezed_under_the_budget():
    # The worst case: a 20 x 20 m home whose counts vary cell to cell, so rows
    # can't merge into long runs.
    grid = CoverageGrid(cell=50, brush=50)
    for y in range(400):
        for x in range(400):
            grid._cells[(x, y)] = 1 + ((x * 7 + y * 13) % 6)
    assert grid.covered_cells() == 160_000
    svg = render_svg(make(grid, last_run=[serpentine(19000, 19000)]))
    print(f"dense whole-home map: {len(svg)} bytes")
    assert len(svg) < MAX_BYTES
    parse(svg)


def test_a_realistic_whole_home_map_is_small():
    grid = CoverageGrid()
    grid.add_run([serpentine(19000, 19000, lane=280)])
    grid.add_run([serpentine(19000, 9000, lane=280)])
    assert grid.covered_cells() > 100_000
    svg = render_svg(make(grid))
    print(f"realistic whole-home map: {len(svg)} bytes")
    assert len(svg) < 100_000


def test_bands_cover_every_possible_count_once():
    for count in range(1, 256):
        assert sum(low <= count <= high for low, high, _ in BANDS) == 1
