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
