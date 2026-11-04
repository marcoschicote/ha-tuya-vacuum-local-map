# Working in this repo

Start with [docs/PLAN.md](docs/PLAN.md), then [docs/PROTOCOL.md](docs/PROTOCOL.md). Work through the plan's phases in order.

**Work as if this repo were already public.** It's private for now, but it will go public with its full history, so assume everything pushed here gets read and copied, and that deleting it later doesn't undo that. Git history, issues, PRs, CI logs and screenshots all count. Only the owner decides when it goes public.

## Rules

1. **Nothing private goes in the repo.** That means device IDs, local keys, IP addresses, Tuya user IDs, `mapId` values, real room names and real coordinates from someone's home. It applies to code, fixtures, docs, commit messages, issues, PRs, test output and images. Anonymise captures before they become fixtures (PLAN, phase 0). Raw captures stay in `captures/`, which is git-ignored. If something private gets pushed, stop and tell the owner: a key may need rotating and history rewriting, and that's their decision.
2. **Read-only towards the vacuum.** The integration may ask for status and send heartbeats. It must never set a data point or send a command. Sending anything else needs the owner's explicit approval first, every time, including in a one-off test: a command can start the vacuum, and DP 129 wipes the map.
3. **`core/` has no Home Assistant imports.** A test enforces this. Everything that decodes, assembles or draws lives there and gets tested without Home Assistant. The HA glue on top stays thin.
4. **Never log the local key.** Redact it, the device ID and the host in diagnostics.
5. **Don't tie anything to one vacuum.** DP numbers differ by maker, so recognise frames by content (PROTOCOL, "Recognising a frame"). If a behaviour was only seen on one model, say so in a comment.
6. **Mark what you verify.** When a capture confirms or disproves something in `docs/PROTOCOL.md`, update its confidence label in the same change.
7. **Ask before posting elsewhere.** Show the owner the text before posting to another project's issues or PRs.
8. **Run the privacy check before every commit** (below), and look at any image yourself before adding it, since the check can't read images. Screenshots and sample maps must use made-up data: a real map shows the layout of someone's home.

## Checks

```sh
uv venv && uv pip install -e ".[test]"
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
.venv/bin/python tools/check_private.py
```

`check_private.py` scans tracked and staged files, commit messages and git emails for things that look like private data. A fresh clone needs `git config user.email` set to a GitHub noreply address first, since a machine's default email can reveal its hostname. CI runs it too, but by then it's already public, so run it first. For the owner's own values, it also reads `.private-terms`, a git-ignored file with one term per line. Never commit that file, and never copy its contents anywhere.

## Lessons from setting this up

Things that nearly went wrong, kept here because they will again. None of them involve real values, and none should.

- **A machine's default git email can reveal its hostname and network name.** It lands in every commit and in public searches by email. Use a GitHub noreply address, set in `git config --global user.email`, before the first commit. The check fails on any other email.
- **Rewriting history is not an undo.** A pushed commit can stay reachable by its ID for a while. Prevent leaks instead of cleaning them up, and let the owner decide how to clean up.
- **Scripts written against a real device tend to have its ID, host or key hardcoded.** Take them from environment variables, as `tools/capture.py` does.
- **Command output ends up in logs and transcripts.** Don't print device IDs, keys or hosts, not even while debugging.
- **Shifting or scaling a real map still shows the home's layout.** Generate made-up coordinates for fixtures and samples.
- **Look at the real thing before writing about it.** Several message shapes here came from notes, and the raw captures were gone. Keep anonymised fixtures, so the formats stay checkable.

Home Assistant tests (phase 3 onwards) also need `pytest-homeassistant-custom-component`.
