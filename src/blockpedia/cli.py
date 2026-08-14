"""The two supported Blockpedia product commands."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

import uvicorn


WEB_HOST = "127.0.0.1"
WEB_PORT = 8765
MCP_NOT_IMPLEMENTED_R4 = "MCP_NOT_IMPLEMENTED_R4"


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

    mcp_parser = subparsers.add_parser("mcp", help="MCP stdio entry point reserved for R4")
    mcp_parser.add_argument("--data-root", default=None, help="override the local data root")
    mcp_parser.set_defaults(handler=_run_mcp)
    return parser


def _run_web(args: argparse.Namespace) -> int:
    # Import lazily so the R4 placeholder does not require a Web adapter just
    # to report that MCP is not implemented.
    from .web import create_app

    app = create_app(data_root=args.data_root)
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level=args.log_level, access_log=False)
    return 0


def _run_mcp(args: argparse.Namespace) -> int:
    del args
    print(MCP_NOT_IMPLEMENTED_R4, file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


__all__ = ["MCP_NOT_IMPLEMENTED_R4", "WEB_HOST", "WEB_PORT", "build_parser", "main"]
