"""A fake Tuya vacuum: a TCP server that speaks protocol 3.3 on localhost.

It uses tinytuya's own framing and cipher, so the real `tinytuya.Device` (the one the
integration uses) can connect to it. Everything it serves is made up. It records every
command it receives, so a test can show that the integration only ever asks for the
status and sends heartbeats.

Run it by hand to develop against, with made-up credentials:

    python -m tests.fake_vacuum --port 6668

then point the integration at host 127.0.0.1, device ID `example-device-id`, local key
`0123456789abcdef`, protocol 3.3.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import struct
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

from tinytuya.core import header as H
from tinytuya.core.command_types import DP_QUERY, DP_QUERY_NEW, HEART_BEAT, STATUS
from tinytuya.core.crypto_helper import AESCipher
from tinytuya.core.message_helper import TuyaMessage, pack_message, unpack_message

KEY = "0123456789abcdef"  # made up
WRONG_KEY = "f" * 16  # a different made-up key, for tests of a wrong one
DEVICE_ID = "example-device-id"  # made up
READ_ONLY_COMMANDS = {HEART_BEAT, DP_QUERY, DP_QUERY_NEW}
_HEADER = struct.Struct(">4I")


# --- what the vacuum says ---------------------------------------------------------


def envelope(info_type: int, data: dict[str, Any], **extra: Any) -> str:
    """A DP value as the vacuum sends it: base64 of a JSON envelope."""
    body = {"data": data, "infoType": info_type, "message": "ok", **extra}
    return base64.b64encode(json.dumps(body).encode()).decode()


def chunk_dps(
    path_id: int, start: int, points: list[tuple[int, int]], total: int | None = None
) -> dict[str, str]:
    data = {
        "pathID": path_id,
        "pointCounts": len(points),
        "posArray": [list(p) for p in points],
        "startPos": start,
        "totalPoints": start + len(points) if total is None else total,
    }
    return {"123": envelope(21011, data, ts=path_id)}


def rooms_dps(
    map_id: int = 1000000001,
    rooms: Iterable[tuple[int, str]] = ((1, "Hall"), (2, "Study")),
) -> dict[str, str]:
    entries = [{"id": i, "name": n, "mode": "autolayer"} for i, n in rooms]
    data = {"autoAreaValue": entries, "mapId": map_id, "userId": "u", "sn": "s"}
    return {"127": envelope(21004, data, dInfo={"userId": "u"})}


def command_dps(rooms: list[int]) -> dict[str, str]:
    cmds = [
        {"infoType": 21023, "data": {"segmentId": rooms, "mapId": 1000000001}},
        {"infoType": 21005, "data": {"mode": "reAppointClean"}},
    ]
    return {"127": envelope(30000, {"cmds": cmds, "mainCmds": [21005]})}


def serpentine(
    width: int = 2000, height: int = 1000, step: int = 50, lane: int = 250
) -> list[tuple[int, int]]:
    points, y, forward = [], 0, True
    while y <= height:
        xs = range(0, width + 1, step)
        points += [(x, y) for x in (xs if forward else reversed(xs))]
        forward = not forward
        y += lane
    return points


def run_messages(
    path_id: int, points: list[tuple[int, int]], overlap: int = 3
) -> Iterator[dict[str, str]]:
    """The path as the vacuum streams it: empty chunks first, then mostly one point at
    a time, now and then a bigger chunk that repeats a few earlier points."""
    total = len(points)
    yield chunk_dps(path_id, 0, [], total=0)
    yield chunk_dps(path_id, 0, [], total=1)
    done = 0
    while done < total:
        size = 1 if done % 7 else 5
        start = max(0, done - (overlap if done % 11 == 0 else 0))
        end = min(total, done + size)
        yield chunk_dps(path_id, start, points[start:end], total=total)
        done = end


# --- the server -------------------------------------------------------------------


class FakeVacuum:
    def __init__(
        self,
        key: str = KEY,
        device_id: str = DEVICE_ID,
        port: int = 0,
        status: dict[str, Any] | None = None,
        one_connection_only: bool = False,
    ) -> None:
        self.key = key
        self.device_id = device_id
        self.status = status or {"101": False, "105": "chargring", "106": 100}
        self.one_connection_only = one_connection_only
        self.commands: list[int] = []  # every command code received, in order
        self.connections = 0  # connections accepted, including refused ones
        self.refused = 0
        self.sent_bytes = 0
        self._cipher = AESCipher(key.encode())
        self._clients: dict[socket.socket, threading.Lock] = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._threads = [threading.Thread(target=self._accept, daemon=True)]

    # lifecycle

    def start(self) -> FakeVacuum:
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.drop_clients()
        self._listener.close()
        for thread in self._threads:
            thread.join(2)

    def __enter__(self) -> FakeVacuum:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def connected_clients(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def only_reads(self) -> bool:
        """Whether every command received so far was a status query or a heartbeat."""
        return set(self.commands) <= READ_ONLY_COMMANDS

    @property
    def heartbeats(self) -> int:
        return self.commands.count(HEART_BEAT)

    # what tests do to it

    def push(self, dps: dict[str, Any]) -> int:
        """Send a status report to every connected client. Returns how many got it."""
        body = json.dumps({"devId": self.device_id, "dps": dps, "t": int(time.time())})
        return self._send_all(STATUS, body, header=True)

    def drop_clients(self) -> None:
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            self._close(conn)

    # protocol

    def _frame(self, cmd: int, body: str | None, header: bool) -> bytes:
        payload = struct.pack(H.MESSAGE_RETCODE_FMT, 0)
        if body is not None:
            encrypted = self._cipher.encrypt(body.encode(), use_base64=False)
            payload += (H.PROTOCOL_33_HEADER if header else b"") + encrypted
        with self._lock:
            self._seq += 1
            seq = self._seq
        message = TuyaMessage(seq, cmd, 0, payload, 0, True, H.PREFIX_55AA_VALUE, None)
        return pack_message(message)

    def _send_all(self, cmd: int, body: str, header: bool) -> int:
        data = self._frame(cmd, body, header)
        with self._lock:
            clients = list(self._clients.items())
        sent = 0
        for conn, lock in clients:
            try:
                with lock:
                    conn.sendall(data)
                self.sent_bytes += len(data)
                sent += 1
            except OSError:
                self._close(conn)
        return sent

    def _close(self, conn: socket.socket) -> None:
        with self._lock:
            self._clients.pop(conn, None)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            if self.one_connection_only and self.connected_clients:
                self.refused += 1
                conn.close()
                continue
            conn.settimeout(0.5)
            with self._lock:
                self._clients[conn] = threading.Lock()
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket, size: int) -> bytes | None:
        data = b""
        while len(data) < size:
            try:
                chunk = conn.recv(size - len(data))
            except TimeoutError:
                if self._stop.is_set():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _serve(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            head = self._read(conn, _HEADER.size)
            if head is None:
                break
            length = _HEADER.unpack(head)[3]
            rest = self._read(conn, length) if length else b""
            if rest is None:
                break
            try:
                message = unpack_message(head + rest, no_retcode=True)
            except Exception:  # noqa: BLE001 - a fake: just ignore what it can't read
                continue
            self.commands.append(message.cmd)
            if message.cmd == HEART_BEAT:
                self._reply(conn, HEART_BEAT, None, header=False)
            elif message.cmd in (DP_QUERY, DP_QUERY_NEW):
                body = json.dumps({"devId": self.device_id, "dps": self.status})
                self._reply(conn, message.cmd, body, header=False)
            # anything else is recorded and gets no answer
        self._close(conn)

    def _reply(
        self, conn: socket.socket, cmd: int, body: str | None, header: bool
    ) -> None:
        data = self._frame(cmd, body, header)
        with self._lock:
            lock = self._clients.get(conn)
        if lock is None:
            return
        try:
            with lock:
                conn.sendall(data)
            self.sent_bytes += len(data)
        except OSError:
            self._close(conn)


# --- run it by hand -----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=6668)
    parser.add_argument("--rate", type=float, default=4.0, help="messages per second")
    parser.add_argument(
        "--pause", type=float, default=20.0, help="seconds between runs"
    )
    parser.add_argument("--runs", type=int, default=0, help="0 means forever")
    args = parser.parse_args()
    runs_done = 0
    with FakeVacuum(port=args.port) as vacuum:
        print(
            f"fake vacuum on 127.0.0.1:{vacuum.port} (made-up ID and key, see --help)"
        )
        while not args.runs or runs_done < args.runs:
            runs_done += 1
            start = int(time.time())
            vacuum.push(rooms_dps())
            vacuum.push(command_dps([1]))
            time.sleep(2)
            for message in run_messages(start, serpentine(4000, 2000)):
                vacuum.push(message)
                time.sleep(1 / args.rate)
            print(f"run {runs_done} sent; clients={vacuum.connected_clients}")
            time.sleep(args.pause)


if __name__ == "__main__":
    main()
