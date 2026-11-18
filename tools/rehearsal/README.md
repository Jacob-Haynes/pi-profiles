# Integration rehearsal (one Mac, no Pi)

Runs every kit tool in simulate/replay mode against local stand-ins for the Pi's services, and scores the run against `docs/CONTRACT.md`.

| piece | stands in for |
|---|---|
| `broker.py` | RabbitMQ's MQTT plugin (3.1.1 and 5, retained, wildcards; any login) on :18830. `--rabbit-retained` replays retained messages only to subscriptions without wildcards, as RabbitMQ does |
| `cot_relay.py` | OpenTAKServer 1.8.3's CoT TCP port on :18087, **with OTS's rules** (contract v5): CoT from unregistered senders is dropped; a client whose first `<contact>` arrives less than 2.5 s after connecting is never bound; a duplicate callsign under a new uid gets no EUD row. `--lax` relays everything. |
| `cop_traffic.py` | the tools' CoT for the launcher's `/map`, all SIMULATED: a drone with course and speed, OP detections with bearing lines, an RF hit, a BFT patrol, a geo-locate circle, a civil aircraft and a stale contact. `--site LAT,LON` (a placeholder, never a real OP), `--port 18087` for the relay |
| `stub_llm.py` | an Ollama/OpenAI-compatible LLM that quotes its prompt (no real model) |
| `run_all.sh` | clones (`clone`), venvs (`setup`), starts broker and relay (`infra`), tools (`start`), and `stop` |
| `measure.py` | scores C4.2 (no 0,0), C4.3 (labels), C4.4 (rates), C5 (registration, 3 s wait, no drops, SA every 60 s, receiver bound) |

```bash
export ISR_REHEARSAL_DIR=~/Repos/isr-kit/rehearsal   # work dir (clones, venvs, logs)
tools/rehearsal/run_all.sh clone && tools/rehearsal/run_all.sh setup
tools/rehearsal/run_all.sh infra && tools/rehearsal/run_all.sh start
# … exercise it, then:
tools/rehearsal/measure.py --logs "$ISR_REHEARSAL_DIR/logs" --since "$(cat "$ISR_REHEARSAL_DIR/logs/run.start")"
tools/rehearsal/run_all.sh stop
```

The launcher runs from this repo against the same broker. See the commands in the work dir's `REHEARSAL.md`.
