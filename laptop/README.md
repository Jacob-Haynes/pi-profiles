# Mac stations: HQ Mac and OP Mac

The Pi stays the hub: hotspot, OpenTAKServer, mesh bridge, MQTT, launcher and the non-vision tools. Two Apple Silicon Macs join its hotspot and take the heavy AI work:

| station | Mac | runs | Bonjour name |
|---|---|---|---|
| **HQ** (`--role hq --extra fpv-vision`) | M4 Max, 48 GB, on the cable, usually tunnelled | Ollama (LLM for tac-advisor, VLM for the vision tools), **change-detect**, **geo-locate** (`photoAnalysis`, :8150), **fpv-vision** (:8110, FPV replay "as if receiving the drone feed"), ATAK/WinTAK or browser HQ display | `isr-hq.local` |
| **OP** (`--role op`) | M4 Pro, 24 GB, on the BFT-TAK hotspot | **op-sentry** (360 camera, C920, iPhone): the remote sensor post sending alerts to HQ. Its own VLM, and the Wi-Fi keep-awake | `isr-op.local` |

This is the demo layout. 24 GB can't hold fpv-vision, op-sentry and the VLM together (it swapped 9 GB and detection fell to 0.5 fps), so fpv-vision moved to HQ. The older `--role droneops` (fpv-vision and op-sentry on one Mac, 32 GB or more) still works and shows as "Drone-ops Mac".

Windows is not supported for the stations (macOS is the primary path). The tool repos' `scripts/run-laptop.*` may still run on Windows. See each repo.

## 1. Join the hotspot

1. On the Mac, choose Wi-Fi **BFT-TAK**. The password is `HOTSPOT_PASS` in `/etc/blufor-tracker.env` on the Pi.
2. Check: `ping isr-pi.local` answers from `192.168.4.1`.
3. The hotspot has **no internet** unless the Pi has an uplink. Do every download (Homebrew, repos, models) **before** joining, on normal Wi-Fi.
4. In System Settings › Wi-Fi › BFT-TAK › Details, turn **Limit IP address tracking** off. Leave Private Wi-Fi address on or off, either works.

## 2. Set up (once per Mac, on normal internet)

> **Managed (corporate) Mac? Use tunnelled HQ.** A managed profile can set `NoMulticastAdvertisements=1` (in `/Library/Managed Preferences/com.apple.mDNSResponder`), so the Mac never advertises its Bonjour name: `<name>.local` resolves nowhere, not even on the Mac, and renaming it doesn't help. `setup-mac.sh` detects this and says so. Tunnelled HQ needs no Mac settings changed: see [Tunnelled HQ](#tunnelled-hq-recommended-for-managed-macs) below.

The hotspot has no internet, so the order is: **download everything on normal internet, then join the hotspot, then start from Terminal.app.**

```bash
# 1. On normal internet
git clone -b feature/one-pi-platform https://github.com/Jacob-Haynes/pi-profiles.git ~/isr-kit/pi-profiles
cd ~/isr-kit/pi-profiles
gh auth login && gh auth setup-git          # private repos (op-sentry, photoAnalysis)
laptop/setup-mac.sh --role hq --extra fpv-vision --fpv-footage ~/Desktop/ISR-demo/fpv   # HQ Mac: about 23 GB of models
laptop/setup-mac.sh --role op --set-hostname        # OP Mac: op-sentry and its own VLM, about 6 GB

# 2. On the BFT-TAK hotspot (OP Mac): MQTT login, site position, hub checks (add --hub 192.168.4.1 if isr-pi.local fails)
laptop/setup-mac.sh --role op --from-pi <user>@isr-pi.local --skip-prefetch --skip-clone --skip-brew
#    HQ Mac on the cable, tunnelled: see "Tunnelled HQ" below (--role hq --tunnel --extra fpv-vision ...)

# 3. From Terminal.app (camera permission)
laptop/start-mac.sh start
```

Step 1 installs brew packages (git, gh, python 3.12, ffmpeg, uv), clones the repos, installs Ollama with its models, and runs each repo's `scripts/run-laptop.sh --setup-only`: pip packages, model weights (YOLO, YOLOE, CLIP) and fpv-vision's sample clips. A repo that has no `--setup-only` yet is named in the warnings; start it once on normal internet instead.

