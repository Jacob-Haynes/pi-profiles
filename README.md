# pi-profiles: the one-Pi platform for the ISTAR demo kit

Runs a whole observation, surveillance, detection and drone-ops demo from **one Raspberry Pi 5 hub** (Bookworm 64-bit, SSD boot). The Pi can do it alone, or two Apple Silicon Macs can join its hotspot to run the vision tools and a local LLM/VLM. It hot-swaps between demo scenarios without dropping ATAK clients.

- **`bootstrap.sh`**: one idempotent installer: base packages, every tool repo, env files, profiles
- **`switch.sh`** (`pi-profiles`): hot-swap-safe systemd profile switcher
- **Launcher on :8050**: a projector **mission board** (`/`) with one card per tool in the active profile, grouped Detect / Watch / Analyse / Share, a live **COP map** (`/map`) of the TAK picture without ATAK, and an **engineer view** (`/engineer`) to switch profile and see Pi health, devices, maps and checks. It is also the home of the shared UI kit (`ui/`, served at `/ui/`)
- **`profiles.d/`**: the three demo profiles (`demo-full-mac`, `demo-step-mac`, `demo-full`) and `bft`, their hidden include base
- **`laptop/`**: Mac station setup (`setup-mac.sh --role hq|op`, `start-mac.sh`), see [laptop/README.md](laptop/README.md)
- **Clock:** offline, the Pi has no time source (and no RTC battery by default), so the Macs set its clock when they connect, and the launcher's status pill shows the clock; when it is really off, the board shows a red banner with a "Set Pi clock from this device" button. The RTC battery is on the shopping list.
- **Tunnelled HQ:** a managed Mac that can't advertise a Bonjour name works over a direct cable plus one ssh tunnel (`laptop/hq-tunnel.sh`, `isr-hq-mode tunnel` on the Pi); no Mac settings change. See [laptop/README.md](laptop/README.md#tunnelled-hq-recommended-for-managed-macs).
- **Offline ATAK maps:** `isr-maps` on :8160 serves MBTiles basemaps to every phone on the hotspot, and `isr-push-maps` (or the launcher button) sends a pack to every connected ATAK device as a TAK data package. Build packs on a Mac with `laptop/build-map-pack.py`: by default a small OpenTopoMap pack (≤ 5000 tiles, ≤ 2 req/s) around the Pi's site position, attributed "Map data: © OpenStreetMap contributors, SRTM | Map style: © OpenTopoMap (CC-BY-SA)". OpenTopoMap's operators appreciate a short note of use; for larger areas use OS Data Hub with your own `OS_API_KEY` (see [laptop/README.md](laptop/README.md#offline-maps-for-atak-build-map-packpy--the-pis-isr-maps)).
- Hub networking: `isr-pi.local` mDNS alias, `isrkit` MQTT user for the Macs, `isr-netcheck`
- Power check (bootstrap and launcher): says plainly whether the Pi 5 negotiated 5 A or is **USB current-limited** (600 mA)
- Node onboarding: `sudo isr-mesh-channel --pause-bridge` caches the bridge's channel, and the launcher shows it as a QR behind the PIN
- Docs: [contract](docs/CONTRACT.md) · [architecture](docs/ARCHITECTURE.md) · [SSD tips](docs/SSD-TIPS.md) · [Mac stations](laptop/README.md)
- Demo playbooks (event runbooks, station layouts, rehearsal schedules, kit lists) live in a separate private repo, not here.

## Install on a fresh Pi

```bash
sudo apt-get install -y git
git clone https://github.com/Jacob-Haynes/pi-profiles.git
cd pi-profiles
sudo ./bootstrap.sh
```

The log goes to `/var/log/isr-kit-install.log`, and bootstrap ends with a summary table (repo, branch, commit, install result, env file). Re-running it is safe: it updates checkouts, re-runs installers and never overwrites an existing `/etc/<repo>.env`.

| option | |
|---|---|
| `--only acoustic-detector,rf-spectrum` | install just these repos (the platform is always installed) |
| `--branch fpv-vision=main` | override a branch from `repos.conf` (repeatable). A missing branch falls back to the default branch, marked `fallback` |
| `--token-file /root/.isr-gh-token` | GitHub token for the private repos (see below) |
| `--tune-ssd` | journald cap plus 2 GB swap (see [SSD tips](docs/SSD-TIPS.md)) |
| `--offline` / `--online` | force or forbid offline pip/uv (no index, no retries; `/etc/pip.conf` backed up and restored). Auto when pypi is unreachable; also skips apt then. |
| `--keep-network` | don't change NetworkManager's wired profiles. By default they're set to **pure link-local** (169.254/fe80, deterministic on a direct cable to the HQ Mac) |
| `--wired-dhcp` | wired Ethernet uses DHCP instead (router/switch cable; 10 s timeout, never blocks boot) |
| `--skip-apt`, `--skip-platform`, `--dry-run`, `--src-dir DIR` | |

Bootstrap checks for a Pi 5, Bookworm, aarch64 and an SSD root. It warns rather than failing.

### Kit repos, visibility and licences

| repo | runs on | visibility | licence |
|---|---|---|---|
| pi-profiles (this repo) | Pi + Mac setup | public | MIT |
| blufor-tracker, drone-detector, tac-advisor, mesh-deadrop | Pi | public | see each repo |
| acoustic-detector, rf-spectrum | Pi | public | see each repo |
| fpv-vision, change-detect | Mac (Pi fallback) | public | AGPL-3.0 (they use Ultralytics YOLO) |
| op-sentry | Mac (Pi fallback) | **private** | see the repo |
| photoAnalysis (geo-locate) | HQ Mac only | **private** | see the repo |

`repos.conf` carries the same visibility column.

### Private repos

`op-sentry` and `photoAnalysis` are private. bootstrap.sh and `setup-mac.sh` need GitHub access for them. Everything else is public. Pick **one** of these. **Never** commit a token or put it in a URL.

1. **gh login (simplest).** As the normal Pi user, run `gh auth login`, then `gh auth setup-git`. `sudo ./bootstrap.sh` runs git as `$SUDO_USER`, so it uses that login.
2. **Token file (no browser on the Pi).** Create a fine-grained token with read-only **Contents** on just those repos, then:
   ```bash
   sudo install -m 600 /dev/stdin /root/.isr-gh-token   # paste the token, then Ctrl-D
   sudo ./bootstrap.sh --token-file /root/.isr-gh-token
   ```
   The token reaches git through `GIT_ASKPASS` from the environment only. It is never written to `.git/config`, a remote URL, argv or the log. Delete the file afterwards if you like, because updates of existing checkouts need it again.

3. **SSH or deploy keys.** `sudo ./bootstrap.sh --ssh` clones from `git@github.com:Jacob-Haynes/<repo>.git` as `$SUDO_USER`, using that user's SSH key. A single deploy key only covers one repo, so give the Pi user a key that has read access to all the private repos, or a `~/.ssh/config` with one host alias per repo.

### Site position and station names (contract v4)

```bash
sudo isr-set-site --sensor 51.0000,-1.0000    # OP / sensor position, set once (or bootstrap.sh --sensor …)
```

This writes `/etc/isr-kit/site.env` (`ISR_HOST=isr-pi`, `SENSOR_LAT`, `SENSOR_LON`) and syncs those keys into every `/etc/<repo>.env`. The Macs get the same position with `laptop/setup-mac.sh --from-pi …` or `--sensor LAT,LON`, and their own `ISR_HOST` (`isr-hq` / `isr-droneops`).

### Gemini (optional)

```bash
sudo isr-set-gemini-key                      # prompts, hidden input
sudo isr-set-gemini-key --model gemini-3.6-flash
sudo pi-profiles --restart                   # restart running tools to pick it up
sudo isr-set-gemini-key --clear              # back to offline-only
```

This writes `GEMINI_API_KEY` (and `GEMINI_MODEL`) into every `/etc/<repo>.env` with mode 600, plus a non-secret marker `/etc/isr-kit/gemini.configured`. The launcher's AI tier shows **Gemini** only when that marker exists and the API is reachable. Otherwise it shows **Mac VLM** if the HQ Ollama is up, or **offline/template**.

## Profiles

The launcher's switcher offers three profiles:

| profile | title | units (after `include bft`) | USB devices |
|---|---|---|---|
| **`demo-full-mac`** (default) | Core demo (Pi + 2 Macs) | hub + drone-detector, acoustic, rf-spectrum, tac-advisor, mesh-deadrop; **change-detect, geo-locate (`photoAnalysis`, :8150), fpv-vision on the HQ Mac; op-sentry on the OP Mac**. The simulators run continuously. | Heltec, mic |
| **`demo-step-mac`** | Core demo (step by step) | the same tools, with `DEMO_MODE=on_demand`. Nothing simulated runs on its own; the presenter fires each event from `/presenter` (contract v6). Real inputs stay live. | Heltec, mic |
| `demo-full` | Pi only (fallback: no Macs) | everything the Pi can run on its own: + op-sentry, change-detect (**no fpv-vision**: one camera tool per Pi). AI: Gemini with venue internet, else the offline templates. | Heltec, mic, 360 camera (Alfa, SDR optional) |
| `bft` (hidden) | BFT core | opentakserver, mesh-bridge: the include base of the others, not shown in the switcher | Heltec V3 |

**What the prune changed:**
- The per-segment profiles (`demo-op`, `demo-drone-threat`, `demo-isr`, `demo-sitrep`, their `-mac` variants and `demo-op-split`) were removed on 2026-11-17.
- `install.sh` deletes them from the Pi.
- A Pi whose active profile was one of them switches to `demo-full-mac` when installing, or at boot.

**Split OP mode** (the Pi camera served as MJPEG by `op-sentry-stream`, detection on the OP Mac) no longer has a profile. To use it:
- add `op-sentry-stream` to a profile, or start the unit by hand;
- `start-mac.sh` then points op-sentry at `http://isr-pi.local:8120/raw.mjpg` whenever the Pi's active profile runs `op-sentry-stream`;
- `setup-mac.sh --op-split` pins that.

The launcher (`isr-launcher`) runs in every profile. Rule: **at most one camera-class unit** (`fpv-vision`, `op-sentry`) per profile. `switch.sh` and the launcher refuse a profile that breaks it, and `--force` overrides it from the shell only.

Profile files are one systemd unit per line, plus:

```
include bft                  # pull in another profile
# @title Drone threat (C-UAS)
# @devices heltec-v3, alfa-wifi, usb-mic, rtl-sdr
# @note shown on the launcher
# @optional alfa-wifi, rtl-sdr # nice to have; the tool simulates without it
# @remote fpv-vision@hq   # expected on a Mac (hq, op, droneops); the launcher tracks its heartbeat
# @hotspot off                # only for standalone profiles that bring their own AP
# @env rf-spectrum ALERT_COT=1  # per-tool env override while this profile is active
```

Device keys: `heltec-v3 alfa-wifi usb-mic rtl-sdr capture-card 360-camera pi-camera gopro`.

`services.conf` is the catalogue of every unit with its port, CPU class and repo, and `repos.conf` lists the repos with their branches.

## Switching

```bash
sudo pi-profiles demo-step-mac       # switch
pi-profiles --plan demo-step-mac     # what would stop/start, no changes
pi-profiles --list | --status
pi-profiles --units demo-full        # resolved unit list
pi-profiles --env demo-full          # per-tool env overrides the profile sets (@env)
sudo pi-profiles --restart           # restart the active profile's tools (keeps OTS/bridge up)
```

Hot-swap rules:
- Units in both the old and new profile, such as OTS and the mesh bridge, are **left running**, so ATAK clients on the hotspot stay connected.
- Units being dropped are stopped **first**. switch.sh waits until each unit's processes have exited (camera, SDR, serial and audio released) before starting anything new, and SIGKILLs a unit after 20 s.
- Stray tool units running outside any profile are stopped too.
- Units missing from a partly installed kit are skipped and reported (`partial`). A failed start is reported as `degraded`, and the rest of the profile still starts.
- The NetworkManager hotspot `bft-hotspot` is brought back up if it dropped.
- A lock stops two switches running at once.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Launcher (:8050)

`http://192.168.4.1:8050` (or `isr-pi.local:8050` over the cable) is the **mission board** for the projector:
- the header: "ISTAR kit", the active profile, and one status pill that rolls up MQTT, the TAK server, AI (LLM/VLM), clock, power and disk (click it for the list)
- a card per tool the profile runs, grouped Detect / Watch / Analyse / Share: status from its heartbeat, its purpose, where it runs (Pi / HQ Mac / OP Mac), SIM / REC / LIVE, and one Open button. The TAK server card shows how many devices are connected and a **Show QR** button (this page, the hotspot Wi-Fi if `LAUNCHER_HOTSPOT_PSK` is set (PIN), or a map pack for ATAK)
- a banner only when the operator must act (the clock really off, under-voltage)

`/map` (the **Map** button) is the common operating picture, for the projector, the Macs and phones without ATAK. It runs fully offline:
- **What it shows:** everything the TAK server routes, as APP-6 / MIL-STD-2525 symbols (milsymbol), from the CoT type: `a-f-…` friend, `a-h-…` hostile, `a-u-…` unknown, `a-n-…` neutral.
  - Small mono callsign labels with SIM / REC tags: on hover or selection, and always from zoom 17 on a wide screen.
  - Course leaders, one minute ahead, for tracks with course and speed (drone-detector, BFT).
  - op-sentry's bearing lines (`u-d-f`).
  - geo-locate's uncertainty circles (`u-d-c-c`).
  - The sensor site, from `/etc/isr-kit/site.env`.
  - Detections with no range of their own (op-sentry, rf-spectrum, acoustic, a "probable" drone, placed at the sensor) are never drawn at a position. They show only as thin bearing lines from the sensor, plus one badge on it: "OP1 · 3 person · 1 vehicle". The badge's popup lists each with its bearing. Per sensor, only the newest 6 show, fading over 3 minutes and then hidden, whatever their stale time.
  - Only contacts with a real position get a full symbol, sized to the zoom. Below zoom 16, symbols that would overlap merge into one count in the commonest affiliation's colour; click it to zoom in. The stations' registration SAs merge into one "Sensor site ×N".
  - CoT deletes (`t-x-d-d`, forcedelete) and updates that arrive already stale remove an item at once.
  - Stale items fade for 2 minutes, then go.
- **Controls:** the layers (friendly, hostile/unknown, sensors, bearing lines), a contacts list (newest report first, with its age and a "Contacts 12 (5 stale hidden)" count; click to centre), **Clear old** (hides, in this browser only, whatever hasn't reported for 2 minutes; OTS is never touched), the cursor's MGRS grid, a scale bar, a legend and **Auto-follow alerts**, which pans to each new hostile or unknown contact (for the projector).
- **Base map:** the first offline pack from isr-maps (`?pack=<name>` picks another), through the launcher's `/tiles/` proxy, so it works from whatever address the page was opened with. Leaflet and milsymbol are vendored in `launcher/static/vendor/`, with no CDN.
- **Data:** the launcher keeps one TAK connection to OTS (`LAUNCHER_TAK_HOST`:`LAUNCHER_TAK_PORT`, default 127.0.0.1:8087). It registers as a pure viewer under contract v5 (uid `isrkit-launcher-map-<hub>`, callsign `COP-VIEW <hub>`, the "unknown position" SA, so no TAK client draws it) and never sends anything else. `GET /api/cop` returns the picture as JSON; `GET /api/cop/stream` streams it as server-sent events, falling back to polling once a second. `LAUNCHER_COP=0` turns it off.
- **Rehearse it without the tools:** `tools/rehearsal/cop_traffic.py --site <placeholder lat,lon> --port 8087` sends labelled simulated traffic.

`/presenter` (the **Presenter** button) drives a step-by-step demo from a phone while the projector shows `/map`:
- One big button per scenario of every tool in the profile, in run-of-show order: BFT, OP, counter-drone, FPV, analysis. Pressing one shows "Sent → watch the map".
- **Clear demo** per tool, and **Reset picture** to clear every tool's simulated events.
- Each tool's mode (Step by step / Continuous), with offline tools listed with the reason.
- The board's header shows which style the profile is.

Contract v6 (`docs/CONTRACT.md`) has the tool API.

`/engineer` has everything else:
- the active profile and a one-tap switch (switching lives here only)
- every service's state, with a link to its UI
- CPU temperature, load, throttling flags, RAM and disk
- which kit USB devices are detected
- whether the internet and Gemini are reachable, and whether the HQ Mac's Ollama (`LLM_URL`/`VLM_URL`) is up, with the AI tier (Mac VLM → Gemini → off)
- every tool's heartbeat (`isrkit/heartbeat/<tool>`): where it runs (Pi, HQ Mac, OP Mac), its mode, and live/stale/offline

Security model, for a closed hotspot:
- The launcher runs as the unprivileged user `isr-launcher`.
- Its only sudo right is `/opt/pi-profiles/bin/isr-switch-helper`. The helper is root-owned and accepts a single `[a-z0-9-]` profile name that exists in `profiles.d`. It never runs a shell or any other command.
- An optional PIN is set with `LAUNCHER_PIN` in `/etc/isr-launcher.env`. It has a lockout after 5 wrong tries, and `LAUNCHER_PIN_FOR_VIEW=1` makes the page itself need the PIN.
- Switch requests need a custom header, which blocks cross-site form posts.

**Shared UI kit.** `ui/isr-ui.css`, `ui/isr-ui.js`, `ui/fonts/` and `ui/UI-SPEC.md` are the source of truth for every tool's look. The launcher and isr-maps serve them at `/ui/`. `tools/sync-ui-kit.sh` copies them into each sibling repo's static folder, and `tools/sync-ui-kit.sh --check` reports drift by sha256 (tests/run.sh prints it). Never edit a copy: change `ui/` here, update the hashes in `tests/test_ui.py`, then sync.

## Tests (run on the Mac)

```bash
tests/run.sh
```

This runs the shell tests for `switch.sh` against a stubbed `systemctl` (includes, hot-swap order, core kept up, device-release wait, missing units, camera limit, lock, bad names), pytest for the launcher (status, switch API, PIN and lockout, input validation, device parsing) and for the Gemini helper, then shellcheck.

## Integration gate

```bash
tools/contract_gate.py            # clones each repo's branch from repos.conf and checks it against docs/CONTRACT.md
tools/contract_gate.py --src DIR  # or check existing checkouts in DIR/<repo>
```

It's a heuristic grep (unit files, install.sh, env example, profiles, ports, env vars, heartbeat, laptop scripts, simulate mode), so treat each finding as something to go and look at.

## Repo contract (for tool repos)

The full contract (addresses, MQTT users and topics, heartbeat, env vars, laptop scripts) is in [docs/CONTRACT.md](docs/CONTRACT.md). In short:

- Code goes in `/opt/<repo>`, the venv in `/opt/<repo>/.venv`, data in `/var/lib/<repo>`, secrets in `/etc/<repo>.env`.
- The systemd unit is named after the repo. `deploy/install.sh` is idempotent, non-interactive and does **not** enable the unit at boot.
- `profiles.d/<repo>.conf` holds the repo's standalone profile.
- Only `mesh-bridge` opens the radio. Everyone else uses MQTT `meshbus/tx` and `meshbus/rx` on `127.0.0.1:1883`.
- Ports: 8050 launcher, 8060 drone-detector, 8070 mesh-deadrop, 8090 tac-advisor, 8100 acoustic-detector, 8110 fpv-vision, 8120 op-sentry, 8130 change-detect, 8140 rf-spectrum. OTS owns 80/443, 8081, 8087, 8089 and 1883, and its nginx also takes 8080 (Marti), which is why drone-detector moved to 8060.

## License

pi-profiles is MIT (see [LICENSE](LICENSE)). The kit's tool repos have their own licences; fpv-vision and change-detect are AGPL-3.0 because of their Ultralytics dependency. See the table above.
