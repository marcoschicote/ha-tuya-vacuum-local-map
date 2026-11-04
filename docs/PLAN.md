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
custom_components/tuya_vacuum_map/
  __init__.py        setup and unload of the config entry
  manifest.json
  const.py
  config_flow.py     host, device ID, local key, protocol version; options
  connection.py      worker thread around tinytuya
  coordinator.py     feeds frames into core, owns state and storage
  image.py           the map
  sensor.py          rooms, current run
  binary_sensor.py   connected
  button.py          reset map
  diagnostics.py
  strings.json, translations/en.json
  brand/icon.png
  core/
    protocol.py      frames and room names
    path.py          assembling chunks into runs
    grid.py          coverage grid
    render.py        SVG output
tests/
  fixtures/frames.json
  test_*.py
```

## Phase 0: confirm the formats (needs the owner)

The raw Rowenta captures weren't kept, so several shapes in PROTOCOL.md are only described. Do this before writing the decoder.

**Ask the owner** to run `tools/capture.py`, since it needs the local key and a vacuum run. It's read-only. Capture:

1. App map screen opened while docked, for the room list.
2. A short clean of one room, started from the app.
3. A clean of two rooms, for `segmentId`.
4. A room renamed in the app using an accented character, for the name encoding.
5. A complete run through to docking, for run end behaviour.

**Then:**

- Answer these and update the labels in PROTOCOL.md:
  - Is `autoAreaValue` under `data`? What shape is `pos`?
  - Is `cmds` under `data` in 30000?
  - What are the field names of the rectangles in `value`?
  - Do encoded names use Unicode code points?
  - Does the last-run re-send use the same `pathID` as the original run?
- Add anonymised fixtures to `tests/fixtures/frames.json`. To anonymise: replace `mapId` and names, and shift and scale coordinates so the home's layout can't be recovered. Keep the structure and sizes. Check the result with the owner before committing.
- **Spike the image format.** In a throwaway custom integration on a test HA, return an SVG from an `ImageEntity` (`_attr_content_type = "image/svg+xml"`) and check it displays in a picture-entity card, in the app as well as the browser. If it doesn't, use PNG via Pillow in `render.py` and update this plan.

**Done when:** every question above is answered and the spike has a result.

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
    name: str
    pos: tuple[int, int] | None


def decode_dp(dp: str, value: object) -> Frame | None: ...
def parse_path(frame: Frame) -> PathChunk | None: ...
def parse_rooms(frame: Frame) -> tuple[int, list[Room]] | None:  # (map_id, rooms)
    ...
def parse_room_command(frame: Frame) -> list[int] | None: ...  # segment IDs
def decode_name(name: str) -> str: ...
```

Rules:

- **Never raise on device data.** Anything malformed returns `None`. The vacuum's firmware is outside our control, and a bad message must not kill the connection, which is exactly the bug in #900.
- A non-string DP value returns `None` straight away. A value too short or not base64 returns `None` without logging.
- Allow extra fields, and missing `message` and `ts`.

**Tests:** every fixture; the real Nova frame in `fixtures/frames.json`; the name table in PROTOCOL.md; garbage input (not base64, base64 of non-JSON, JSON list, missing fields, wrong types, non-integer points).

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

- Store points by index (`dict[int, point]`), because chunks repeat and overlap. `points[chunk.start + i] = p`.
- A different `path_id` starts a new run. The caller finishes the old one first (see below).
- `segments()` splits the path where indexes are missing, since chunks are lost if the connection drops, and where consecutive points jump more than `MAX_JUMP` (1500 units). Never draw a line across a gap: it'd paint coverage through a wall.
- Ignore points whose index is far beyond `total`, as a sanity limit.

**When a run ends:** a new `pathID` arrives, or no new point has arrived for 120 s. Don't rely on state DPs, which differ by model. An idle gap is safe: if chunks resume with the same `pathID`, reopen the run, and let the grid handle it (next section).

**Re-sent runs:** a re-send of a finished run must not count as a second cleaning. Keep the IDs of the last 50 finished runs (persisted), and ignore chunks for those, except to add points that weren't received before.

### `grid.py`

```python
class CoverageGrid:
    def __init__(self, cell: int = 50, brush: int = 300) -> None: ...
    def add_run(self, segments: list[list[tuple[int, int]]]) -> None: ...
    def bounds(self) -> tuple[int, int, int, int] | None: ...
    def covered_cells(self) -> int: ...
    def to_bytes(self) -> bytes: ...
    @classmethod
    def from_bytes(cls, data: bytes) -> "CoverageGrid": ...
```

- Units are the device's own. `cell` and `brush` are in those units (50 and 300 are 5 cm and 30 cm if units are mm).
- Stamp a disc of `brush` diameter at steps along each segment, no further apart than `cell`. Dedupe the stamps into a set per run, then add 1 (saturating at 255) to each cell in the set, so a value means "cleaned in N runs".
- A sparse store is fine, such as a dict of `(x, y) -> count` or fixed tiles. Don't allocate a dense array from `bounds()`, since one bad point would make it enormous. Drop points outside ±100,000 units for the same reason.
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

- Then the last run as a thin `<polyline>` per segment, room labels at `pos` when known, and the position as a small circle.
- Skip rooms in `hidden_rooms`. Some detected rooms are places the vacuum never goes, like a balcony.
- **Escape room names with `xml.sax.saxutils.escape`.** They come from the network.
- Keep colours readable on both light and dark dashboards: transparent background, and labels with a contrasting outline (`paint-order: stroke`).
- With no data, return a valid small SVG saying that no runs are recorded yet.
- Keep a whole-home map under 500 KB. If it isn't, merge rows further or double the render cell size.

