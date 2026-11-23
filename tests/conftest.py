"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from property_scout.config import AppConfig

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_listings.json"


@pytest.fixture
def config() -> AppConfig:
    """Default-valid config (weights sum to 100)."""
    return AppConfig()


@pytest.fixture
def sample_path() -> Path:
    return SAMPLE
