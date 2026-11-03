"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_tv_media_aggregator.const import (
    CONF_CACHE_DIR,
    CONF_CACHE_ENABLED,
    CONF_SOURCE_ENTITY_ID,
    CONF_TMDB_API_KEY,
    CONF_TMDB_LANGUAGE,
    CONF_VIDEO_THRESHOLD,
    DOMAIN,
    SUBENTRY_TYPE_PLAYER,
    TITLE,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant

TMDB_KEY = "0123456789abcdef0123456789abcdef"
SOURCE = "media_player.pablo"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""


@pytest.fixture
def cache_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Return an allowed cache directory."""
    path = tmp_path / "tv-streamer-media"
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    return path


def player_subentry(
    data: dict[str, Any] | None = None, title: str = "Pablo Aggregated"
) -> ConfigSubentryData:
    """Return subentry data for an aggregated player."""
    return ConfigSubentryData(
        data={CONF_SOURCE_ENTITY_ID: SOURCE, **(data or {})},
        subentry_type=SUBENTRY_TYPE_PLAYER,
        title=title,
        unique_id=(data or {}).get(CONF_SOURCE_ENTITY_ID, SOURCE),
    )


def make_entry(
    cache_dir: Path,
    *,
    api_key: str | None = TMDB_KEY,
    cache_enabled: bool = True,
    subentries: list[ConfigSubentryData] | None = None,
) -> MockConfigEntry:
    """Create a hub config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        data={CONF_TMDB_API_KEY: api_key} if api_key else {},
        options={
            CONF_VIDEO_THRESHOLD: 20,
            CONF_CACHE_ENABLED: cache_enabled,
            CONF_CACHE_DIR: str(cache_dir),
            CONF_TMDB_LANGUAGE: "en-AU",
        },
        subentries_data=subentries if subentries is not None else [player_subentry()],
    )


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Avoid setting up the integration in config flow tests."""
    with patch(
        "custom_components.google_tv_media_aggregator.async_setup_entry",
        return_value=True,
    ):
        yield


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
