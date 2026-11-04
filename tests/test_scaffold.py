"""Checks on the repo itself, which hold from the first commit onwards."""

import ast
import base64
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests/fixtures/frames.json").read_text())
CORE = ROOT / "custom_components/tuya_vacuum_local_map/core"


def test_fixture_base64_matches_decoded():
    frames = [frame for frame in FIXTURES if "b64" in frame]
    assert frames
    for frame in frames:
        assert json.loads(base64.b64decode(frame["b64"])) == frame["decoded"]


def test_path_chunk_fixture_indexes_add_up():
    data = next(f for f in FIXTURES if f["name"] == "path_chunk_nova")["decoded"][
        "data"
    ]
    assert data["startPos"] + data["pointCounts"] == data["totalPoints"]
    assert len(data["posArray"]) == data["pointCounts"]


def test_path_chunks_are_consistent():
    chunks = [f["decoded"] for f in FIXTURES if f["decoded"]["infoType"] == 21011]
    assert len(chunks) > 5
    for chunk in chunks:
        data = chunk["data"]
        assert len(data["posArray"]) == data["pointCounts"]
        assert data["startPos"] + data["pointCounts"] <= data["totalPoints"]


def test_live_run_chunks_agree_where_they_overlap():
    live = [
        f["decoded"]["data"] for f in FIXTURES if f["name"].startswith("path_live_")
    ]
    points = {}
    for data in live:
        for offset, point in enumerate(data["posArray"]):
            assert points.setdefault(data["startPos"] + offset, point) == point
    assert sorted(points) == list(range(len(points)))


def test_room_names_decode_to_unicode_code_points():
    rooms = next(f for f in FIXTURES if f["name"] == "room_list")
    names = [r["name"] for r in rooms["decoded"]["data"]["autoAreaValue"]]
    decoded = [
        "".join(chr(int(p, 36)) for p in n.split("+")) for n in names if "+" in n
    ]
    assert "Montaña" in decoded and "Sótano" in decoded


def test_core_does_not_import_home_assistant():
    for path in CORE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("homeassistant"), f"{path}: {name}"


def test_manifest_domain_matches_folder():
    folder = ROOT / "custom_components/tuya_vacuum_local_map"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["domain"] == folder.name
