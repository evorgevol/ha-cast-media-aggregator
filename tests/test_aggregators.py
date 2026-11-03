"""Tests for the aggregators."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.google_tv_media_aggregator.aggregators import (
    AggregatorDeps,
    AggregatorRegistry,
    HubSettings,
    MediaContext,
    MediaKind,
    PlayerConfig,
    StremioInfo,
    TransientLookupError,
)
from custom_components.google_tv_media_aggregator.aggregators.base import (
    normalise_title,
    split_app_list,
)
from custom_components.google_tv_media_aggregator.aggregators.default import classify
from custom_components.google_tv_media_aggregator.aggregators.stremio import (
    extract_imdb_id,
    titles_match,
)
from custom_components.google_tv_media_aggregator.cache import ImageCache
from custom_components.google_tv_media_aggregator.const import (
    APP_FILTER_ALLOW,
    APP_FILTER_DENY,
)
from custom_components.google_tv_media_aggregator.tmdb import (
    TmdbAuthError,
    TmdbConnectionError,
    TmdbMatch,
)
from homeassistant.core import HomeAssistant

SOURCE = "media_player.pablo"


def ctx(**kwargs) -> MediaContext:
    """Build a media context."""
    return MediaContext(source_entity_id=SOURCE, state="playing", **kwargs)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (ctx(title="Inception"), MediaKind.MOVIE),
        (ctx(title="The Office", artist="Diversity Day"), MediaKind.TV),
        (ctx(title="Song", artist="Band", duration=200), MediaKind.MUSIC),
        (ctx(title="Inception", duration=8880), MediaKind.MOVIE),
        (ctx(title="Episode", artist="Show", duration=1800), MediaKind.TV),
        (ctx(title="Song", content_type="music"), MediaKind.MUSIC),
        (ctx(title="Inception", artist="x", content_type="movie"), MediaKind.MOVIE),
        (ctx(title="Short", duration=60, content_type="episode"), MediaKind.TV),
        (ctx(), MediaKind.UNKNOWN),
    ],
)
def test_classify(context: MediaContext, expected: MediaKind) -> None:
    assert classify(context, 20) is expected


def test_normalise_title() -> None:
    assert normalise_title("  Dune   (2021) ") == ("Dune", 2021)
    assert normalise_title("Blade Runner 2049") == ("Blade Runner 2049", None)


def test_split_app_list() -> None:
    assert split_app_list(None) == []
    assert split_app_list("a, b,,a") == ["a", "b"]
    assert split_app_list(["Netflix", "YouTube, Plex"]) == [
        "Netflix",
        "YouTube",
        "Plex",
    ]


def test_extract_imdb_id() -> None:
    assert extract_imdb_id("tt0903747:1:2") == "tt0903747"
    assert extract_imdb_id("kitsu:123") is None
    assert extract_imdb_id(None) is None


def test_titles_match() -> None:
    info = StremioInfo(
        entity_id="media_player.stremio",
        state="playing",
        imdb_id="tt0903747",
        media_type="series",
        title="Pilot",
        series_title="Breaking Bad",
        poster=None,
    )
    assert titles_match(ctx(title="Breaking Bad", artist="Pilot"), info)
    assert titles_match(ctx(title="breaking bad"), info)
    assert not titles_match(ctx(title="The Office"), info)
    assert not titles_match(ctx(), info)


def test_app_filter() -> None:
    netflix = ctx(title="x", app_id="com.netflix.ninja", app_name="Netflix")
    youtube = ctx(title="x", app_id="com.google.android.youtube.tv", app_name="YouTube")

    allow = PlayerConfig(
        SOURCE, app_filter_mode=APP_FILTER_ALLOW, app_filter_list=("netflix",)
    )
    assert allow.app_allowed(netflix)
    assert not allow.app_allowed(youtube)

    deny = PlayerConfig(
        SOURCE, app_filter_mode=APP_FILTER_DENY, app_filter_list=("YouTube",)
    )
    assert deny.app_allowed(netflix)
    assert not deny.app_allowed(youtube)

    assert PlayerConfig(SOURCE).app_allowed(youtube)


def test_is_stremio_app() -> None:
    player = PlayerConfig(SOURCE)
    assert player.is_stremio_app(ctx(app_id="com.stremio.one"))
    assert player.is_stremio_app(ctx(app_name="Stremio Beta"))
    assert not player.is_stremio_app(ctx(app_id="com.netflix.ninja"))


@pytest.fixture
async def deps(hass: HomeAssistant, tmp_path: Path) -> AggregatorDeps:
    """Aggregator deps with a mocked TMDb client and a disabled cache."""
    cache = ImageCache(hass, tmp_path, enabled=False)
    await cache.async_setup()
    tmdb = MagicMock()
    tmdb.async_search_movie = AsyncMock(return_value=None)
    tmdb.async_search_tv = AsyncMock(return_value=None)
    tmdb.async_search_multi = AsyncMock(return_value=None)
    tmdb.async_find_by_imdb = AsyncMock(return_value=None)
    return AggregatorDeps(
        session=MagicMock(),
        cache=cache,
        settings=HubSettings(20, False, str(tmp_path), None),
        tmdb=tmdb,
    )


def _match(tmdb_id: int, media_type: str = "movie") -> TmdbMatch:
    return TmdbMatch(tmdb_id, media_type, "x", f"/{tmdb_id}.jpg")


async def test_default_movie_retries_without_year(deps: AggregatorDeps) -> None:
    deps.tmdb.async_search_movie.side_effect = [None, _match(1)]
    registry = AggregatorRegistry(deps)

    result = await registry.async_resolve(
        ctx(title="Dune (2021)"), PlayerConfig(SOURCE)
    )

    assert result is not None
    assert result.aggregator == "default"
    assert result.media_kind is MediaKind.MOVIE
    assert result.url == "https://image.tmdb.org/t/p/w500/1.jpg"
    assert result.image is None
    assert deps.tmdb.async_search_movie.await_args_list[0].args == ("Dune", 2021)
    assert deps.tmdb.async_search_movie.await_args_list[1].args == ("Dune",)


async def test_default_tv_uses_title_as_show(deps: AggregatorDeps) -> None:
    deps.tmdb.async_search_tv.return_value = _match(2, "tv")
    registry = AggregatorRegistry(deps)

    result = await registry.async_resolve(
        ctx(title="The Office", artist="Diversity Day"), PlayerConfig(SOURCE)
    )

    assert result is not None
    assert result.media_kind is MediaKind.TV
    deps.tmdb.async_search_tv.assert_awaited_once_with("The Office", None)


async def test_default_falls_back_to_multi(deps: AggregatorDeps) -> None:
    deps.tmdb.async_search_multi.return_value = _match(3, "tv")
    registry = AggregatorRegistry(deps)
    result = await registry.async_resolve(ctx(title="Something"), PlayerConfig(SOURCE))
    assert result is not None
    assert result.tmdb_id == 3


async def test_default_skips_music_and_filtered_apps(deps: AggregatorDeps) -> None:
    registry = AggregatorRegistry(deps)
    assert (
        await registry.async_resolve(
            ctx(title="Song", artist="Band", duration=180), PlayerConfig(SOURCE)
        )
        is None
    )
    deny = PlayerConfig(
        SOURCE, app_filter_mode=APP_FILTER_DENY, app_filter_list=("YouTube",)
    )
    assert (
        await registry.async_resolve(ctx(title="Inception", app_name="YouTube"), deny)
        is None
    )
    deps.tmdb.async_search_movie.assert_not_awaited()
    deps.tmdb.async_search_multi.assert_not_awaited()


async def test_misses_are_memoised(deps: AggregatorDeps) -> None:
    registry = AggregatorRegistry(deps)
    assert (
        await registry.async_resolve(ctx(title="Nothing"), PlayerConfig(SOURCE)) is None
    )
    assert (
        await registry.async_resolve(ctx(title="Nothing"), PlayerConfig(SOURCE)) is None
    )
    assert deps.tmdb.async_search_movie.await_count == 1


async def test_without_tmdb_nothing_found(deps: AggregatorDeps) -> None:
    deps.tmdb = None
    registry = AggregatorRegistry(deps)
    assert (
        await registry.async_resolve(ctx(title="Inception"), PlayerConfig(SOURCE))
        is None
    )


async def test_transient_and_auth_errors(deps: AggregatorDeps) -> None:
    registry = AggregatorRegistry(deps)
    deps.tmdb.async_search_movie.side_effect = TmdbConnectionError
    with pytest.raises(TransientLookupError):
        await registry.async_resolve(ctx(title="Inception"), PlayerConfig(SOURCE))

    deps.tmdb.async_search_movie.side_effect = TmdbAuthError
    with pytest.raises(TmdbAuthError):
        await registry.async_resolve(ctx(title="Inception"), PlayerConfig(SOURCE))


def _stremio(**kwargs) -> StremioInfo:
    data = {
        "entity_id": "media_player.stremio",
        "state": "playing",
        "imdb_id": "tt0903747",
        "media_type": "series",
        "title": "Breaking Bad",
        "series_title": "Breaking Bad",
        "poster": "https://images.metahub.space/poster/medium/tt0903747/img",
    }
    data.update(kwargs)
    return StremioInfo(**data)


STREMIO_PLAYER = PlayerConfig(SOURCE, stremio_entity_id="media_player.stremio")


async def test_stremio_uses_imdb_id(deps: AggregatorDeps) -> None:
    deps.tmdb.async_find_by_imdb.return_value = _match(1396, "tv")
    registry = AggregatorRegistry(deps)
    result = await registry.async_resolve(
        ctx(title="Breaking Bad", app_id="com.stremio.one", stremio=_stremio()),
        STREMIO_PLAYER,
    )
    assert result is not None
    assert result.aggregator == "stremio"
    assert result.media_kind is MediaKind.TV
    assert result.imdb_id == "tt0903747"
    assert result.tmdb_id == 1396
    deps.tmdb.async_find_by_imdb.assert_awaited_once_with("tt0903747", "tv")


async def test_stremio_poster_fallback(deps: AggregatorDeps) -> None:
    registry = AggregatorRegistry(deps)
    result = await registry.async_resolve(
        ctx(title="Breaking Bad", app_id="com.stremio.one", stremio=_stremio()),
        STREMIO_PLAYER,
    )
    assert result is not None
    assert result.aggregator == "stremio"
    assert result.url == "https://images.metahub.space/poster/medium/tt0903747/img"
    assert result.imdb_id == "tt0903747"


@pytest.mark.parametrize(
    ("context", "player"),
    [
        # Stremio is playing something else (e.g. on another device).
        (
            ctx(title="The Office", app_id="com.stremio.one", stremio=_stremio()),
            STREMIO_PLAYER,
        ),
        # Source isn't running Stremio.
        (
            ctx(title="Breaking Bad", app_id="com.netflix.ninja", stremio=_stremio()),
            STREMIO_PLAYER,
        ),
        # Stremio is idle.
        (
            ctx(
                title="Breaking Bad",
                app_id="com.stremio.one",
                stremio=_stremio(state="idle"),
            ),
            STREMIO_PLAYER,
        ),
        # No Stremio entity configured.
        (
            ctx(title="Breaking Bad", app_id="com.stremio.one", stremio=_stremio()),
            PlayerConfig(SOURCE),
        ),
    ],
)
async def test_stremio_falls_back_to_default(
    deps: AggregatorDeps, context: MediaContext, player: PlayerConfig
) -> None:
    deps.tmdb.async_search_movie.return_value = _match(5)
    registry = AggregatorRegistry(deps)
    result = await registry.async_resolve(context, player)
    assert result is not None
    assert result.aggregator == "default"
    deps.tmdb.async_find_by_imdb.assert_not_awaited()
