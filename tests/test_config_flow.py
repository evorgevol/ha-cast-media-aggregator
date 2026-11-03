"""Tests for the config, options and subentry flows."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.google_tv_media_aggregator.const import (
    CONF_APP_FILTER_LIST,
    CONF_APP_FILTER_MODE,
    CONF_CACHE_DIR,
    CONF_CACHE_ENABLED,
    CONF_NAME,
    CONF_PREFER_SOURCE_IMAGE,
    CONF_SOURCE_ENTITY_ID,
    CONF_STREMIO_APP_IDS,
    CONF_STREMIO_ENTITY_ID,
    CONF_TMDB_API_KEY,
    CONF_TMDB_LANGUAGE,
    CONF_VIDEO_THRESHOLD,
    DEFAULT_STREMIO_APP_IDS,
    DOMAIN,
    SUBENTRY_TYPE_PLAYER,
)
from custom_components.google_tv_media_aggregator.tmdb import (
    TmdbAuthError,
    TmdbConnectionError,
)
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from .conftest import SOURCE, TMDB_KEY, make_entry, setup_integration

VALIDATE = (
    "custom_components.google_tv_media_aggregator.config_flow.TmdbClient.async_validate"
)


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_with_key(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(VALIDATE, side_effect=TmdbAuthError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TMDB_API_KEY: "bad"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(VALIDATE, side_effect=TmdbConnectionError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TMDB_API_KEY: TMDB_KEY}
        )
    assert result["errors"] == {"base": "cannot_connect"}

    with patch(VALIDATE, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TMDB_API_KEY: f" {TMDB_KEY} "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_TMDB_API_KEY: TMDB_KEY}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_without_key(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE) as validate:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    validate.assert_not_called()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_single_instance(hass: HomeAssistant, cache_dir: Path) -> None:
    make_entry(cache_dir).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure(hass: HomeAssistant, cache_dir: Path) -> None:
    entry = make_entry(cache_dir)
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(VALIDATE, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TMDB_API_KEY: "new-key"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_TMDB_API_KEY: "new-key"}

    # Blank removes the key.
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TMDB_API_KEY: ""}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth(hass: HomeAssistant, cache_dir: Path) -> None:
    entry = make_entry(cache_dir)
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(VALIDATE, side_effect=TmdbAuthError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TMDB_API_KEY: "still-bad"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(VALIDATE, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TMDB_API_KEY: "good"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_TMDB_API_KEY: "good"}


async def test_options_flow(
    hass: HomeAssistant, cache_dir: Path, tmp_path: Path
) -> None:
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VIDEO_THRESHOLD: 30,
            CONF_CACHE_ENABLED: True,
            CONF_CACHE_DIR: "relative/path",
        },
    )
    assert result["errors"] == {CONF_CACHE_DIR: "cache_dir_not_absolute"}

    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VIDEO_THRESHOLD: 30,
            CONF_CACHE_ENABLED: True,
            CONF_CACHE_DIR: str(outside),
        },
    )
    assert result["errors"] == {CONF_CACHE_DIR: "cache_dir_not_allowed"}

    new_dir = tmp_path / "artwork"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VIDEO_THRESHOLD: 30.0,
            CONF_CACHE_ENABLED: True,
            CONF_CACHE_DIR: f" {new_dir} ",
            CONF_TMDB_LANGUAGE: " de-DE ",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_VIDEO_THRESHOLD: 30,
        CONF_CACHE_ENABLED: True,
        CONF_CACHE_DIR: str(new_dir),
        CONF_TMDB_LANGUAGE: "de-DE",
    }
    assert (new_dir / "default_apps").is_dir()
    await hass.async_block_till_done()
    assert entry.runtime_data.settings.video_threshold_minutes == 30
    assert entry.runtime_data.settings.language == "de-DE"


async def test_options_flow_cache_disabled_skips_validation(
    hass: HomeAssistant, cache_dir: Path, tmp_path: Path
) -> None:
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VIDEO_THRESHOLD: 20,
            CONF_CACHE_ENABLED: False,
            CONF_CACHE_DIR: "relative/path",
        },
    )
    assert result["errors"] == {CONF_CACHE_DIR: "cache_dir_not_absolute"}

    # Outside allowlist_external_dirs, but accepted because caching is off.
    outside = tmp_path.parent / f"{tmp_path.name}_disabled"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_VIDEO_THRESHOLD: 20,
            CONF_CACHE_ENABLED: False,
            CONF_CACHE_DIR: str(outside),
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert not entry.runtime_data.cache.enabled
    assert not outside.exists()


async def _start_subentry_flow(hass: HomeAssistant, entry_id: str) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry_id, SUBENTRY_TYPE_PLAYER), context={"source": SOURCE_USER}
    )


async def test_add_player(hass: HomeAssistant, cache_dir: Path) -> None:
    hass.states.async_set(
        SOURCE,
        "playing",
        {
            "friendly_name": "Pablo",
            "app_id": "com.netflix.ninja",
            "app_name": "Netflix",
        },
    )
    entry = make_entry(cache_dir, subentries=[])
    await setup_integration(hass, entry)

    result = await _start_subentry_flow(hass, entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY_ID: SOURCE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Pablo Aggregated"
    await hass.async_block_till_done()

    subentry = next(iter(entry.subentries.values()))
    assert subentry.unique_id == SOURCE
    assert dict(subentry.data) == {
        CONF_SOURCE_ENTITY_ID: SOURCE,
        CONF_STREMIO_APP_IDS: list(DEFAULT_STREMIO_APP_IDS),
        CONF_APP_FILTER_MODE: "none",
        CONF_APP_FILTER_LIST: [],
        CONF_PREFER_SOURCE_IMAGE: True,
    }
    state = hass.states.get("media_player.pablo_aggregated")
    assert state is not None
    assert state.name == "Pablo Aggregated"


async def test_add_player_custom_name_and_options(
    hass: HomeAssistant, cache_dir: Path
) -> None:
    entry = make_entry(cache_dir, subentries=[])
    await setup_integration(hass, entry)

    result = await _start_subentry_flow(hass, entry.entry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE_ENTITY_ID: SOURCE,
            CONF_NAME: " Lounge TV ",
            CONF_STREMIO_ENTITY_ID: "media_player.stremio",
            CONF_STREMIO_APP_IDS: list(DEFAULT_STREMIO_APP_IDS),
            CONF_APP_FILTER_MODE: "deny",
            CONF_APP_FILTER_LIST: ["YouTube, Spotify", "Plex"],
            CONF_PREFER_SOURCE_IMAGE: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Lounge TV"
    assert result["data"][CONF_NAME] == "Lounge TV"
    assert result["data"][CONF_STREMIO_ENTITY_ID] == "media_player.stremio"
    assert result["data"][CONF_APP_FILTER_LIST] == ["YouTube", "Spotify", "Plex"]
    await hass.async_block_till_done()
    assert hass.states.get("media_player.lounge_tv") is not None


async def test_add_player_errors(hass: HomeAssistant, cache_dir: Path) -> None:
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)

    result = await _start_subentry_flow(hass, entry.entry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY_ID: SOURCE}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCE_ENTITY_ID: "source_already_configured"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE_ENTITY_ID: "media_player.kitchen",
            CONF_STREMIO_ENTITY_ID: "media_player.kitchen",
        },
    )
    assert result["errors"] == {CONF_STREMIO_ENTITY_ID: "stremio_is_source"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY_ID: "media_player.kitchen"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 2
    assert hass.states.get("media_player.pablo_aggregated") is not None
    assert hass.states.get("media_player.kitchen_aggregated") is not None


async def test_reconfigure_player(hass: HomeAssistant, cache_dir: Path) -> None:
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)
    subentry_id = next(iter(entry.subentries))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("media_player", DOMAIN, subentry_id)
    assert entity_id == "media_player.pablo_aggregated"

    # MockConfigEntry.start_subentry_reconfigure_flow changed signature across
    # HA versions, so start the flow directly.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PLAYER),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE_ENTITY_ID: SOURCE,
            CONF_NAME: "Lounge TV",
            CONF_APP_FILTER_MODE: "allow",
            CONF_APP_FILTER_LIST: ["Netflix"],
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Lounge TV"
    assert subentry.data[CONF_APP_FILTER_MODE] == "allow"
    # The entity id is kept; only the name changes.
    assert (
        registry.async_get_entity_id("media_player", DOMAIN, subentry_id) == entity_id
    )
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.name == "Lounge TV"
