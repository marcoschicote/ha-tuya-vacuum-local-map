import base64
import json
import pathlib
from collections import Counter

import pytest

from custom_components.tuya_vacuum_local_map.core.protocol import (
    Frame,
    PathChunk,
    Room,
    decode_dp,
    decode_name,
    parse_path,
    parse_room_command,
    parse_rooms,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = {
    f["name"]: f for f in json.loads((ROOT / "tests/fixtures/frames.json").read_text())
}
CAPTURE = ROOT / "captures/messages.jsonl"


def b64(obj: object) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def frame(info_type: int, data: object = None, **extra: object) -> Frame:
    envelope = {"infoType": info_type, "data": {} if data is None else data, **extra}
    result = decode_dp("1", b64(envelope))
    assert result is not None
    return result


def fixture_frame(name: str) -> Frame:
    item = FIXTURES[name]
    result = decode_dp(item["dp"], item["b64"])
    assert result is not None
    return result


# --- decode_dp ---------------------------------------------------------------------


def test_every_base64_fixture_decodes_to_its_decoded_form():
    for name, item in FIXTURES.items():
        if "b64" not in item:
            continue
        result = decode_dp(item["dp"], item["b64"])
        assert result is not None, name
        assert result.dp == item["dp"]
        assert result.info_type == item["decoded"]["infoType"]
        assert result.ts == item["decoded"].get("ts")


def test_frame_drops_private_fields_and_hides_data_in_repr():
    result = fixture_frame("room_list")
    assert "userId" not in result.data and "sn" not in result.data
    assert "example-user-id" not in repr(result)
    assert "mapId" not in repr(result)


def test_extra_and_missing_envelope_fields_are_fine():
    only_type = decode_dp("5", b64({"infoType": 21011}))
    assert only_type == Frame(dp="5", info_type=21011, data={}, ts=None)
    assert decode_dp("5", b64({"infoType": 7, "other": [1], "ts": "x"})).ts is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        5,
        1.5,
        b"eyJpbmZvVHlwZSI6IDF9",
        ["x"],
        {"infoType": 1},
        "",
        "short",
        "not base64 at all, definitely not base64!",
        "eyJpbmZvVHlwZSI6IDF9" + "!",
        b64("a string"),
        b64([{"infoType": 21011}]),
        b64(None),
        b64(12345),
        b64({"data": {}}),
        b64({"infoType": "21011"}),
        b64({"infoType": 21011.0}),
        b64({"infoType": True}),
        b64({"infoType": None}),
        b64({"infoType": 21011, "data": []}),
        b64({"infoType": 21011, "data": "x"}),
        base64.b64encode(b"this is not json, just a long string").decode(),
        base64.b64encode(b"\xff\xfe\x00 not utf8 \xc3\x28 padding").decode(),
        base64.b64encode(b"[" * 100_000).decode(),
    ],
)
def test_garbage_dp_values_return_none(value):
    assert decode_dp("1", value) is None


def test_plain_dp_values_seen_on_the_rowenta_are_not_frames():
    for value in ["fullcharge", "selectroom", "chargring", "backcharge", "request"]:
        assert decode_dp("105", value) is None


# --- parse_path --------------------------------------------------------------------


def test_nova_path_chunk():
    chunk = parse_path(fixture_frame("path_chunk_nova"))
    assert chunk is not None
    assert (chunk.path_id, chunk.start, chunk.total) == (1708023426, 99, 107)
    assert len(chunk.points) == 8
    assert chunk.points[0] == (-1911, -1311) and chunk.points[-1] == (-2223, -1291)


def test_every_path_fixture_parses_and_indexes_agree():
    names = [n for n in FIXTURES if n.startswith("path_")]
    assert len(names) > 10
    for name in names:
        chunk = parse_path(fixture_frame(name))
        assert chunk is not None, name
        decoded = FIXTURES[name]["decoded"]["data"]
        assert len(chunk.points) == decoded["pointCounts"]
        assert chunk.start + len(chunk.points) <= chunk.total


def test_empty_chunk_is_valid():
    chunk = parse_path(fixture_frame("path_live_0"))
    assert chunk is not None
    assert chunk.points == [] and chunk.total == 0