**op-sentry's source.** By default op-sentry on a Mac plays the newest file in `~/Movies/Insta360`, or its simulator. In split mode (the Pi runs `op-sentry-stream` and streams its camera, the Mac detects; no shipped profile does this since the 2026-11 prune), it needs `OPSENTRY_SOURCE=http://isr-pi.local:8120/raw.mjpg`. `start-mac.sh start` sets that by itself when the Pi's active profile runs `op-sentry-stream`; `setup-mac.sh --op-split` writes it into station.env permanently. An `OPSENTRY_SOURCE` you set in station.env or `~/.config/isr-kit/op-sentry.env` always wins.

**fpv-vision on HQ.** `--extra fpv-vision` adds it to `ISR_REPOS` and its port 8110 to `ISR_TUNNEL_PORTS`, so the Pi relays it like change-detect and geo-locate. `--fpv-footage DIR` writes `FPV_FOOTAGE_DIRS` (folders separated by `:`, no spaces); a re-run without it keeps the old value. It uses HQ's own VLM.

**OP VLM.** op-sentry (and fpv-vision, on a drone-ops Mac) use a VLM to name air objects and describe scenes. The OP Mac runs its own: Ollama on 127.0.0.1 with `qwen3-vl:8b-instruct` only (one model loaded, no LLM), and `VLM_URL=http://127.0.0.1:11434` in station.env. A tunnelled HQ keeps its Ollama on its own loopback, so the OP Mac can't share it. `--skip-ollama` leaves Ollama alone and points `VLM_URL` at the HQ Mac instead (only for an HQ that isn't tunnelled). `start-mac.sh start` checks this Mac's Ollama is up and has its model.

- `--from-pi` copies the MQTT login (`isrkit`) from the Pi's `/etc/isr-kit/mqtt.env` over SSH, so the Mac must be on the hotspot for that step. If you set up off the hotspot, leave it out and re-run later with it. You can also use `--mqtt-pass-file`, or type the password when prompted.
- `--set-hostname` sets the Bonjour name to `isr-hq`, `isr-op` or `isr-droneops`. The launcher labels each tool by the station's announced role (`ISR_STATION`: "HQ Mac", "OP Mac"), else by these names.
- The script writes `~/.config/isr-kit/station.env` (mode 600) with `TAK_HOST`/`MQTT_HOST` = `isr-pi.local`, `MQTT_USER=isrkit`, `MQTT_PASS`, `LLM_URL`/`VLM_URL` (HQ Ollama) and `LLM_MODEL`/`VLM_MODEL`. Put per-tool overrides in `~/.config/isr-kit/<repo>.env`.
- On the HQ Mac it installs Ollama, sets it to listen on `0.0.0.0:11434` (`127.0.0.1` with `--tunnel`) (`OLLAMA_HOST`) with `OLLAMA_KEEP_ALIVE=24h` and `OLLAMA_MAX_LOADED_MODELS=2`, then pulls and warms the models.

### Models (HQ)

