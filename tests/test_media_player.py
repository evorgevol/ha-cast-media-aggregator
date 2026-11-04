"""Tests for the aggregated media player entity."""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.google_tv_media_aggregator.const import (
    CONF_PREFER_SOURCE_IMAGE,
    CONF_STREMIO_ENTITY_ID,
    DOMAIN,
)
from homeassistant.components.media_player import (
    DATA_COMPONENT,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import EVENT_CALL_SERVICE, Event, HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import SOURCE, TMDB_KEY, make_entry, player_subentry, setup_integration

API = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w500/inception.jpg"
JPEG = b"\xff\xd8\xff\xe0inception"
ENTITY = "media_player.pablo_aggregated"
STREMIO = "media_player.stremio"
FEATURES = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.GROUPING
)


def set_source(hass: HomeAssistant, state: str = "playing", **attrs: Any) -> None:
    """Set the source player's state."""
    hass.states.async_set(
        SOURCE,
        state,
        {
            "friendly_name": "Pablo",
            "supported_features": int(FEATURES),
            "app_id": "com.netflix.ninja",
            "app_name": "Netflix",
            **attrs,
        },
    )


def mock_inception(aioclient_mock: AiohttpClientMocker) -> None:
    """Register TMDb search and poster responses for Inception."""
    aioclient_mock.get(
        f"{API}/search/movie",
        json={
            "results": [
                {"id": 27205, "title": "Inception", "poster_path": "/inception.jpg"}
            ]
        },
    )
    aioclient_mock.get(POSTER, content=JPEG, headers={"Content-Type": "image/jpeg"})


async def get_image(hass: HomeAssistant, entity_id: str = ENTITY):
    """Return the image served through the media player proxy."""
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    return await entity.async_get_media_image()


def api_calls(aioclient_mock: AiohttpClientMocker) -> list[str]:
    """Return requested URLs (without query)."""
    return [str(url.with_query(None)) for _, url, _, _ in aioclient_mock.mock_calls]


async def test_mirrors_source(hass: HomeAssistant, cache_dir: Path) -> None:
    set_source(
        hass,
        media_title="Inception",
        media_content_type="movie",
        volume_level=0.4,
        source_list=["HDMI 1"],
    )
    await setup_integration(hass, make_entry(cache_dir, api_key=None))

    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == "playing"
    assert state.name == "Pablo Aggregated"
    assert state.attributes["media_title"] == "Inception"
    assert state.attributes["app_id"] == "com.netflix.ninja"
    assert state.attributes["volume_level"] == 0.4
    assert state.attributes["source_list"] == ["HDMI 1"]
    assert state.attributes["source_entity_id"] == SOURCE
    assert state.attributes["image_source"] == "none"
    assert state.attributes["supported_features"] == int(
        FEATURES & ~MediaPlayerEntityFeature.GROUPING
    )

    set_source(hass, "paused", media_title="Inception")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).state == "paused"

    hass.states.async_set(SOURCE, "unavailable")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).state == "unavailable"


