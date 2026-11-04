"""Checks on the repo itself, which hold from the first commit onwards."""

import ast
import base64
import json
import pathlib
import re

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


FOLDER = ROOT / "custom_components/tuya_vacuum_local_map"


def test_icons_exist_with_the_sizes_home_assistant_expects():
    import struct

    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        data = (FOLDER / "brand" / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
        assert struct.unpack(">II", data[16:24]) == (size, size), name


def test_strings_and_the_english_translation_agree():
    strings = json.loads((FOLDER / "strings.json").read_text())
    english = json.loads((FOLDER / "translations/en.json").read_text())
    assert strings == english


def test_every_entity_and_issue_has_a_translation():
    strings = json.loads((FOLDER / "strings.json").read_text())
    for platform in ("sensor", "binary_sensor", "button", "image"):
        text = (FOLDER / f"{platform}.py").read_text()
        keys = re.findall(r'coordinator, "(\w+)"\)', text)
        assert keys, platform
        for key in keys:
            assert key in strings["entity"][platform], (platform, key)
    assert "connection_stalled" in strings["issues"]


def test_hacs_and_manifest_metadata_are_consistent():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads((FOLDER / "manifest.json").read_text())
    assert hacs["name"] == manifest["name"]
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == ["tinytuya>=1.20.0,<2"]
    assert "hacs/action" in (ROOT / ".github/workflows/ci.yml").read_text()


def test_the_readme_has_no_private_looking_values_and_links_resolve_locally():
    readme = (ROOT / "README.md").read_text()
    for target in re.findall(r"\]\((?!http|#)([^)\s]+)\)", readme):
        assert (ROOT / target).exists(), target


def test_the_hacs_job_is_skipped_while_the_repo_is_private():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    job = ci[ci.index("  hacs:") :]
    assert "if: ${{ !github.event.repository.private }}" in job
