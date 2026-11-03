"""Default aggregator: heuristics on title/artist/duration + TMDb search."""

from __future__ import annotations

from ..tmdb import TmdbMatch
from .base import (
    Aggregator,
    ImageResult,
    LookupHit,
    MediaContext,
    MediaKind,
    PlayerConfig,
    normalise_title,
)

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
    content_type = (ctx.content_type or "").casefold()
    if content_type in MUSIC_CONTENT_TYPES:
        return MediaKind.MUSIC
    if content_type in TV_CONTENT_TYPES:
        return MediaKind.TV
    if content_type in MOVIE_CONTENT_TYPES:
        return MediaKind.MOVIE
    if not ctx.title:
        return MediaKind.UNKNOWN
    if ctx.duration and ctx.duration < video_threshold_minutes * 60:
        return MediaKind.MUSIC
    return MediaKind.TV if ctx.artist else MediaKind.MOVIE


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

    async def async_resolve(
        self, ctx: MediaContext, player: PlayerConfig
    ) -> ImageResult | None:
        """Search TMDb for a movie or TV show poster."""
        kind = classify(ctx, self._deps.settings.video_threshold_minutes)
        if kind not in (MediaKind.MOVIE, MediaKind.TV):
            return None

        # For TV, media_title is the show name (media_artist is episode info).
        raw_title = (ctx.series_title if kind is MediaKind.TV else None) or ctx.title
        if not raw_title:
            return None
        query, year = normalise_title(raw_title)
        if not query:
            return None

        key = f"{query} {year}" if year else query
        category = kind.value

        async def _lookup() -> LookupHit | None:
            tmdb = self._deps.tmdb
            if tmdb is None:
                return None
            if kind is MediaKind.TV:
                match = await tmdb.async_search_tv(query, year)
            else:
                match = await tmdb.async_search_movie(query, year)
                if match is None and year is not None:
                    match = await tmdb.async_search_movie(query)
            if match is None:
                match = await tmdb.async_search_multi(query)
            return _hit(match)

        return await self._async_cached_lookup(category, key, kind, _lookup)
