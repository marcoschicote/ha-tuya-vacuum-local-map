# Implementation plan

**Goal:** a HACS integration that listens to a Tuya vacuum locally and shows a coverage map in Home Assistant, built up across runs, with room labels and the latest path.

**Background:** the tuya-local maintainer [preferred this to be separate](https://github.com/make-all/tuya-local/issues/288) from their integration. The message formats are in [PROTOCOL.md](PROTOCOL.md).

**Not in scope for now:** floor plans, sending commands to the vacuum (AGENTS.md rule 2), a custom Lovelace card, and the binary protocol family.

## Decisions already made

| Decision | Why |
|---|---|
| Own read-only local connection, not on top of tuya-local or localtuya | tuya-local uses tinytuya, whose 1440-byte limit rejects these frames, and a code search found no override. localtuya rejects them too until [#901](https://github.com/xZetsubou/hass-localtuya/pull/901) ships. A second connection works on the Rowenta alongside localtuya |
| `tinytuya>=1.20.0,<2`, not an exact pin | Python packages are shared across a Home Assistant install, and tuya-local pins `tinytuya==1.20.0`. A range lets both be satisfied |
| `Device` with `persist=True` in a loop, not `tinytuya.Monitor` | The loop in `tools/capture.py` has run against the real vacuum with big frames. Monitor is marked experimental and has its own framing code, untested with these frames |
| Recognise frames by content, not DP number | DP numbers differ by maker, and it means no DP configuration for users |
| `core/` free of Home Assistant | Testable without HA, and reusable by a future card or other integration |
| A grid is the source of truth, and the image is drawn from it | Coverage accumulates across runs without keeping every path |
| Map exposed as an `image` entity | Works with stock picture cards, so no custom card needed yet |

## Target layout

```
custom_components/tuya_vacuum_local_map/
  __init__.py        setup and unload of the config entry
  manifest.json
  const.py
  config_flow.py     host, device ID, local key, protocol version; options
  connection.py      worker thread around tinytuya (no HA imports)
  hub.py             owns the connection for an entry, hands data to the event loop
  coordinator.py     feeds frames into core, owns state and storage (replaces hub.py)
  entity.py          shared base for the entities
  image.py           the map
  sensor.py          rooms, current run
  binary_sensor.py   connected
  button.py          reset map
  diagnostics.py
  strings.json, translations/en.json
  brand/icon.png
  core/
    protocol.py      frames and room names
    path.py          assembling chunks into one run
    runs.py          deciding when runs end, counting each once
    grid.py          coverage grid
    render.py        SVG output
tests/
  fixtures/frames.json
  test_*.py
```

## Phase 0: confirm the formats (done)

**Captured 2026-11-04** with `tools/capture.py`: 12 minutes, 1346 messages, covering the map screen opened while docked, a one-room clean from the app, the whole run, the return to the dock, and the map screen again. The formats are now **[Seen]** in `docs/PROTOCOL.md`. The raw capture stays on the owner's machine (git-ignored); `tests/fixtures/frames.json` has synthetic frames with the same structure and made-up values.

What the capture settled, and what it changed in this plan:

- **Rooms have no label position and no outlines.** Each room in the 21004 list has an `id` and an encoded `name`; its `vertexs` are all `[0, 0]`, and `pos` isn't there. So `Room` has no `pos`, phase 2 draws no room labels, and outlines and labels wait for phase 5.
- **The room command nests under `data`.** `data.cmds[0].data.segmentId` holds the room IDs (`infoType` 21023).
- **Names are Unicode code points in base 36**, confirmed with `ñ` and `ó`.
- **`pathID` is the run's start time** (Unix seconds), equal to the first chunk's `ts`.
- **Opening the map screen re-sends the previous run whole**, as one chunk with its own `pathID`. If we haven't recorded that run, it's a complete run and can be added (phase 2).
- **Runs start with empty chunks** (`pointCounts` 0).
- **Chunks overlap, with identical points** (337 re-sent points, none differing), so index by position. No gaps occurred in the live run.
- **Largest frame was about 2.8 KB** in this 10-minute run, well inside the 65536 limit set in `connection.py`.
- **Private fields** in the room list and command: `dInfo.userId`, `data.userId`, `data.sn`, `data.mapId`. See the privacy rule in `PROTOCOL.md`.
- Unit guess (mm) and the origin (the dock) are still guesses, to be settled by phase 5's calibration.

**Decided by the owner (2026-11-04):**

- **The map is an SVG.** Check early in phase 4 that an `ImageEntity` returning `image/svg+xml` displays in a picture-entity card, in the app as well as the browser. If it doesn't, switch `render.py` to PNG via Pillow and update this plan.
- **Accented-name rename and two-room clean weren't captured, on purpose.** The names in the capture already had non-ASCII characters, and a two-room command differs only in having more IDs.

**Done:** the fixtures were reviewed by the owner and committed. Start at phase 1.

## Phase 1: decode messages (`core/protocol.py`)

```python
@dataclass(frozen=True)
class Frame:
    dp: str
    info_type: int
    data: dict[str, Any]
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


def decode_dp(dp: str, value: object) -> Frame | None: ...
def parse_path(frame: Frame) -> PathChunk | None: ...
def parse_rooms(frame: Frame) -> tuple[int, list[Room]] | None:  # (map_id, rooms)
    ...  # rooms come from data.autoAreaValue; data.value (no-go areas) is not rooms
def parse_room_command(
    frame: Frame,
) -> list[int] | None: ...  # data.cmds[].data.segmentId
def decode_name(name: str) -> str: ...
```

Rules:

- **Never raise on device data.** Anything malformed returns `None`. The vacuum's firmware is outside our control, and a bad message must not kill the connection, which is exactly the bug in #900.
- A non-string DP value returns `None` straight away. A value too short or not base64 returns `None` without logging.
- Allow extra fields, and missing `message`, `ts` and `dInfo`. Read only what's needed: never copy `userId`, `sn` or `dInfo` into a `Frame`, an entity, a log line or diagnostics (PROTOCOL, "Recognising a frame"). `Frame.data` keeps `mapId` (the room list needs it) but is left out of the dataclass's `repr`.
- `parse_rooms` returns `None` without an integer `mapId`, and skips entries without an integer `id` and a string `name`.
- Empty chunks (`pointCounts` 0) are valid.

**Tests:** every fixture; the Nova-shaped frame in `fixtures/frames.json` (made-up values); the name table in PROTOCOL.md; garbage input (not base64, base64 of non-JSON, JSON list, missing fields, wrong types, non-integer points).

Also a test over `captures/messages.jsonl` when that file exists (the owner's real capture, git-ignored; skip the test when it's missing, as CI has none). It checks the decoder against real data: nothing raises, the counts per `infoType` are non-zero, every path chunk is indexed consistently. It must print nothing from the file.

**Done when:** those pass with no HA installed, including the test that fails if `core/` imports `homeassistant`.

## Phase 2: path, grid, drawing (`core/path.py`, `grid.py`, `render.py`)

### `path.py`

```python
class RunAssembler:
    def add(self, chunk: PathChunk) -> None: ...
    @property
    def path_id(self) -> int | None: ...
    def segments(self) -> list[list[tuple[int, int]]]: ...
```

- Store points by index (`dict[int, point]`), because chunks repeat and overlap. `points[chunk.start + i] = p`. In the capture every re-sent point was identical, but keep the latest anyway.
- `path_id` is the run's start time in Unix seconds, so keep it as the run's `started`.
- A different `path_id` starts a new run. The caller finishes the old one first (see below).
- `add()` returns whether the chunk brought any new or changed point. A re-send of known points returns `False`, which is how the tracker tells progress from a re-send and times the 120 s.
- `segments()` splits the path where indexes are missing, since chunks are lost if the connection drops, and where consecutive points jump more than `MAX_JUMP` (1500 units). Never draw a line across a gap: it'd paint coverage through a wall.
- Ignore points whose index is far beyond `total`, as a sanity limit.

**When a run ends:** a new `pathID` arrives, or no new point has arrived for 120 s. Don't rely on state DPs, which differ by model. An idle gap is safe: if chunks resume with the same `pathID`, reopen the run, and let the grid handle it (next section).

**Re-sent runs (`core/runs.py`, added to the plan):** `RunAssembler` only holds one run's points. A second class, `RunTracker(grid, finished_ids)`, sits on top: it takes `add(chunk, now)`, `poll(now)` and `flush()`, with the clock passed in, and adds each run to the grid exactly once. It keeps the last three runs' points in memory, so a run that resumes after an idle gap grows without being counted twice: `CoverageGrid.add_run` takes the cells it returned last time and only counts new ones (this is what "let the grid handle it" means). Opening the app's map screen re-sends the previous run whole, as one chunk. A re-send of a run we already recorded must not count as a second cleaning: keep the IDs of the last 50 finished runs (persisted), and ignore chunks for those, except to add points that weren't received before. But if its `pathID` is unknown, it's a complete run we missed (the integration was down, or the connection dropped): record it as a run of its own.

### `grid.py`

```python
class CoverageGrid:
    def __init__(self, cell: int = 50, brush: int = 300) -> None: ...
    def add_run(
        self,
        segments: list[list[tuple[int, int]]],
        already: frozenset[tuple[int, int]] = frozenset(),
    ) -> frozenset[tuple[int, int]]: ...
    def bounds(self) -> tuple[int, int, int, int] | None: ...
    def covered_cells(self) -> int: ...
    def to_bytes(self) -> bytes: ...
    @classmethod
    def from_bytes(cls, data: bytes) -> "CoverageGrid": ...
```

- Units are the device's own. `cell` and `brush` are in those units (50 and 300 are 5 cm and 30 cm if units are mm).
- Stamp a disc of `brush` diameter at steps along each segment, no further apart than `cell`. Dedupe the stamps into a set per run, then add 1 (saturating at 255) to each cell in the set, so a value means "cleaned in N runs".
- A sparse store is fine, such as a dict of `(x, y) -> count` or fixed tiles. Don't allocate a dense array from `bounds()`, since one bad point would make it enormous. Drop points outside ±100,000 units for the same reason.
- `add_run` returns the cells covered; pass them back as `already` if the run grows. Also available: `count(x, y)`, `cells()`, `stamp(segments)`.
- `from_bytes` raises `ValueError` on corrupt data (it reads our own file, not the device): the coordinator catches it and starts empty.
- `to_bytes` must round-trip and be compact: zlib over a simple packed form, with a leading version byte so the format can change later.
- Expect up to about 160,000 cells (a 20 × 20 m home). Stamping a whole run must stay under 1 s on a Raspberry Pi class CPU, so precompute the disc's cell offsets once.

### `render.py`

```python
@dataclass(frozen=True)
class RenderInput:
    grid: CoverageGrid
    last_run: list[list[tuple[int, int]]]
    rooms: list[Room]
    hidden_rooms: frozenset[int]
    position: tuple[int, int] | None


def render_svg(data: RenderInput) -> bytes: ...
```

- **Flip y.** SVG y grows downwards and device y grows upwards. A helper does this in one place: `sx = x - min_x`, `sy = max_y - y`, in device units, with `viewBox` set from the bounds plus a margin.
- **Don't emit one element per cell.** Group cells into 3 or 4 bands by count, merge each row into runs, and emit one `<path>` per band:

  ```xml
  <path fill="#4a90d9" fill-opacity=".35" d="M100 200h450v50h-450zM100 250h600v50h-600z"/>
  ```

- Then the last run as a thin `<polyline>` per segment and the position as a small circle. No room labels yet: the room list has no positions. Phase 5 adds them from the learned outlines. Until then the visible room names go in a `<desc>` element (which is also where the escaping test has something to bite on).
- Skip rooms in `hidden_rooms` once labels exist. Some detected rooms are places the vacuum never goes, like a balcony.
- **Escape room names with `xml.sax.saxutils.escape`.** They come from the network.
- Keep colours readable on both light and dark dashboards: transparent background, and labels with a contrasting outline (`paint-order: stroke`).
- With no data, return a valid small SVG saying that no runs are recorded yet.
- Keep a whole-home map under 500 KB. If it isn't, merge rows further or double the render cell size (`render_svg` doubles it up to three times). The SVG has a `viewBox` and no `width` or `height`: check in phase 4 how the card sizes it.

**Tests:** overlapping and repeated chunks; gaps and jumps produce separate segments; a repeated run isn't double counted; grid round-trip; one absurd point doesn't blow up memory; the y flip, with a point known to be top right; hostile room name `<script>` comes out escaped; output parses with `xml.etree`; size budget against a simulated 3000-point run. Save one rendered sample to `docs/`, drawn from a simulated run and not a real home, so a person can look at it.

**Done when:** those pass and a person has looked at the sample image.

## Phase 3: connection and setup (`connection.py`, `config_flow.py`)

### `connection.py`

A dedicated daemon `threading.Thread`, one per device, not an executor job, since it runs forever. Shape of the loop, based on `tools/capture.py`:

```python
def _run(self) -> None:
    backoff = 5
    while not self._stop.is_set():
        try:
            device = tinytuya.Device(
                self._id, self._host, self._key, version=self._version, persist=True
            )
            device.set_socketTimeout(5)
            first = device.status()
            self._check(first)  # raises on an error reply
            self._on_connected(True)
            backoff = 5
            self._deliver(first)
            last_beat = time.monotonic()
            while not self._stop.is_set():
                message = device.receive()
                if message:
                    self._check(message)
                    self._deliver(message)
                if time.monotonic() - last_beat > HEARTBEAT_SECONDS:
                    device.heartbeat(nowait=True)
                    last_beat = time.monotonic()
        except Exception as err:
            _LOGGER.debug("Connection lost: %s", type(err).__name__)
        self._on_connected(False)
        self._stop.wait(backoff)
        backoff = min(backoff * 2, 60)
```

- Check the `_check` behaviour against tinytuya's source: it can return an error dict rather than raise. Treat a dict containing `Err` as a lost connection, but look at which codes are just a timeout (`receive()` returning `None` is the normal idle case, and must not reconnect).
- Set the payload limit once at import, in one place (see PROTOCOL.md), to 65536.
- Deliver to the event loop with `hass.loop.call_soon_threadsafe`. Decode in the event loop or the thread, but never block the loop.
- On unload, set the stop event and join with a timeout. Close the socket.
- **Never send anything except `status()` and `heartbeat()`.** Add a test that uses a fake `Device` and fails on any other method that writes.
- Log the first failure at warning level and the rest at debug, so a vacuum that's off the network doesn't flood the log. Use HA's `async_create_issue` if the connection stays refused for a long time: the likely cause is a device that only takes one connection, and the message should say so.

### `config_flow.py`

Fields: host, device ID, local key, protocol version (3.1, 3.3, 3.4, 3.5, default 3.3).

- Validate by connecting and calling `status()` in an executor. Errors: `cannot_connect`, `invalid_auth` (wrong key), and `already_configured` using the device ID as the unique ID.
- Explain in the form text where to get the local key, and that the vacuum must allow a second connection if another integration is using it.

Options flow: hidden rooms, brush width (default 300), units per metre (default 1000).

**What the code settled (written against tinytuya 1.20.0's source):**

- Failures come back as `{"Err": "<code>"}` with the code as a *string*, not as exceptions. In receive mode a timeout is `None`, and no `Err` code means idle: `ERR_TIMEOUT` (902) is only returned on the send path (`status()`), so any `Err` seen while receiving is a real failure and reconnects.
- `914` (key or version) and `904` (payload that won't decode, which `status()` itself blames on key or version) mean a wrong key or protocol version: the flow shows `invalid_auth`. Every other code, `None`, and any exception show `cannot_connect`.
- `connection.py` imports nothing from Home Assistant and takes callbacks that run on its thread. `hub.py` does the handover with `call_soon_threadsafe`, so the thread can be tested with a fake `Device` and no HA.
- The device is wrapped (`ReadOnlyDevice`) so that only `status`, `heartbeat`, `receive`, `set_socketTimeout` and `close` can be called; anything else raises. A test also checks that those names exist on the real `tinytuya.Device`.
- A connection that worked and then dropped resets the backoff to 5 s. (A test caught the first version never resetting it.)
- The repair issue is raised after 600 s without a connection (`STALLED_AFTER`) and removed on recovery or unload. The thread is a daemon, and `stop()` closes the socket to wake a blocked read, so unload doesn't wait out the 5 s socket timeout.
- `hub.py` keeps only what the flows need (connection state, rooms merged by ID, a count of frames by type). Phase 4's coordinator takes over the routing. The options flow lists the rooms from the hub, and rooms already hidden stay selectable.
- `tinytuya` is imported in the import executor (`async_add_import_executor_job`), not in the event loop.
- **Found by the fake-vacuum end-to-end tests (`tests/fake_vacuum.py`, `tests/ha/test_e2e.py`), which use the real tinytuya over a real socket:** an idle `receive()` blocks for the whole socket timeout, so with the first design heartbeats went out only every 5 to 10 s and stopping took about 10 s; closing the socket from another thread made tinytuya reconnect instead of stop; and a wrong host took 20 s to report `cannot_connect` (tinytuya retries 5 times, 5 s apart). Now the loop uses a 2 s receive timeout (set after the first status), retries are 2 tries a second apart, and stopping only sets the flag. The fake vacuum also records every command it receives, and the tests assert that only status queries and heartbeats ever arrive.
- Not done: nothing has run against a real vacuum or a real Home Assistant instance. The fake speaks protocol 3.3 only (3.4 and 3.5 need a session-key handshake it doesn't implement). The fakes follow tinytuya's source, not its behaviour on the wire.

**Tests** (`pytest-homeassistant-custom-component`, fake `Device`): flow success and each error; reconnect after a failure; error dict handled; clean unload stops the thread; read-only test above.

**Done when:** those pass, and the integration holds a connection on the real vacuum for 24 h with the owner's OK, alongside localtuya, with neither one disconnecting more than before.

## Phase 4: entities and storage (`coordinator.py`, entities)

**First, check the image format** (see phase 0): SVG in a picture-entity card, in the app and the browser, before building the rest on it.

**Coordinator.** Takes each decoded frame and routes it: path chunks to `RunAssembler`, room lists merged per room ID, room commands kept for the current run. Finishes runs, and resets the grid when `mapId` changes.

**Rate limits, which matter at 4 messages a second:**

- Update entities at most once per second.
- Re-render the image at most every 30 s during a run, and once at the run's end. Render in an executor.
- Save 30 s after a change, and always on run end and on unload. It uses an own 30 s timer, not `Store.async_delay_save`: that calls its data function on the event loop, and compressing a whole-home grid takes about 80 ms here (more on a Pi). The grid is copied on the loop (3 ms) and encoded in the executor.

**Storage** (`homeassistant.helpers.storage.Store`, one per config entry): grid bytes (base64), merged rooms, a **salted hash of `mapId`** (not the `mapId`: PROTOCOL says never store it, and a hash is enough to notice a change, also after a restart), recent finished run IDs, the last run's segments, and the previous grid for one generation. The store is deleted when the entry is removed. Include a version, and handle a missing or corrupt store by starting empty with a warning.

**Entities.** All on one device, with `has_entity_name`.

| Entity | State | Notes |
|---|---|---|
| `image` Coverage map | Image, updated timestamp | SVG (PNG if the check above fails) |
| `sensor` Rooms | Number of rooms | Attribute `rooms`: list of ID and name. For building room buttons later |
| `sensor` Run points | Points in the current or last run | Attributes `path_id`, `started` |
| `sensor` Covered area | m², from cells and units per metre | Diagnostics category |
| `binary_sensor` Connected | Whether the socket is up | Connectivity device class |
| `button` Reset map | | Clears local data only, sends nothing to the vacuum. Disabled by default |

- **Keep frames and paths out of state attributes.** HA's recorder would write them to the database many times a second. Mark anything sizeable in `_unrecorded_attributes`.

**Diagnostics.** Redact key, device ID, host and `mapId`, and leave out room names and coordinates. Include counts per `infoType`, unknown `infoType`s seen, the largest frame size, and reconnect count. This is how support requests for new models will get answered, so also include the field names (without values) of each unknown type.

**What the code settled:**

- The tracker counts a run when it goes quiet for 120 s or a new run starts. Run IDs are kept after a reset, so the app re-sending the run that was just wiped isn't counted again.
- A new map (a different `mapId` hash) starts an empty grid and forgets the rooms, keeping the old grid for one generation. The first `mapId` ever seen does not reset, so coverage from before the first room list is kept.
- The room command is held in `run_rooms` until the run ends (phase 5 uses it).
- Unknown `infoType`s are counted, and their field names (never values) go in diagnostics.
- Entities update at most once a second, the image is redrawn at most every 30 s (the first change after a quiet spell redraws at once) and when a run ends, and a burst of 200 frames produced fewer than 12 state changes in a test.
- `async_remove_entry` deletes the stored map: it shows the layout of a home.
- The live position is still not drawn (phase 5), and `Room` positions are still unknown.
- **The SVG in a picture card, checked in a real browser.** A real Home Assistant 2026.2.3 process (installed in a scratch venv, with the frontend), the integration set up through the config-flow REST API against the fake vacuum, a `picture-entity` card on a dashboard, and headless Chromium (Playwright) logged in as a user. The image request returned `200 image/svg+xml`, the `<img>` loaded, and the map, the room label, the trail and the position dot painted in the light theme, the dark theme and a phone-sized viewport (iPhone 13 emulation). An SVG with a `viewBox` and no `width` or `height` scales to the card width, so nothing needs changing. The map and the sensors also survived four restarts of that Home Assistant. **Not covered:** the native Home Assistant app (its webview is a different engine) and a real vacuum.
- **The recorder, measured in that same run:** the largest attribute blob stored for any of the entities was 101 bytes, so no frames, paths or room lists reach the database. But `run_points` wrote a state row about once a second while a run was under way (60 rows in 2.5 minutes), which is more than a normal sensor does. It now shows a fresh count at most every 10 s during a run (`RUN_POINTS_INTERVAL`), and is exact when no run is active and when a different run starts, so a minute of cleaning writes about 6 rows, not 60. The state is only as fresh as the last notification, which is fine here: the count is for looking at, not for automations.
- **A soak test (`tests/ha/test_soak.py`)** runs the real connection thread and tinytuya against the fake vacuum at ten times a real vacuum's message rate, with the connection dropped every 6 s. Ten minutes gave 34 runs, 15,949 messages and 94 dropped connections (each recovered, one warning per outage, no errors); threads and tasks stayed flat; there was one drawing and one save per run; entity updates came to about 1.3 a second (at most one a second for frame traffic, plus two immediate ones per connection change); and the process used about 3 % of one core. Memory read 200, 204, 208 and 209 MB at the quarters, a rise that slows. An allocation trace over 5 minutes (`SOAK_TRACEMALLOC=1`) found growth only in things that are bounded by design (the cells of the last three runs, the room votes, which saturate) or that the test harness itself keeps (the latest saved copy). Nothing grew without bound, but ten minutes is not a day: **the 24-hour hold on the real vacuum is the real soak**, and it should record memory at the start and end. The default run in CI is 15 s; `SOAK_SECONDS=600 SOAK_RATE=40` is the long one.

**Tests:** frames through to entity state; restart restores the map; `mapId` change resets it; rate limits with a burst of frames; diagnostics contain nothing redacted; a corrupt store starts empty.

**Done when:** on the real vacuum, a complete run shows on a dashboard, survives an HA restart, and the recorder database grows no more than a normal sensor would.

## Phase 5: better labels and accuracy

1. **Calibration check.** Compare covered area with what the vacuum reports for a run (`cur_clean_area` on the Rowenta, `cleanArea` in 20001). Report the ratio in diagnostics. Only then decide whether units are really mm. If a device's scale differs, the units-per-metre option handles it. Coverage will read a bit higher than reported, since the brush estimate isn't exact: expect agreement within about 25%, not exactness.
2. **Room outlines.** A run started with `segmentId: [7]` only covers room 7, so its cells can be attributed to that room. A multi-room run only tells us the union, so use single-room runs first, and subtract already-known rooms for multi-room ones. Store this as a cell → room layer, drawn as a boundary or tint, and put each room's label at the centre of its cells. The room list gives names and IDs but no positions or shapes, so this is the only source of both. Rooms the vacuum never cleaned alone stay unknown, and unlabelled.
3. **Current room sensor**, from those outlines, falling back to `unknown`. The nearest label is too rough near boundaries, so don't use that.
4. **Live position** on the image, from the latest point, at the same 30 s render rate. A faster overlay would need a custom card, which is out of scope here.

**What is built (code and tests only; none of it has met a real run):**

1. **Calibration.** The coordinator keeps the latest `cleanArea` from a 20001 status (`parse_status`, **[Reported]** on the Nova only) and, each time a run is counted, records the map's area for that run against it: `map_area`, `reported_area` and `ratio` (the last 5, in diagnostics). It does not read DP 108 (`cur_clean_area`): that is the Rowenta's own DP, and `core/` and the coordinator recognise frames by content, not DP number (AGENTS.md rule 5). So on the Rowenta there's no ratio until DP 108 gets a reader. Units of the reported number are still unknown, and so is whether the units are mm.
2. **Room outlines (`core/rooms.py`, `RoomLayer`).** Each finished run gets the room command that came before it (kept for 120 s, bound to the first run that appears after it, so two commands close together go to their own runs). A one-room run gives that room's cells a vote. A multi-room run counts only when all rooms but one already have cells, and then only the cells nobody else owns go to the remaining room. A cell belongs to the room with the most votes (ties to the lower ID), so the strip a vacuum drives through other rooms to reach a room is outvoted by those rooms' own runs. This is a heuristic with no real data behind it: **check it against real runs first** (does transit leave stray strips? do ties matter?). Stored with the map, drawn as a tint per room with the name at the owned cell nearest the centre of the room (rooms under 20 cells get no label), and hidden rooms are skipped.
3. **Current room sensor.** The learned room under the vacuum while a run is under way; `unknown` otherwise, or where nothing is learned. Hidden rooms are still named here.
4. **Live position.** The newest point (by index) of a run that hasn't gone quiet, drawn as a dot at the 30 s render rate.

**Done when:** each one is checked against a real run, or dropped from this list with a reason noted here.

## Phase 6: ready for others

**Done in this repo:**

- `brand/icon.png` (256 px) and `icon@2x.png` (512 px), drawn by `tools/make_icon.py` from made-up shapes.
- The [HACS action](https://www.hacs.xyz/docs/publish/action/) in CI (`hacs` job), next to hassfest. The action refuses private repositories (it said so on the first run), so the job is skipped while the repo is private (`if: ${{ !github.event.repository.private }}`) and starts running once it is public. **A skipped job shows as passing, but HACS validation has not happened until it runs.** It also needs a repository description and topics, which are repository settings and not files. Expect to adjust it on its first real run.
- `hacs.json` names the Home Assistant version it was tested on (2026.2.0) as the minimum. That is the tested version, not a measured lower bound.
- README: what it does and doesn't, install, where to get the local key, options, the map card, limits, which vacuums, how to report a model with the diagnostics file, privacy, developer commands. It carries the simulated sample and says so.
- Issue templates: a new-model report that asks for the diagnostics file and warns off keys and captures, and a bug report.
- Tests that the icons are PNGs of the right size, `strings.json` equals `translations/en.json`, every entity key has a translation, and the README's relative links resolve.
- `tools/check_private.py` no longer reads image names like `icon@2x.png` as an email address.

**Not done, waiting on a tested integration (the owner decides when):**

- **A full release.** `0.1.0-alpha.1` is tagged as a pre-release so the real-vacuum test (docs/TESTING.md) runs on a known version. Tag a full release only after the 24-hour hold and a real run, with `version` in the manifest set to match the tag.
- **Repository settings** HACS needs: description and topics (such as `home-assistant`, `hacs`, `tuya`, `vacuum`). Public or private is the owner's call (AGENTS.md).
- **Outward steps, only after testing and only with the owner's OK:** ask whether a Kabum Smart 900 owner (MaikeMota, on [#900](https://github.com/xZetsubou/hass-localtuya/issues/900)) would test it, reply on [tuya-local #288](https://github.com/make-all/tuya-local/issues/288) that it exists, and submit to the HACS default list. The text gets shown to the owner before anything is posted (AGENTS.md rule 7).

## Later, not planned yet

See [IDEAS.md](IDEAS.md): a Lovelace card, a localtuya event source, zones, room commands, and other things that are written down and not planned.

## Risks

| Risk | What to do |
|---|---|
| Other models don't accept a second connection | Clear repair issue explaining it, then the localtuya event source above |
| Another integration updates tinytuya past our range | Range upper bound is `<2`, so only a major version conflicts. Watch for it |
| Firmware changes the message format | Decoder never raises, and diagnostics show unknown types |
| SVG doesn't display everywhere | Checked in headless Chromium in a real Home Assistant (light, dark, phone-sized); the native app is not checked. PNG is the fallback |
| Room IDs change on a remap | `mapId` change resets the data |
| `mapId` changes without a real remap, and wipes coverage | Keep the previous grid for one generation in storage, so it can be restored by hand |
