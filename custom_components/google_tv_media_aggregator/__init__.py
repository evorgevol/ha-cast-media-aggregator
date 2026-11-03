"""The Google TV Streamer Media Aggregator integration."""

from __future__ import annotations

from pathlib import Path

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .aggregators import AggregatorDeps, AggregatorRegistry
from .cache import ImageCache
from .const import CONF_TMDB_API_KEY
from .runtime import (
    AggregatorConfigEntry,
    RuntimeData,
    SeenApps,
    settings_from_options,
)
from .tmdb import TmdbClient

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: AggregatorConfigEntry) -> bool:
    """Set up the hub entry and one media player per subentry."""
    settings = settings_from_options(hass, dict(entry.options))
    session = async_get_clientsession(hass)

    tmdb: TmdbClient | None = None
    if api_key := (entry.data.get(CONF_TMDB_API_KEY) or "").strip():
        tmdb = TmdbClient(session, api_key, settings.language)

    cache = ImageCache(hass, Path(settings.cache_dir), settings.cache_enabled)
    await cache.async_setup()

    seen_apps = SeenApps(hass)
    await seen_apps.async_load()

    registry = AggregatorRegistry(
        AggregatorDeps(session=session, cache=cache, settings=settings, tmdb=tmdb)
    )
    entry.runtime_data = RuntimeData(
        settings=settings,
        cache=cache,
        registry=registry,
        seen_apps=seen_apps,
        tmdb=tmdb,
    )

    # Fires for option changes and when player subentries are added/edited/removed.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: AggregatorConfigEntry
) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AggregatorConfigEntry) -> bool:
    """Unload the hub entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.seen_apps.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AggregatorConfigEntry) -> None:
    """Remove stored data when the hub is deleted. Cached images are kept."""
    await SeenApps(hass).async_remove()