def test_resent_run_is_one_chunk_from_zero():
    chunk = parse_path(fixture_frame("path_resend_previous_run"))
    assert chunk is not None
    assert chunk.start == 0 and len(chunk.points) == chunk.total == 12


def test_missing_total_falls_back_to_what_was_received():
    chunk = parse_path(
        frame(21011, {"pathID": 1, "startPos": 3, "posArray": [[1, 2], [3, 4]]})
    )
    assert chunk == PathChunk(path_id=1, start=3, total=5, points=[(1, 2), (3, 4)])


def test_path_chunk_ignores_a_wrong_pointcounts():
    data = {"pathID": 1, "startPos": 0, "posArray": [[1, 2]], "pointCounts": 9}
    chunk = parse_path(frame(21011, data))
    assert chunk is not None and chunk.points == [(1, 2)]


GOOD_PATH = {"pathID": 5, "startPos": 0, "totalPoints": 2, "posArray": [[1, 2], [3, 4]]}


@pytest.mark.parametrize(
    "change",
    [
        {"pathID": None},
        {"pathID": "5"},
        {"pathID": 5.5},
        {"pathID": True},
        {"startPos": -1},
        {"startPos": "0"},
        {"startPos": None},
        {"posArray": None},
        {"posArray": "[[1,2]]"},
        {"posArray": {"0": [1, 2]}},
        {"posArray": [[1, 2], [3]]},
        {"posArray": [[1, 2], [3, 4, 5]]},
        {"posArray": [[1, 2], "34"]},
        {"posArray": [[1, 2], [3.5, 4]]},
        {"posArray": [[1, 2], ["3", 4]]},
        {"posArray": [[1, 2], [None, 4]]},
        {"posArray": [[1, 2], [True, 4]]},
        {"posArray": [[1, 2], None]},
    ],
)
def test_malformed_path_returns_none(change):
    assert parse_path(frame(21011, GOOD_PATH | change)) is None


def test_path_missing_required_fields_returns_none():
    for key in ("pathID", "startPos", "posArray"):
        data = {k: v for k, v in GOOD_PATH.items() if k != key}
        assert parse_path(frame(21011, data)) is None


def test_path_parser_rejects_other_types():
    assert parse_path(frame(21004, GOOD_PATH)) is None
    assert parse_path(fixture_frame("room_list")) is None


# --- parse_rooms -------------------------------------------------------------------


def test_room_list_fixture():
    result = parse_rooms(fixture_frame("room_list"))
    assert result is not None
    map_id, rooms = result
    assert map_id == 1000000001
    assert [r.id for r in rooms] == [1, 2, 3, 4, 5, 6]
    names = {r.id: r.name for r in rooms}
    assert names[5] == "Room5"  # plain names stay as they are
    assert "Montaña" in names.values() and "Sótano" in names.values()


def test_no_go_areas_are_not_rooms():
    _, rooms = parse_rooms(fixture_frame("room_list"))
    assert "forbid1" not in [r.name for r in rooms]
    assert "room0" not in [r.name for r in rooms]  # decoded name of the no-go tag


def test_rooms_without_optional_fields():
    data = {"mapId": 7, "autoAreaValue": [{"id": 4, "name": "x"}]}
    assert parse_rooms(frame(21004, data)) == (7, [Room(4, "x")])
    assert parse_rooms(frame(21004, {"mapId": 7, "autoAreaValue": []})) == (7, [])


def test_bad_room_entries_are_skipped_but_good_ones_kept():
    data = {
        "mapId": 7,
        "autoAreaValue": [
            "x",
            None,
            {"id": "1", "name": "a"},
            {"id": True, "name": "a"},
            {"id": 2, "name": 3},
            {"id": 3},
            {"name": "n"},
            {"id": 9, "name": "ok"},
        ],
    }
    assert parse_rooms(frame(21004, data)) == (7, [Room(9, "ok")])


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"mapId": 7},
        {"autoAreaValue": []},
        {"mapId": "7", "autoAreaValue": []},
        {"mapId": 7.0, "autoAreaValue": []},
        {"mapId": None, "autoAreaValue": []},
        {"mapId": 7, "autoAreaValue": None},
        {"mapId": 7, "autoAreaValue": {"id": 1, "name": "a"}},
        {"mapId": 7, "autoAreaValue": "rooms"},
    ],
)
def test_malformed_room_list_returns_none(data):
    assert parse_rooms(frame(21004, data)) is None


