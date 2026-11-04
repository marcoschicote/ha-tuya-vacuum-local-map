import xml.etree.ElementTree as ET

import pytest

from custom_components.tuya_vacuum_local_map.core.grid import CoverageGrid
from custom_components.tuya_vacuum_local_map.core.protocol import (
    Room,
    Status,
    decode_dp,
    parse_status,
)
from custom_components.tuya_vacuum_local_map.core.render import (
    LABEL_FILL,
    ROOM_OPACITY,
    RenderInput,
    render_svg,
)
from custom_components.tuya_vacuum_local_map.core.rooms import (
    MIN_LABEL_CELLS,
    RoomLayer,
)

from .test_protocol import b64, frame

NS = "{http://www.w3.org/2000/svg}"


def block(x0, y0, w, h):
    return {(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h)}


# --- learning ---


def test_a_single_room_run_gives_that_room_its_cells():
    layer = RoomLayer()
    assert layer.add_run([3], block(0, 0, 10, 10)) is True
    assert layer.known_rooms() == {3}
    assert layer.owner((5, 5)) == 3 and layer.owner((50, 5)) is None
    assert len(layer.owners()) == 100


def test_nothing_to_learn_from_empty_input():
    layer = RoomLayer()
    assert layer.add_run([], block(0, 0, 3, 3)) is False
    assert layer.add_run([1], []) is False
    assert not layer and layer.owners() == {}


def test_the_most_votes_win_a_cell_and_ties_go_to_the_lower_id():
    layer = RoomLayer()
    layer.add_run([2], block(0, 0, 5, 5))
    layer.add_run([1], block(4, 4, 5, 5))  # overlaps one cell, (4, 4)
    assert layer.owner((4, 4)) == 1  # a tie
    layer.add_run([2], block(0, 0, 5, 5))
    assert layer.owner((4, 4)) == 2  # now room 2 has more
    assert layer.owner((8, 8)) == 1


def test_transit_through_another_room_is_outvoted_by_its_own_runs():
    layer = RoomLayer()
    room_a = block(0, 0, 20, 20)
    transit = {(x, 10) for x in range(20, 60)} | block(60, 0, 20, 20)
    layer.add_run([9], room_a | transit)  # the run for room 9 drove through A
    layer.add_run([1], room_a)
    layer.add_run([1], room_a)
    assert layer.owner((10, 10)) == 1 and layer.owner((70, 10)) == 9


def test_a_multi_room_run_counts_when_all_but_one_room_is_known():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 10, 10))
    layer.add_run([2], block(10, 0, 10, 10))
    union = block(0, 0, 20, 10) | block(20, 0, 10, 10)
    assert layer.add_run([1, 2, 3], union) is True
    assert layer.owner((25, 5)) == 3  # only what nobody else owns
    assert layer.owner((5, 5)) == 1 and layer.owner((15, 5)) == 2


def test_a_multi_room_run_with_two_unknown_rooms_teaches_nothing():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 10, 10))
    assert layer.add_run([1, 2, 3], block(0, 0, 30, 10)) is False
    assert layer.known_rooms() == {1}
    assert RoomLayer().add_run([4, 5], block(0, 0, 5, 5)) is False


def test_a_multi_room_run_of_known_rooms_teaches_nothing():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 10, 10))
    layer.add_run([2], block(10, 0, 10, 10))
    assert layer.add_run([1, 2], block(0, 0, 20, 10)) is False


def test_cells_already_counted_for_a_run_get_no_second_vote():
    layer = RoomLayer()
    first = frozenset(block(0, 0, 5, 5))
    layer.add_run([1], first)
    layer.add_run([2], block(0, 0, 5, 5))
    layer.add_run([1], first | block(5, 0, 5, 5), already=first)  # the run grew
    assert layer.owner((2, 2)) == 1  # a tie with room 2, so lower ID: not 2 votes
    layer.add_run([2], block(0, 0, 5, 5))
    assert layer.owner((2, 2)) == 2  # proves room 1 only had one vote there
    assert layer.owner((7, 2)) == 1


def test_votes_saturate():
    layer = RoomLayer()
    for _ in range(300):
        layer.add_run([1], [(0, 0)])
    assert layer.add_run([1], [(0, 0)]) is False


# --- labels ---


def test_the_label_cell_is_the_owned_cell_nearest_the_centre():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 10, 10))
    assert layer.label_cells()[1] in {(4, 4), (4, 5), (5, 4), (5, 5)}


def test_an_l_shaped_rooms_label_stays_inside_the_room():
    layer = RoomLayer()
    l_shape = block(0, 0, 40, 5) | block(0, 5, 5, 35)
    layer.add_run([1], l_shape)
    cell = layer.label_cells()[1]
    assert cell in l_shape


def test_tiny_rooms_get_no_label():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 3, 3))
    assert 3 * 3 < MIN_LABEL_CELLS and layer.label_cells() == {}


# --- storage ---


def test_round_trip():
    layer = RoomLayer()
    layer.add_run([1], block(-30, -5, 40, 12))
    layer.add_run([2], block(10, -5, 20, 12) | {(100, 100)})
    layer.add_run([2], block(10, -5, 5, 5))
    restored = RoomLayer.from_bytes(layer.to_bytes())
    assert restored.owners() == layer.owners()
    assert restored.known_rooms() == {1, 2}
    assert restored.to_bytes() == layer.to_bytes()


def test_empty_layer_round_trips():
    assert not RoomLayer.from_bytes(RoomLayer().to_bytes())


def test_storage_is_compact():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 400, 200))
    assert len(layer.to_bytes()) < 2000


