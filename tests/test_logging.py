"""Tests for production logging helpers."""

from __future__ import annotations

import json
from pathlib import Path

from frisquet_bridge.frame import ADDR_BOILER, ADDR_CONNECT, MSG_READ, Frame
from frisquet_bridge.logging import RawMessageRecorder


def test_raw_message_recorder_writes_frame_metadata(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl"
    recorder = RawMessageRecorder(str(path), max_bytes=10_000, backup_count=1)
    frame = Frame(
        to_addr=ADDR_BOILER,
        from_addr=ADDR_CONNECT,
        association_id=0xEA,
        request_id=0x28,
        control=0x01,
        msg_type=MSG_READ,
        payload=bytes.fromhex("79e0001c"),
    )

    try:
        recorder.record_frame("tx", frame, frame.encode(), outcome="attempt")
    finally:
        recorder.close()

    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["event"] == "rf_frame"
    assert entry["direction"] == "tx"
    assert entry["to_addr"] == "0x80"
    assert entry["from_addr"] == "0x7e"
    assert entry["association_id"] == "0xea"
    assert entry["request_id"] == "0x28"
    assert entry["msg_name"] == "READ"
    assert entry["payload_hex"] == "79e0001c"
    assert entry["outcome"] == "attempt"


def test_raw_message_recorder_line_capture_is_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl"
    recorder = RawMessageRecorder(str(path), max_bytes=10_000, backup_count=1)
    try:
        recorder.record_line("rx", "HB")
    finally:
        recorder.close()

    assert not path.exists() or path.read_text(encoding="utf-8") == ""
