"""Stremio aggregator: uses the IMDb ID exposed by the Stremio integration."""

from __future__ import annotations

from dataclasses import replace
import logging
import re

from ..tmdb import MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV
from ..trace import trace
from .base import (
    Aggregator,
    ImageResult,
    LookupHit,
    MediaContext,
    MediaKind,
    PlayerConfig,
    StremioInfo,
)

_LOGGER = logging.getLogger(__name__)

_IMDB_ID = re.compile(r"tt\d{5,}")
_TOKEN = re.compile(r"[^\W_]+")


def extract_imdb_id(value: str | None) -> str | None:
    """Extract 'tt1234567' from values such as 'tt1234567:1:2'."""
    if not value:
        return None
    match = _IMDB_ID.search(value)
    return match.group(0) if match else None


def _tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.casefold()))


def titles_match(ctx: MediaContext, info: StremioInfo) -> bool:
    """Loosely check that the source and Stremio are playing the same thing.

    Stremio state is account wide, so another device could be the one playing.
    """
    source = [v for v in (ctx.title, ctx.series_title, ctx.artist) if v]
    stremio = [v for v in (info.title, info.series_title) if v]
    if not source or not stremio:
        return False
    for left in source:
        left_tokens = _tokens(left)
        left_norm = " ".join(sorted(left_tokens))
        for right in stremio:
            right_tokens = _tokens(right)
            if not left_tokens or not right_tokens:
                continue
            if left_norm == " ".join(sorted(right_tokens)):
                return True
            if left_tokens <= right_tokens or right_tokens <= left_tokens:
                return True
            overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
            if overlap >= 0.5:
                return True
    return False


class StremioAggregator(Aggregator):
    """Deterministic lookups when the source is running Stremio."""

    name = "stremio"

    def matches(self, ctx: MediaContext, player: PlayerConfig) -> bool:
        """Only when a Stremio entity is configured and the source runs Stremio."""
        return (
            player.stremio_entity_id is not None
            and ctx.stremio is not None
            and player.is_stremio_app(ctx)
        )

    def skip_reason(self, ctx: MediaContext, player: PlayerConfig) -> str:
        """Explain why matches() returned False."""
        if player.stremio_entity_id is None:
            return "no Stremio media player configured"
        if ctx.stremio is None:
            return f"Stremio media player {player.stremio_entity_id} not found"
        return (
            f"source app (id {ctx.app_id!r}, name {ctx.app_name!r}) is not one of"
            f" the Stremio app IDs ({', '.join(player.stremio_app_ids)})"
        )

    async def async_resolve(
        self, ctx: MediaContext, player: PlayerConfig
    ) -> ImageResult | None:
        """Look up TMDb by IMDb ID, falling back to Stremio's own poster."""
        info = ctx.stremio
        if info is None or info.state not in ("playing", "paused", "buffering"):
            trace(
                _LOGGER,
                "stremio: Stremio player is %s, not playing",
                info.state if info else None,
            )
            return None
        imdb_id = extract_imdb_id(info.imdb_id)
        if imdb_id is None:
            trace(_LOGGER, "stremio: no IMDb ID in %r", info.imdb_id)
            return None
        if not titles_match(ctx, info):
            trace(
                _LOGGER,
                "stremio: titles don't match, so Stremio may be playing on another"
                " device (source: %s; Stremio: %s)",
                [v for v in (ctx.title, ctx.series_title, ctx.artist) if v],
                [v for v in (info.title, info.series_title) if v],
            )
            return None

        media_type = (info.media_type or "").casefold()
        if media_type in ("series", "tvshow", "tv"):
            kind, prefer = MediaKind.TV, MEDIA_TYPE_TV
        elif media_type == "movie":
            kind, prefer = MediaKind.MOVIE, MEDIA_TYPE_MOVIE
        else:
            kind, prefer = MediaKind.UNKNOWN, None
        trace(
            _LOGGER,
            "stremio: IMDb ID %s, type %r (%s)",
            imdb_id,
            info.media_type,
            kind.value,
        )

        async def _tmdb_lookup() -> LookupHit | None:
            if (tmdb := self._deps.tmdb) is None:
                trace(_LOGGER, "stremio: no TMDb API key configured")
                return None
            match = await tmdb.async_find_by_imdb(imdb_id, prefer)
            if match is None or (url := match.poster_url) is None:
                return None
            return LookupHit(url=url, tmdb_id=match.tmdb_id, imdb_id=imdb_id)

        result = await self._async_cached_lookup("imdb", imdb_id, kind, _tmdb_lookup)
        if result is None:
            trace(_LOGGER, "stremio: trying Stremio's own poster")

            async def _poster_lookup() -> LookupHit | None:
                if not info.poster:
                    trace(_LOGGER, "stremio: Stremio player has no poster")
                    return None
                return LookupHit(url=info.poster, imdb_id=imdb_id)

            result = await self._async_cached_lookup(
                "stremio", imdb_id, kind, _poster_lookup
            )
        if result is not None and result.imdb_id is None:
            result = replace(result, imdb_id=imdb_id)
        return result
