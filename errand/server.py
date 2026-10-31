"""Web server for the demo console, standard library only.

POST /api/turn streams the agent's work as NDJSON: steps and trace rows as they happen,
then the reply text in chunks, then options, the approval card and final snapshots.
A demo crash really ends the process (exit code 17); the supervisor restarts it, and
the console's next call runs recovery.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent import InjectedCrash
from .clock import SystemClock
from .config import Settings
from .domain import UserInput
from .factory import World, build
from .tracing import summarize

CRASH_EXIT = 17
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

FAULTS: dict[str, tuple[str, str, str]] = {
    "book_timeout_booked": ("book", "timeout_booked", "Next booking times out after it has succeeded"),
    "book_timeout_lost": ("book", "timeout_lost", "Next booking times out without booking"),
    "book_slot_taken": ("book", "slot_taken", "Next booking finds the slot taken"),
    "hold_price_up": ("hold_slot", "price:+200", "Next hold quotes ₹200 more than the search"),
    "reschedule_conflict": ("reschedule", "conflict", "Next reschedule hits a slot conflict"),
    "crash_after_book_sent": ("agent", "crash:after_book_sent", "Kill the agent right after it sends a booking"),
    "crash_after_outbox_write": ("agent", "crash:after_outbox_write",
                                 "Kill the agent after the outbox write, before sending"),
    "search_error": ("search_providers", "error", "Next search raises an unexpected error"),
}


class App:
    def __init__(self, world: World) -> None:
        self.world = world
        self.agent = world.agent

    def trace_tail(self, session_id: str, n: int = 120) -> list[dict]:
        path = self.agent.tracer.path(session_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        out = []
        for line in lines:
            e = json.loads(line)
            out.append({"seq": e["seq"], "type": e["type"], "summary": summarize(e), "ms": e.get("latency_ms"),
                        "level": "error" if e["type"] in ("error", "injection_suspected", "crash")
                        or e.get("error_class") in ("UNKNOWN_OUTCOME", "UNEXPECTED") else None})
        return out

    def faults(self) -> dict:
        armed = [f"{a['tool']}:{a['outcome']}" for a in self.world.provider.armed_faults()]
        return {"catalogue": [{"id": k, "label": v[2], "armed": f"{v[0]}:{v[1]}" in armed} for k, v in FAULTS.items()],
                "armed": armed}

    def reset(self) -> None:
        self.world.provider.reset()
        db = self.agent.db
        for table in ("sessions", "approvals", "outbox", "memory_facts", "memory_tombstones", "messages"):
            db.run(f"DELETE FROM {table}")
        tdir = self.agent.tracer.dir
        for name in os.listdir(tdir):
            os.remove(os.path.join(tdir, name))
        self.agent.tracer._seq.clear()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Errand/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
            if "/api/turn" in (args[0] if args else ""):
                sys.stderr.write("%s\n" % (fmt % args))

        # ---------------------------------------------------------- helpers
        def _json(self, code: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _stream_start(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._lock = threading.Lock()

        def _emit(self, ev: dict) -> None:
            line = (json.dumps(ev, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            with self._lock:
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # ---------------------------------------------------------- routes
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if url.path == "/api/health":
                self._json(200, {"ok": True, "pid": os.getpid(), "model": getattr(app.agent.model, "name", "?")})
                return
            if url.path == "/api/snapshot":
                sid = (parse_qs(url.query).get("session_id") or ["default"])[0][:64]
                snap = app.agent.snapshot(sid)
                snap["trace"] = app.trace_tail(sid)
                snap["open_writes"] = app.agent.has_open_writes(sid)
                snap["faults"] = app.faults()
                self._json(200, snap)
                return
            if url.path == "/api/faults":
                self._json(200, app.faults())
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "bad request"})
                return
            sid = str(body.get("session_id") or "default")[:64]
            if url.path in ("/api/turn", "/api/resume"):
                if url.path == "/api/resume":
                    if not app.agent.has_open_writes(sid):
                        self._json(200, {"resumed": False})
                        return
                    inp = UserInput(kind="resume")
                else:
                    kind = body.get("kind", "message")
                    if kind not in ("message", "confirm", "decline"):
                        self._json(400, {"error": "bad kind"})
                        return
                    inp = UserInput(kind=kind, text=str(body.get("text", ""))[:2000],
                                    approval_id=body.get("approval_id"))
                self._stream_start()
                if url.path == "/api/resume":
                    self._emit({"type": "new_turn", "title": "Recovery after restart"})
                try:
                    result = app.agent.handle(sid, inp, on_event=self._emit)
                except InjectedCrash as crash:
                    self._emit({"type": "crash", "point": crash.point})
                    sys.stderr.write(f"Injected crash at {crash.point}; exiting with {CRASH_EXIT}\n")
                    os._exit(CRASH_EXIT)
                for word in _chunks(result.text):
                    self._emit({"type": "text", "delta": word})
                    time.sleep(0.012)
                if result.options:
                    self._emit({"type": "options", "options": result.options})
                if result.approval:
                    self._emit({"type": "approval", "approval": result.approval})
                self._emit({"type": "chips", "chips": result.chips})
                snap = app.agent.snapshot(sid)
                self._emit({"type": "state", "state": snap["state"]})
                self._emit({"type": "memory", "facts": snap["memory"]})
                self._emit({"type": "outbox", "rows": snap["outbox"]})
                self._emit({"type": "clock", "display": snap["clock"]})
                self._emit({"type": "turn_end", "outcome": result.outcome})
                return
            if not app.world.settings.demo_mode:
                self._json(403, {"error": "demo controls are disabled"})
                return
            if url.path == "/api/faults":
                if body.get("action") == "clear":
                    app.world.provider.clear_faults()
                else:
                    fault = FAULTS.get(body.get("id", ""))
                    if not fault:
                        self._json(400, {"error": "unknown fault"})
                        return
                    app.world.provider.arm(fault[0], fault[1])
                self._json(200, app.faults())
                return
            if url.path == "/api/clock":
                minutes = max(0, min(120, int(body.get("minutes", 11))))
                app.world.clock.advance(minutes=minutes)
                self._json(200, {"advanced": minutes})
                return
            if url.path == "/api/reset":
                app.reset()
                self._json(200, {"reset": True})
                return
            self._json(404, {"error": "not found"})

    return Handler


def _chunks(text: str) -> list[str]:
    out, word = [], ""
    for ch in text:
        word += ch
        if ch in " \n":
            out.append(word)
            word = ""
    if word:
        out.append(word)
    return out


def serve(host: str = "127.0.0.1", port: int = 8765, latency_scale: float = 1.0) -> None:
    settings = Settings.from_env()
    world = build(settings, clock=SystemClock(latency_scale=latency_scale))
    app = App(world)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    model = getattr(world.agent.model, "name", "?")
    print(f"Errand console on http://{host}:{port}  (interpreter: {model}, data: {settings.data_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def supervise(argv: list[str]) -> int:
    """Run the server as a child process and restart it after a demo crash."""
    while True:
        child = subprocess.run([sys.executable, "-m", "errand", "serve", "--child", *argv])
        if child.returncode != CRASH_EXIT:
            return child.returncode
        print("Agent process exited after an injected crash; restarting in 1 s…", flush=True)
        time.sleep(1.0)