async def test_movie_poster_cached(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    set_source(hass, media_title="Inception")
    mock_inception(aioclient_mock)
    await setup_integration(hass, make_entry(cache_dir))

    state = hass.states.get(ENTITY)
    assert state.attributes["image_source"] == "aggregator"
    assert state.attributes["aggregator"] == "default"
    assert state.attributes["media_kind"] == "movie"
    assert state.attributes["tmdb_id"] == 27205
    assert state.attributes["entity_picture"].startswith(
        f"/api/media_player_proxy/{ENTITY}?token="
    )
    assert (cache_dir / "movie" / "inception.jpg").read_bytes() == JPEG
    assert await get_image(hass) == (JPEG, "image/jpeg")
    assert api_calls(aioclient_mock) == [f"{API}/search/movie", POSTER]
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query["language"] == "en-AU"

    # Position updates don't trigger new lookups.
    set_source(hass, media_title="Inception", media_position=120)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 2


async def test_cache_checked_before_api(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    (cache_dir / "movie").mkdir(parents=True)
    (cache_dir / "movie" / "inception.png").write_bytes(b"png")
    set_source(hass, media_title="Inception")
    await setup_integration(hass, make_entry(cache_dir))

    state = hass.states.get(ENTITY)
    assert state.attributes["image_source"] == "aggregator"
    assert await get_image(hass) == (b"png", "image/png")
    assert aioclient_mock.call_count == 0


async def test_cache_disabled_uses_remote_url(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    set_source(hass, media_title="Inception")
    mock_inception(aioclient_mock)
    await setup_integration(hass, make_entry(cache_dir, cache_enabled=False))

    state = hass.states.get(ENTITY)
    assert state.attributes["image_source"] == "aggregator"
    assert state.attributes["entity_picture"] == POSTER
    assert api_calls(aioclient_mock) == [f"{API}/search/movie"]
    assert not (cache_dir / "movie").exists()


async def test_tv_show(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/search/tv",
        json={"results": [{"id": 2316, "name": "The Office", "poster_path": "/o.jpg"}]},
    )
    aioclient_mock.get(
        "https://image.tmdb.org/t/p/w500/o.jpg",
        content=JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    set_source(hass, media_title="The Office", media_artist="S2 E1 The Dundies")
    await setup_integration(hass, make_entry(cache_dir))

    state = hass.states.get(ENTITY)
    assert state.attributes["media_kind"] == "tv"
    assert state.attributes["tmdb_id"] == 2316
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query["query"] == "The Office"
    assert (cache_dir / "tv" / "the_office.jpg").exists()


async def test_music_skipped_uses_app_fallback(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    (cache_dir / "default_apps").mkdir(parents=True)
    (cache_dir / "default_apps" / "spotify.png").write_bytes(b"spotify")
    set_source(
        hass,
        media_title="Song",
        media_artist="Band",
        media_duration=200,
        app_id="com.spotify.tv.android",
        app_name="Spotify",
    )
    await setup_integration(hass, make_entry(cache_dir))

    state = hass.states.get(ENTITY)
    assert state.attributes["image_source"] == "app_fallback"
    assert "aggregator" not in state.attributes
    assert state.attributes["entity_picture"].startswith("/api/media_player_proxy/")
    assert await get_image(hass) == (b"spotify", "image/png")
    assert aioclient_mock.call_count == 0


async def test_app_fallback_when_not_found(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    (cache_dir / "default_apps").mkdir(parents=True)
    (cache_dir / "default_apps" / "default.jpg").write_bytes(b"generic")
    aioclient_mock.get(f"{API}/search/movie", json={"results": []})
    aioclient_mock.get(f"{API}/search/multi", json={"results": []})
    set_source(hass, media_title="Obscure Thing")
    await setup_integration(hass, make_entry(cache_dir))

    state = hass.states.get(ENTITY)
    assert state.attributes["image_source"] == "app_fallback"
    assert await get_image(hass) == (b"generic", "image/jpeg")


async def test_app_fallback_without_title_or_tmdb(
    hass: HomeAssistant, cache_dir: Path
) -> None:
    (cache_dir / "default_apps").mkdir(parents=True)
    (cache_dir / "default_apps" / "netflix.webp").write_bytes(b"netflix")
    set_source(hass, "idle")
    await setup_integration(hass, make_entry(cache_dir, api_key=None))

    assert hass.states.get(ENTITY).attributes["image_source"] == "app_fallback"


async def test_prefers_source_image(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    set_source(hass, media_title="Inception", entity_picture="/api/source/pic")
    await setup_integration(hass, make_entry(cache_dir))

    state = hass.states.get(ENTITY)
    assert state.attributes["image_source"] == "source"
    assert state.attributes["entity_picture"] == "/api/source/pic"
    assert aioclient_mock.call_count == 0


async def test_aggregator_over_source_image_when_disabled(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    set_source(hass, media_title="Inception", entity_picture="/api/source/pic")
    mock_inception(aioclient_mock)
    entry = make_entry(
        cache_dir,
        subentries=[player_subentry({CONF_PREFER_SOURCE_IMAGE: False})],
    )
    await setup_integration(hass, entry)

    assert hass.states.get(ENTITY).attributes["image_source"] == "aggregator"


async def test_off_clears_artwork(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    set_source(hass, media_title="Inception")
    mock_inception(aioclient_mock)
    await setup_integration(hass, make_entry(cache_dir))
    assert hass.states.get(ENTITY).attributes["image_source"] == "aggregator"

    set_source(hass, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(ENTITY)
    assert state.state == "off"
    assert state.attributes["image_source"] == "none"
    assert "entity_picture" not in state.attributes


async def test_stremio(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/find/tt0903747",
        json={
            "tv_results": [
                {"id": 1396, "name": "Breaking Bad", "poster_path": "/bb.jpg"}
            ]
        },
    )
    aioclient_mock.get(
        "https://image.tmdb.org/t/p/w500/bb.jpg",
        content=JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    hass.states.async_set(
        STREMIO,
        "playing",
        {
            "imdb_id": "tt0903747",
            "type": "series",
            "media_title": "Breaking Bad",
            "series_title": "Breaking Bad",
        },
    )
    set_source(
        hass, media_title="Breaking Bad", app_id="com.stremio.one", app_name="Stremio"
    )
    entry = make_entry(
        cache_dir, subentries=[player_subentry({CONF_STREMIO_ENTITY_ID: STREMIO})]
    )
    await setup_integration(hass, entry)

    state = hass.states.get(ENTITY)
    assert state.attributes["aggregator"] == "stremio"
    assert state.attributes["imdb_id"] == "tt0903747"
    assert state.attributes["tmdb_id"] == 1396
    assert state.attributes["media_kind"] == "tv"
    assert (cache_dir / "imdb" / "tt0903747.jpg").exists()


async def test_stremio_poster_fallback(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    poster = "https://images.metahub.space/poster/medium/tt1375666/img"
    aioclient_mock.get(f"{API}/find/tt1375666", json={})
    aioclient_mock.get(poster, content=JPEG, headers={"Content-Type": "image/jpeg"})
    hass.states.async_set(
        STREMIO,
        "playing",
        {
            "imdb_id": "tt1375666",
            "type": "movie",
            "media_title": "Inception",
            "poster": poster,
        },
    )
    set_source(hass, media_title="Inception", app_id="com.stremio.one")
    entry = make_entry(
        cache_dir, subentries=[player_subentry({CONF_STREMIO_ENTITY_ID: STREMIO})]
    )
    await setup_integration(hass, entry)

    state = hass.states.get(ENTITY)
    assert state.attributes["aggregator"] == "stremio"
    assert state.attributes["imdb_id"] == "tt1375666"
    assert (cache_dir / "stremio" / "tt1375666.jpg").exists()


async def test_stremio_state_change_triggers_lookup(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/find/tt1375666",
        json={
            "movie_results": [
                {"id": 27205, "title": "Inception", "poster_path": "/i.jpg"}
            ]
        },
    )
    aioclient_mock.get(
        "https://image.tmdb.org/t/p/w500/i.jpg",
        content=JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    aioclient_mock.get(f"{API}/search/movie", json={"results": []})
    aioclient_mock.get(f"{API}/search/multi", json={"results": []})
    hass.states.async_set(STREMIO, "idle", {})
    set_source(hass, media_title="Inception", app_id="com.stremio.one")
    entry = make_entry(
        cache_dir, subentries=[player_subentry({CONF_STREMIO_ENTITY_ID: STREMIO})]
    )
    await setup_integration(hass, entry)
    assert hass.states.get(ENTITY).attributes["image_source"] == "none"

    # The Stremio integration updates after the Google TV player.
    hass.states.async_set(
        STREMIO,
        "playing",
        {"imdb_id": "tt1375666", "type": "movie", "media_title": "Inception"},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(ENTITY)
    assert state.attributes["aggregator"] == "stremio"
    assert state.attributes["tmdb_id"] == 27205


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/search/movie", status=401)
    set_source(hass, media_title="Inception")
    entry = make_entry(cache_dir)
    await setup_integration(hass, entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert hass.states.get(ENTITY).attributes["image_source"] == "none"


async def test_transient_error_retries(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/search/movie", status=500)
    set_source(hass, media_title="Inception")
    await setup_integration(hass, make_entry(cache_dir))
    assert hass.states.get(ENTITY).attributes["image_source"] == "none"

    aioclient_mock.clear_requests()
    mock_inception(aioclient_mock)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).attributes["image_source"] == "aggregator"


async def test_service_forwarding(hass: HomeAssistant, cache_dir: Path) -> None:
    set_source(hass)
    await setup_integration(hass, make_entry(cache_dir, api_key=None))
    entity = hass.data[DATA_COMPONENT].get_entity(ENTITY)

    pause = async_mock_service(hass, "media_player", "media_pause")
    volume = async_mock_service(hass, "media_player", "volume_set")
    play_media = async_mock_service(hass, "media_player", "play_media")

    await entity.async_media_pause()
    await entity.async_set_volume_level(0.25)
    await entity.async_play_media("movie", "abc", enqueue=None)

    assert pause[0].data == {"entity_id": SOURCE}
    assert volume[0].data == {"entity_id": SOURCE, "volume_level": 0.25}
    assert play_media[0].data == {
        "entity_id": SOURCE,
        "media_content_type": "movie",
        "media_content_id": "abc",
    }


async def test_service_call_through_entity(
    hass: HomeAssistant, cache_dir: Path
) -> None:
    calls: list[Event] = []
    set_source(hass)
    await setup_integration(hass, make_entry(cache_dir, api_key=None))

    hass.bus.async_listen(EVENT_CALL_SERVICE, calls.append)
    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": ENTITY}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    targets = [str(c.data["service_data"]["entity_id"]) for c in calls]
    assert ENTITY in targets[0]
    assert SOURCE in targets[1]


def history(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the entity's lookup traces as dicts."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return [
        lookup.as_dict()
        for traces in entry.runtime_data.lookup_history.values()
        for lookup in traces
    ]


async def test_debug_logging(
    hass: HomeAssistant,
    cache_dir: Path,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.DEBUG, logger="custom_components.google_tv_media_aggregator"
    )
    set_source(hass, media_title="Inception")
    mock_inception(aioclient_mock)
    await setup_integration(hass, make_entry(cache_dir))

    text = caplog.text
    assert f"{ENTITY}: tracking {SOURCE};" in text
    assert f"{ENTITY}: looking up artwork (trigger: startup; changed: -)" in text
    assert "stremio aggregator skipped: no Stremio media player configured" in text
    assert "default: classified as movie (duration unknown; no media_artist)" in text
    assert "default: no cached image for 'movie/Inception'" in text
    assert "TMDb GET /search/movie" in text
    assert "default: saved image to" in text
    assert "default aggregator found artwork" in text
    assert (
        f"{ENTITY}: artwork: {{'status': 'done', 'image_source': 'aggregator'" in text
    )
    assert TMDB_KEY not in text

    # Position updates keep the key, so nothing is looked up or logged.
    caplog.clear()
    set_source(hass, media_title="Inception", media_position=120)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "looking up artwork" not in caplog.text

    set_source(hass, media_title="Inception", app_name="Plex")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert f"trigger: {SOURCE} changed; changed: app_name)" in caplog.text
    assert "default: cached image for 'movie/Inception'" in caplog.text

    set_source(hass, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert f"{ENTITY}: {SOURCE} is off; clearing artwork" in caplog.text

    lookups = history(hass)
    assert len(lookups) == 2
    first = lookups[0]
    assert first["trigger"] == "startup"
    assert first["context"]["title"] == "Inception"
    assert first["outcome"]["status"] == "done"
    assert first["outcome"]["aggregator"] == "default"
    assert first["outcome"]["tmdb_id"] == 27205
    assert first["outcome"]["file"].endswith("inception.jpg")
    assert any("TMDb GET /search/movie" in step for step in first["steps"])
    assert all(step.startswith("+") for step in first["steps"])


async def test_trace_remembered_miss(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/search/movie", json={"results": []})
    aioclient_mock.get(f"{API}/search/multi", json={"results": []})
    set_source(hass, media_title="Obscure Thing")
    await setup_integration(hass, make_entry(cache_dir))
    set_source(hass, media_title="Obscure Thing", app_name="Plex")
    await hass.async_block_till_done(wait_background_tasks=True)

    first, second = history(hass)
    assert any(
        "default: nothing found for 'movie/Obscure Thing'; remembering that for 6h"
        in step
        for step in first["steps"]
    )
    assert second["changed"] == ["app_name"]
    assert any(
        "'movie/Obscure Thing' found nothing recently; not looking it up again for 5h"
        in step
        for step in second["steps"]
    )
    assert second["outcome"] == {"status": "done", "image_source": "none"}
    assert aioclient_mock.call_count == 2


async def test_trace_source_image_preferred(
    hass: HomeAssistant, cache_dir: Path
) -> None:
    set_source(hass, media_title="Inception", entity_picture="/api/source/pic")
    await setup_integration(hass, make_entry(cache_dir))

    (lookup,) = history(hass)
    assert lookup["context"]["has_source_picture"] is True
    assert "'prefer source image' is on; not searching" in lookup["steps"][0]
    assert lookup["outcome"] == {"status": "done", "image_source": "source"}


async def test_trace_retry(
    hass: HomeAssistant, cache_dir: Path, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/search/movie", status=500)
    set_source(hass, media_title="Inception")
    await setup_integration(hass, make_entry(cache_dir))

    (lookup,) = history(hass)
    steps = "\n".join(lookup["steps"])
    assert "default aggregator failed: TMDb returned HTTP 500" in steps
    assert "nothing found, but an aggregator hit a temporary error" in steps
    assert "retrying in 1m 0s (retry 1 of 5)" in steps

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert history(hass)[-1]["trigger"] == "retry 1 of 5"
