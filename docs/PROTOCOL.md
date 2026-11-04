# The protocol

What we know about the JSON family of Tuya vacuum messages. Nothing here comes from Tuya documentation: it was all observed on the wire.

**Confidence labels**

| Label | Meaning |
|---|---|
| **[Seen]** | The full message was read and the exact format is known |
| **[Described]** | Written up in notes from a real capture, but the raw message wasn't kept, so details of its shape are unconfirmed |
| **[Reported]** | Someone else reported it on their vacuum |
| **[Guess]** | Inferred. Verify before relying on it |

The raw Rowenta captures weren't kept, so phase 0 of the plan re-captures them and upgrades these labels.

## Transport

- Normal Tuya local protocol over TCP port 6668. tinytuya handles the framing and encryption. **[Seen]**
- Frames arrive as `STATUS` (cmd 8) with a `55aa` prefix on protocol 3.3. **[Seen]**
- Frames reach about 5 KB, well over the limits in tinytuya (1440 bytes) and localtuya (2000 bytes, being raised in [#901](https://github.com/xZetsubou/hass-localtuya/pull/901)). Largest so far: 4805 on the Rowenta, 5131 on the Kabum. **[Seen]** / **[Reported]**
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

**Identify frames by content, not DP number**, since DP numbers differ by maker: 123 and 127 on the Rowenta and Kabum, 102 on the Nova.

A DP value is one of ours if it's a string that base64-decodes to a JSON object with an integer `infoType`:

```json
{"infoType": 21011, "data": {}, "message": "ok", "ts": 1708023502}
```

`message` and `ts` were on the one complete path frame we hold, and not on the Nova's `20001` frame. Treat both as optional. **[Reported]**

Ordinary string DPs will reach this check, so a failed decode is normal: skip it silently, without logging an error.

## Message types

| `infoType` | What | Direction | Confidence |
|---|---|---|---|
| 20001 | Status: runtime, area, battery | Vacuum → | **[Reported]** Nova |
| 21003 | Zone list with virtual walls | App → vacuum, echoed back | **[Reported]** Kabum |
| 21004 | Room list | Vacuum → | **[Described]** Rowenta |
| 21005 | Start a clean, nested inside 30000 | App → vacuum | **[Described]** Rowenta |
| 21011 | Cleaning path chunk | Vacuum → | **[Seen]** Nova, **[Described]** Rowenta, **[Reported]** Kabum |
| 21023 | Rooms to clean, nested inside 30000 | App → vacuum | **[Described]** Rowenta |
| 30000 | Command wrapper | App → vacuum, visible to other local clients | **[Described]** Rowenta |

Unknown types must be ignored, and counted for diagnostics.

### 21011: path chunk

A real frame, from a public report on the Nova ([tuya-local #1660](https://github.com/make-all/tuya-local/issues/1660)). **[Seen]**

```json
{
  "data": {
    "pathID": 1708023426,
    "pointCounts": 8,
    "posArray": [[-1911, -1311], [-1947, -1331], [-1995, -1339], [-2047, -1339],
                 [-2099, -1331], [-2143, -1319], [-2187, -1307], [-2223, -1291]],
    "startPos": 99,
    "totalPoints": 107
  },
  "infoType": 21011,
  "message": "ok",
  "ts": 1708023502
}
```

- `pathID`: identifies the run, and looks like its start as a Unix time (76 s before `ts` here). **[Guess]** on the meaning; only use it as an ID.
- `posArray[i]` is point number `startPos + i` in the run. Here 99 + 8 = 107 = `totalPoints`. **[Seen]**
- Chunks arrive about 4 times a second during a run, usually 200–600 bytes. **[Described]**
- The vacuum also re-sends the last run when the app opens its map screen. **[Described]** So a chunk can repeat earlier points: index by position, never append.
- `posArray` is what grows frames to around 5 KB late in a run, so some chunks carry many points. **[Described]**

**Coordinates.** On the Rowenta, x grows to the right and y upwards, matching the app's map. **[Described]** The unit looks like millimetres: one room's points spanned about 6 × 4.5 m at that scale. **[Guess]** It isn't calibrated. The origin looks like the dock or the map's reference point. **[Guess]**

### 21004: room list

**[Described]** from the Rowenta. The exact nesting and the shape of `pos` need confirming in phase 0. Reconstructed example with made-up values:

```json
{
  "infoType": 21004,
  "data": {
    "mapId": 1000000001,
    "autoAreaValue": [
      {"id": 1, "name": "Room1", "pos": [-4000, 1000], "cleanType": "sweepOnly",
       "workNoisy": "auto", "cleanOrder": 0, "active": true},
      {"id": 2, "name": "2a+33+33+31+1f", "cleanOrder": 0, "active": true}
    ],
    "value": [
      {"mode": "area", "tag": "36+33+33+31+1c",
       "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]}
    ]
  }
}
```

- It's about 2.6 KB for 9 rooms, sent when the app opens the map screen and several times per run.
- `autoAreaValue` has one entry per room. `id` is a small integer that stays the same when the room is renamed.
- **`pos` and `workNoisy` can be missing.** After rooms were renamed in the app, the list came back without them. So merge each list into what's already known, per room `id`, instead of replacing it: otherwise a rename loses every label's position.
- `value` holds rectangles set in the app, which are probably zones or no-go areas. Its field names are a guess.
- `mapId` changes when the vacuum makes a new map. **[Guess]** That's the reset signal for accumulated coverage.

### Room names

Default names are plain text. Names set in the app are encoded: each character's code in base 36, joined by `+`. **[Described]**

| Encoded | Decoded |
|---|---|
| `2a+33+33+31+1f` | `Room3` |
| `36+33+33+31+1c` | `room0` |
| `6p` | `ñ`, if codes are Unicode code points **[Guess]** |

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

A one-character encoded name has no `+`, so it can't be told apart from a plain name. Leave it as it is. **Check accented names in phase 0**: real names on the Rowenta use `ñ`, so confirm the code-point guess by renaming a room.

### 30000: command wrapper

**[Described]** The app's room-clean command, which a second local client can see. Whether this sits at the top level or under `data` needs confirming.

```json
{
  "infoType": 30000,
  "cmds": [
    {"infoType": 21023, "data": {"segmentId": [2, 7], "mapId": 1000000001,
                                 "cleanId": [-3], "extraAreas": []}},
    {"infoType": 21005, "data": {"mode": "reAppointClean"}}
  ],
  "mainCmds": [21005]
}
```

`segmentId` holds the room IDs. The vacuum picks the order. **Read this message, never send it** (see AGENTS.md). It's what tells us which rooms a run was asked to clean, which the room outlines in phase 5 depend on.

One report on a vacuum in the binary family says room commands get overwritten unless they carry a session token from Tuya's cloud ([tuya-local discussion #5556](https://github.com/make-all/tuya-local/discussions/5556)). So don't assume sending works here either.

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

These are normal, unencoded DPs, specific to that model. Useful for checking the design, but don't depend on them in `core/`.

| DP | Code | Notes |
|---|---|---|
| 105 | `robot_state` | `sweep`, `selectroom`, `tocharge`, `chargring`, ... |
| 107 | `cur_clean_time` | Seconds, current run |
| 108 | `cur_clean_area` | Current run, presumably m². Useful for calibrating |
| 125 | `request_data` | `map`, `path` or `both`. Sending `map` while docked produced nothing large |
| 128 | `message_report` | `6` = clean finished |
| 129 | `reset_map` | **Wipes the map. Never send it** |

## No floor plan locally

On the Rowenta, nothing resembling a floor plan appeared on a docked map view or during a 10-minute run: nothing bigger than the room list. **[Described]** Treat it as absent.
