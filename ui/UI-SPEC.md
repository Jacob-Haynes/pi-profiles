# ISR kit UI spec v3 (command post, British understated)

v3 (2026-11-17): `[hidden] { display: none !important; }`, so the `hidden` attribute hides any element,
including ones the kit gives a display (`.isr-tag`, `.isr-btn`). Nothing else changed from v2. Re-copy
`isr-ui.css` and `isr-ui.js` and link them as `?v=3` (`tools/sync-ui-kit.sh` does both).

The audience is a room watching a projector. Simplify and make every page look like one product.
The user's complaints: too much text, things hard to find, inconsistent look, confusing status.

## Shared files

- `isr-ui.css`, `isr-ui.js` and the `fonts/` folder (4 woff2 files plus their OFL licences) are the
  shared look. Copy them unchanged into your package's static folder (`<package>/static/`, keeping
  `fonts/` beside the CSS) and serve them. Never edit a copy: if the kit needs a change, say so
  and it is changed at the source (`pi-profiles/ui/`) and re-copied everywhere.
- Load them before your page's own CSS. Your own CSS should only handle layout specific to your tool.
  Delete the old colours, fonts and status-chip styles.
- No CDN or web fonts: the kit runs offline.
- Link them with the kit version as a query string so browsers drop old copies:
  `isr-ui.css?v=3` and `isr-ui.js?v=3` (bump with the kit version, i.e. the `vN` on line 1 of each file).
  Do the same for your own page CSS/JS (a version or content hash).

## Tone

British Army, not American: refined and understated. No camouflage, no stencil shouting, no
"tactical" decoration, no glow. Graphite and off-white; brass only for the one thing to act on;
red only for faults or hostiles. Let the content (imagery, video, the map) carry the page.
- British English throughout: colour, analyse, centre, metres, licence.
- Say ISTAR, not ISR, in anything on screen or in text a person reads (titles, the launcher's "ISTAR kit",
  SITREPs, help). Don't rename repos, services, env vars, MQTT topics, uids or files (ISR_*, isrkit/*).
- Military terms where they're natural (OP, SITREP, grid, DTG, callsign), no Americanisms
  ("ordnance" fine, "roger that"/"copy" not in UI text).
- Titles and buttons in normal case. Only the small section labels and status words are in
  spaced capitals (the CSS does that; don't type text in capitals).
- Numbers and grids in the mono font (`.isr-mono`, `.isr-grid-ref`); counts may be zero-padded.
- Colour for map objects uses the NATO APP-6 affiliation classes (`.aff.friend/hostile/neutral/unknown`).
- The header shows a small EXERCISE band and the Zulu DTG automatically (isr-ui.js).

## Every page

1. Header: `<header class="isr-bar">` with the tool name (`h1`, sentence case, no ALL CAPS),
   a sub-title of 2 to 4 words, one status pill (`#isr-status`) and the "Kit" link
   (`#isr-kit-link`) back to the launcher. See the comment at the top of `isr-ui.js` for the markup.
2. Status: one pill, one word (Ready / Degraded / Offline / Off). Feed it with
   `ISR.setStatus([...])`, one entry per check (AI, TAK, MQTT, camera, SDR …). Checks that the
   demo doesn't need are `optional: true`. Delete the row of separate status chips.
3. The first screen shows the one thing the tool does: the main input or live view and its main
   button (one `.isr-btn.primary` per screen), then the results. No scrolling to find the main action.
4. Text: at most one sentence of explanation visible. Everything else (help, privacy notes,
   model names, file paths, hints, diagnostics, advanced settings) goes into
   `<details class="isr-more"><summary>Details</summary>…</details>` or the status pill's list.
5. Keep the contract labels: `SIMULATED:`/`RECORDED:` in mesh and CoT text, and on screen the
   `.isr-tag.sim` / `.isr-tag.rec` badges (`SIM` / `REC`; `LIVE` with `.isr-tag.live`).
6. Words: sentence case, verb-first buttons ("Compare", "Locate", "Start"), no "please",
   no exclamation marks, no raw exception text (say what happened and what to do).
7. Big enough for a projector: body 16px, key numbers with `.isr-big`, buttons at least 44px tall.
   Must still work on a phone (the CSS handles narrow widths).
8. Banners only for something the operator must act on now (`.isr-banner`, `.isr-banner.bad`).

## Don't change

Routes, APIs, ports, MQTT/CoT behaviour, config, tests of behaviour. Update tests that assert on
HTML text you changed. Keep JS dependency-free.

## Done means

- Pages use the kit (v2, fonts included), the header and the single pill; old styles are gone.
- Existing tests pass; add a test that the page links `isr-ui.css` and `isr-ui.js` and that the
  served copies are byte-identical to the kit (sha256 in the test is fine).
- A screenshot of each page at 1280x800, before and after, saved under
  `~/Repos/isr-kit/ui-kit/screens/<repo>-<page>-{before,after}.png`.
- Commit on your existing feature branch and push. Don't merge, don't open PRs.
