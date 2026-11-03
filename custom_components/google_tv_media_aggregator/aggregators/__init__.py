"""Pluggable per-app artwork aggregators."""

from __future__ import annotations

import logging

from ..tmdb import TmdbAuthError, TmdbError
from .base import (
    Aggregator,
    AggregatorDeps,
    HubSettings,
    ImageResult,
    LookupMemo,
    MediaContext,
    MediaKind,
    PlayerConfig,
    StremioInfo,
)
from .default import DefaultAggregator
from .stremio import StremioAggregator

_LOGGER = logging.getLogger(__name__)

# Order matters: the first aggregator returning a result wins. App-specific
# aggregators go before DefaultAggregator.
AGGREGATOR_CLASSES: tuple[type[Aggregator], ...] = (
    StremioAggregator,
    DefaultAggregator,
)


class TransientLookupError(Exception):
    """An aggregator failed for a reason that may go away (network, rate limit)."""


class AggregatorRegistry:
    """Runs aggregators in order until one finds artwork."""

    def __init__(self, deps: AggregatorDeps) -> None:
        """Initialise with the built-in aggregators."""
        self.deps = deps
        self.aggregators: list[Aggregator] = [cls(deps) for cls in AGGREGATOR_CLASSES]

    async def async_resolve(
        self, ctx: MediaContext, player: PlayerConfig
    ) -> ImageResult | None:
        """Return the first result.

        Raises TmdbAuthError if credentials are rejected and TransientLookupError if
        nothing was found but at least one aggregator hit a transient error.
        """
        transient = False
        for aggregator in self.aggregators:
            if not aggregator.matches(ctx, player):
                continue
            try:
                result = await aggregator.async_resolve(ctx, player)
            except TmdbAuthError:
                raise
            except TmdbError as err:
                _LOGGER.debug("%s aggregator failed: %s", aggregator.name, err)
                transient = True
                continue
            if result is not None:
                return result
        if transient:
            raise TransientLookupError
        return None


__all__ = [
    "AGGREGATOR_CLASSES",
    "Aggregator",
    "AggregatorDeps",
    "AggregatorRegistry",
    "HubSettings",
    "ImageResult",
    "LookupMemo",
    "MediaContext",
    "MediaKind",
    "PlayerConfig",
    "StremioInfo",
    "TransientLookupError",
]
