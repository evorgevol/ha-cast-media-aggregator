"""Default aggregator: heuristics on title/artist/duration + TMDb search."""

from __future__ import annotations

import logging

from ..tmdb import TmdbMatch
from ..trace import trace
from .base import (
    Aggregator,
    ImageResult,
    LookupHit,
    MediaContext,
    MediaKind,
    PlayerConfig,
    normalise_title,
)

_LOGGER = logging.getLogger(__name__)

MUSIC_CONTENT_TYPES = frozenset(
    {
        "music",
        "track",
        "album",
        "artist",
        "playlist",
        "podcast",
        "composer",
        "contributing_artist",
        "genre",
    }
)
TV_CONTENT_TYPES = frozenset({"tvshow", "episode", "season"})
MOVIE_CONTENT_TYPES = frozenset({"movie"})


def classify(ctx: MediaContext, video_threshold_minutes: float) -> MediaKind:
    """Guess what is playing.

    An explicit media_content_type wins. Otherwise anything shorter than the
    threshold is assumed to be music; longer (or unknown duration) is video:
    title only => movie, title + artist => TV show.
    """
    return classify_with_reason(ctx, video_threshold_minutes)[0]


def classify_with_reason(
    ctx: MediaContext, video_threshold_minutes: float
) -> tuple[MediaKind, str]:
    """Like classify(), also returning a human readable reason."""
    content_type = (ctx.content_type or "").casefold()
    if content_type in MUSIC_CONTENT_TYPES:
        return MediaKind.MUSIC, f"media_content_type is {ctx.content_type!r}"
    if content_type in TV_CONTENT_TYPES:
        return MediaKind.TV, f"media_content_type is {ctx.content_type!r}"
    if content_type in MOVIE_CONTENT_TYPES:
        return MediaKind.MOVIE, f"media_content_type is {ctx.content_type!r}"
    if not ctx.title:
        return MediaKind.UNKNOWN, "no media_title"
    threshold = video_threshold_minutes * 60
    if ctx.duration and ctx.duration < threshold:
        return MediaKind.MUSIC, (
            f"duration {ctx.duration:.0f}s is under the"
            f" {video_threshold_minutes:g} min video threshold"
        )
    duration = (
        f"duration {ctx.duration:.0f}s is at least the"
        f" {video_threshold_minutes:g} min video threshold"
        if ctx.duration
        else "duration unknown"
    )
    if ctx.artist:
        return MediaKind.TV, f"{duration}; has media_artist {ctx.artist!r}"
    return MediaKind.MOVIE, f"{duration}; no media_artist"


def _hit(match: TmdbMatch | None) -> LookupHit | None:
    if match is None or (url := match.poster_url) is None:
        return None
    return LookupHit(url=url, tmdb_id=match.tmdb_id)


class DefaultAggregator(Aggregator):
    """Fallback aggregator used for any (allowed) app."""

    name = "default"

    def matches(self, ctx: MediaContext, player: PlayerConfig) -> bool:
        """Handle anything with a title from an allowed app."""
        return bool(ctx.title) and player.app_allowed(ctx)

    def skip_reason(self, ctx: MediaContext, player: PlayerConfig) -> str:
        """Explain why matches() returned False."""
        if not ctx.title:
            return "the source has no media_title"
        return (
            f"app {ctx.app_name or ctx.app_id!r} is excluded by the app filter"
            f" ({player.app_filter_mode}: {', '.join(player.app_filter_list)})"
        )

    async def async_resolve(
        self, ctx: MediaContext, player: PlayerConfig
    ) -> ImageResult | None:
        """Search TMDb for a movie or TV show poster."""
        kind, reason = classify_with_reason(
            ctx, self._deps.settings.video_threshold_minutes
        )
        trace(_LOGGER, "default: classified as %s (%s)", kind.value, reason)
        if kind not in (MediaKind.MOVIE, MediaKind.TV):
            trace(_LOGGER, "default: only movies and TV shows are looked up")
            return None

        # For TV, media_title is the show name (media_artist is episode info).
        raw_title = (ctx.series_title if kind is MediaKind.TV else None) or ctx.title
        if not raw_title:
            return None
        query, year = normalise_title(raw_title)
        if not query:
            trace(_LOGGER, "default: title %r is empty after cleanup", raw_title)
            return None

        key = f"{query} {year}" if year else query
        category = kind.value

        async def _lookup() -> LookupHit | None:
            tmdb = self._deps.tmdb
            if tmdb is None:
                trace(_LOGGER, "default: no TMDb API key configured")
                return None
            if kind is MediaKind.TV:
                match = await tmdb.async_search_tv(query, year)
            else:
                match = await tmdb.async_search_movie(query, year)
                if match is None and year is not None:
                    trace(_LOGGER, "default: retrying the movie search without year")
                    match = await tmdb.async_search_movie(query)
            if match is None:
                trace(_LOGGER, "default: falling back to TMDb multi search")
                match = await tmdb.async_search_multi(query)
            return _hit(match)

        return await self._async_cached_lookup(category, key, kind, _lookup)
