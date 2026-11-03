"""Runtime objects shared by the hub config entry and its players."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .aggregators import AggregatorRegistry, HubSettings
from .cache import ImageCache
from .const import (
    CONF_CACHE_DIR,
    CONF_CACHE_ENABLED,
    CONF_TMDB_LANGUAGE,
    CONF_VIDEO_THRESHOLD,
    DEFAULT_CACHE_DIR,
    DEFAULT_CACHE_ENABLED,
    DEFAULT_VIDEO_THRESHOLD,
    SEEN_APPS_STORAGE_KEY,
    SEEN_APPS_STORAGE_VERSION,
)
from .tmdb import TmdbClient

SEEN_APPS_SAVE_DELAY = 30


def settings_from_options(hass: HomeAssistant, options: dict[str, Any]) -> HubSettings:
    """Build hub settings from entry options, applying defaults."""
    language = options.get(CONF_TMDB_LANGUAGE)
    if language is None:
        language = hass.config.language
    return HubSettings(
        video_threshold_minutes=float(
            options.get(CONF_VIDEO_THRESHOLD, DEFAULT_VIDEO_THRESHOLD)
        ),
        cache_enabled=options.get(CONF_CACHE_ENABLED, DEFAULT_CACHE_ENABLED),
        cache_dir=options.get(CONF_CACHE_DIR) or DEFAULT_CACHE_DIR,
        language=(language or "").strip() or None,
    )


class SeenApps:
    """Remembers apps seen on source players, to offer them in the app filter."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        self._store: Store[dict[str, str]] = Store(
            hass, SEEN_APPS_STORAGE_VERSION, SEEN_APPS_STORAGE_KEY
        )
        self._apps: dict[str, str] = {}
        self._dirty = False

    async def async_load(self) -> None:
        """Load from storage."""
        if isinstance(data := await self._store.async_load(), dict):
            self._apps = {str(k): str(v) for k, v in data.items()}

    @callback
    def async_add(self, app_id: str | None, app_name: str | None) -> None:
        """Record an app."""
        key = app_id or app_name
        if not key:
            return
        label = app_name or app_id or key
        if self._apps.get(key) == label:
            return
        self._apps[key] = label
        self._dirty = True
        self._store.async_delay_save(self._data_to_save, SEEN_APPS_SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, str]:
        self._dirty = False
        return dict(self._apps)

    async def async_flush(self) -> None:
        """Write pending changes."""
        if self._dirty:
            await self._store.async_save(self._data_to_save())

    @property
    def apps(self) -> dict[str, str]:
        """Return app id -> app name."""
        return dict(self._apps)

    async def async_remove(self) -> None:
        """Delete stored data."""
        await self._store.async_remove()


@dataclass(slots=True)
class RuntimeData:
    """Data stored on the hub config entry."""

    settings: HubSettings
    cache: ImageCache
    registry: AggregatorRegistry
    seen_apps: SeenApps
    tmdb: TmdbClient | None


type AggregatorConfigEntry = ConfigEntry[RuntimeData]
