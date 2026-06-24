"""Shared fixtures for frisquet-bridge tests."""

from __future__ import annotations

import pytest

from tests.helpers import FakeTransport


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()
