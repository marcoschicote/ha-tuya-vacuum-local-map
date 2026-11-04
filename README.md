# Tuya Vacuum Local Map

A Home Assistant integration that draws where a Tuya robot vacuum has cleaned, using only its local connection. No cloud, no Tuya account.

> **Status: not implemented yet.** This repo holds the scaffolding and the plan. Don't install it. See [docs/PLAN.md](docs/PLAN.md).

## What it will do

- Listen to the vacuum on the local network and read the cleaning path and room list it already sends.
- Build a coverage map across runs: shaded by how often each spot gets cleaned, with room labels and the latest path.
- Show the map as a Home Assistant image entity, plus sensors for rooms and the current run.

## What it won't do

It isn't a floor plan. Many Tuya vacuums only upload their floor plan to the cloud, so this shows where the vacuum went, not where the walls are. Under furniture and behind a closed door look the same. For the cloud floor plan, see [Tuya Vacuum Maps](https://github.com/jaidenlabelle/tuya-vacuum-maps).

## Which vacuums

Vacuums in the Tuya family that send base64 JSON messages with an `infoType` field on raw data points. Seen so far:

| Vacuum | Evidence |
|---|---|
| Rowenta X-plorer (category `sd`, protocol 3.3) | Captured locally; the integration is being built against it |
| Kabum Smart 900 (category `sd`, protocol 3.3) | [Reported](https://github.com/xZetsubou/hass-localtuya/issues/900) |
| iHome AutoVac Nova | [Reported](https://github.com/make-all/tuya-local/issues/1660) |

Vacuums using Tuya's binary protocol (`path_data` on DP 14, `command_trans` on DP 15) aren't covered.

## Docs

- [docs/PLAN.md](docs/PLAN.md): what to build, in order
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the message formats, and how sure we are of each
- [AGENTS.md](AGENTS.md): rules for working in this repo

## License

MIT
