"""Tests for setup, unload, subentry lifecycle and diagnostics."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.google_tv_media_aggregator.const import (
    CONF_SOURCE_ENTITY_ID,
    CONF_TMDB_API_KEY,
    DOMAIN,
    SEEN_APPS_STORAGE_KEY,
)
from custom_components.google_tv_media_aggregator.diagnostics import (
    async_get_config_entry_diagnostics,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import SOURCE, TMDB_KEY, make_entry, player_subentry, setup_integration


async def test_setup_and_unload(
    hass: HomeAssistant, cache_dir: Path, hass_storage: dict[str, Any]
) -> None:
    hass.states.async_set(
        SOURCE, "playing", {"app_id": "com.netflix.ninja", "app_name": "Netflix"}
    )
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert (cache_dir / "default_apps").is_dir()
    assert entry.runtime_data.tmdb is not None
    assert entry.runtime_data.cache.writable
    assert entry.runtime_data.seen_apps.apps == {"com.netflix.ninja": "Netflix"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    # Seen apps are flushed on unload.
    assert hass_storage[SEEN_APPS_STORAGE_KEY]["data"] == {
        "com.netflix.ninja": "Netflix"
    }


async def test_seen_apps_saved_later(
    hass: HomeAssistant, cache_dir: Path, hass_storage: dict[str, Any]
) -> None:
    hass.states.async_set(
        SOURCE, "playing", {"app_id": "com.plexapp.android", "app_name": "Plex"}
    )
    await setup_integration(hass, make_entry(cache_dir))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass_storage[SEEN_APPS_STORAGE_KEY]["data"] == {
        "com.plexapp.android": "Plex"
    }


async def test_unusable_cache_dir_does_not_fail_setup(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    entry = make_entry(blocker / "cache")
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.cache.writable


async def test_multiple_players_and_remove_subentry(
    hass: HomeAssistant, cache_dir: Path
) -> None:
    entry = make_entry(
        cache_dir,
        subentries=[
            player_subentry(),
            player_subentry(
                {CONF_SOURCE_ENTITY_ID: "media_player.kitchen"},
                title="Kitchen Aggregated",
            ),
        ],
    )
    await setup_integration(hass, entry)
    assert hass.states.get("media_player.pablo_aggregated") is not None
    assert hass.states.get("media_player.kitchen_aggregated") is not None

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    subentry_id = next(
        sid
        for sid, sub in entry.subentries.items()
        if sub.data[CONF_SOURCE_ENTITY_ID] == SOURCE
    )
    entity_entry = entity_registry.async_get("media_player.pablo_aggregated")
    assert entity_entry is not None
    assert entity_entry.config_subentry_id == subentry_id
    device = device_registry.async_get(entity_entry.device_id)
    assert device is not None
    assert device.name == "Pablo Aggregated"
    assert (DOMAIN, subentry_id) in device.identifiers

    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    assert hass.states.get("media_player.pablo_aggregated") is None
    assert entity_registry.async_get("media_player.pablo_aggregated") is None
    assert device_registry.async_get(device.id) is None
    assert hass.states.get("media_player.kitchen_aggregated") is not None


async def test_remove_entry_clears_storage(
    hass: HomeAssistant, cache_dir: Path, hass_storage: dict[str, Any]
) -> None:
    hass.states.async_set(SOURCE, "playing", {"app_id": "com.netflix.ninja"})
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert SEEN_APPS_STORAGE_KEY not in hass_storage


async def test_diagnostics_redacts_key(hass: HomeAssistant, cache_dir: Path) -> None:
    hass.states.async_set(SOURCE, "idle", {})
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert TMDB_KEY not in str(diag)
    assert diag["entry"]["data"][CONF_TMDB_API_KEY] == "**REDACTED**"
    assert diag["tmdb_configured"] is True
    assert diag["aggregators"] == ["stremio", "default"]
    assert diag["players"][0]["entity_id"] == "media_player.pablo_aggregated"
