"""Which room each cell belongs to, learned from where the vacuum cleaned.

The room list gives names and IDs but no shapes (PROTOCOL, "21004: room list"), so the
only source is the clean command: a run started for room 7 alone covered room 7. Its
cells get a vote for room 7. A run for several rooms only tells us the union, so it
counts only when all but one of those rooms are already known, and then only the
cells nobody else owns go to the remaining one. A cell belongs to the room with the
most votes (ties go to the lower ID). Rooms the vacuum never cleaned alone stay
unknown. This is a heuristic, not seen on a real vacuum yet: the vacuum also drives
through other rooms on its way, which is what the votes are there to outweigh.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable

from .grid import Cell

FORMAT_VERSION = 1
MIN_LABEL_CELLS = 20  # smaller rooms are too small to be a room: no label
_MAX_SAVED_BYTES = 64 * 1024 * 1024
_MAX_SAVED_CELLS = 4_000_000
_HEADER = struct.Struct("<I")  # room count
_ROOM = struct.Struct("<iI")  # room id, run count
_RUN = struct.Struct("<iiHB")  # y, x start, length, votes


class RoomLayer:
    def __init__(self) -> None:
        self._votes: dict[int, dict[Cell, int]] = {}

    # --- learning -----------------------------------------------------------------

    def add_run(
        self,
        rooms: Iterable[int],
        cells: Iterable[Cell],
        already: frozenset[Cell] = frozenset(),
    ) -> bool:
        """Learn from one finished run that was asked to clean `rooms`.

        Returns whether anything changed. If the same run is added again after it
        grew, pass the cells it was added with as `already`: they get no second vote.
        """
        wanted = sorted(set(rooms))
        cells = frozenset(cells) - already
        if not wanted or not cells:
            return False
        if len(wanted) == 1:
            return self._vote(wanted[0], cells)
        unknown = [room for room in wanted if room not in self._votes]
        if len(unknown) != 1:
            return False
        owners = self.owners()
        free = {cell for cell in cells if owners.get(cell) not in wanted}
        return self._vote(unknown[0], free)

    def _vote(self, room: int, cells: Iterable[Cell]) -> bool:
        votes = self._votes.setdefault(room, {})
        changed = False
        for cell in cells:
            count = votes.get(cell, 0)
            if count < 255:
                votes[cell] = count + 1
                changed = True
        if not votes:
            del self._votes[room]
        return changed

    # --- queries ------------------------------------------------------------------

    def known_rooms(self) -> set[int]:
        return set(self._votes)

    def owner(self, cell: Cell) -> int | None:
        best = None
        best_votes = 0
        for room in sorted(self._votes):
            count = self._votes[room].get(cell, 0)
            if count > best_votes:
                best, best_votes = room, count
        return best

    def owners(self) -> dict[Cell, int]:
        """Every owned cell and its room. Costs a pass over all votes: call rarely."""
        best: dict[Cell, tuple[int, int]] = {}
        for room in sorted(self._votes):
            for cell, count in self._votes[room].items():
                if count > best.get(cell, (0, 0))[0]:
                    best[cell] = (count, room)
        return {cell: room for cell, (_, room) in best.items()}

    def label_cells(self, owners: dict[Cell, int] | None = None) -> dict[int, Cell]:
        """For each big enough room, the owned cell nearest the centre of its cells,
        so the label sits inside even for an L-shaped room."""
        owners = self.owners() if owners is None else owners
        by_room: dict[int, list[Cell]] = {}
        for cell, room in owners.items():
            by_room.setdefault(room, []).append(cell)
        result = {}
        for room, cells in by_room.items():
            if len(cells) < MIN_LABEL_CELLS:
                continue
            cx = sum(x for x, _ in cells) / len(cells)
            cy = sum(y for _, y in cells) / len(cells)
            result[room] = min(
                cells, key=lambda c: ((c[0] - cx) ** 2 + (c[1] - cy) ** 2, c)
            )
        return result

    def __bool__(self) -> bool:
        return bool(self._votes)

    def copy(self) -> RoomLayer:
        other = RoomLayer()
        other._votes = {room: dict(cells) for room, cells in self._votes.items()}
        return other

    # --- storage ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        body = bytearray(_HEADER.pack(len(self._votes)))
        for room in sorted(self._votes):
            rows: dict[int, list[tuple[int, int]]] = {}
            for (x, y), count in self._votes[room].items():
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
            body += _ROOM.pack(room, run_count) + runs
        return bytes([FORMAT_VERSION]) + zlib.compress(bytes(body), 9)

    @classmethod
    def from_bytes(cls, data: bytes) -> RoomLayer:
        """Raises ValueError on corrupt data (it is our own file)."""
        if not data or data[0] != FORMAT_VERSION:
            raise ValueError("unknown room layer version")
        try:
            decompressor = zlib.decompressobj()
            body = decompressor.decompress(data[1:], _MAX_SAVED_BYTES)
            if decompressor.unconsumed_tail or not decompressor.eof:
                raise ValueError("bad room layer")
            if decompressor.unused_data:
                raise ValueError("bad room layer")
            (rooms,) = _HEADER.unpack_from(body, 0)
            offset = _HEADER.size
            layer = cls()
            total = 0
            for _ in range(rooms):
                room, run_count = _ROOM.unpack_from(body, offset)
                offset += _ROOM.size
                end = offset + run_count * _RUN.size
                if end > len(body):
                    raise ValueError("room layer length mismatch")
                votes = layer._votes.setdefault(room, {})
                for y, start, length, count in _RUN.iter_unpack(body[offset:end]):
                    if not 1 <= count <= 255 or length < 1:
                        raise ValueError("bad room layer run")
                    total += length
                    if total > _MAX_SAVED_CELLS:
                        raise ValueError("room layer too large")
                    for x in range(start, start + length):
                        votes[(x, y)] = count
                offset = end
            if offset != len(body):
                raise ValueError("room layer length mismatch")
        except (zlib.error, struct.error) as err:
            raise ValueError("corrupt room layer") from err
        return layer
