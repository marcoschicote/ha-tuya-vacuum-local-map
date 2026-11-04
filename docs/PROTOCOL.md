# The protocol

What we know about the JSON family of Tuya vacuum messages. Nothing here comes from Tuya documentation: it was all observed on the wire.

**Confidence labels**

| Label | Meaning |
|---|---|
| **[Seen]** | The full message was read and the exact format is known |
| **[Described]** | Written up in earlier notes from a real capture, but the raw message wasn't kept or has been re-checked since |
| **[Reported]** | Someone else reported it on their vacuum |
| **[Guess]** | Inferred. Verify before relying on it |

**The capture behind the [Seen] labels:** a Rowenta X-plorer (category `sd`, protocol 3.3), recorded read-only on 2026-11-04 with `tools/capture.py` for 12 minutes: the app's map screen opened while docked, a one-room clean started from the app, the whole run, the return to the dock, and the map screen opened again. 1346 messages. The raw capture holds private data and isn't in the repo. `tests/fixtures/frames.json` has synthetic frames with the same structure and made-up values.

## Transport

- Normal Tuya local protocol over TCP port 6668. tinytuya handles the framing and encryption. **[Seen]**
- Frames arrive as `STATUS` (cmd 8) with a `55aa` prefix on protocol 3.3. **[Seen]**
- Frames reach about 5 KB, well over the limits in tinytuya (1440 bytes) and localtuya (2000 bytes, being raised in [#901](https://github.com/xZetsubou/hass-localtuya/pull/901)). Largest so far: 4805 on the Rowenta late in a 35-minute run, 5131 on the Kabum. **[Described]** / **[Reported]** The 2026-11-04 capture, a 10-minute run, topped out at about 2.8 KB: the room list (2748 base64 characters) and the re-sent previous run. **[Seen]**
- Tuya's published definitions cap these raw DPs at 128 bytes. The vacuum ignores that, so don't validate against it. **[Seen]**
- The Rowenta accepts a second local connection alongside another client. **[Seen]** Other Tuya devices often allow only one. **[Reported]**

### Raising tinytuya's limit

`parse_header` reads the copy in `message_helper`, so setting `const` alone does nothing. This worked on the real vacuum **[Seen]**:

```python
from tinytuya.core import const, message_helper

const.MAX_PAYLOAD_LENGTH = message_helper.MAX_PAYLOAD_LENGTH = 65536
```

`tinytuya.core.Monitor` holds a third copy, so set that too if you use Monitor. This is process-wide: it also affects any other integration using tinytuya in the same Home Assistant, such as tuya-local. That's harmless, since it only accepts more, but don't lower the limit.

## Recognising a frame

**Identify frames by content, not DP number**, since DP numbers differ by maker: 123 (paths) and 127 (rooms, commands) on the Rowenta and Kabum, 102 on the Nova.

A DP value is one of ours if it's a string that base64-decodes to a JSON object with an integer `infoType`:

```json
{"infoType": 21011, "data": {}, "message": "ok"}
```

What else is in the envelope varies by type, so treat everything beyond `infoType` and `data` as optional: **[Seen]**

| Field | On |
|---|---|
| `message` (`"ok"`) | Every Rowenta frame; also on the Nova path frame |
| `ts` (Unix seconds, an integer) | Path frames (21011) |
| `dInfo` (`{"ts": "<13 digits, ms>", "userId": "..."}`) | Room list and command frames on DP 127 |

**Private fields:** `dInfo.userId`, `data.userId`, `data.sn` and `data.mapId` identify the owner, the vacuum or the home. Never log, store, expose or put them in diagnostics. Read only what's needed (`mapId` to detect a new map): to notice a change across restarts, store a salted hash of it, never the value.

Ordinary string DPs will reach this check, so a failed decode is normal: skip it silently, without logging an error.

## Message types

| `infoType` | What | Direction | Confidence |
|---|---|---|---|
| 20001 | Status: runtime, area, battery | Vacuum → | **[Reported]** Nova |
| 21003 | Zone list with virtual walls | App → vacuum, echoed back | **[Reported]** Kabum |
| 21004 | Room list | Vacuum → | **[Seen]** Rowenta |
| 21005 | Start a clean, nested inside 30000 | App → vacuum | **[Seen]** Rowenta |
| 21011 | Cleaning path chunk | Vacuum → | **[Seen]** Rowenta and Nova, **[Reported]** Kabum |
| 21023 | Rooms to clean, nested inside 30000 | App → vacuum | **[Seen]** Rowenta |
| 30000 | Command wrapper | App → vacuum, visible to other local clients | **[Seen]** Rowenta |

Unknown types must be ignored, and counted for diagnostics. Only these three were seen in 12 minutes covering a whole run: 21004, 21011 and 30000. **[Seen]**

### 21011: path chunk

The structure of a frame reported publicly on the Nova ([tuya-local #1660](https://github.com/make-all/tuya-local/issues/1660)), with made-up values. **[Seen]**

```json
{
  "data": {
    "pathID": 1600100000,
    "pointCounts": 8,
    "posArray": [[640, -210], [684, -226], [732, -232], [784, -232],
                 [836, -224], [880, -212], [924, -200], [960, -184]],
    "startPos": 99,
    "totalPoints": 107
  },
  "infoType": 21011,
  "message": "ok",
  "ts": 1600100076
}
```

The Rowenta sends exactly the same shape. What the capture showed, over 1300 chunks: **[Seen]**

- **Indexing.** `posArray[i]` is point number `startPos + i` of the run, and `pointCounts` always equals `len(posArray)`. `totalPoints` never decreased within a run and ended equal to the number of points (1528 in the run captured).
- **`pathID` is the run's start time** in Unix seconds: the first chunk's `ts` equalled it exactly. `ts` is also in seconds.
- **A run starts with empty chunks.** The first chunks had `posArray` empty, with `totalPoints` 0, then 1. Handle `pointCounts: 0`.
- **Chunks mostly carry one point**, arriving a median 0.28 s apart (95th percentile 1.8 s, longest gap 27 s). Larger chunks happen too: up to 69 points in the live run.
- **Chunks overlap.** 47 of the 1299 live chunks re-sent earlier points (1 to 69 of them). All 337 re-sent points were identical to what had been sent before. So index by position and never append.
- **No gaps** in the live run: every index from 0 to the end arrived. A dropped connection would still leave gaps, so handle them.
- **Opening the map screen re-sends the previous run, whole.** One chunk with `startPos` 0 and all of that run's points (144 here), carrying that run's own `pathID`, with a `ts` hours after it. It came right after the room list, 1 s later.
- **The path keeps coming while the vacuum returns to the dock.** The last chunk arrived about 60 s after the robot state became `tocharge`, just before it became `chargring`.
- **Step size.** Consecutive points are a median 56 units apart (|dx| + |dy|), 99th percentile 100, largest 256.
- **Coordinates.** x grows to the right and y upwards, matching the app's map **[Described]**. The first points of a run sit near (0, 0), which looks like the dock **[Guess]**. The unit looks like millimetres: a one-room run spanned about 6 m in x **[Guess]**, not calibrated.

### 21004: room list

**[Seen]** on the Rowenta. A synthetic example (the structure is real, every value is made up; the full set is in `tests/fixtures/frames.json`):

```json
{
  "dInfo": {"ts": "1600000000000", "userId": "example-user-id"},
  "data": {
    "autoAreaValue": [
      {"active": "normal", "cleanOrder": 0, "cleanType": "sweepOnly", "id": 1,
       "mode": "autolayer", "name": "20+2p+30+30", "tag": "0",
       "vertexs": [[0, 0], [0, 0], [0, 0], [0, 0]]}
    ],
    "mapId": 1000000001,
    "sn": "EXAMPLE-SN-0001",
    "userId": "example-user-id",
    "value": [
      {"active": "normal", "forbidType": "all", "id": 1, "mode": "area",
       "name": "forbid1", "tag": "36+33+33+31+1c",
       "vertexs": [[1000, 1000], [1000, 2000], [2000, 2000], [2000, 1000]]}
    ]
  },
  "infoType": 21004,
  "message": "ok"
}
```

- **`autoAreaValue` has one entry per room**, 10 in the capture. `id` is a small integer, 1 to 10, that stays the same when the room is renamed. Every entry had `active: "normal"`, `cleanOrder: 0`, `cleanType: "sweepOnly"` and `mode: "autolayer"`. `tag` is a string of digits; how it relates to `id` wasn't checked.
- **No geometry and no label position.** Every room's `vertexs` is four points at `[0, 0]`, and there is no `pos`. The earlier notes mentioned a `pos` (label position) and `workNoisy` (suction) on a list captured before the rooms were renamed **[Described]**; neither is in this one. So treat any field beyond `id` and `name` as optional, and don't rely on the list for room shapes: outlines have to be learned from where the vacuum cleans (PLAN, phase 5).
- **`value` holds the no-go areas** set in the app (`forbidType: "all"`), as an axis-aligned rectangle in `vertexs` with real coordinates. Its `name` is plain text and its `tag` is encoded like a room name. **[Seen]** They are not rooms.
- **`mapId` changes when the vacuum makes a new map** **[Guess]**, and equals the `mapId` in the room command **[Seen]**. That's the reset signal for accumulated coverage.
- **Timing.** The list is sent in pairs about a second apart: when the map screen opens, and several times during a run. It was identical every time (8 sends).
- **Merge each list into what's already known, per room `id`**, instead of replacing it, since fields come and go (see above).

### Room names

Names set in the app are encoded: each character's Unicode code point in base 36, lowercase, joined by `+`. **[Seen]** All 10 rooms were encoded, and decoding them gave names containing `ñ` (U+00F1) and `ó` (U+00F3), so it isn't limited to ASCII. Default names were plain text (`Room1`) in the earlier notes. **[Described]**

| Encoded | Decoded |
|---|---|
| `2a+33+33+31+1f` | `Room3` |
| `36+33+33+31+1c` | `room0` |
| `6p` | `ñ` (a single character has no `+`, see below) |

```python
import re

_ENCODED = re.compile(r"[0-9a-z]+(\+[0-9a-z]+)+")


def decode_name(name: str) -> str:
    """Decode a room name, leaving plain names unchanged."""
    if not _ENCODED.fullmatch(name):
        return name
    try:
        return "".join(chr(int(part, 36)) for part in name.split("+"))
    except (ValueError, OverflowError):
        return name
```

A one-character encoded name has no `+`, so it can't be told apart from a plain name. Leave it as it is.

### 30000: command wrapper

**[Seen]** once, when the user started a one-room clean in the app. This is a message other local clients can see, so it tells us which rooms a run was asked to clean. Synthetic example:

```json
{
  "dInfo": {"ts": "1600000000000", "userId": "example-user-id"},
  "data": {
    "cmds": [
      {"data": {"cleanId": [-3], "extraAreas": [], "mapId": 1000000001,
                "segmentId": [3]},
       "infoType": 21023},
      {"data": {"mode": "reAppointClean"}, "infoType": 21005}
    ],
    "mainCmds": [21005]
  },
  "infoType": 30000,
  "message": "ok"
}
```

- **Everything is under `data`.** `cmds[0]` (`infoType` 21023) holds `segmentId`, the room IDs to clean (`id`s from the room list), plus `cleanId`, `extraAreas` and `mapId`. `cmds[1]` (`infoType` 21005) holds a single `mode` key. Its value, `reAppointClean`, comes from the earlier notes **[Described]**.
- **Several rooms** put several IDs in `segmentId` (`[2, 7]` in the earlier notes). The vacuum picks the order, and every room's `cleanOrder` stays 0. **[Described]**
- **Timing:** the command appeared about 2 s before the robot state changed to `selectroom`, and the first path chunk followed 1 s later.

**Read this message, never send it** (see AGENTS.md). One report on a vacuum in the binary family says room commands get overwritten unless they carry a session token from Tuya's cloud ([tuya-local discussion #5556](https://github.com/make-all/tuya-local/discussions/5556)), so don't assume sending works here either.

### 21003: zone list

**[Reported]** on the Kabum ([comment](https://github.com/xZetsubou/hass-localtuya/issues/900)). When the app starts a zone clean, it sends every zone and virtual wall on the map, and the vacuum echoes that back. About 1050 bytes with two zones and one wall. The shape is unknown. Saving a zone in the app sends nothing on the LAN, so this echo is the only local way to learn zones.

### 20001: status

**[Reported]** on the Nova. Its units are unconfirmed.

```json
{"data": {"cleanArea": 11, "cleanTime": 570, "elec": 82, "elecReal": 82,
          "mode": "sweep", "subMode": "smart", "timeStamp": 1708024009},
 "infoType": 20001}
```

## Other data points on the Rowenta

These are normal, unencoded DPs, specific to that model. Useful for checking the design, but don't depend on them in `core/`. **[Seen]** in the capture unless noted.

| DP | Code | Notes |
|---|---|---|
| 101 | `clean_switch` | `true` while a run is under way |
| 103 | `charge_switch` | `false` while cleaning, `true` once it is sent back to the dock |
| 104 | `clean_mode` | `selectroom` for a room clean, `backcharge` when returning |
| 105 | `robot_state` | `fullcharge` → `selectroom` (run starts) → `tocharge` (returning) → `chargring` (docked, charging) |
| 106 | `battery` | Percent |
| 107 | `cur_clean_time` | Updated about once a minute during a run |
| 108 | `cur_clean_area` | Current run, presumably m². Updated a few times per run. Useful for calibrating |
| 116–121 | Lifetime counters and brush timers | All updated together right after docking |
| 125 | `request_data` | `map`, `path` or `both`. Sending `map` while docked produced nothing large **[Described]** |
| 128 | `message_report` | `6` = clean finished **[Described]**; it was `6` in the first status of the capture |
| 129 | `reset_map` | **Wipes the map. Never send it** |

**A run, as DPs.** Start: command (30000) → 104 and 105 become `selectroom`, 101 `true`, 103 `false` → first path chunk. End: 105 `tocharge`, 101 `false`, 103 `true`, 104 `backcharge` → path keeps arriving for about a minute → 105 `chargring` → counters 116–121 change. A run is best ended by the path going quiet, since these DPs differ by model.

## No floor plan locally

Nothing resembling a floor plan appeared in the capture, which covered a map screen view and a whole run. **[Seen]** Treat it as absent.
