"""python -m errand [serve|chat|evals]"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="errand", description="Home-services booking agent")
    sub = parser.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="web console with chat and live trace")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--fast", action="store_true", help="shorten simulated provider latency")
    s.add_argument("--no-supervise", action="store_true", help="do not restart after a demo crash")
    s.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    c = sub.add_parser("chat", help="terminal chat")
    c.add_argument("--slow", action="store_true", help="simulate provider latency")
    sub.add_parser("evals", help="run eval scenarios and write reports/eval_table.md")
    args = parser.parse_args()

    if args.cmd == "serve":
        from .server import serve, supervise
        if args.child or args.no_supervise:
            serve(args.host, args.port, 0.35 if args.fast else 1.0)
            return 0
        extra = ["--host", args.host, "--port", str(args.port)] + (["--fast"] if args.fast else [])
        return supervise(extra)
    if args.cmd == "chat":
        from .cli import main as chat
        chat(1.0 if args.slow else 0.0)
        return 0
    if args.cmd == "evals":
        from evals.run import main as run
        return run()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
