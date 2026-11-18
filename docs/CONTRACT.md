# ISTAR kit integration contract (v5)

Every repo in the kit follows this contract, and `pi-profiles` gates on it (see the conformance check in the platform session's report). Where this file and a tool repo disagree, raise it rather than diverging.

## Nodes and addresses

| node | address | role |
|---|---|---|
| **Pi 5 hub** | `isr-pi.local` = **192.168.4.1** (static) | hotspot, OpenTAKServer, mesh bridge, RabbitMQ (MQTT), launcher, drone-detector, acoustic-detector, rf-spectrum, mesh-deadrop, tac-advisor |
| **HQ Mac** | `isr-hq.local` (DHCP), or tunnelled on the cable | Ollama (LLM and VLM), change-detect, geo-locate (photoAnalysis), fpv-vision, HQ display |
| **OP Mac** | `isr-op.local` (DHCP) | op-sentry, its own VLM |
| *Drone-ops Mac (older layout)* | `isr-droneops.local` (DHCP) | fpv-vision, op-sentry |
| ATAK phones, other clients | DHCP | TAK clients on 192.168.4.1:8087 |

- **Hotspot**: NetworkManager connection `bft-hotspot` on `wlan0`, created by blufor-tracker's `deploy/install.sh`. SSID `BFT-TAK` (`HOTSPOT_SSID`), WPA2 password `HOTSPOT_PASS` in `/etc/blufor-tracker.env`. 2.4 GHz channel 7, `ipv4.method shared`, address `192.168.4.1/24`.
- **DHCP and DNS**: NetworkManager's shared mode runs its own dnsmasq. blufor-tracker doesn't set a pool, so NM's default for a /24 applies, expected to be **192.168.4.10–192.168.4.254**. **Integration-day check:** `isr-netcheck` on the Pi prints the actual `--dhcp-range` and flags any difference. Only `192.168.4.1` is fixed, so address the Macs by their Bonjour names.
- **Wired backup link**: a direct Ethernet cable Pi↔Mac is **pure link-local** on the Pi (`isr-wired-setup` default: 169.254/16 and fe80::). The Mac gets link-local by itself when there's no DHCP. Use the Pi's own `<hostname>.local` (or `isr-pi.local` if it's named `isr-pi`) over it. A router cable needs `--wired-dhcp`.
- **mDNS**: `isr-mdns-alias.service` publishes `isr-pi.local → 192.168.4.1` with `avahi-publish` (set in `/etc/isr-kit/hub.env`) and leaves the Pi's own hostname alone. If the Pi's hostname is already `isr-pi`, avahi publishes it natively and the alias stays off. The Macs advertise themselves through Bonjour. `laptop/setup-mac.sh --set-hostname` sets `isr-hq` and `isr-droneops`.

## Mesh channel (node onboarding)

The launcher shows the bridge radio's channel as a Meshtastic QR/URL **only behind the launcher PIN** (the URL carries the PSK). It reads the first valid `https://meshtastic.org/e/#…` line from:
1. `/var/lib/blufor-tracker/channel-url`: written by mesh-bridge (blufor-tracker `b951147`+) on connect and every 60 s. It's atomic, never logged, mode 0640, group `isr-launcher`, in a 2750 setgid directory, and can be overridden or disabled with `CHANNEL_URL_FILE`. A channel change made from another device shows up after `systemctl restart mesh-bridge`. The launcher's service user belongs to group `isr-launcher` (install.sh and bootstrap ensure it).
2. `/etc/isr-kit/mesh-channel.url`: written by `sudo isr-mesh-channel [--pause-bridge]`, which opens the serial port only while `mesh-bridge` is stopped.

No other process opens the radio's serial port.

## Demo-day self-check

`/opt/blufor-tracker/.venv/bin/python -m bridge.check [--mesh-text "radio check"]` reports PASS/FAIL for TAK send and receive through OTS and for the MQTT login, and lists the heartbeats. The launcher's **Run checks** button runs it as the launcher user with the launcher's own localhost MQTT login, so it needs no root. **Run checks + radio check** also transmits a fixed short text on the mesh.

## Ports

**Bind dual-stack** (recommended for every web UI): listen on `::` with `IPV6_V6ONLY` off (uvicorn: `host="::"`), falling back to `0.0.0.0` where IPv6 is disabled. A Mac on the cable that is also on Wi-Fi can reach the Pi only over IPv6 link-local, so a `0.0.0.0`-only page is unreachable from it. The launcher and isr-maps do this.

| port | owner |
|---|---|
| 80, 443, **8080** (Marti), 8443, 8446 | OpenTAKServer nginx |
| 8081 | OTS API |
| **8087** | CoT TCP (plain, no auth); every tool sends CoT here |
| 8088, 8089 | OTS TCP default and SSL |
| **1883** (MQTT), 8883, 5672 (AMQP) | RabbitMQ |
| 8554 (RTSP out), 1935 (RTMP in), 8888, 8889, 8890 | mediamtx, installed by OpenTAKServer (OTS video). **Optional** for op-sentry's best-effort live 360: `rtmp://192.168.4.1:1935/op360` in, `rtsp://isr-pi.local:8554/op360` out. Live RTMP from the Insta360 ONE is **unconfirmed**. `bootstrap.sh --with-mediamtx` only checks and enables OTS's instance and never downloads one. |
| 8050 | isr-launcher |
| 8060 | drone-detector (moved off 8080, which OTS nginx owns; `WEB_PORT`) |
| 8070 | mesh-deadrop |
| 8090 | tac-advisor |
| 8100 | acoustic-detector |
| 8110 | fpv-vision |
| 8120 | op-sentry |
| 8130 | change-detect |
| 8140 | rf-spectrum |
| 8150 | geo-locate (`photoAnalysis` repo), **HQ Mac only** |
| 8170 | blufor-tracker's demo server (`DEMO_PORT`; `/api/demo/{scenarios,trigger,clear}`, `/api/status`), named in its heartbeat's `demo_url`. Its heartbeat `url` stays the OTS UI |
| 8160 | **isr-maps**: offline ATAK basemaps on the Pi (`/tiles/<pack>/{z}/{x}/{y}.png`, `/atak/<pack>.xml`, `/download/<pack>.mbtiles`, `/api/packs`); always on, like the launcher. `isr-push-maps` sends a pack to all ATAK devices as a TAK data package through OTS (uid `isrkit-maps-<host>`, callsign `MAPS <host>`) |
| 11434 | Ollama on the HQ Mac (`OLLAMA_HOST=0.0.0.0`) |

Tools bind their web UI on `0.0.0.0` and serve it at `/`. `isr-netcheck` on the Pi checks that 1883, 8087 and 8050 are reachable from the hotspot (listeners and firewall).

## MQTT

- The broker is RabbitMQ's MQTT plugin on the Pi, port 1883, vhost `/`.
- **On the Pi**, tools use `127.0.0.1` with `guest`/`guest`. `guest` is loopback-only.
- **Off the Pi**, tools use `MQTT_USER=isrkit` with `MQTT_PASS` from `/etc/isr-kit/mqtt.env` (mode 600, random, generated by `isr-setup-mqtt-user`). RabbitMQ topic permissions on `amq.topic` (MQTT `/` = `.`): **write** `meshbus.tx` and `isrkit.#`, **read** `meshbus.rx` only.
- Topics:
  - `meshbus/tx`: JSON `{"text","to":null|"!nodeid","channel":0,"source"}`. Rate-limited by the bridge (burst 5, then 1 per 3 s per source). Summarise, don't send one per detection.
  - `meshbus/rx`: JSON `{"from","text","channel","ts","from_short"?,"from_long"?}`.
  - **`isrkit/heartbeat/<tool>`**: see below.

## Heartbeat

Every tool, on the Pi or a Mac, publishes **retained** JSON every **10 s** to `isrkit/heartbeat/<tool>`, where `<tool>` is the repo name:

```json
{"tool": "fpv-vision", "host": "isr-droneops", "url": "http://isr-droneops.local:8110/",
 "mode": "mps", "status": "ok", "ts": 1794834375}
```

| field | values |
|---|---|
| `host` | **C4.1:** env `ISR_HOST` if set, else the lowercased short `socket.gethostname()`. Expected values: `isr-pi`, `isr-hq`, `isr-op` (`isr-droneops` in the older layout) |
| `url` | full http(s) URL of the tool's UI. **If env `ISR_PUBLIC_URL` is set, use it verbatim** (every tool, Pi or Mac). Otherwise use the `.local` name: `http://<ISR_HOST>.local:<port>/`. `start-mac.sh` sets `ISR_PUBLIC_URL=<STATION_PUBLIC_BASE>:<port>/` on a tunnelled HQ (for example `http://192.168.4.1:8150/`), because a managed Mac's `.local` name may never resolve. It also sets `PUBLIC_URL` (unless already set), which change-detect and geo-locate read today |
| `mode` | `cpu` · `cuda` · `mps` · `gemini` · `vlm` · `llm` (remote LLM server, e.g. tac-advisor via openai_compat) · `simulate` · `replay` |
| `status` | `ok` · `degraded` · `error`, and `stopped` on clean exit (publish it retained) |
| `ts` | unix seconds (informational; the launcher times freshness by arrival, not by `ts`) |
| `station` | *optional*: the station role, `pi` · `hq` · `op` · `droneops` (env `ISR_STATION`, which start-mac.sh exports). The launcher shows the role ("HQ Mac") instead of the hostname; without it, it falls back to `LAUNCHER_STATIONS`, then to the profile's `@remote` station, then to the hostname |
| `detail` | *optional*: short free text the launcher shows under the tool (e.g. "replaying clip 3/7", "SIMULATED") |

**Station announce.** `laptop/start-mac.sh start` also publishes a retained `isrkit/station/<ISR_HOST>` = `{"host": "<ISR_HOST>", "station": "<ISR_STATION>", "ip": "<192.168.4.x>"}`, and re-sends it every 60 s while the tools run.
- **What it's for:** the launcher labels every heartbeat from that host with its role, so a managed Mac that keeps its own hostname still shows as "HQ Mac" or "OP Mac".
- **`ip`:** only for a Mac on the hotspot (not a tunnelled HQ). It lets the Pi relay that Mac's tool pages on `http://<hub>:<port>/` like the tunnel ports, or link them by IP, because phones may not resolve `.local`. The Pi relays only to an address on the hotspot's /24, and never on a port a unit in the profile runs on the Pi.
- **Why the launcher also subscribes to `isrkit/station/<host>` exactly** for every heartbeat host it has no role for: RabbitMQ's MQTT plugin replays retained messages only to subscriptions without wildcards. The card shows where a tool runs now (the live heartbeat); the profile's planned station is used only when there's no heartbeat.

Exception: op-sentry ships a second unit, **`op-sentry-stream`** (the Pi serves its camera as MJPEG on 8120 for op-sentry on the OP Mac), with its own heartbeat `isrkit/heartbeat/op-sentry-stream`. Its heartbeat `mode` is the detection device (`cpu|mps|cuda|simulate|replay`); the AI backend is shown in its UI.

The launcher shows a tool as **live** if it received a heartbeat in the last 30 s, **stale** after 30 s, and **offline** after 120 s. A retained heartbeat replayed when the launcher subscribes counts as offline until a live one arrives.

## Environment variables

`/etc/<repo>.env` on the Pi, `~/.config/isr-kit/station.env` (plus `<repo>.env`) on a Mac:

| var | Pi value | Mac value |
|---|---|---|
| `TAK_HOST`, `TAK_PORT` | `127.0.0.1`, `8087` | `isr-pi.local`, `8087` |
| `MQTT_HOST`, `MQTT_PORT` | `127.0.0.1`, `1883` | `isr-pi.local`, `1883` |
| `MQTT_USER`, `MQTT_PASS` | `guest`, `guest` | `isrkit`, from the Pi |
| `LLM_URL` | `http://isr-hq.local:11434` (tac-advisor) | HQ: `http://127.0.0.1:11434`; OP Mac: `http://isr-hq.local:11434` (its VLM_URL is its own `http://127.0.0.1:11434`) |
| `LLM_MODEL` | `qwen3.5:27b` | same |
| `LLM_BACKEND` | **`ollama`** to use the HQ Mac (Ollama native API with thinking off). `openai_compat` timed out at 90 s with qwen3.5 thinking in the rehearsal, so avoid it until tac-advisor disables thinking there. `auto`/`gemini`/`template` otherwise. | `ollama` |
| `VLM_URL`, `VLM_MODEL` | (vision runs on the Macs) | as `LLM_URL`; **`qwen3-vl:8b-instruct`** |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | optional, via `isr-set-gemini-key` | optional |
| `WEB_PORT` | contract port | same |
| `ISR_HOST` | `isr-pi` (site.env) | `isr-hq` / `isr-op` (station.env) |
| `SENSOR_LAT`, `SENSOR_LON` | OP position from site.env | same, from station.env |

`LLM_URL` and `VLM_URL` are the **bare Ollama base URL** with no path. Tools append `/v1/chat/completions` (OpenAI-compatible) or `/api/chat` themselves.

**Model defaults and Ollama gotchas** (from geo-locate's benchmark on the HQ Mac):
- **VLM: `qwen3-vl:8b-instruct`**, the official Ollama tag with no thinking capability. The stock `qwen3-vl:8b` keeps "thinking" even with `think:false`, on both `/api/chat` and `/api/generate`: 123–146 s per image in geo-locate, sometimes with empty content. The instruct tag takes 1–5 s raw, gave valid JSON 12/12, and runs geo-locate's 3-call pipeline in 18–28 s. Lite: `qwen3-vl:4b-instruct`.
- **LLM: `qwen3.5:27b`** (lite `qwen3.5:9b`). Ollama keeps at most 2 models loaded (`OLLAMA_MAX_LOADED_MODELS=2`): the LLM plus the instruct VLM.
- **Don't rely on Ollama `format:"json"`.** It returns **HTTP 501 on MLX-runner models** such as `qwen3.5:27b`. Ask for JSON in the prompt and parse leniently (strip code fences, take the first `{…}`).

## Repo visibility and licences

pi-profiles is MIT. fpv-vision and change-detect are AGPL-3.0 (Ultralytics). op-sentry and photoAnalysis are private (everything else is public), and bootstrap.sh and setup-mac.sh need GitHub access for them. The authoritative list is the README table and `repos.conf`.

## Mac-only tools

`repos.conf` marks a repo `mac` in its 4th column when it never runs on the Pi. bootstrap.sh, `isr-set-site` and `isr-set-gemini-key` skip it, and `laptop/setup-mac.sh` installs it for its role. Its `services.conf` class is `mac`, so the launcher lists it only under **Mac stations**.

| tool | repo | station | port | heartbeat | modes |
|---|---|---|---|---|---|
| Geo-locate (photo geolocation, Google ADK multi-agent) | `photoAnalysis` (private, `feature/isr-kit`) | HQ Mac | 8150 | `isrkit/heartbeat/photoAnalysis` | `vlm` (local Ollama `qwen3-vl:8b-instruct`, imagery stays on the Mac) · `gemini` (optional) |

A Mac-only tool follows the Mac-station rules: `scripts/run-laptop.sh`, MQTT login `isrkit`, `VLM_URL`/`VLM_MODEL`, `ISR_HOST`, heartbeat, `0.0.0.0` bind, and the v4 labels if it sends CoT or mesh. Geolocation results are always labelled **`ESTIMATE`** with a confidence, and a CoT position derived from them carries `ESTIMATE` in its remarks. It never presents an estimate as a fix.

## v4 rules (from the 2026-11-16 Mac integration rehearsal)

**C4.1 Station hostname.** The heartbeat `host` is `ISR_HOST` if set, else `socket.gethostname().split(".")[0].lower()`.
- On the Pi, `ISR_HOST=isr-pi` comes from `/etc/isr-kit/site.env` and is synced into every `/etc/<repo>.env`.
- On a Mac, `setup-mac.sh` writes `ISR_HOST=isr-hq` / `isr-op` into `station.env`.

**C4.2 One sensor position.** Every tool reads `SENSOR_LAT` / `SENSOR_LON` (decimal degrees, WGS84), the OP / sensor position. It's set once:
- Pi: `sudo isr-set-site --sensor LAT,LON` (or `bootstrap.sh --sensor LAT,LON`). It writes `/etc/isr-kit/site.env` (0644) and syncs `SENSOR_LAT`, `SENSOR_LON` and `ISR_HOST` into every `/etc/<repo>.env`, leaving other keys alone.
- Mac: `setup-mac.sh --sensor LAT,LON`, or `--from-pi`, which copies the Pi's `site.env`.

When unset, a tool **never sends a 0,0 point**. It skips position-bearing CoT, or uses a position the operator entered, and says in its UI that no sensor position is set.

**C4.3 Labelling.**

| data | CoT `remarks` start with | mesh text starts with |
|---|---|---|
| simulated (simulate mode, synthetic samples) | `SIMULATED: ` | `[SIM] ` |
| recorded (replayed footage or files) | `RECORDED: ` | `[REC] ` |
| live | no prefix | no prefix |

A `SIM-` callsign prefix alone is **not** enough. tac-advisor quotes remarks into SITREPs.

**C4.4 Rates.**
- Mesh alerts: at most **1 per 30 s per tool** (summarise or debounce). Replies to a direct query are exempt.
- CoT: at most **1 event per 5 s per uid** (throttle track updates).

The bridge's own limits (burst 5, then 1 per 3 s per source) are a safety net, not the target.

`tools/contract_gate.py` checks C4.1–C4.3 statically (by grep). The rehearsal (`~/Repos/isr-kit/rehearsal`) measures C4.3 and C4.4 at run time.

## v5 rules: talking to OpenTAKServer (from the OTS 1.8.3 source analysis)

OTS 1.8.3 on PostgreSQL enforces three things that a lax TAK server doesn't:
- CoT whose sender has **no EUD row is dropped**. `cot.sender_uid` is a foreign key to `euds.uid`; the insert fails and the message is never routed.
- A TCP client's receive queues are bound **only if its first `<contact>` event arrives after its RabbitMQ channel has opened**, and this is never retried on that connection.
- `euds.callsign` is UNIQUE. A new uid with a callsign that's already taken **silently gets no EUD row**, so all of that client's CoT is then dropped.

So every tool that sends or receives CoT does this:

**C5.1 One connection.** Each tool process keeps one long-lived TCP connection to `TAK_HOST:TAK_PORT` (8087) and reconnects with backoff. Every reconnect repeats C5.2.

**C5.2 Register first.** On every (re)connect, **wait 3 s**, then send one registration SA before anything else:

```xml
<event version="2.0" uid="isrkit-<repo>-<ISR_HOST>" type="a-f-G-E-S" how="m-g"
       time="…Z" start="…Z" stale="…Z">
  <point lat="SENSOR_LAT" lon="SENSOR_LON" hae="9999999.0" ce="9999999.0" le="9999999.0"/>
  <detail>
    <contact callsign="<TOOL-SHORT> <ISR_HOST>" endpoint="*:-1:stcp"/>
    <__group name="Cyan" role="Team Member"/>
    <takv platform="isr-kit" device="<repo>" os="linux|macos" version="<x>"/>
  </detail>
</event>
```

- **uid** is `isrkit-<repo>-<ISR_HOST>`, for example `isrkit-drone-detector-isr-pi`. It's stable and unique per tool and station, never random.
- **callsign** is `<TOOL-SHORT> <ISR_HOST>`, for example `DRONE-DET isr-pi` or `OP-SENTRY isr-droneops`. It's stable and unique; two uids never share one.
- **takv platform** is `isr-kit`. It must never be `Meshtastic`, `OpenTAK ICU` or `DMRCOT`, because OTS won't bind those.
- **time, start and stale** are ISO-8601 UTC ending in `Z` (`%Y-%m-%dT%H:%M:%S[.%f]Z`).
- **Position set** (`SENSOR_LAT`/`SENSOR_LON`): the SA is at that position with `stale` = now + 120 s, re-sent every 60 s.
- **Position unset:** the SA **must still be sent**, or OTS drops everything the tool sends. Use the CoT "unknown position" convention, `<point lat="0" lon="0" hae="9999999" ce="9999999" le="9999999"/>`, with `stale` = `start` (already stale), so ATAK never draws it.
  - This is the **only** 0,0 allowed in the kit (the C4.2 exception), apart from a `b-f-t-r` fileshare event (never drawn) sent while the position is unset.
  - While the position is unset, the tool sends **no position-bearing markers**.

**C5.3 Same connection for everything.** Send all markers on that connection after registration. Markers keep their own uids (`DRONE-…`, `OPSENTRY-…`), and OTS stores them with `sender_uid` = the tool's registered uid. **Re-send the SA every 60 s** to keep the EUD "Connected".

**C5.4 Receivers.** tac-advisor, bridge.check's listener and the launcher's COP map (`isrkit-launcher-map-<hub>`, callsign `COP-VIEW <hub>`) register the same way, with the 3 s wait, and read the same socket. A pure viewer like the COP registers with the "unknown position" SA, so nothing draws it, and sends nothing else. OTS never echoes a client's own CoT back to it, so don't expect it.

**Checks:**
- `tools/contract_gate.py` checks statically for an `isrkit-` uid, `takv` platform `isr-kit`, and a 3 s pre-registration wait.
- The rehearsal CoT relay (`tools/rehearsal/cot_relay.py`) **emulates these OTS rules**. It drops CoT from unregistered senders, never delivers to a client that registered less than 3 s after connecting, and rejects a duplicate callsign under a new uid, logging the reason for each. So a regression shows up on the Mac before the Pi.

## v6 rules: demo triggers (step-by-step demo)

There are two demo styles, and the Pi profile chooses between them:
- **Continuous** (`demo-full-mac`, "Core demo (Pi + 2 Macs)", unchanged): the simulators run on their own, as before.
- **Step by step** (`demo-step-mac`, "Core demo (step by step)"): nothing simulated runs on its own. The presenter fires each event, from the tool's own page or from the launcher's Presenter panel (`/presenter`), so the room sees cause and effect on ATAK and the map. Real inputs (mic, SDR, Wi-Fi capture, cameras, recorded clips) still work normally.

**C6.1 Environment.** `DEMO_MODE=continuous|on_demand` (default `continuous`).
- **On the Pi:** the profile sets it per unit with `# @env <unit> DEMO_MODE=on_demand`. `demo-step-mac` sets it for drone-detector, acoustic-detector, rf-spectrum and mesh-bridge (blufor-tracker's BFT sim).
- **On a Mac:** `start-mac.sh start` asks the Pi's launcher (`/api/status` → `demo_mode`). When the active profile is step by step, it gives every tool on that Mac `DEMO_MODE=on_demand`. `DEMO_MODE` in `station.env` or `~/.config/isr-kit/<repo>.env` wins. A background follower re-checks every 15 s (`ISR_DEMO_FOLLOW_EVERY`). When the Pi's profile changes mode, it restarts the Mac's demo tools in the new mode (`ISR_DEMO_REPOS`, default op-sentry, fpv-vision, drone-detector, acoustic-detector, rf-spectrum, blufor-tracker), so their heartbeats match the profile within about 30 s. Other tools aren't restarted. A tool with `DEMO_MODE` pinned is left alone. It logs to `~/isr-kit/logs/demo-follow.log`.
- **In `on_demand`:** a tool starts no background simulation, but keeps every real input running.

**C6.2 HTTP API** (every tool that can simulate, on its own port):
- `GET /api/demo/scenarios` → `{"mode": "on_demand", "scenarios": [{"id", "label", "hint", "duration_s"}], "active": [ids]}`. An `id` is `[A-Za-z0-9][A-Za-z0-9_.-]{0,47}`.
- `POST /api/demo/trigger {"id": "<scenario>", ...options}` → `{"ok": true, "id", "until": <epoch>}`. It starts one scenario and works in both modes; in continuous mode it adds to what's running. Options are simple scalars (`bearing`, `clip` …).
- `POST /api/demo/clear` → `{"ok": true, "cleared": n}`. It stops every scenario this tool started and removes their simulated CoT at once (stale = now, plus a `t-x-d-d` forcedelete, as op-sentry does). It never touches real detections.
  - **Optional body `{"background": true}`.** The launcher sends it once to each tool after the Pi switches into a step-by-step profile. A tool that knows it also expires, at once, the CoT its continuous background simulator sent before (stale = now, as above), so the old SIM tracks don't stay on ATAK for up to 5 minutes. A tool that doesn't know it ignores it and clears its scenarios as usual.
  - **On shutdown, and on a mode change,** a tool should do the same for its background-sim tracks.
- Scenarios end on their own after `duration_s`, and their CoT expires as normal.

**C6.3 Page.** A **Demo** card on the tool's page (kit v2 look), with one button per scenario plus **Clear demo**. It's visible in both modes; in on_demand mode it's on the first screen. Simulated output keeps the `SIMULATED:` / `[SIM]` / SIM-tag rules (C4.3).

**C6.4 Heartbeat.** Add `"demo_mode": "continuous|on_demand"` and `"demo_active": [ids]`. The launcher reads them at the top level, or inside `detail` when `detail` is an object (its text then goes in `detail.text`). It shows each tool's mode on the Presenter panel.

**C6.5 The launcher's Presenter panel.**
- **What it shows:** `/presenter` (the **Presenter** button on the board) lists every tool in the active profile that has scenarios, in run-of-show order: BFT, OP, counter-drone, FPV, analysis. Each scenario gets one big button, and each tool a **Clear demo**. A global **Reset picture** first asks each tool for `/api/demo/scenarios`, then runs `/api/demo/clear` only on the tools that answered. A tool whose scenarios answer 404 (no demo API: tac-advisor, change-detect, geo-locate) isn't called, gets no card and isn't an error. It's named on one quiet line, "No demo buttons". It works on a phone.
- **How it reaches each tool:**
  - Pi tools on `127.0.0.1:<port>`, where `<port>` is from services.conf. A unit without a fixed port, like blufor-tracker, uses the port in its heartbeat `url` when that names the Pi.
  - Mac tools through the Pi's relay (the HQ tunnel, or the station relay for a hotspot Mac), else the Mac's hotspot address.
  - A tool that's offline or has no route is listed with the reason, not as an error.
  - A tool whose heartbeat mode differs from the profile's (a Mac tool not restarted yet, or one with `DEMO_MODE` pinned) shows an amber tag, e.g. "Continuous (profile: step by step)".
- **After a switch into step by step:** the launcher clears the old simulated CoT.
  - It notices the change within 5 s, once the switch has finished. A launcher restart doesn't count.
  - It drops `SIM` items received before the switch from `/map` at once.
  - For up to 2 minutes, it sends `POST /api/demo/clear {"background": true}` to each demo tool once that tool answers again in on_demand mode (or reports no mode).
  - It does this once per tool, and skips any tool the presenter has already fired since the switch.
- **The launcher's own API:**
  - `GET /api/demo/scenarios` returns `{profile, title, mode, tools: [{unit, title, group, where, reachable, has_demo, error, mode, scenarios, active}]}`. `has_demo` is true, false (404) or null (not reached).
  - `POST /api/demo/trigger {"tool", "id", …}` and `POST /api/demo/clear {"tool"}` or `{"all": true}` are PIN-guarded like every launcher POST.
- **Mode:** the mission board's header shows the demo style: "Continuous" or "Step by step".

**C6.6 Minimum scenarios.**

| Tool | Scenarios |
|---|---|
| drone-detector | `inbound` (one Remote ID drone flying at the OP: closing, CPA, then lands), `crossing`, `outbound`, `swarm` (3 at once) |
| acoustic-detector | `drone-sound` (a canned drone signature injected for about 20 s). The real mic stays live, so playing the clip through the speaker also works. |
| rf-spectrum | its existing scenarios (FPV flight etc.) through this API |
| blufor-tracker | `patrol` (2–3 simulated friendly units walking a route near the OP for about 3 min) |
| op-sentry | `drone-on-bearing` (existing; options `bearing`, `clip`) |
| fpv-vision | none needed (the source picker covers clips and the live GoPro); a `Clear` that removes its own spot reports is welcome |

## Repo layout

- On the Pi: code in `/opt/<repo>`, venv in `/opt/<repo>/.venv`, data in `/var/lib/<repo>`, secrets in `/etc/<repo>.env` (mode 600, created from `deploy/<repo>.env.example`).
- systemd unit `<repo>.service` (blufor-tracker's is `mesh-bridge`), **not** enabled at boot; pi-profiles starts it. `EnvironmentFile=-/etc/<repo>.env`. Release devices on SIGTERM, with `TimeoutStopSec` of 15 s or less.
- `deploy/install.sh` is idempotent and non-interactive. bootstrap runs it from `/opt/isr-kit/src/<repo>`.
- `profiles.d/<repo>.conf` holds the standalone profile.
- A scenario profile may override a tool's settings with `# @env <unit> KEY=VALUE` (switch.sh applies them via a systemd drop-in that is read after `/etc/<repo>.env`). So tools should read their switches from the environment at start-up, and treat a restart as the way settings change.
- Vision repos (`fpv-vision`, `op-sentry`, `change-detect`, optionally `acoustic-detector`) ship **`scripts/run-laptop.sh`**. It reads config from the environment only, runs in the foreground until SIGTERM, binds the contract port on `0.0.0.0`, and publishes heartbeats. A `scripts/run-laptop.ps1` for Windows is optional.
- Every tool has a **simulate or replay mode** reachable from its env file (so demo fallbacks work without hardware), and reports it in the heartbeat `mode`.