@pytest.mark.parametrize(
    "data", [b"", b"\x00", b"\x02abc", b"\x01", b"\x01nope", b"\x01" + b"z" * 40]
)
def test_corrupt_storage_raises_value_error(data):
    with pytest.raises(ValueError):
        RoomLayer.from_bytes(data)


def test_truncated_storage_raises_value_error():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 30))
    blob = layer.to_bytes()
    for bad in (blob[:-5], blob[: len(blob) // 2]):
        with pytest.raises(ValueError):
            RoomLayer.from_bytes(bad)


def test_copy_is_independent():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 5, 5))
    clone = layer.copy()
    layer.add_run([2], block(10, 10, 5, 5))
    assert clone.known_rooms() == {1}


# --- drawing it ---


def drawn(layer, rooms, hidden=(), grid=None, **extra):
    grid = grid or CoverageGrid(cell=50, brush=50)
    for cells in layer._votes.values():
        grid.add_run([[(x * 50 + 10, y * 50 + 10)] for x, y in cells])
    return render_svg(
        RenderInput(grid, [], rooms, frozenset(hidden), None, layer, **extra)
    )


def test_each_room_is_tinted_and_labelled():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 20))
    layer.add_run([2], block(30, 0, 30, 20))
    root = ET.fromstring(drawn(layer, [Room(1, "Hall"), Room(2, "Study")]))
    tints = [
        p for p in root.iter(f"{NS}path") if p.get("fill-opacity") == str(ROOM_OPACITY)
    ]
    assert len(tints) == 2 and len({p.get("fill") for p in tints}) == 2
    labels = [t.text for t in root.iter(f"{NS}text")]
    assert sorted(labels) == ["Hall", "Study"]


def test_the_labels_are_readable_on_light_and_dark():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 20))
    svg = drawn(layer, [Room(1, "Hall")]).decode()
    group = ET.fromstring(svg).find(f".//{NS}g[@paint-order]")
    assert group.get("paint-order") == "stroke"
    assert group.get("stroke") == "#ffffff" and group.get("fill") == LABEL_FILL


def test_the_first_label_sits_at_the_top_right_of_a_known_layout():
    # room 1 left, room 2 to the right and higher: its label must be right and up
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 20))
    layer.add_run([2], block(40, 40, 30, 20))
    root = ET.fromstring(drawn(layer, [Room(1, "A"), Room(2, "B")]))
    at = {
        t.text: (float(t.get("x")), float(t.get("y"))) for t in root.iter(f"{NS}text")
    }
    assert at["B"][0] > at["A"][0] and at["B"][1] < at["A"][1]


def test_hidden_rooms_get_no_tint_and_no_label():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 20))
    layer.add_run([2], block(30, 0, 30, 20))
    root = ET.fromstring(
        drawn(layer, [Room(1, "Hall"), Room(2, "Balcony")], hidden={2})
    )
    assert [t.text for t in root.iter(f"{NS}text")] == ["Hall"]
    tints = [
        p for p in root.iter(f"{NS}path") if p.get("fill-opacity") == str(ROOM_OPACITY)
    ]
    assert len(tints) == 1


def test_a_hostile_room_name_in_a_label_is_escaped():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 20))
    svg = drawn(layer, [Room(1, "<script>alert(1)</script>&")]).decode()
    assert "<script" not in svg and "&lt;script&gt;" in svg and "&amp;" in svg
    ET.fromstring(svg)


def test_a_room_without_a_name_is_tinted_but_not_labelled():
    layer = RoomLayer()
    layer.add_run([1], block(0, 0, 30, 20))
    root = ET.fromstring(drawn(layer, []))
    assert not list(root.iter(f"{NS}text"))
    assert any(
        p.get("fill-opacity") == str(ROOM_OPACITY) for p in root.iter(f"{NS}path")
    )


def test_no_layer_draws_as_before():
    grid = CoverageGrid()
    grid.add_run([[(0, 0), (500, 0)]])
    plain = render_svg(RenderInput(grid, [], [], frozenset(), None))
    empty = render_svg(RenderInput(grid, [], [], frozenset(), None, RoomLayer()))
    assert plain == empty and b"<text" not in plain


def test_a_whole_home_with_rooms_stays_in_the_budget():
    grid = CoverageGrid(cell=50, brush=50)
    layer = RoomLayer()
    for i in range(8):
        cells = {
            (x + (i % 4) * 100, y + (i // 4) * 200)
            for x in range(100)
            for y in range(200)
        }
        for x, y in cells:
            grid._cells[(x, y)] = 1 + (x * 7 + y * 13) % 6
        layer.add_run([i + 1], cells)
    svg = render_svg(
        RenderInput(
            grid, [], [Room(i + 1, f"R{i}") for i in range(8)], frozenset(), None, layer
        )
    )
    assert len(svg) < 500_000
    ET.fromstring(svg)


# --- the status message ---


def test_status_fixture_gives_the_clean_area():
    from .test_protocol import FIXTURES

    data = FIXTURES["status_nova"]["decoded"]
    result = parse_status(frame(20001, data["data"]))
    assert result == Status(clean_area=11)


@pytest.mark.parametrize("area", ["11", None, True, -1, [], {}])
def test_a_malformed_area_is_none(area):
    assert parse_status(frame(20001, {"cleanArea": area})) == Status(None)


def test_a_status_without_an_area_or_of_another_type():
    assert parse_status(frame(20001, {})) == Status(None)
    assert parse_status(frame(21011, {"cleanArea": 5})) is None
    assert decode_dp("1", b64({"infoType": 20001})) is not None
