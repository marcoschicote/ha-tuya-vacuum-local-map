# Testing on a real vacuum

The automated tests use a fake vacuum (`tests/fake_vacuum.py`). They can't show what a
real model sends or how it behaves over a day. Run this before a release, after a
`tinytuya` upgrade, and whenever someone tries a new model.

Everything here is **read-only**: the integration asks for status and sends heartbeats,
nothing else (AGENTS.md, rule 2). Don't start, stop or change anything on the vacuum
from this integration. Start runs from the vacuum's own app, as you normally would.

Nothing private goes into the repo, issues or PRs: no device ID, local key, host, `mapId`,
room names or real coordinates, and no screenshot of a real map (AGENTS.md, rule 1).

## Before you start

- Home Assistant 2026.2 or newer, and the vacuum's local key, device ID, IP address and
  protocol version (README, "Set up").
- Install the integration (README, "Install"). Releases so far are pre-releases (for
  example `0.1.0-alpha.1`), which HACS lists only if "Show beta versions" is on for this
  repository. Without it, copy `custom_components/tuya_vacuum_local_map` from the tag you
  are testing into Home Assistant's `custom_components` folder instead. Restart afterwards.
- Note the version you are testing (Settings, Devices & services, the integration's
  page, or `manifest.json`), for the results table.
- If LocalTuya or Tuya Local already holds the vacuum's connection, some models accept
  only one local connection. Note whether both work together on yours.
- Note Home Assistant's memory use now (Settings, System, Repairs, or the host's own
  monitor), for step 6.

## Steps

Each step says what good looks like. Write down what you see, even when it's fine.

1. **Connect.** Add the integration with the vacuum's details.
   Good: the form accepts it, the entities appear, and the connection sensor is on.
   If it says the key or version looks wrong, try protocol 3.4 or 3.5 before assuming a bug.
2. **Idle.** Leave it a few minutes with the vacuum docked.
   Good: stays connected, no repair issue, no errors in the log naming the key, ID or host
   (none should appear at all).
3. **One run.** Start a clean from the vacuum's own app and watch.
   Good: the map image starts drawing within a minute or so, the trail follows the real
   path, the live position moves, and the run points sensor climbs in steps.
4. **Run end.** Let the run finish and dock.
   Good: the run is counted once, the map keeps its coverage, and the last run's trail is
   still shown.
5. **The picture.** Open the image entity on a dashboard in light and dark themes and on a phone.
   Good: readable, correctly oriented (up on the map is up in the home), and under a
   second to load.
6. **A day.** Leave it running for 24 hours with a run or two in it. Compare memory with
   the start.
   Good: memory roughly flat, no repair issue, a restart of Home Assistant keeps the map
   (check after restarting once), and the reconnect count in diagnostics is low.
7. **Units.** Open the diagnostics download and look at the map-area comparison.
   Good: the covered area is close to what the vacuum reports. If not, the units setting
   is probably wrong (README, "Units are a guess").

## Changes to check on the way

Changes since `0.1.0-alpha.1` that the automated tests cover only with the fake vacuum.
Each one is a question to answer in the steps above, or in the notes column of the
results table.

- **Several runs in one session** (step 4, and `core/runs.py`): after a few runs, each
  run is still counted once, and the last run's trail is still the one just finished.
- **Position dot** (step 3): it follows the vacuum during a run and stays where the
  vacuum stopped once it's docked.
- **Run start time** (sensor "Run points", `started` attribute): a real ISO time, not
  missing or an error.
- **Calibration** (diagnostics, "reported area" and "ratio"): the ratio stays filled in
  after a status frame that has no area, instead of disappearing.
- **Largest frame** (diagnostics): the size of the decoded message, well under the
  65536 limit in `connection.py`. Note the number.
- **Room names with accents or symbols** (sensor "Rooms", `rooms` attribute): shown
  correctly, not as escape codes or question marks.

## What to capture

- The **diagnostics download** (Settings, Devices & services, Tuya Vacuum Local Map,
  three-dot menu, Download diagnostics). It leaves out the key, device ID, host, `mapId`,
  room names and coordinates. Read it before sharing it anyway.
- For a message the integration doesn't recognise: use `tools/capture.py`, with the
  device details in environment variables. Raw captures stay in `captures/` (git-ignored).
  Before one becomes a fixture, anonymise it (PLAN, phase 0), and update the confidence
  labels in `docs/PROTOCOL.md` that it confirms or disproves (AGENTS.md, rule 6).

## Results

One row per model and run of this procedure. Add yours with a pull request or an issue
from the new-model template. Keep it to what happened, with no private values.

| Model | Version | Protocol | Date | Result | Notes |
| --- | --- | --- | --- | --- | --- |
| Rowenta X-plorer (Aspi) | 0.1.0-alpha.2 | 3.3 | 2026-11-05 | Steps 1–5 pass; 6 and 7 not run yet | One room, run counted once: covered area 8.88 m² against 8.0 m² from the vacuum's own log. One room outline learned. No reported area on this model, so no calibration ratio. Connected, no reconnects, no errors. Map checked in light and dark themes. |
| Rowenta X-plorer (Aspi) | 0.1.0-alpha.2 | 3.3 | 2026-11-05 | Steps 3 and 4 pass; 5 partly (light and dark themes); 6 and 7 not run yet | Full house, second run, counted once: covered area 62.83 m² against 64.0 m² from the vacuum's own log (about 2% low). Outline count unchanged at 1. Connected, no reconnects, no errors. |

## Releasing

Releases are cut by hand with the **Release** workflow (Actions tab, Run workflow). It
takes a version such as `0.1.0` or `0.1.0-alpha.1`, checks that it matches `version` in
`manifest.json`, that `test` and `hassfest` passed on the commit, and that the tag is new.
It then tags the commit and publishes the release, as a pre-release if the version has a
`-`. Set the manifest version first, and cut a full release only after this procedure has
passed on at least one real model.
