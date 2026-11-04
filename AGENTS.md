# Working in this repo

Start with [docs/PLAN.md](docs/PLAN.md), then [docs/PROTOCOL.md](docs/PROTOCOL.md). Work through the plan's phases in order.

## Rules

1. **Nothing private goes in the repo.** That means device IDs, local keys, IP addresses, Tuya user IDs, `mapId` values, real room names and real coordinates from someone's home. It applies to code, fixtures, docs, commit messages, issues and PRs. Anonymise captures before they become fixtures (PLAN, phase 0). Raw captures stay in `captures/`, which is git-ignored.
2. **Read-only towards the vacuum.** The integration may ask for status and send heartbeats. It must never set a data point or send a command. Sending anything else needs the owner's explicit approval first, every time, including in a one-off test: a command can start the vacuum, and DP 129 wipes the map.
3. **`core/` has no Home Assistant imports.** A test enforces this. Everything that decodes, assembles or draws lives there and gets tested without Home Assistant. The HA glue on top stays thin.
4. **Never log the local key.** Redact it, the device ID and the host in diagnostics.
5. **Don't tie anything to one vacuum.** DP numbers differ by maker, so recognise frames by content (PROTOCOL, "Recognising a frame"). If a behaviour was only seen on one model, say so in a comment.
6. **Mark what you verify.** When a capture confirms or disproves something in `docs/PROTOCOL.md`, update its confidence label in the same change.
7. **Ask before anything public.** Show the owner the text before posting to another project's issues or PRs, and before making this repo public.

## Checks

```sh
uv venv && uv pip install -e ".[test]"
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

Home Assistant tests (phase 3 onwards) also need `pytest-homeassistant-custom-component`.
