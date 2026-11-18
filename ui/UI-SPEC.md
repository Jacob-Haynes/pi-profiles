# ISR kit UI spec v4 (command post, British understated)

v4 (2026-11-18): the Kit link opens in the launcher's named tab (`isr-kit`) instead of a new one.

v3 (2026-11-17): `[hidden] { display: none !important; }`, so the `hidden` attribute hides any element,
including ones the kit gives a display (`.isr-tag`, `.isr-btn`). Nothing else changed from v2. Re-copy
`isr-ui.css` and `isr-ui.js` and link them as `?v=4` (`tools/sync-ui-kit.sh` does both).

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
  `isr-ui.css?v=4` and `isr-ui.js?v=4` (bump with the kit version, i.e. the `vN` on line 1 of each file).
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
   (`#isr-kit-link`) back to the launcher. From v4 it opens in the tab named `isr-kit`, which the launcher gives its own tab, so it returns to the launcher tab instead of opening another (`window.ISR_KIT_SAME_TAB = true` navigates in place). See the comment at the top of `isr-ui.js` for the markup.
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

## Tabs (v4)

One launcher tab, one tab per tool, however many times people click:

- **Launcher tab.** The launcher names its tab `isr-kit` (`window.name`, set by `common.js` on the board, `/engineer`, `/map` and `/presenter`). Board, Map, Engineer and Presenter are plain links, so they stay in that tab.
- **Tool tabs.** Each **Open** targets `isr-tool-<unit>` (e.g. `isr-tool-op-sentry`) with `rel="opener"`, so opening a tool again reuses its tab.
- **Kit link.** The kit's Kit link targets `isr-kit` with `rel="opener"`, so it navigates the launcher tab instead of opening another one.
- **Why `rel="opener"`:** browsers look a tab up by name only within its browsing-context group. `noopener` (and `target="_blank"`, which implies it) puts the tool tab in a new group, where the launcher's tab can't be found.
- **A tool page opened by hand** (typed URL, bookmark): its first Kit click opens one new `isr-kit` tab. Later clicks reuse that tab.
- **Fallback:** a page that sets `window.ISR_KIT_SAME_TAB = true` before `isr-ui.js` gets a Kit link that navigates in place.

Checked (2026-11-18):

| Browser | Result |
|---|---|
| Chrome 157 on macOS | Works. Checked with real mouse events in a two-port harness: :8050 named `isr-kit`, :8120 a tool page with the real `isr-ui.js`. Second Open: still 2 tabs. Kit from the tool: still 2 tabs, launcher tab reloaded. Map: same tab. Tool opened by hand: Kit opens one tab, then reuses it. |
| Safari on macOS | Not checked yet. |
| Chrome on Android (Pixel) | Not checked yet. |

**Manual check** (any browser, on the kit):
1. Open the board.
2. Click a tool's **Open** twice. You should still have 2 tabs.
3. On the tool page, click **Kit**. You should be back on the board tab, still with 2 tabs.

If a browser opens a new tab on step 3, set `ISR_KIT_SAME_TAB` for it and note it in the table.
