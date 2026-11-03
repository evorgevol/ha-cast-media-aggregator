"""Minimal async client for The Movie Database (TMDb) API."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w500"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
DEFAULT_RETRY_AFTER = 10.0

MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


class TmdbError(Exception):
    """Base TMDb error."""


class TmdbAuthError(TmdbError):
    """The API key or token was rejected."""


class TmdbConnectionError(TmdbError):
    """TMDb could not be reached."""


class TmdbRateLimitError(TmdbError):
    """TMDb rate limit hit."""


@dataclass(frozen=True, slots=True)
class TmdbMatch:
    """A TMDb title with a poster."""

    tmdb_id: int
    media_type: str
    title: str
    poster_path: str | None

    @property
    def poster_url(self) -> str | None:
        """Return the full poster URL."""
        if not self.poster_path:
            return None
        return f"{TMDB_IMAGE_BASE}/{POSTER_SIZE}{self.poster_path}"


def is_bearer_token(api_key: str) -> bool:
    """Return True if the key looks like a v4 read access token (JWT)."""
    return api_key.startswith("eyJ") and api_key.count(".") == 2


def _match_from_result(
    result: dict[str, Any], media_type: str | None = None
) -> TmdbMatch | None:
    media_type = media_type or result.get("media_type")
    if media_type not in (MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV):
        return None
    if not isinstance(result.get("id"), int):
        return None
    return TmdbMatch(
        tmdb_id=result["id"],
        media_type=media_type,
        title=result.get("title") or result.get("name") or "",
        poster_path=result.get("poster_path"),
    )


def _first_with_poster(
    results: list[dict[str, Any]], media_type: str | None = None
) -> TmdbMatch | None:
    for result in results:
        match = _match_from_result(result, media_type)
        if match is not None and match.poster_path:
            return match
    return None


def _parse_retry_after(value: str | None) -> float:
    try:
        return max(float(value), 1.0) if value else DEFAULT_RETRY_AFTER
    except ValueError:
        return DEFAULT_RETRY_AFTER


class TmdbClient:
    """TMDb API client supporting v3 API keys and v4 read access tokens."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        language: str | None = None,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._api_key = api_key.strip()
        self._bearer = is_bearer_token(self._api_key)
        self._language = language or None
        self._blocked_until = 0.0

    async def _async_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if (remaining := self._blocked_until - time.monotonic()) > 0:
            raise TmdbRateLimitError(f"Rate limited for another {remaining:.0f}s")

        headers = {"Accept": "application/json"}
        query: dict[str, Any] = {
            k: v for k, v in (params or {}).items() if v is not None
        }
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            query["api_key"] = self._api_key
        if self._language and "language" not in query:
            query["language"] = self._language

        try:
            async with self._session.get(
                f"{TMDB_API_BASE}{path}",
                params=query,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    raise TmdbAuthError("TMDb rejected the API key")
                if resp.status == 429:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    self._blocked_until = time.monotonic() + retry_after
                    raise TmdbRateLimitError(
                        f"Rate limited, retry after {retry_after:.0f}s"
                    )
                if resp.status == 404:
                    return {}
                if resp.status >= 400:
                    raise TmdbError(f"TMDb returned HTTP {resp.status} for {path}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            # Never include str(err): aiohttp errors can embed the request URL (and api_key).
            raise TmdbConnectionError(
                f"Error talking to TMDb: {type(err).__name__}"
            ) from None
        if not isinstance(data, dict):
            raise TmdbError(f"Unexpected TMDb response for {path}")
        return data

    async def async_validate(self) -> None:
        """Validate the credentials. Raises TmdbAuthError/TmdbConnectionError."""
        await self._async_get("/configuration")

    async def async_search_movie(
        self, query: str, year: int | None = None
    ) -> TmdbMatch | None:
        """Return the first movie with a poster matching the query."""
        data = await self._async_get(
            "/search/movie", {"query": query, "year": year, "include_adult": "false"}
        )
        return _first_with_poster(data.get("results") or [], MEDIA_TYPE_MOVIE)

    async def async_search_tv(
        self, query: str, year: int | None = None
    ) -> TmdbMatch | None:
        """Return the first TV show with a poster matching the query."""
        data = await self._async_get(
            "/search/tv",
            {"query": query, "first_air_date_year": year, "include_adult": "false"},
        )
        return _first_with_poster(data.get("results") or [], MEDIA_TYPE_TV)

    async def async_search_multi(self, query: str) -> TmdbMatch | None:
        """Return the first movie or TV show with a poster matching the query."""
        data = await self._async_get(
            "/search/multi", {"query": query, "include_adult": "false"}
        )
        return _first_with_poster(data.get("results") or [])

    async def async_get_tv(self, tv_id: int) -> TmdbMatch | None:
        """Return a TV show by TMDb ID."""
        data = await self._async_get(f"/tv/{tv_id}")
        if not data:
            return None
        return _match_from_result(data, MEDIA_TYPE_TV)

    async def async_find_by_imdb(
        self, imdb_id: str, prefer: str | None = None
    ) -> TmdbMatch | None:
        """Find a movie or show by IMDb ID, preferring the given TMDb media type."""
        data = await self._async_get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        movie = _first_with_poster(data.get("movie_results") or [], MEDIA_TYPE_MOVIE)
        tv = _first_with_poster(data.get("tv_results") or [], MEDIA_TYPE_TV)
        ordered = [tv, movie] if prefer == MEDIA_TYPE_TV else [movie, tv]
        for match in ordered:
            if match is not None:
                return match

        # Episode/season IMDb IDs resolve to the parent show.
        for key in ("tv_episode_results", "tv_season_results"):
            for result in data.get(key) or []:
                if isinstance(show_id := result.get("show_id"), int):
                    show = await self.async_get_tv(show_id)
                    if show is not None and show.poster_path:
                        return show
        return None
