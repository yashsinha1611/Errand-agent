"""Terminal chat. Shows each step the agent takes, then its reply.

Slash commands: /faults, /fault <id>, /clock <minutes>, /memory, /state, /trace, /new, /reset, /quit
"""
from __future__ import annotations

import json
import os
import sys
import uuid

from .agent import InjectedCrash
from .clock import SystemClock
from .config import Settings
from .domain import UserInput
from .factory import build
from .server import FAULTS
from .util import fmt_inr

ICON = {"running": "…", "done": "✓", "warn": "!", "error": "✗", "stopped": "■"}
DIM, BOLD, RESET = ("\033[2m", "\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "", "")


def main(latency_scale: float = 0.0) -> None:
    settings = Settings.from_env()
    world = build(settings, clock=SystemClock(latency_scale=latency_scale))
    agent = world.agent
    session = f"cli-{uuid.uuid4().hex[:8]}"
    model = getattr(agent.model, "name", "?")
    print(f"{BOLD}Errand{RESET} · interpreter: {model} · type /help for commands\n")

    def on_event(ev: dict) -> None:
        if ev["type"] == "step" and ev["status"] != "running":
            ms = f" {DIM}{ev['ms']} ms{RESET}" if ev.get("ms") is not None else ""
            print(f"  {ICON.get(ev['status'], '·')} {ev['label']}{ms}")

    def show(result) -> None:
        print(f"\n{result.text}")
        for i, o in enumerate(result.options, 1):
            print(f"  {i}. {o['name']} ({o['meta']}): {o['slot']}, {fmt_inr(o['price'])}")
        if result.approval:
            a = result.approval
            print(f"\n  {BOLD}Approve:{RESET} {a['action']} {a['provider']}, {a['when']}, {fmt_inr(a['price'])}"
                  f"  {DIM}(approval {a['id']}, fingerprint {a['fingerprint']}){RESET}")
            print("  Reply “yes” to approve or “no” to decline.")
        if result.chips:
            print(f"  {DIM}Try: " + " | ".join(result.chips) + RESET)
        print()

    if agent.has_open_writes(session):
        show(agent.handle(session, UserInput(kind="resume"), on_event))
    while True:
        try:
            text = input(f"{BOLD}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            if cmd in ("/quit", "/exit"):
                return
            if cmd == "/help":
                print(__doc__)
            elif cmd == "/faults":
                for k, v in FAULTS.items():
                    print(f"  {k:26} {v[2]}")
                print(f"  armed: {world.provider.armed_faults()}")
            elif cmd == "/fault" and arg in FAULTS:
                tool, outcome, label = FAULTS[arg]
                world.provider.arm(tool, outcome)
                print(f"  armed: {label}")
            elif cmd == "/clock":
                world.clock.advance(minutes=int(arg or 11))
                print(f"  agent clock moved forward {int(arg or 11)} minutes")
            elif cmd == "/memory":
                print(json.dumps(agent.memory.view(settings.user_id), indent=1, ensure_ascii=False))
            elif cmd == "/state":
                print(json.dumps(agent.snapshot(session)["state"], indent=1, ensure_ascii=False))
            elif cmd == "/trace":
                print(f"  {os.path.abspath(agent.tracer.path(session))}")
            elif cmd == "/new":
                session = f"cli-{uuid.uuid4().hex[:8]}"
                print(f"  new session {session}")
            elif cmd == "/reset":
                from .server import App
                App(world).reset()
                print("  world reset")
            else:
                print("  unknown command; /help lists them")
            continue
        try:
            show(agent.handle(session, UserInput(text=text), on_event))
        except InjectedCrash as crash:
            print(f"\n  ■ Injected crash at {crash.point}. Restarting the agent on the same data…\n")
            world.close()
            world = build(settings, clock=world.clock)
            agent = world.agent
            show(agent.handle(session, UserInput(kind="resume"), on_event))