def test_room_parser_rejects_other_types():
    assert parse_rooms(fixture_frame("path_live_1")) is None


# --- parse_room_command ------------------------------------------------------------


def test_one_and_two_room_commands():
    assert parse_room_command(fixture_frame("room_command_one_room")) == [3]
    assert parse_room_command(fixture_frame("room_command_two_rooms")) == [2, 5]


def test_room_command_finds_the_right_entry_wherever_it_is():
    cmds = [
        {"infoType": 21005, "data": {"mode": "reAppointClean"}},
        "junk",
        {"infoType": 21023, "data": {"segmentId": [4]}},
    ]
    assert parse_room_command(frame(30000, {"cmds": cmds})) == [4]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"cmds": None},
        {"cmds": {}},
        {"cmds": []},
        {"cmds": ["x"]},
        {"cmds": [{"infoType": 21005, "data": {"mode": "x"}}]},
        {"cmds": [{"infoType": 21023}]},
        {"cmds": [{"infoType": 21023, "data": []}]},
        {"cmds": [{"infoType": 21023, "data": {}}]},
        {"cmds": [{"infoType": 21023, "data": {"segmentId": 3}}]},
        {"cmds": [{"infoType": 21023, "data": {"segmentId": ["3"]}}]},
        {"cmds": [{"infoType": 21023, "data": {"segmentId": [3, None]}}]},
        {"cmds": [{"infoType": 21023, "data": {"segmentId": [True]}}]},
    ],
)
def test_malformed_room_command_returns_none(data):
    assert parse_room_command(frame(30000, data)) is None


def test_room_command_rejects_other_types():
    assert parse_room_command(fixture_frame("room_list")) is None


# --- decode_name -------------------------------------------------------------------


def to_base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def encode_name(text: str) -> str:
    return "+".join(to_base36(ord(c)) for c in text)


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [
        ("2a+33+33+31+1f", "Room3"),
        ("36+33+33+31+1c", "room0"),
        ("2b+6r+38+2p+32+33", "Sótano"),
        ("6p", "6p"),  # one character has no "+": can't tell it from a plain name
        ("Room1", "Room1"),
        ("forbid1", "forbid1"),
        ("", ""),
        ("Salón 2", "Salón 2"),
        ("1+2+", "1+2+"),
        ("+1+2", "+1+2"),
        ("A+B", "A+B"),  # uppercase is not part of the encoding
    ],
)
def test_decode_name(encoded, decoded):
    assert decode_name(encoded) == decoded


def test_names_round_trip_through_the_encoding():
    for text in ["Room3", "room0", "Montaña", "Sótano", "<script>", "日本"]:
        assert decode_name(encode_name(text)) == text


@pytest.mark.parametrize(
    "encoded",
    [
        "zzzzzzzzzz+1",  # past U+10FFFF
        "1+" + "z" * 5000,  # absurdly large
        f"{to_base36(0xD800)}+{to_base36(97)}",  # a lone surrogate
    ],
)
def test_undecodable_names_stay_as_they_are(encoded):
    assert decode_name(encoded) == encoded


# --- the owner's real capture (git-ignored, absent in CI) ----------------------------


@pytest.mark.skipif(not CAPTURE.exists(), reason="captures/messages.jsonl not present")
def test_real_capture_decodes_consistently():
    """Asserts on counts only, and prints nothing from the file."""
    counts: Counter[int] = Counter()
    runs: dict[int, dict[int, tuple[int, int]]] = {}
    for line in CAPTURE.read_text().splitlines():
        try:
            record = json.loads(line)
            dps = record["data"]["dps"]
        except (ValueError, KeyError, TypeError):
            continue
        for dp, value in dps.items():
            result = decode_dp(str(dp), value)
            if result is None:
                continue
            counts[result.info_type] += 1
            chunk = parse_path(result)
            if chunk is not None:
                run = runs.setdefault(chunk.path_id, {})
                assert chunk.start + len(chunk.points) <= chunk.total
                for i, point in enumerate(chunk.points):
                    # chunks overlap, and re-sent points must be identical
                    assert run.setdefault(chunk.start + i, point) == point
            parse_rooms(result)
            parse_room_command(result)
    assert counts[21011] > 0 and counts[21004] > 0 and counts[30000] > 0
    assert runs
