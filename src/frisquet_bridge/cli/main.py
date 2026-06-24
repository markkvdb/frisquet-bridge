"""frisquet-bridge CLI — `uv run frisquet-bridge <command>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from frisquet_bridge.cli import listen, outside_temp, pair, read, recover_id, serve
from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import LoggingConfig, load
from frisquet_bridge.logging import RawMessageRecorder, bind_process_context, configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="frisquet-bridge",
        description="Host tools for the Frisquet RF bridge",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )
    add_logging_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    listen.register(sub)
    outside_temp.register(sub)
    pair.register(sub)
    read.register(sub)
    recover_id.register(sub)
    serve.register(sub)

    args = parser.parse_args(argv)
    raw_recorder = _configure_cli_logging(args)
    args.raw_recorder = raw_recorder
    try:
        return args.func(args)
    finally:
        if raw_recorder is not None:
            raw_recorder.close()


def _configure_cli_logging(args: argparse.Namespace) -> RawMessageRecorder | None:
    config = LoggingConfig()
    config_path = getattr(args, "config", None)
    if config_path is not None and Path(config_path).exists():
        config = load(config_path).logging
    if getattr(args, "raw_lines", False):
        config.raw_lines = True
    raw_recorder = configure_logging(
        config,
        level=getattr(args, "log_level", None),
        log_format=getattr(args, "log_format", None),
        log_file=getattr(args, "log_file", None),
        raw_log_file=getattr(args, "raw_log_file", None),
    )
    bind_process_context(command=args.command, config_path=config_path)
    return raw_recorder


if __name__ == "__main__":
    sys.exit(main())
