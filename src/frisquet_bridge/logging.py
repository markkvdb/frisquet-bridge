"""Logging setup for long-running bridge processes."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from frisquet_bridge.config import LoggingConfig
from frisquet_bridge.frame import MSG_TYPE_NAMES, Frame, device_name


class RawMessageRecorder:
    """Append raw serial/RF traffic as rotating JSON lines."""

    def __init__(
        self,
        path: str,
        *,
        max_bytes: int,
        backup_count: int,
        include_lines: bool = False,
    ) -> None:
        self.include_lines = include_lines
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger("frisquet_bridge.raw")
        self._logger.handlers.clear()
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)

    def record(self, event: str, **fields: object) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))

    def record_line(self, direction: str, line: str) -> None:
        if self.include_lines:
            self.record("serial_line", direction=direction, line=line)

    def record_frame(
        self,
        direction: str,
        frame: Frame,
        raw: bytes,
        *,
        rssi: int | None = None,
        outcome: str | None = None,
    ) -> None:
        fields: dict[str, object] = {
            "direction": direction,
            "raw_hex": raw.hex(),
            "payload_hex": frame.payload_hex,
            "payload_len": len(frame.payload),
            "to_addr": f"0x{frame.to_addr:02x}",
            "to": device_name(frame.to_addr),
            "from_addr": f"0x{frame.from_addr:02x}",
            "from": device_name(frame.from_addr),
            "association_id": f"0x{frame.association_id:02x}",
            "request_id": f"0x{frame.request_id:02x}",
            "control": f"0x{frame.control:02x}",
            "msg_type": f"0x{frame.msg_type:02x}",
            "msg_name": MSG_TYPE_NAMES.get(frame.msg_type),
            "ack": frame.is_ack,
        }
        if rssi is not None:
            fields["rssi"] = rssi
        if outcome is not None:
            fields["outcome"] = outcome
        self.record("rf_frame", **fields)


def configure_logging(
    config: LoggingConfig,
    *,
    level: str | None = None,
    log_format: str | None = None,
    log_file: str | None = None,
    raw_log_file: str | None = None,
) -> RawMessageRecorder | None:
    """Configure application logging and return an optional raw recorder."""

    effective_level = (level or config.level).upper()
    effective_format = log_format or config.format
    effective_file = config.file if log_file is None else log_file
    effective_raw_file = config.raw_file if raw_log_file is None else raw_log_file

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Any
    if effective_format == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if effective_file:
        log_path = Path(effective_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=config.file_max_bytes,
                backupCount=config.file_backup_count,
                encoding="utf-8",
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(_level(effective_level))
    for handler in handlers:
        root.addHandler(handler)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return (
        RawMessageRecorder(
            effective_raw_file,
            max_bytes=config.raw_max_bytes,
            backup_count=config.raw_backup_count,
            include_lines=config.raw_lines,
        )
        if effective_raw_file
        else None
    )


def bind_process_context(**fields: object) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**fields)


def _level(name: str) -> int:
    return getattr(logging, name, logging.INFO)
