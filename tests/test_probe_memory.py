from __future__ import annotations

import pytest

from frisquet_bridge.cli.probe_memory import changed_offsets, parse_address_specs
from frisquet_bridge.config import ConfigError


def test_parse_address_specs_single_and_range() -> None:
    assert parse_address_specs(["0x7a18", "0x79c4-0x79fc/0x1c"], default_step=0x10) == [
        0x79C4,
        0x79E0,
        0x79FC,
        0x7A18,
    ]


def test_parse_address_specs_colon_range_uses_default_step() -> None:
    assert parse_address_specs(["0x7a18:0x7a50"], default_step=0x1C) == [0x7A18, 0x7A34, 0x7A50]


def test_parse_address_specs_deduplicates_and_sorts() -> None:
    assert parse_address_specs(["0x7a34", "0x7a18-0x7a34/0x1c"], default_step=0x1C) == [0x7A18, 0x7A34]


def test_parse_address_specs_rejects_bad_range() -> None:
    with pytest.raises(ConfigError, match="range end"):
        parse_address_specs(["0x7a50-0x7a18"], default_step=0x1C)


def test_changed_offsets_reports_changed_added_and_removed_bytes() -> None:
    assert changed_offsets(bytes.fromhex("010203"), bytes.fromhex("01040305")) == [
        (1, 0x02, 0x04),
        (3, None, 0x05),
    ]
    assert changed_offsets(bytes.fromhex("010203"), bytes.fromhex("0102")) == [(2, 0x03, None)]
