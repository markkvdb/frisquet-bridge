"""Shared CLI option helpers."""

from __future__ import annotations

import argparse


def add_logging_options(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_default else None
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=default,
        help="Override logging.level",
    )
    parser.add_argument("--log-format", choices=("console", "json"), default=default, help="Override logging.format")
    parser.add_argument("--log-file", default=default, help="Override logging.file")
    parser.add_argument("--raw-log-file", default=default, help="Override logging.raw_file")
    parser.add_argument(
        "--raw-lines",
        action="store_true",
        default=argparse.SUPPRESS if suppress_default else False,
        help="Also store raw host/modem serial lines in the raw log",
    )
