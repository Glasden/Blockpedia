"""The local Blockpedia Studio command."""

from __future__ import annotations

import argparse
from typing import Sequence

import uvicorn


WEB_HOST = "127.0.0.1"
WEB_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="block-index", description="Blockpedia local Index Studio")
    subparsers = parser.add_subparsers(dest="command", required=True)

    web_parser = subparsers.add_parser("web", help="start the loopback Index Studio")
    web_parser.add_argument("--data-root", default=None, help="override the local data root")
    web_parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
        help="set the local WebUI log level",
    )
    web_parser.set_defaults(handler=_run_web)

    return parser


def _run_web(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(data_root=args.data_root)
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level=args.log_level, access_log=False)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import platform
    if platform.python_implementation() != "CPython" or platform.python_version() != "3.14.7":
        raise SystemExit("Blockpedia requires CPython 3.14.7")
    return int(args.handler(args))


__all__ = ["WEB_HOST", "WEB_PORT", "build_parser", "main"]
