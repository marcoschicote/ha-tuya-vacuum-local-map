# Ideas

Things that came up and are **not planned**. Nothing here is a commitment, and none of it is in scope for [PLAN.md](PLAN.md). It is written down so it isn't lost.

## Out of scope on purpose

- **Floor plans.** Many Tuya vacuums only upload their floor plan to the cloud, so this project shows where the vacuum went, not where the walls are. For the cloud floor plan, see [Tuya Vacuum Maps](https://github.com/jaidenlabelle/tuya-vacuum-maps).
- **Smoothing the coverage edges.** The map's edges are stair-steps, since it is a grid of 5 cm cells. On 2026-11-08 the owner tried rounding them on a real map, by putting a blur and a threshold filter over each coverage band (done outside the repo, viewed in Chrome only). It cleaned up the speckle along the edges and made no other visible difference. It also closes narrow gaps between strips, which hides real detail, and it adds no wall information. It wasn't built. Tracing and rounding each outline in `core/` would give the same look in every renderer, if someone wants it later. It came up in [#19](https://github.com/marcoschicote/ha-tuya-vacuum-local-map/issues/19), where a map built from Tuya's cloud data looks better because it has walls.
- **Vacuums on the binary protocol family** (`path_data` on DP 14, `command_trans` on DP 15). A different set of messages; none was captured here.

## Ideas

- **A Lovelace card** for the live position and tapping a room. It would be a separate repository, as HACS requires for frontend cards (`tuya-vacuum-local-map-card`, with `tuya-vacuum-local-map-card.js`). The integration only redraws the image every 30 s during a run, so anything faster than that needs a card.
- **A localtuya event source** for vacuums that only allow one local connection. localtuya fires `localtuya_device_dp_triggered` with `dp` and `value`, but only when exactly one DP changed in a message, and only once [#901](https://github.com/xZetsubou/hass-localtuya/pull/901) has shipped. Whether raw DPs without a configured entity get through is untested. It would sit behind the same interface as `connection.py`.
- **Zones from the 21003 echo.** When the app starts a zone clean it sends every zone and virtual wall, and the vacuum echoes that back (**[Reported]** on the Kabum, shape unknown). It is the only local way to learn zones.
- **Room commands.** Starting a clean for chosen rooms is the first thing that would write to the vacuum, so it needs the owner's explicit approval (AGENTS.md rule 2), starting with one approved test. One report on a vacuum in the binary family says room commands get overwritten unless they carry a session token from Tuya's cloud ([tuya-local discussion #5556](https://github.com/make-all/tuya-local/discussions/5556)), so sending may not work here either.
- **Protocol versions 3.4 and 3.5.** The connection is written for them, but the fake vacuum in the tests only speaks 3.3. It would need the session-key handshake.
- **PNG output** (via Pillow) if an SVG doesn't show in some dashboard or app.
- **More than one vacuum** in a home, with the maps kept apart. Today each config entry is one vacuum, so this may already work: untested.
- **Translations** beyond English.
