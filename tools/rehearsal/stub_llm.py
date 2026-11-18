#!/usr/bin/env python3
"""Stub OpenAI-compatible LLM for the rehearsal (no Ollama on this Mac). /v1/chat/completions
answers with a labelled 'STUB SITREP' that quotes the lines of the prompt it was given, so we can
see exactly which sensor events tac-advisor put into the LLM context. Every request is logged."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = open(sys.argv[2], "a", buffering=1)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/api/tags"):
            return self._json(200, {"data": [{"id": "stub-sitrep"}], "models": [{"name": "stub-sitrep"}]})
        if self.path == "/api/version":
            return self._json(200, {"version": "stub"})
        self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        msgs = req.get("messages", [])
        prompt = "\n".join(str(m.get("content", "")) for m in msgs if isinstance(m, dict))
        LOG.write(json.dumps({"t": time.time(), "path": self.path, "model": req.get("model"), "prompt": prompt}) + "\n")
        keep = [l.strip() for l in prompt.splitlines()
                if any(k in l.upper() for k in ("DRONE", "ACOUSTIC", "RF", "SENTRY", "CHANGE", "HOSTILE", "UNKNOWN",
                                                 "SIM", "CONTACT", "SENSOR", "FPV", "ALERT"))]
        text = "STUB SITREP (rehearsal stub LLM, not a model). Events in context:\n" + "\n".join(keep[:25])
        if self.path.startswith("/api/chat"):   # Ollama-native format
            return self._json(200, {"model": req.get("model", "stub"), "done": True,
                                    "message": {"role": "assistant", "content": text}})
        if self.path.startswith("/api/generate"):
            return self._json(200, {"model": req.get("model", "stub"), "done": True, "response": text})
        self._json(200, {"id": "stub", "object": "chat.completion", "model": req.get("model", "stub"),
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
