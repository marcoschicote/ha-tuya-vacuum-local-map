# Contributing

Thanks for helping. The most useful thing is often a report from a vacuum we haven't seen: see the [new model issue template](https://github.com/marcoschicote/ha-tuya-vacuum-local-map/issues/new/choose), and [README, "My vacuum isn't drawn right"](README.md#my-vacuum-isnt-drawn-right-or-isnt-recognised). Don't paste raw captures or your local key into an issue: they hold the layout of your home and the keys to your vacuum.

## Set up and check

```sh
uv venv && uv pip install -e ".[test]"
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
.venv/bin/python tools/check_private.py
```

Run all four before you open a pull request. `check_private.py` looks for device IDs, keys, addresses and other private values in what you are about to commit. CI runs the checks too, but by then the change is already public.

## Where things are

- [AGENTS.md](AGENTS.md): the rules for working in this repo. They apply to everyone, not only to AI assistants. The main ones: nothing private goes in the repo, the integration never sends anything to the vacuum, and `core/` has no Home Assistant imports.
- [docs/PLAN.md](docs/PLAN.md): what was built, in order, and what is still unchecked.
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the message formats, and how sure we are of each.
- [docs/TESTING.md](docs/TESTING.md): how to test on a real vacuum, a table of results, and how releases are cut.
- [docs/IDEAS.md](docs/IDEAS.md): things that came up and are not planned, including ones that were tried and not built.
- `custom_components/tuya_vacuum_local_map/core/` has no Home Assistant imports and is tested without it.
- `tools/make_sample_map.py` and `tools/make_icon.py` regenerate the sample image and the icon from made-up data.
