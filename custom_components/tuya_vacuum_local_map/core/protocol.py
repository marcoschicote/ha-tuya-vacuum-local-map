"""Decode the JSON family of Tuya vacuum messages (docs/PROTOCOL.md).

Nothing here raises on device data: whatever the firmware sends, a function returns
``None`` (or skips the bad entry) instead. Frames are recognised by content, never by
DP number, because DP numbers differ by maker.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

INFO_PATH = 21011
INFO_ROOMS = 21004
INFO_STATUS = 20001
INFO_COMMAND = 30000
INFO_ROOM_SELECT = 21023

# The shortest object we would accept, `{"infoType":1}`, is 14 bytes (20 in base64).
_MIN_VALUE_LENGTH = 16

# Fields that identify the owner or the vacuum. They are dropped on decode and must
# never be logged, stored or exposed (PROTOCOL, "Recognising a frame"). `mapId` is
# kept, since parse_rooms reads it, but a Frame never shows `data` in its repr.
_PRIVATE_DATA_KEYS = frozenset({"userId", "sn"})

_ENCODED_NAME = re.compile(r"[0-9a-z]+(\+[0-9a-z]+)+")


@dataclass(frozen=True)
class Frame:
    dp: str
    info_type: int
    data: dict[str, Any] = field(repr=False)
    ts: int | None


@dataclass(frozen=True)
class PathChunk:
    path_id: int
    start: int
    total: int
    points: list[tuple[int, int]]


@dataclass(frozen=True)
class Room:
    id: int
    name: str  # already decoded


@dataclass(frozen=True)
class Status:
    clean_area: float | None  # of the current or last run; units unconfirmed


def _is_int(value: object) -> bool:
    # bool is an int subclass, and `true` is not a number we want.
    return isinstance(value, int) and not isinstance(value, bool)


def decode_dp(dp: str, value: object) -> Frame | None:
    """Decode one DP value into a Frame, or None if it isn't one of ours.

    Ordinary DPs (booleans, numbers, short strings) reach this too, so a failed
    decode is normal and is never logged.
    """
    if not isinstance(value, str) or len(value) < _MIN_VALUE_LENGTH:
        return None
    try:
        envelope = json.loads(base64.b64decode(value, validate=True))
    except (ValueError, RecursionError):  # binascii.Error and bad UTF-8 are ValueErrors
        return None
    if not isinstance(envelope, dict):
        return None
    info_type = envelope.get("infoType")
    if not _is_int(info_type):
        return None
    data = envelope.get("data", {})
    if not isinstance(data, dict):
        return None
    ts = envelope.get("ts")
    return Frame(
        dp=str(dp),
        info_type=info_type,
        data={k: v for k, v in data.items() if k not in _PRIVATE_DATA_KEYS},
        ts=ts if _is_int(ts) else None,
    )


def _point(raw: object) -> tuple[int, int] | None:
    if (
        isinstance(raw, list | tuple)
        and len(raw) == 2
        and _is_int(raw[0])
        and _is_int(raw[1])
    ):
        return raw[0], raw[1]
    return None


def parse_path(frame: Frame) -> PathChunk | None:
    """A path chunk (21011). `posArray[i]` is point number `startPos + i` of the run.

    An empty chunk (`pointCounts` 0) is valid. `pointCounts` is not trusted: the
    points actually present are what counts.
    """
    if frame.info_type != INFO_PATH:
        return None
    data = frame.data
    path_id = data.get("pathID")
    start = data.get("startPos")
    raw_points = data.get("posArray")
    if not (_is_int(path_id) and _is_int(start) and start >= 0):
        return None
    if not isinstance(raw_points, list):
        return None
    points = []
    for raw in raw_points:
        point = _point(raw)
        if point is None:
            return None
        points.append(point)
    total = data.get("totalPoints")
    if not _is_int(total) or total < 0:
        total = start + len(points)
    return PathChunk(path_id=path_id, start=start, total=total, points=points)


def parse_rooms(frame: Frame) -> tuple[int, list[Room]] | None:
    """The room list (21004) as `(map_id, rooms)`.

    Rooms come from `autoAreaValue`; `value` holds no-go areas and is not read.
    An entry without an integer `id` and a string `name` is skipped, and a list
    without an integer `mapId` is rejected, since the caller needs it to detect a
    new map. Fields beyond `id` and `name` are ignored.
    """
    if frame.info_type != INFO_ROOMS:
        return None
    map_id = frame.data.get("mapId")
    entries = frame.data.get("autoAreaValue")
    if not _is_int(map_id) or not isinstance(entries, list):
        return None
    rooms = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        room_id, name = entry.get("id"), entry.get("name")
        if _is_int(room_id) and isinstance(name, str):
            rooms.append(Room(id=room_id, name=decode_name(name)))
    return map_id, rooms


def parse_status(frame: Frame) -> Status | None:
    """The status message (20001), seen only on the Nova and only reported. Used to
    check the map against the area the vacuum says it cleaned."""
    if frame.info_type != INFO_STATUS:
        return None
    area = frame.data.get("cleanArea")
    if isinstance(area, bool) or not isinstance(area, int | float) or area < 0:
        return Status(clean_area=None)
    return Status(clean_area=area)


def parse_room_command(frame: Frame) -> list[int] | None:
    """The room IDs of a clean command (30000 wrapping 21023), as seen on the wire.

    This only reads a message the app sent. Nothing here sends one.
    """
    if frame.info_type != INFO_COMMAND:
        return None
    commands = frame.data.get("cmds")
    if not isinstance(commands, list):
        return None
    for command in commands:
        if not isinstance(command, dict) or command.get("infoType") != (
            INFO_ROOM_SELECT
        ):
            continue
        inner = command.get("data")
        segments = inner.get("segmentId") if isinstance(inner, dict) else None
        if isinstance(segments, list) and all(_is_int(s) for s in segments):
            return list(segments)
        return None
    return None


def decode_name(name: str) -> str:
    """Decode a room name, leaving plain names unchanged.

    Names set in the app are each character's code point in base 36, joined by `+`.
    A one-character name has no `+` and can't be told apart from a plain one.
    """
    if not _ENCODED_NAME.fullmatch(name):
        return name
    try:
        decoded = "".join(chr(int(part, 36)) for part in name.split("+"))
        decoded.encode("utf-8")  # a lone surrogate would break everything later
    except (ValueError, OverflowError):
        return name
    return decoded