Checked against [ollama.com/library](https://ollama.com/library) on 2026-11-16:

| use | default | size | `--lite` (for less than 48 GB) |
|---|---|---|---|
| instruct LLM (tac-advisor SITREPs, Q&A) | `qwen3.5:27b` | 17 GB | `qwen3.5:9b` (6.6 GB) |
| vision-language (scene descriptions in fpv-vision, op-sentry, change-detect, geo-locate) | `qwen3-vl:8b-instruct` | 6.2 GB | `qwen3-vl:4b-instruct` (3.3 GB) |

Use the **`-instruct`** VLM tag. The stock `qwen3-vl:8b` keeps "thinking" even with `think:false`: about 2 minutes per image, sometimes with empty output. The instruct tag answers in 1–5 s. The stock tag isn't pulled, and is optional if you want to compare. Don't use Ollama's `format:"json"` with `qwen3.5:27b`, because MLX-runner models return HTTP 501 for it (see CONTRACT).

**Sizing for the 48 GB HQ Mac:**
- macOS lets the GPU use about 75% of unified memory by default, about **36 GB**.
- The two models are about 23 GB of weights. Add the KV cache at the 8k context `setup-mac.sh` sets (`OLLAMA_CONTEXT_LENGTH`, about 3–5 GB) and the total is about 27 GB on the GPU.
- That leaves about 9 GB of GPU headroom, and about 12 GB of RAM for change-detect, ATAK/browser and macOS.
- After warming, `setup-mac.sh` prints `ollama ps`. Both models should show **100% GPU**; a CPU split means it's too tight.
- The trade-off if it's tight: `qwen3.5:9b` (6.6 GB) is faster but writes plainer SITREPs. Override with `--llm-model`/`--vlm-model` or `ISR_LLM_MODEL`/`ISR_VLM_MODEL`.
- Not yet measured on the real HQ Mac (Ollama isn't installed on the machine this was written on). Alternatives: `gemma4:26b` (16 GB, vision built in) or `qwen3.5:35b` (22 GB) as the LLM. **Rehearse the latency.** If SITREPs take too long, drop to `qwen3.5:9b` with `--llm-model`.

The Pi's tac-advisor uses the HQ Ollama by setting in `/etc/tac-advisor.env`:

```
LLM_BACKEND=ollama          # native API, thinking off (openai_compat timed out at 90 s with qwen3.5)
LLM_URL=http://isr-hq.local:11434
LLM_MODEL=qwen3.5:27b
```

## Camera and microphone permission (read this)

macOS grants camera and microphone access **per app that starts the tools**. If you start them from inside another app's helper (an IDE terminal, an agent shell, some launchers), OpenCV capture is **silently denied**: no prompt, and no entry under Privacy & Security › Camera. So:

1. **Launch `laptop/start-mac.sh` from Terminal.app** (or iTerm). `setup-mac.sh` and `start-mac.sh` warn when they're not in one.
2. On the first run, **allow Camera** for Terminal on the OP Mac (op-sentry), and **Microphone** if acoustic-detector runs on a Mac. `setup-mac.sh --role op` grabs one test frame to trigger the prompt at setup time, not on demo day.
3. If a tool reports the camera as **"not authorized"**: System Settings › Privacy & Security › **Camera** › turn **Terminal** on (and Microphone if needed), then **fully quit Terminal (Cmd-Q)** and reopen it. A new window isn't enough.

## Tunnelled HQ (recommended for managed Macs)

The HQ Mac talks to the Pi over a **direct Ethernet cable** (link-local; `isr-pi.local` resolves over it because the Pi advertises), plus **one ssh connection** that carries everything the Pi or the phones need from the Mac. Nothing on the Mac changes: no hostname, no Bonjour, no firewall, and Ollama stays on `127.0.0.1`.

```bash
ssh-copy-id <user>@isr-pi.local                       # once: the tunnel uses key login (BatchMode)
laptop/setup-mac.sh --role hq --tunnel --from-pi <user>@isr-pi.local   # --skip-ollama keeps Ollama exactly as it is
laptop/start-mac.sh start                             # starts the tools and laptop/hq-tunnel.sh
```

On the Pi, once: `sudo ./bootstrap.sh --hq-tunnel` (or just `sudo isr-hq-mode tunnel`). This points `LLM_URL`/`VLM_URL` in `/etc/isr-launcher.env` and the tools' `/etc/<repo>.env` (tac-advisor) at `http://127.0.0.1:11434`. `isr-hq-mode mdns` switches back, and `isr-hq-mode status` shows what each file uses.

What the tunnel does (`laptop/hq-tunnel.sh`, plain `ssh`, no extra tools):
- `ssh -N -R 127.0.0.1:11434:127.0.0.1:11434 -R 127.0.0.1:18130:127.0.0.1:8130 -R 127.0.0.1:18150:127.0.0.1:8150 <user>@isr-pi.local` (tool ports land at port + 10000 on the Pi's loopback), with `ServerAliveInterval=15` (a dead link is noticed in about 45 s), `ExitOnForwardFailure=yes` and a reconnect loop with backoff (2 s doubling to 30 s). Every forward binds the Pi's **127.0.0.1 only**, so no sshd change on the Pi.
- **Ollama** is then at `http://127.0.0.1:11434` on the Pi (the launcher's AI tier and tac-advisor).
- **The tool pages** (change-detect 8130, geo-locate 8150): while the tunnel is up, the Pi's launcher listens on `<port>` on **every** address and relays it to `127.0.0.1:<port + 10000>`, so phones on the hotspot open `http://192.168.4.1:8150/` and this Mac on the cable opens `http://isr-pi.local:8150/`. The launcher builds its links from the address you opened it with, so "open ›" works from either. It does this only for ports of class `mac` in services.conf, or of tools the active profile runs on a Mac (`@hq`), and only while the tunnel answers, so it never takes a port a Pi tool needs. The launcher shows those tools **via Pi tunnel**, with that link.
- The tools' heartbeats advertise `ISR_PUBLIC_URL` (start-mac.sh sets `http://192.168.4.1:<port>/` from `STATION_PUBLIC_BASE`). A tool that doesn't honour `ISR_PUBLIC_URL` yet still gets the working link on the launcher, which matches by port.
- On every connect it also **sets the Pi's clock from this Mac** if they differ by more than 2 s (the Pi has no time source offline). The log shows `pi-clock … clock set from hq <name>: corrected …s`. `push-to-pi.sh` and `setup-mac.sh --from-pi` do the same. `laptop/pi-clock.py <user>@isr-pi.local` runs it by hand.
- `laptop/hq-tunnel.sh status` (also in `start-mac.sh status`), log in `~/isr-kit/logs/hq-tunnel.log`. `laptop/hq-tunnel.sh --print` shows the ssh command.
- Limit: the OP Mac can't reach a tunnelled HQ's Ollama (it's on the HQ's loopback and the Pi's loopback), so it runs its own VLM (`--role op`).

## 3. Run

```bash
laptop/start-mac.sh start     # from Terminal.app! runs each repo's scripts/run-laptop.sh, keeps the Mac awake
laptop/start-mac.sh status
laptop/start-mac.sh logs fpv-vision
laptop/start-mac.sh stop
```

On the Pi's launcher engineer view (`http://isr-pi.local:8050/engineer`), pick a `*-mac` profile, for example **Core demo (Pi + 2 Macs)**. Every Mac tool then appears on the mission board (`http://isr-pi.local:8050/`) and under **Mac stations** with its station, mode (`mps`/`vlm`/`gemini`/`simulate`/`replay`), a link to its UI, and live/stale/offline from its heartbeat.

The first time each tool starts, macOS asks whether to **allow incoming connections** for `python3` (and `ollama` on HQ). Click Allow, or the Pi and the other Mac can't reach it.

## Updating a Pi that has no internet (`push-to-pi.sh`)

If the Pi has no uplink (its Wi-Fi is the hotspot, and Internet Sharing on the Mac may be blocked by policy), update it from a Mac that does have internet:

```bash
laptop/push-to-pi.sh --host <user>@<pi-hostname>.local                # mirrors, git insteadOf, pip check
laptop/push-to-pi.sh --host <user>@<pi-hostname>.local --bootstrap    # … plus sudo ./bootstrap.sh --skip-apt in tmux
```

It does five things:
1. Refreshes `git clone --mirror` copies of pi-profiles and every Pi-side repo in `~/isr-kit/mirror` (private repos use your `gh` login).
2. Copies them to a persistent staging dir on the Pi (`~/isr-mirror.staging`) with **rsync over ssh**, excluding `._*` and `.DS_Store`.
   - The copy has keep-alives and is retried up to 5 times (`ISR_COPY_RETRIES`), resuming where it stopped. A cable blip doesn't abort it.
   - If either end lacks rsync, it falls back to one tar stream with `COPYFILE_DISABLE=1 --no-xattrs --no-mac-metadata`, so there are no AppleDouble files or `LIBARCHIVE.xattr` headers.
   - Each mirror is checked with `git fsck`, then the staging copy is swapped into `~/isr-mirror` with hard links.
   - **If the copy fails, the Pi is left unchanged** and the script says so. Re-run it to resume.
3. Sets `git config --global url."<pi-home>/isr-mirror/".insteadOf "https://github.com/<owner>/"` for the SSH user, so bootstrap's normal URLs resolve to the mirrors, and fast-forwards the Pi's pi-profiles checkout (`--pi-profiles-dir`, default `~/pi-profiles`).
4. **Checks Python dependencies offline.** pip can't fetch anything that isn't already in `/opt/<repo>/.venv`. The script dry-runs each repo's new `requirements.txt` with `--no-index` against the installed venv and lists what's missing, plus any repo that has never been installed. Fix those by giving the Pi internet once (a phone tethered to eth0 or a USB Wi-Fi adapter), or leave that repo out with `-- --only <repos>`.
5. With `--bootstrap`, runs `sudo ./bootstrap.sh --skip-apt` (add args after `--`) in tmux session `isr-bootstrap`, waits, and prints the summary table. Attach with `ssh -t <host> tmux attach -t isr-bootstrap`.

**Offline pip/uv:** when bootstrap can't reach pypi (or with `--offline`) it switches pip and uv to offline mode for the run.
- `isr-pip-offline on` backs up `/etc/pip.conf` (Pi OS ships one with piwheels) and `/etc/uv/uv.toml`, and writes no-index, no-retry, 3 s timeout versions. It also exports `PIP_NO_INDEX`, `PIP_RETRIES=0` etc. for root-run pip.
- So requirements that are already installed pass instantly, `pip install --upgrade pip` is a no-op, and new dependencies fail in seconds instead of retrying pypi and piwheels for minutes.
- The originals are restored on exit, Ctrl-C included, and a crashed run is repaired by the next bootstrap. `--online` disables the switch. By hand: `sudo isr-pip-offline on|off|status`.

Bootstrap must run as the SSH user (`sudo ./bootstrap.sh`, not `--token-file`), because the `insteadOf` lives in that user's git config.

**SSH target over a direct Ethernet cable:** `isr-pi.local` is the hotspot alias (192.168.4.1) and only works on BFT-TAK. Over a cable use the Pi's own `<hostname>.local` (avahi on eth0) or its 169.254.x.y link-local address (on the Pi: `ip -4 addr show eth0`). If the Pi's hostname is `isr-pi`, `isr-pi.local` works over the cable too. On the real Pi, `dns-sd -G v4v6 isr-pi.local` returns the eth0 `fe80::…%enX` and `169.254.x.y` addresses.

*Troubleshooting:* if the name doesn't resolve over the cable (for example while eth0 is still settling), use the IPv6 link-local address directly.
1. Find the Mac's interface for the cable: `networksetup -listallhardwareports`, for example `en8`.
2. Find the Pi: `ping6 -c2 ff02::1%en8` (the Pi's `fe80::…` answers).
3. Connect with `ssh <user>@fe80::…%en8`.

Also check `systemctl is-active isr-mdns-alias` on the Pi. It should be inactive when the hostname is `isr-pi`.

*Cable dead on the Mac side, Pi fine:* check **System Settings › General › Sharing › Internet Sharing** on the Mac. If corporate policy turned Internet Sharing off but the USB Ethernet adapter is **still ticked** as a sharing port, macOS keeps that port in a half-configured bridge state. The Pi then gets nothing over the cable: no 169.254 address and no mDNS, surviving replugs and Pi reboots. **Untick the adapter** there and the cable works straight away. This was the real cause of a "Pi lost eth0" incident, not the Pi.

*`isr-netcheck` with the cable in:* it may report `isr-pi.local -> 169.254.x.y here (this Pi's eth0 address …)` as a **note**. That's expected: avahi answers per interface, and hotspot clients still get 192.168.4.1. It only FAILs if wlan0 has lost the hub IP, or the name resolves to an address that isn't the Pi's.

## Offline maps for ATAK (`build-map-pack.py` + the Pi's isr-maps)

ATAK on the hotspot shows a blank map: there's no internet, and OpenStreetMap's tile servers refuse ATAK (403) under their tile policy anyway. Instead the Pi serves basemap packs it already holds (**isr-maps**, port 8160, always on).

**1. Build a pack on a Mac while online.** Nothing downloads without `--yes`, and it prints the tile count, size and time first.

```bash
# DEFAULT: OpenTopoMap, centred on the Pi's own site position (SENSOR_LAT/LON in /etc/isr-kit/site.env,
# read over ssh and never written anywhere), 3 km radius, zooms 10-17 (16 if 17 would pass the cap)
laptop/build-map-pack.py --name op --title "OP" --from-site <user>@isr-pi.local          # estimate only
laptop/build-map-pack.py --name op --title "OP" --from-site <user>@isr-pi.local --yes    # download

# centre from this Mac's station.env instead, or give one
laptop/build-map-pack.py --name op --from-station --yes
laptop/build-map-pack.py --name balcony --center <lat>,<lon> --radius-km 0.5 --zoom 12-16 --yes

# Ordnance Survey (GB), the upgrade path for larger areas: your own OS Data Hub key, OpenData zooms 7-16 only
export OS_API_KEY=…          # never committed or logged; not written into the pack
laptop/build-map-pack.py --source os --name venue --title "Venue" \
    --from-site <user>@isr-pi.local --radius-km 10 --zoom 10-16 --confirm-terms --yes

# a pack you already have (e.g. from your geo cell): MBTiles is checked and copied; GeoPackage (EPSG:3857 tiles) is converted
laptop/build-map-pack.py --source file:~/Downloads/area.mbtiles --name venue
```

When the centre comes from `--from-site` or `--from-station`, the builder prints "around the … position (coordinates not shown)" rather than the numbers, so its output is safe to paste.

**OpenTopoMap is a volunteer-run free service.** The builder keeps to its rules: at most **5000 tiles per pack** (refused above that unless you've asked the operators and pass `--i-asked-the-operator`); at most **2 requests/s** on a single connection, spread over the a/b/c servers; backoff on 429/5xx, and a full stop (keeping what it has) after 5 failures in a row; each tile fetched once, so re-running resumes; and an honest User-Agent, `isr-kit-map-pack/<version> (one-off small offline pack; contact via project repo)`. The operators appreciate a short note about how their maps are used ([opentopomap.org/about](https://opentopomap.org/about)), so please send one if you build a pack. For anything bigger than a few km, use OS (`--source os` with your own `OS_API_KEY`).

Packs land in `~/isr-kit/maps/<name>.mbtiles`. They're written as `.partial` first, so re-running resumes. Then send them to the Pi with `laptop/push-to-pi.sh --host … --maps-only` (or `--maps` alongside a code update). The copy uses a resumable rsync to `~/isr-maps.staging`, then `sudo install` into `/var/lib/isr-maps`. isr-maps picks new packs up within 30 s.

**2. Send it to the phones.** Easiest: on the launcher's engineer view (`http://192.168.4.1:8050/engineer` › **Offline maps**) press **Push maps to all ATAK devices** (PIN), or on the Pi run `isr-push-maps <pack>`. It builds a TAK data package (the pack's map source XML, plus the `.mbtiles` with `--with-mbtiles` or the checkbox), uploads it to OTS, and sends it to every ATAK device connected to the TAK server, as a fileshare from "MAPS isr-pi". Each phone shows a file-transfer notification and imports it; the map is then in **Maps & Favorites**. The package also stays in OTS's data packages. `isr-push-maps` with no arguments lists packs; `--dry-run` only builds and checks the zip. Phones that join later: push again, or enable the optional `isr-push-maps-auto` service (off by default; see `deploy/isr-push-maps-auto.service`), which sends once to each newly connected device.

**By hand on one phone (on BFT-TAK):** open `http://192.168.4.1:8050/engineer` and go to **Offline maps**, or on the mission board press the TAK server's **Show QR** › the map and scan it.
- **Add to ATAK** downloads `<pack>.xml`. In ATAK: ☰ › **Import** › **Local SD** › Downloads › the `.xml`, then **Maps & Favorites** (the layers icon) › select the pack. Its tiles stream from `http://192.168.4.1:8160`.
- **Download pack** gets the `.mbtiles` itself, to import into ATAK for use away from the Pi.

**Provider terms (checked 2026-11-16; re-check before building, since they change):**

| source | what's allowed | attribution | what the builder enforces |
|---|---|---|---|
| **OS Maps API** ([layers and zooms](https://docs.os.uk/os-apis/accessing-os-apis/os-maps-api/layers-and-styles), [ZXY spec](https://docs.os.uk/os-apis/accessing-os-apis/os-maps-api/technical-specification/zxy), [rate limits](https://docs.os.uk/os-apis/core-concepts/rate-limiting-policy), [branding](https://docs.os.uk/os-apis/core-concepts/os-api-branding)) | EPSG:3857 zooms **7–16 are OpenData** (free on any plan); **17+ is Premium**. 600 transactions/min live, 50/min in development. **Not confirmed:** we couldn't find OS wording on storing API tiles offline or serving them to phones on a local network (the plans and OGL pages didn't load). Check your Data Hub plan and the [OGL](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) yourself. | OS's copyright statement, terms and error-reporting link must appear (for mobile apps, in the menu). The pack carries "Contains OS data © Crown copyright and database rights <year>"; check the exact wording in OS's [branding assets](https://github.com/OrdnanceSurvey/os-api-branding). | zooms 7–16 only (17+ refused); key from `OS_API_KEY`, masked in every message; 0.8 req/s; `--confirm-terms` required |
| **OpenTopoMap** (default; [about / usage](https://opentopomap.org/about)) | Use is fine as long as the server isn't overloaded by mass downloads ("Massendownloads"). CC-BY-SA. No availability guarantee. They appreciate a short note about your use. | "Map data: © OpenStreetMap contributors, SRTM \| Map style: © OpenTopoMap (CC-BY-SA)" (their German original: "Kartendaten: © OpenStreetMap-Mitwirkende, SRTM \| Kartendarstellung: © OpenTopoMap (CC-BY-SA)"), clearly visible. Share-alike applies to redistributed packs. | **5000 tiles** per pack unless `--i-asked-the-operator`, max zoom 17, ≤ 2 req/s, one connection, a/b/c subdomains, backoff on 429/5xx, stop after 5 failures in a row, resumable, User-Agent `isr-kit-map-pack/<ver> (one-off small offline pack; contact via project repo)` |
| **Your own MBTiles/GeoPackage** | whatever its licence says (e.g. your geo cell's terms) | carry it over in the MBTiles `attribution` metadata | tables and tiles checked; GeoPackage must be the standard EPSG:3857 pyramid |
| OpenStreetMap tile servers | **not supported:** the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/) doesn't allow bulk downloading or offline prefetching, and the servers return 403 to ATAK | — | — |

The attribution is stored in each pack's MBTiles metadata (`attribution`, plus a short `credit`). The launcher's Maps card and the isr-maps index page show it, the ATAK layer name carries the short credit (e.g. "OP (offline, © OpenStreetMap contributors, © OpenTopoMap (CC-BY-SA))"), and the ATAK XML has the full text in a comment. Keep it visible when you present.

## AI fallback ladder

1. **Mac VLM**: HQ Ollama reachable with the model loaded. The launcher header shows `AI: Mac VLM`.
2. **Gemini**: if the HQ Mac is down and the hub has internet with `GEMINI_API_KEY` set (`sudo isr-set-gemini-key` on the Pi, `--gemini-key-file` on the Macs).
3. **Off**: detections, tracks and CoT still work; only the natural-language descriptions and SITREP prose disappear (tac-advisor falls back to its template).

## Troubleshooting

| symptom | check |
|---|---|
| `isr-pi.local` doesn't resolve | You're not on BFT-TAK, or the Pi's `isr-mdns-alias` isn't running (`isr-netcheck` on the Pi). Use `192.168.4.1` meanwhile. |
| MQTT login failed | Re-run with `--from-pi`. On the Pi: `sudo isr-setup-mqtt-user` (re-syncs the user from `/etc/isr-kit/mqtt.env`). |
| Launcher says "Ollama unreachable" | On HQ: `curl localhost:11434/api/version`. `lsof -iTCP:11434 -sTCP:LISTEN` must show `*:11434`. Check the firewall prompt. |
| Tool shows "offline" | `laptop/start-mac.sh status`, then `logs <repo>`. Heartbeats need the MQTT login. |
| Mac sleeps mid-demo | `start-mac.sh` runs `caffeinate`. Also plug in power and turn off "Put hard disks to sleep". |
| Mac on the hotspot keeps timing out (pages hang, heartbeats stale, Pi→Mac ping loses most packets) | Wi-Fi power save plus the Pi's AP buffering. `start-mac.sh start` runs a keep-awake `ping -i 0.2` to the hub; `start-mac.sh status` shows it. `ISR_WIFI_KEEPALIVE=0` turns it off. On the Pi, `isr-hotspot-band --status` should say band a, channel 36. |
