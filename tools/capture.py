"""Log every message a Tuya device sends on its local connection.

Read-only: asks for status once, then only sends heartbeats. Writes JSON lines to
captures/messages.jsonl, which is git-ignored because it holds private data.

Usage:
    TUYA_DEVICE_ID=... TUYA_HOST=... TUYA_LOCAL_KEY=... \
        python tools/capture.py [minutes]

TUYA_VERSION is optional and defaults to 3.3.
"""

import json
import os
import pathlib
import sys
import time

import tinytuya
from tinytuya.core import const, message_helper

# tinytuya rejects anything over 1440 bytes as corrupt, and vacuums send more.
const.MAX_PAYLOAD_LENGTH = message_helper.MAX_PAYLOAD_LENGTH = 65536

OUT = pathlib.Path(__file__).resolve().parent.parent / "captures" / "messages.jsonl"
HEARTBEAT_SECONDS = 8


def log(kind, data):
    size = len(json.dumps(data)) if data is not None else 0
    record = {"t": time.time(), "kind": kind, "size": size, "data": data}
    with OUT.open("a") as f:
        f.write(json.dumps(record) + "\n")
    dps = sorted((data or {}).get("dps", {})) if isinstance(data, dict) else []
    print(time.strftime("%H:%M:%S"), kind, size, "dps:", dps, flush=True)


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    OUT.parent.mkdir(exist_ok=True)
    device = tinytuya.Device(
        os.environ["TUYA_DEVICE_ID"],
        os.environ["TUYA_HOST"],
        os.environ["TUYA_LOCAL_KEY"],
        version=float(os.environ.get("TUYA_VERSION", "3.3")),
        persist=True,
    )
    device.set_socketTimeout(10)

    log("status", device.status())
    end = time.time() + minutes * 60
    last_beat = time.time()
    while time.time() < end:
        try:
            message = device.receive()
        except Exception as err:  # noqa: BLE001 - keep listening, but record it
            log("exception", {"error": repr(err)})
            continue
        if message:
            log("push", message)
        if time.time() - last_beat > HEARTBEAT_SECONDS:
            device.heartbeat(nowait=True)
            last_beat = time.time()


if __name__ == "__main__":
    main()
