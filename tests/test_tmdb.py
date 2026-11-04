"""Tests for the TMDb client."""

from __future__ import annotations

import logging

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.google_tv_media_aggregator.tmdb import (
    TmdbAuthError,
    TmdbClient,
    TmdbConnectionError,
    TmdbRateLimitError,
    is_bearer_token,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

API = "https://api.themoviedb.org/3"
V3_KEY = "0123456789abcdef0123456789abcdef"
V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"


def test_is_bearer_token() -> None:
    assert is_bearer_token(V4_TOKEN)
    assert not is_bearer_token(V3_KEY)


async def test_v3_key_sent_as_query_param(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/search/movie",
        json={"results": [{"id": 1, "title": "Heat", "poster_path": "/heat.jpg"}]},
    )
    client = TmdbClient(async_get_clientsession(hass), V3_KEY, "en-AU")
    match = await client.async_search_movie("Heat", 1995)

    assert match is not None
    assert match.tmdb_id == 1
    assert match.poster_url == "https://image.tmdb.org/t/p/w500/heat.jpg"
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert url.query["api_key"] == V3_KEY
    assert url.query["language"] == "en-AU"
    assert url.query["year"] == "1995"
    assert "Authorization" not in headers


async def test_v4_token_sent_as_bearer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/configuration", json={"images": {}})
    client = TmdbClient(async_get_clientsession(hass), V4_TOKEN)
    await client.async_validate()

    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert "api_key" not in url.query
    assert "language" not in url.query
    assert headers["Authorization"] == f"Bearer {V4_TOKEN}"


async def test_results_without_poster_skipped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/search/tv",
        json={
            "results": [
                {"id": 1, "name": "No poster", "poster_path": None},
                {"id": 2, "name": "The Office", "poster_path": "/office.jpg"},
            ]
        },
    )
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    match = await client.async_search_tv("The Office")
    assert match is not None
    assert match.tmdb_id == 2
    assert match.media_type == "tv"


async def test_search_multi_ignores_people(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/search/multi",
        json={
            "results": [
                {"id": 1, "media_type": "person", "profile_path": "/p.jpg"},
                {"id": 2, "media_type": "movie", "title": "X", "poster_path": "/x.jpg"},
            ]
        },
    )
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    match = await client.async_search_multi("X")
    assert match is not None
    assert match.tmdb_id == 2


async def test_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/configuration", status=401)
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    with pytest.raises(TmdbAuthError):
        await client.async_validate()


async def test_not_found_returns_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/tv/99", status=404)
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    assert await client.async_get_tv(99) is None


async def test_rate_limit_blocks_requests(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/search/movie", status=429, headers={"Retry-After": "30"})
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    with pytest.raises(TmdbRateLimitError):
        await client.async_search_movie("Heat")
    with pytest.raises(TmdbRateLimitError):
        await client.async_search_movie("Heat")
    assert aioclient_mock.call_count == 1


async def test_connection_error_does_not_leak_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/configuration",
        exc=aiohttp.ClientConnectionError(f"failed {API}?api_key={V3_KEY}"),
    )
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    with pytest.raises(TmdbConnectionError) as err:
        await client.async_validate()
    assert V3_KEY not in str(err.value)


@pytest.mark.parametrize("api_key", [V3_KEY, V4_TOKEN])
async def test_debug_logging_does_not_leak_key(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    api_key: str,
) -> None:
    caplog.set_level(
        logging.DEBUG, logger="custom_components.google_tv_media_aggregator"
    )
    aioclient_mock.get(
        f"{API}/search/movie",
        json={"results": [{"id": 1, "title": "Heat", "poster_path": "/heat.jpg"}]},
    )
    aioclient_mock.get(f"{API}/configuration", exc=aiohttp.ClientConnectionError())
    client = TmdbClient(async_get_clientsession(hass), api_key, "en-AU")
    await client.async_search_movie("Heat", 1995)
    with pytest.raises(TmdbConnectionError):
        await client.async_validate()

    assert "TMDb GET /search/movie" in caplog.text
    assert "'query': 'Heat'" in caplog.text
    assert "using movie 1 'Heat'" in caplog.text
    assert "TMDb /configuration failed: ClientConnectionError" in caplog.text
    assert api_key not in caplog.text


async def test_find_by_imdb_prefers_type(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/find/tt0000001",
        json={
            "movie_results": [{"id": 1, "title": "Movie", "poster_path": "/m.jpg"}],
            "tv_results": [{"id": 2, "name": "Show", "poster_path": "/s.jpg"}],
        },
    )
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    tv = await client.async_find_by_imdb("tt0000001", "tv")
    movie = await client.async_find_by_imdb("tt0000001", "movie")
    assert tv is not None
    assert tv.tmdb_id == 2
    assert movie is not None
    assert movie.tmdb_id == 1
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query["external_source"] == "imdb_id"


async def test_find_by_imdb_episode_resolves_show(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/find/tt0959621",
        json={"tv_episode_results": [{"id": 62085, "show_id": 1396}]},
    )
    aioclient_mock.get(
        f"{API}/tv/1396",
        json={"id": 1396, "name": "Breaking Bad", "poster_path": "/bb.jpg"},
    )
    client = TmdbClient(async_get_clientsession(hass), V3_KEY)
    match = await client.async_find_by_imdb("tt0959621")
    assert match is not None
    assert match.tmdb_id == 1396
    assert match.media_type == "tv"