**Tests:** overlapping and repeated chunks; gaps and jumps produce separate segments; a repeated run isn't double counted; grid round-trip; one absurd point doesn't blow up memory; the y flip, with a point known to be top right; hostile room name `<script>` comes out escaped; output parses with `xml.etree`; size budget against a simulated 3000-point run. Save one rendered sample to `docs/` so a person can look at it.

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

**Tests** (`pytest-homeassistant-custom-component`, fake `Device`): flow success and each error; reconnect after a failure; error dict handled; clean unload stops the thread; read-only test above.

**Done when:** those pass, and the integration holds a connection on the real vacuum for 24 h with the owner's OK, alongside localtuya, with neither one disconnecting more than before.

## Phase 4: entities and storage (`coordinator.py`, entities)

**Coordinator.** Takes each decoded frame and routes it: path chunks to `RunAssembler`, room lists merged per room ID, room commands kept for the current run. Finishes runs, and resets the grid when `mapId` changes.

**Rate limits, which matter at 4 messages a second:**

- Update entities at most once per second.
- Re-render the image at most every 30 s during a run, and once at the run's end. Render in an executor.
- Save with `Store.async_delay_save`, 30 s delay, and always on run end and on unload.

**Storage** (`homeassistant.helpers.storage.Store`, one per config entry): grid bytes (base64), merged rooms, `mapId`, recent finished run IDs, the last run's segments. Include a version, and handle a missing or corrupt store by starting empty with a warning.

**Entities.** All on one device, with `has_entity_name`.

| Entity | State | Notes |
|---|---|---|
| `image` Coverage map | Image, updated timestamp | SVG, or whatever the spike chose |
| `sensor` Rooms | Number of rooms | Attribute `rooms`: list of ID and name. For building room buttons later |
| `sensor` Run points | Points in the current or last run | Attributes `path_id`, `started` |
| `sensor` Covered area | m², from cells and units per metre | Diagnostics category |
| `binary_sensor` Connected | Whether the socket is up | Connectivity device class |
| `button` Reset map | | Clears local data only, sends nothing to the vacuum. Disabled by default |

- **Keep frames and paths out of state attributes.** HA's recorder would write them to the database many times a second. Mark anything sizeable in `_unrecorded_attributes`.

**Diagnostics.** Redact key, device ID, host and `mapId`, and leave out room names and coordinates. Include counts per `infoType`, unknown `infoType`s seen, the largest frame size, and reconnect count. This is how support requests for new models will get answered, so also include the field names (without values) of each unknown type.

**Tests:** frames through to entity state; restart restores the map; `mapId` change resets it; rate limits with a burst of frames; diagnostics contain nothing redacted; a corrupt store starts empty.

**Done when:** on the real vacuum, a complete run shows on a dashboard, survives an HA restart, and the recorder database grows no more than a normal sensor would.

## Phase 5: better labels and accuracy

1. **Calibration check.** Compare covered area with what the vacuum reports for a run (`cur_clean_area` on the Rowenta, `cleanArea` in 20001). Report the ratio in diagnostics. Only then decide whether units are really mm. If a device's scale differs, the units-per-metre option handles it. Coverage will read a bit higher than reported, since the brush estimate isn't exact: expect agreement within about 25%, not exactness.
2. **Room outlines.** A run started with `segmentId: [7]` only covers room 7, so its cells can be attributed to that room. A multi-room run only tells us the union, so use single-room runs first, and subtract already-known rooms for multi-room ones. Store this as a cell → room layer, drawn as a boundary or tint. Rooms the vacuum never cleaned alone stay unknown.
3. **Current room sensor**, from those outlines, falling back to `unknown`. The nearest label is too rough near boundaries, so don't use that.
4. **Live position** on the image, from the latest point, at the same 30 s render rate. A faster overlay would need a custom card, which is out of scope here.

**Done when:** each one is checked against a real run, or dropped from this list with a reason noted here.

## Phase 6: ready for others

- `brand/icon.png`.
- Add the [HACS action](https://www.hacs.xyz/docs/publish/action/) to CI. It needs the repo to be public.
- README: install steps, screenshot (anonymised, no real home), how to get the local key, limits, how to report a new model using diagnostics.
- A release, with `version` in the manifest matching the tag.
- **Ask the owner first**, then: make the repo public, try a second model (MaikeMota reported a Kabum Smart 900 with the same messages on [#900](https://github.com/xZetsubou/hass-localtuya/issues/900), so ask whether they would test it), reply on [tuya-local #288](https://github.com/make-all/tuya-local/issues/288) that it exists, and submit to the HACS default list.

## Later, not planned yet

- A Lovelace card for live position and tapping rooms. A separate repo, as HACS requires: `tuya-vacuum-map-card` with `tuya-vacuum-map-card.js`.
- A localtuya event source for vacuums that only allow one connection. localtuya fires `localtuya_device_dp_triggered` with `dp` and `value`, but only when exactly one DP changed in a message and only once #901 has shipped. Whether raw DPs without a configured entity get through is untested. Put it behind the same interface as `connection.py`.
- Zones from the 21003 echo.
- Room commands, starting with one approved write test (AGENTS.md rule 2).

## Risks

| Risk | What to do |
|---|---|
| Other models don't accept a second connection | Clear repair issue explaining it, then the localtuya event source above |
| Another integration updates tinytuya past our range | Range upper bound is `<2`, so only a major version conflicts. Watch for it |
| Firmware changes the message format | Decoder never raises, and diagnostics show unknown types |
| SVG doesn't display everywhere | Decided by the phase 0 spike; PNG is the fallback |
| Room IDs change on a remap | `mapId` change resets the data |
| `mapId` changes without a real remap, and wipes coverage | Keep the previous grid for one generation in storage, so it can be restored by hand |
