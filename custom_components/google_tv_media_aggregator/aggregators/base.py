"""Aggregator framework: shared models and the cache-first lookup helper."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import re
import time
from typing import Any, ClassVar

import aiohttp

from ..cache import CachedImage, ImageCache, async_download_image
from ..const import (
    APP_FILTER_ALLOW,
    APP_FILTER_DENY,
    APP_FILTER_NONE,
    CONF_APP_FILTER_LIST,
    CONF_APP_FILTER_MODE,
    CONF_NAME,
    CONF_PREFER_SOURCE_IMAGE,
    CONF_SOURCE_ENTITY_ID,
    CONF_STREMIO_APP_IDS,
    CONF_STREMIO_ENTITY_ID,
    DEFAULT_PREFER_SOURCE_IMAGE,
    DEFAULT_STREMIO_APP_IDS,
    NEGATIVE_CACHE_TTL,
)
from ..tmdb import TmdbClient
from ..trace import format_seconds, trace

_LOGGER = logging.getLogger(__name__)


class MediaKind(StrEnum):
    """What kind of media is playing."""

    MOVIE = "movie"
    TV = "tv"
    MUSIC = "music"
    UNKNOWN = "unknown"


def split_app_list(values: list[str] | str | None) -> list[str]:
    """Normalise a list of app ids/names, accepting comma separated entries."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values:
        for part in str(value).split(","):
            if (part := part.strip()) and part not in result:
                result.append(part)
    return result


@dataclass(frozen=True, slots=True)
class StremioInfo:
    """Snapshot of the Stremio integration's media player."""

    entity_id: str
    state: str
    imdb_id: str | None
    media_type: str | None
    title: str | None
    series_title: str | None
    poster: str | None


@dataclass(frozen=True, slots=True)
class MediaContext:
    """Snapshot of what the source media player is doing."""

    source_entity_id: str
    state: str
    app_id: str | None = None
    app_name: str | None = None
    title: str | None = None
    artist: str | None = None
    series_title: str | None = None
    content_type: str | None = None
    duration: float | None = None
    stremio: StremioInfo | None = None


@dataclass(frozen=True, slots=True)
class PlayerConfig:
    """Configuration of one aggregated player (from its config subentry)."""

    source_entity_id: str
    name: str | None = None
    stremio_entity_id: str | None = None
    stremio_app_ids: tuple[str, ...] = tuple(DEFAULT_STREMIO_APP_IDS)
    app_filter_mode: str = APP_FILTER_NONE
    app_filter_list: tuple[str, ...] = ()
    prefer_source_image: bool = DEFAULT_PREFER_SOURCE_IMAGE

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> PlayerConfig:
        """Build from subentry data."""
        return cls(
            source_entity_id=data[CONF_SOURCE_ENTITY_ID],
            name=(data.get(CONF_NAME) or "").strip() or None,
            stremio_entity_id=data.get(CONF_STREMIO_ENTITY_ID) or None,
            stremio_app_ids=tuple(
                split_app_list(data.get(CONF_STREMIO_APP_IDS, DEFAULT_STREMIO_APP_IDS))
            ),
            app_filter_mode=data.get(CONF_APP_FILTER_MODE, APP_FILTER_NONE),
            app_filter_list=tuple(split_app_list(data.get(CONF_APP_FILTER_LIST))),
            prefer_source_image=data.get(
                CONF_PREFER_SOURCE_IMAGE, DEFAULT_PREFER_SOURCE_IMAGE
            ),
        )

    @staticmethod
    def _app_in(ctx: MediaContext, values: tuple[str, ...]) -> bool:
        lowered = {v.casefold() for v in values}
        return any(
            app is not None and app.casefold() in lowered
            for app in (ctx.app_id, ctx.app_name)
        )

    def app_allowed(self, ctx: MediaContext) -> bool:
        """Return True if the default aggregator may handle this app."""
        if self.app_filter_mode == APP_FILTER_ALLOW:
            return self._app_in(ctx, self.app_filter_list)
        if self.app_filter_mode == APP_FILTER_DENY:
            return not self._app_in(ctx, self.app_filter_list)
        return True

    def is_stremio_app(self, ctx: MediaContext) -> bool:
        """Return True if the source is running Stremio."""
        if self._app_in(ctx, self.stremio_app_ids):
            return True
        return bool(ctx.app_name and "stremio" in ctx.app_name.casefold())


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Settings shared by all aggregated players."""

    video_threshold_minutes: float
    cache_enabled: bool
    cache_dir: str
    language: str | None


@dataclass(frozen=True, slots=True)
class ImageResult:
    """Artwork found by an aggregator: a local file or a remote URL."""

    aggregator: str
    media_kind: MediaKind
    image: CachedImage | None = None
    url: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None


@dataclass(frozen=True, slots=True)
class LookupHit:
    """A remote image found by an API lookup."""

    url: str
    tmdb_id: int | None = None
    imdb_id: str | None = None


class LookupMemo:
    """In-memory memo of remote lookups (hits and misses)."""

    def __init__(
        self, ttl: float = NEGATIVE_CACHE_TTL.total_seconds(), size: int = 512
    ) -> None:
        """Initialise."""
        self._ttl = ttl
        self._size = size
        self._misses: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._hits: OrderedDict[tuple[str, str], LookupHit] = OrderedDict()

    def miss_expires_in(self, key: tuple[str, str]) -> float | None:
        """Return seconds until a remembered miss expires, or None if not a miss."""
        if (expires := self._misses.get(key)) is None:
            return None
        if (remaining := expires - time.monotonic()) < 0:
            del self._misses[key]
            return None
        return remaining

    def is_miss(self, key: tuple[str, str]) -> bool:
        """Return True if a recent lookup found nothing."""
        return self.miss_expires_in(key) is not None

    def add_miss(self, key: tuple[str, str]) -> None:
        """Remember a lookup that found nothing."""
        self._misses[key] = time.monotonic() + self._ttl
        self._misses.move_to_end(key)
        while len(self._misses) > self._size:
            self._misses.popitem(last=False)

    def get_hit(self, key: tuple[str, str]) -> LookupHit | None:
        """Return a remembered hit."""
        return self._hits.get(key)

    def add_hit(self, key: tuple[str, str], hit: LookupHit) -> None:
        """Remember a hit (used when images are not cached on disk)."""
        self._hits[key] = hit
        self._hits.move_to_end(key)
        while len(self._hits) > self._size:
            self._hits.popitem(last=False)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Return remembered misses and hits (for diagnostics)."""
        now = time.monotonic()
        return {
            "misses": [
                {
                    "category": category,
                    "key": key,
                    "expires_in": format_seconds(expires - now),
                }
                for (category, key), expires in self._misses.items()
                if expires >= now
            ],
            "hits": [
                {"category": category, "key": key, "url": hit.url}
                for (category, key), hit in self._hits.items()
            ],
        }


