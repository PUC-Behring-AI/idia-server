#!/usr/bin/env python3
"""A stand-in LiteLLM proxy, enough of one to drive scripts/colleague.sh.

Why a real server instead of a stubbed ``curl``: colleague.sh reaches LiteLLM
two different ways — ``curl`` in ``_litellm_api`` and ``urllib`` inside the
embedded Python of ``_litellm_delete_keys_by_alias``. Stubbing ``curl`` leaves
the second one untested, and the second one is where the provisioning path
dies first when the proxy is down.

It keeps real state: a key created through /key/generate is listed by
/key/list, described by /key/info and removed by /key/delete. That is what
makes the "clean the previous keys for this alias" step assertable — with
canned responses the count is whatever the fake decided, not what the script
computed.

Usage:
    fake_litellm.py <port_file> [--mode MODE] [--log JSONL]

Binds an ephemeral port and writes the number to *port_file*, so the caller
never has to guess a free port. Every request is appended to *JSONL* as
{"method", "path", "body"} for the test to assert on.

Modes:
    ok          — normal behaviour (default)
    no-key      — /key/generate answers 200 with no "key" field
    error       — /key/generate answers 500 with an error body
    empty-list  — /key/list always answers with no keys
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# token -> {"key_alias": str, "spend": float, "max_budget": float, ...}
_KEYS: dict[str, dict] = {}
_LOCK = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    log_path: str | None = None

    # ── plumbing ────────────────────────────────────────────────────────────

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the default stderr access log; the JSONL is the record."""

    def _record(self, body: object) -> None:
        if not self.log_path:
            return
        entry = {"method": self.command, "path": self.path, "body": body}
        with _LOCK, open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _read_body(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    def _reply(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── routes ──────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 — http.server's required spelling
        parsed = urlparse(self.path)
        self._record(None)

        if parsed.path == "/key/list":
            if self.mode == "empty-list":
                return self._reply({"keys": []})
            with _LOCK:
                return self._reply({"keys": list(_KEYS)})

        if parsed.path == "/key/info":
            query = parse_qs(parsed.query)
            # colleague.sh queries this endpoint two different ways and expects
            # two different response shapes: `?key=TOKEN` -> {"info": {...}}
            # in _litellm_delete_keys_by_alias, and `?key_alias=ALIAS` ->
            # {"data": {"keys": [...]}} in cmd_status. Which one real LiteLLM
            # actually serves is not established by any test or doc in this
            # repo — see the open issue. Both are served here so each caller
            # is exercised against the shape it was written for; that makes
            # the script's handling testable without settling the question.
            if "key_alias" in query:
                alias = query["key_alias"][0]
                with _LOCK:
                    matches = [
                        {"key": token, **info}
                        for token, info in _KEYS.items()
                        if info.get("key_alias") == alias
                    ]
                return self._reply({"data": {"keys": matches}})

            if not query:
                # A third shape, for a third caller: `./idia user list` GETs
                # /key/info with no parameters and reads `.info` as a list.
                with _LOCK:
                    return self._reply(
                        {"info": [{"key": t, **i} for t, i in _KEYS.items()]}
                    )

            token = (query.get("key") or [""])[0]
            with _LOCK:
                info = _KEYS.get(token)
            if info is None:
                return self._reply({"error": "not found"}, status=404)
            return self._reply({"info": info})

        if parsed.path in ("/health", "/health/liveliness"):
            return self._reply({"status": "healthy"})

        if parsed.path == "/models":
            return self._reply({"data": [{"id": "mistral-7b"}]})

        return self._reply({"error": f"unhandled GET {parsed.path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_body()
        self._record(body)

        if parsed.path == "/key/generate":
            if self.mode == "error":
                return self._reply({"error": {"message": "boom"}}, status=500)
            token = f"sk-fake-{uuid.uuid4().hex[:16]}"
            record = dict(body) if isinstance(body, dict) else {}
            record.setdefault("key_alias", "")
            record["spend"] = 0.0
            with _LOCK:
                _KEYS[token] = record
            if self.mode == "no-key":
                # The shape that used to slip through: 200, plausible JSON,
                # no key. The script must refuse it rather than report success.
                return self._reply({"message": "created"})
            return self._reply({"key": token, **record})

        if parsed.path == "/key/delete":
            keys = (body or {}).get("keys", []) if isinstance(body, dict) else []
            with _LOCK:
                removed = [k for k in keys if _KEYS.pop(k, None) is not None]
            return self._reply({"deleted_keys": removed})

        if parsed.path == "/user/new":
            return self._reply({"user_id": str(uuid.uuid4())})

        return self._reply({"error": f"unhandled POST {parsed.path}"}, status=404)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("port_file")
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--log")
    ap.add_argument("--seed-alias", action="append", default=[],
                    help="pre-existing key for this alias, repeatable")
    args = ap.parse_args()

    _Handler.mode = args.mode
    _Handler.log_path = args.log

    for alias in args.seed_alias:
        _KEYS[f"sk-seed-{uuid.uuid4().hex[:12]}"] = {"key_alias": alias, "spend": 0.0}

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    # Write the port only once the socket is listening, so a test that polls
    # this file never races ahead of the bind.
    with open(args.port_file, "w", encoding="utf-8") as fh:
        fh.write(str(port))
    print(port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