@dataclass(slots=True)
class AggregatorDeps:
    """Shared services for aggregators."""

    session: aiohttp.ClientSession
    cache: ImageCache
    settings: HubSettings
    tmdb: TmdbClient | None = None
    memo: LookupMemo = field(default_factory=LookupMemo)


_YEAR_SUFFIX = re.compile(r"\s*[\(\[](\d{4})[\)\]]\s*$")


def normalise_title(title: str) -> tuple[str, int | None]:
    """Strip whitespace and a trailing '(YYYY)', returning (title, year)."""
    title = " ".join(title.split())
    if match := _YEAR_SUFFIX.search(title):
        return title[: match.start()].strip(), int(match.group(1))
    return title, None


class Aggregator(ABC):
    """Resolves artwork for a media context."""

    name: ClassVar[str]

    def __init__(self, deps: AggregatorDeps) -> None:
        """Initialise."""
        self._deps = deps

    @abstractmethod
    def matches(self, ctx: MediaContext, player: PlayerConfig) -> bool:
        """Return True if this aggregator should handle the context."""

    def skip_reason(self, ctx: MediaContext, player: PlayerConfig) -> str:
        """Explain why matches() returned False (for debug logging)."""
        return "not applicable"

    @abstractmethod
    async def async_resolve(
        self, ctx: MediaContext, player: PlayerConfig
    ) -> ImageResult | None:
        """Return artwork, or None to let the next aggregator try."""

    async def _async_cached_lookup(
        self,
        category: str,
        key: str,
        kind: MediaKind,
        lookup: Callable[[], Awaitable[LookupHit | None]],
    ) -> ImageResult | None:
        """Check the disk cache, then memo, then call lookup and cache the result.

        TMDb errors propagate so transient failures are not remembered as misses.
        """
        deps = self._deps
        name = self.name
        label = f"{category}/{key}"
        if (cached := await deps.cache.async_get(category, key)) is not None:
            trace(_LOGGER, "%s: cached image for %r: %s", name, label, cached.path)
            return ImageResult(self.name, kind, image=cached)
        if deps.cache.enabled:
            trace(_LOGGER, "%s: no cached image for %r", name, label)

        memo_key = (category, key)
        if (remaining := deps.memo.miss_expires_in(memo_key)) is not None:
            trace(
                _LOGGER,
                "%s: %r found nothing recently; not looking it up again for %s",
                name,
                label,
                format_seconds(remaining),
            )
            return None
        if (hit := deps.memo.get_hit(memo_key)) is None:
            trace(_LOGGER, "%s: looking up %r", name, label)
            hit = await lookup()
            if hit is None:
                deps.memo.add_miss(memo_key)
                trace(
                    _LOGGER,
                    "%s: nothing found for %r; remembering that for %s",
                    name,
                    label,
                    format_seconds(NEGATIVE_CACHE_TTL.total_seconds()),
                )
                return None
            trace(_LOGGER, "%s: found %s", name, hit.url)
        else:
            trace(_LOGGER, "%s: remembered result for %r: %s", name, label, hit.url)

        if not deps.cache.writable:
            trace(_LOGGER, "%s: image cache not writable; using remote URL", name)
        elif (
            downloaded := await async_download_image(deps.session, hit.url)
        ) is not None and (
            cached := await deps.cache.async_put(category, key, *downloaded)
        ) is not None:
            trace(_LOGGER, "%s: saved image to %s", name, cached.path)
            return ImageResult(
                self.name,
                kind,
                image=cached,
                tmdb_id=hit.tmdb_id,
                imdb_id=hit.imdb_id,
            )
        else:
            trace(_LOGGER, "%s: could not cache the image; using remote URL", name)

        deps.memo.add_hit(memo_key, hit)
        return ImageResult(
            self.name, kind, url=hit.url, tmdb_id=hit.tmdb_id, imdb_id=hit.imdb_id
        )
