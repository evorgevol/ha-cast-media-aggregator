"""Aggregated media player: mirrors a source player and adds artwork."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, fields, replace
import hashlib
import logging
from typing import Any

from homeassistant.components.media_player import (
    DATA_COMPONENT,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    BrowseError,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ENTITY_PICTURE,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PLAY_PAUSE,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_SEEK,
    SERVICE_MEDIA_STOP,
    SERVICE_REPEAT_SET,
    SERVICE_SHUFFLE_SET,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .aggregators import (
    ImageResult,
    MediaContext,
    PlayerConfig,
    StremioInfo,
    TransientLookupError,
)
from .cache import CachedImage
from .const import (
    APP_FALLBACK_DIR,
    ATTR_AGGREGATOR,
    ATTR_IMAGE_SOURCE,
    ATTR_IMDB_ID,
    ATTR_MEDIA_KIND,
    ATTR_SOURCE_ENTITY_ID,
    ATTR_TMDB_ID,
    DOMAIN,
    IMAGE_SOURCE_AGGREGATOR,
    IMAGE_SOURCE_APP_FALLBACK,
    IMAGE_SOURCE_NONE,
    IMAGE_SOURCE_SOURCE,
    LOOKUP_MAX_RETRIES,
    LOOKUP_RETRY_DELAY,
    SUBENTRY_TYPE_PLAYER,
    TITLE,
)
from .helpers import suggested_object_id
from .runtime import AggregatorConfigEntry
from .tmdb import TmdbAuthError
from .trace import (
    HISTORY_SIZE,
    LookupTrace,
    format_seconds,
    reset_trace,
    set_trace,
    trace,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Grouping and search would need entity-level support beyond simple forwarding.
# SEARCH_MEDIA only exists from HA 2025.5.
UNSUPPORTED_FEATURES = MediaPlayerEntityFeature.GROUPING | getattr(
    MediaPlayerEntityFeature, "SEARCH_MEDIA", MediaPlayerEntityFeature(0)
)
INACTIVE_STATES = frozenset({STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN})
STREMIO_ACTIVE_STATES = ("playing", "paused", "buffering")
# State attributes copied from the source; each matches a MediaPlayerEntity property.
MIRRORED_ATTRIBUTES = (
    "volume_level",
    "is_volume_muted",
    "media_content_id",
    "media_content_type",
    "media_duration",
    "media_position",
    "media_position_updated_at",
    "media_title",
    "media_artist",
    "media_album_name",
    "media_album_artist",
    "media_track",
    "media_series_title",
    "media_season",
    "media_episode",
    "media_channel",
    "media_playlist",
    "app_id",
    "app_name",
    "source",
    "sound_mode",
    "shuffle",
    "repeat",
    "source_list",
    "sound_mode_list",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AggregatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one aggregated player per subentry."""
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_PLAYER:
            continue
        player = PlayerConfig.from_data(dict(subentry.data))
        async_add_entities(
            [AggregatedMediaPlayer(entry, subentry, player)],
            config_subentry_id=subentry_id,
        )


@dataclass(frozen=True, slots=True)
class _Image:
    """Current artwork."""

    source: str = IMAGE_SOURCE_NONE
    local: CachedImage | None = None
    url: str | None = None
    result: ImageResult | None = None

    def describe(self) -> dict[str, Any]:
        """Return a summary for debug logs and diagnostics."""
        details: dict[str, Any] = {"image_source": self.source}
        if self.local is not None:
            details["file"] = str(self.local.path)
        if self.url is not None:
            details["url"] = self.url
        if (result := self.result) is not None:
            details["aggregator"] = result.aggregator
            details["media_kind"] = result.media_kind.value
            if result.tmdb_id is not None:
                details["tmdb_id"] = result.tmdb_id
            if result.imdb_id is not None:
                details["imdb_id"] = result.imdb_id
        return details


class AggregatedMediaPlayer(MediaPlayerEntity):
    """Mirrors a source media player and adds artwork from aggregators."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(
        self,
        entry: AggregatorConfigEntry,
        subentry: ConfigSubentry,
        player: PlayerConfig,
    ) -> None:
        """Initialise."""
        self._entry = entry
        self._runtime = entry.runtime_data
        self._player = player
        self._attr_unique_id = subentry.subentry_id
        self.entity_id = f"{MEDIA_PLAYER_DOMAIN}.{suggested_object_id(player)}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer=TITLE,
            model="Aggregated media player",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._image = _Image()
        self._source_picture: str | None = None
        self._lookup_key: Any = None
        self._lookup_task: asyncio.Task[None] | None = None
        self._retry_unsub: Any = None
        self._retries = 0
        self._history: deque[LookupTrace] = (
            entry.runtime_data.lookup_history.setdefault(
                subentry.subentry_id, deque(maxlen=HISTORY_SIZE)
            )
        )

    def _debug(self, msg: str, *args: Any) -> None:
        _LOGGER.debug("%s: " + msg, self.entity_id, *args, stacklevel=2)

    # ------------------------------------------------------------------ lifecycle

    async def async_added_to_hass(self) -> None:
        """Start tracking the source (and Stremio) entities."""
        await super().async_added_to_hass()
        player = self._player
        tracked = [player.source_entity_id]
        if player.stremio_entity_id:
            tracked.append(player.stremio_entity_id)
        self._debug(
            "tracking %s; prefer source image: %s; app filter: %s %s;"
            " Stremio app IDs: %s",
            ", ".join(tracked),
            player.prefer_source_image,
            player.app_filter_mode,
            list(player.app_filter_list),
            list(player.stremio_app_ids),
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, tracked, self._async_tracked_changed
            )
        )
        self.async_on_remove(self._async_cancel_lookup)
        self._async_sync_from_source()
        self._async_schedule_lookup("startup")

    @callback
    def _async_tracked_changed(self, event: Event[EventStateChangedData]) -> None:
        self._async_sync_from_source()
        self._async_schedule_lookup(f"{event.data['entity_id']} changed")
        self.async_write_ha_state()

    # ------------------------------------------------------------------ mirroring

    @callback
    def _async_sync_from_source(self) -> None:
        state = self.hass.states.get(self._player.source_entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            self._attr_available = False
            self._attr_state = None
            self._source_picture = None
            return

        self._attr_available = True
        try:
            self._attr_state = MediaPlayerState(state.state)
        except ValueError:
            self._attr_state = None

        attrs = state.attributes
        for attr in MIRRORED_ATTRIBUTES:
            setattr(self, f"_attr_{attr}", attrs.get(attr))
        self._attr_supported_features = (
            MediaPlayerEntityFeature(int(attrs.get(ATTR_SUPPORTED_FEATURES) or 0))
            & ~UNSUPPORTED_FEATURES
        )
        self._source_picture = attrs.get(ATTR_ENTITY_PICTURE)

        if self._attr_app_id or self._attr_app_name:
            self._runtime.seen_apps.async_add(self._attr_app_id, self._attr_app_name)

    # ------------------------------------------------------------------ artwork lookup

    @callback
    def _async_build_context(self) -> MediaContext | None:
        state = self.hass.states.get(self._player.source_entity_id)
        if state is None or state.state in INACTIVE_STATES:
            return None
        attrs = state.attributes

        stremio: StremioInfo | None = None
        if self._player.stremio_entity_id and (
            stremio_state := self.hass.states.get(self._player.stremio_entity_id)
        ):
            sattrs = stremio_state.attributes
            stremio = StremioInfo(
                entity_id=stremio_state.entity_id,
                state=stremio_state.state,
                imdb_id=sattrs.get("imdb_id"),
                media_type=sattrs.get("type"),
                title=sattrs.get("media_title"),
                series_title=sattrs.get("series_title"),
                poster=sattrs.get("poster"),
            )

        duration = attrs.get("media_duration")
        return MediaContext(
            source_entity_id=state.entity_id,
            state=state.state,
            app_id=attrs.get("app_id"),
            app_name=attrs.get("app_name"),
            title=attrs.get("media_title"),
            artist=attrs.get("media_artist"),
            series_title=attrs.get("media_series_title"),
            content_type=attrs.get("media_content_type"),
            duration=float(duration) if isinstance(duration, (int, float)) else None,
            stremio=stremio,
        )

    def _lookup_key_for(self, ctx: MediaContext) -> Any:
        """Key of everything that affects the artwork (ignores position etc.)."""
        threshold = self._runtime.settings.video_threshold_minutes * 60
        bucket = None if not ctx.duration else ctx.duration >= threshold
        stremio = ctx.stremio
        if stremio is not None:
            stremio = (
                replace(stremio, state="active")
                if stremio.state in STREMIO_ACTIVE_STATES
                else None
            )
        return (
            replace(ctx, duration=bucket, state="", stremio=stremio),
            self._source_picture is not None,
        )

    @staticmethod
    def _key_changes(old: Any, new: Any) -> list[str]:
        """Names of what changed between two lookup keys."""
        if old is None or new is None:
            return []
        (old_ctx, old_picture), (new_ctx, new_picture) = old, new
        changed = [
            "duration (video threshold)" if f.name == "duration" else f.name
            for f in fields(MediaContext)
            if getattr(old_ctx, f.name) != getattr(new_ctx, f.name)
        ]
        if old_picture != new_picture:
            changed.append("source picture")
        return changed

    def _context_summary(self, ctx: MediaContext) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "state": ctx.state,
            "app_id": ctx.app_id,
            "app_name": ctx.app_name,
            "title": ctx.title,
            "artist": ctx.artist,
            "series_title": ctx.series_title,
            "content_type": ctx.content_type,
            "duration": ctx.duration,
            "has_source_picture": self._source_picture is not None,
        }
        if (stremio := ctx.stremio) is not None:
            summary["stremio"] = {
                "state": stremio.state,
                "imdb_id": stremio.imdb_id,
                "type": stremio.media_type,
                "title": stremio.title,
                "series_title": stremio.series_title,
                "has_poster": bool(stremio.poster),
            }
        return summary

    @callback
    def _async_schedule_lookup(self, trigger: str, force: bool = False) -> None:
        ctx = self._async_build_context()
        key = None if ctx is None else self._lookup_key_for(ctx)
        if key == self._lookup_key and not force:
            return
        if key != self._lookup_key:
            self._retries = 0
        previous_key = self._lookup_key
        self._lookup_key = key
        self._async_cancel_lookup()
        if ctx is None:
            state = self.hass.states.get(self._player.source_entity_id)
            self._debug(
                "%s is %s; clearing artwork",
                self._player.source_entity_id,
                state.state if state else "missing",
            )
            self._image = _Image()
            return
        lookup_trace = LookupTrace(
            entity_id=self.entity_id,
            trigger=trigger,
            context=self._context_summary(ctx),
            changed=self._key_changes(previous_key, key),
        )
        self._debug(
            "looking up artwork (trigger: %s; changed: %s): %s",
            trigger,
            ", ".join(lookup_trace.changed) or "-",
            lookup_trace.context,
        )
        self._history.append(lookup_trace)
        self._lookup_task = self._entry.async_create_background_task(
            self.hass,
            self._async_lookup(ctx, key, lookup_trace),
            f"{DOMAIN} artwork lookup for {self.entity_id}",
        )

    @callback
    def _async_cancel_lookup(self) -> None:
        if self._lookup_task is not None and not self._lookup_task.done():
            self._debug("cancelling the running lookup")
            self._lookup_task.cancel()
        self._lookup_task = None
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

    @callback
    def _async_schedule_retry(self) -> None:
        if self._retry_unsub is not None:
            return
        if self._retries >= LOOKUP_MAX_RETRIES:
            trace(_LOGGER, "giving up after %d retries", LOOKUP_MAX_RETRIES)
            return
        self._retries += 1
        trace(
            _LOGGER,
            "retrying in %s (retry %d of %d)",
            format_seconds(LOOKUP_RETRY_DELAY.total_seconds()),
            self._retries,
            LOOKUP_MAX_RETRIES,
        )
        self._retry_unsub = async_call_later(
            self.hass, LOOKUP_RETRY_DELAY, self._async_retry
        )

    @callback
    def _async_retry(self, _now: Any) -> None:
        self._retry_unsub = None
        self._async_schedule_lookup(
            f"retry {self._retries} of {LOOKUP_MAX_RETRIES}", force=True
        )

    async def _async_lookup(
        self, ctx: MediaContext, key: Any, lookup_trace: LookupTrace
    ) -> None:
        token = set_trace(lookup_trace)
        try:
            image = await self._async_resolve_image(ctx)
        except asyncio.CancelledError:
            lookup_trace.finish("cancelled")
            raise
        finally:
            reset_trace(token)
        if key != self._lookup_key:
            lookup_trace.finish("discarded", **image.describe())
            self._debug("discarding lookup result: what is playing has changed")
            return
        lookup_trace.finish("done", **image.describe())
        self._debug("artwork: %s", lookup_trace.outcome)
        self._image = image
        self.async_write_ha_state()

    async def _async_resolve_image(self, ctx: MediaContext) -> _Image:
        """Pick artwork: aggregator, source's own image, per-app fallback, none."""
        player = self._player
        has_source_picture = self._source_picture is not None
        if player.prefer_source_image and has_source_picture:
            trace(
                _LOGGER,
                "the source has its own image and 'prefer source image' is on;"
                " not searching",
            )
            return _Image(source=IMAGE_SOURCE_SOURCE)

        if ctx.title:
            result: ImageResult | None = None
            try:
                result = await self._runtime.registry.async_resolve(ctx, player)
            except TmdbAuthError:
                _LOGGER.warning("TMDb rejected the configured API key")
                self._entry.async_start_reauth(self.hass)
            except TransientLookupError:
                self._async_schedule_retry()
            except Exception as err:
                trace(_LOGGER, "unexpected error: %r", err)
                _LOGGER.exception(
                    "Unexpected error resolving artwork for %s", ctx.title
                )
            if result is not None:
                return _Image(
                    source=IMAGE_SOURCE_AGGREGATOR,
                    local=result.image,
                    url=result.url,
                    result=result,
                )
        else:
            trace(_LOGGER, "the source has no media_title; not searching")

        if has_source_picture:
            trace(_LOGGER, "using the source's own image")
            return _Image(source=IMAGE_SOURCE_SOURCE)

        fallback = await self._runtime.cache.async_find_app_fallback(
            ctx.app_name, ctx.app_id
        )
        if fallback is not None:
            trace(_LOGGER, "using per-app fallback image %s", fallback.path)
            return _Image(source=IMAGE_SOURCE_APP_FALLBACK, local=fallback)
        trace(
            _LOGGER,
            "no per-app fallback image in %s",
            self._runtime.cache.root / APP_FALLBACK_DIR,
        )
        return _Image()

    # ------------------------------------------------------------------ artwork exposure

    @property
    def media_image_url(self) -> str | None:
        """Remote image URL (only when the image is not cached locally)."""
        if self._image.local is not None:
            return None
        return self._image.url

    @property
    def media_image_remotely_accessible(self) -> bool:
        """Remote TMDb/Stremio URLs can be used by the frontend directly."""
        return self._image.local is None and self._image.url is not None

    @property
    def media_image_hash(self) -> str | None:
        """Hash used as cache buster for the media_player_proxy URL."""
        if self._image.local is not None:
            return self._image.local.image_hash
        if self._image.url is not None:
            return hashlib.sha256(self._image.url.encode()).hexdigest()[:16]
        return None

    @property
    def entity_picture(self) -> str | None:
        """Use the source's picture when that is the chosen image."""
        if self._image.source == IMAGE_SOURCE_SOURCE:
            if self.state == MediaPlayerState.OFF:
                return None
            return self._source_picture
        return super().entity_picture

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Serve cached files from disk; otherwise fetch the remote URL."""
        if (local := self._image.local) is not None:
            if (data := await self._runtime.cache.async_read(local)) is None:
                return None, None
            return data, local.content_type
        return await super().async_get_media_image()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose where the artwork came from."""
        attrs: dict[str, Any] = {
            ATTR_SOURCE_ENTITY_ID: self._player.source_entity_id,
            ATTR_IMAGE_SOURCE: self._image.source,
        }
        if (result := self._image.result) is not None:
            attrs[ATTR_AGGREGATOR] = result.aggregator
            attrs[ATTR_MEDIA_KIND] = result.media_kind.value
            if result.tmdb_id is not None:
                attrs[ATTR_TMDB_ID] = result.tmdb_id
            if result.imdb_id is not None:
                attrs[ATTR_IMDB_ID] = result.imdb_id
        return attrs

    # ------------------------------------------------------------------ forwarding

    async def _async_forward(self, service: str, **data: Any) -> None:
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self._player.source_entity_id, **data},
            blocking=True,
            context=self._context,
        )

    async def async_turn_on(self) -> None:
        """Forward turn_on."""
        await self._async_forward(SERVICE_TURN_ON)

    async def async_turn_off(self) -> None:
        """Forward turn_off."""
        await self._async_forward(SERVICE_TURN_OFF)

    async def async_toggle(self) -> None:
        """Forward toggle."""
        await self._async_forward(SERVICE_TOGGLE)

    async def async_volume_up(self) -> None:
        """Forward volume_up."""
        await self._async_forward(SERVICE_VOLUME_UP)

    async def async_volume_down(self) -> None:
        """Forward volume_down."""
        await self._async_forward(SERVICE_VOLUME_DOWN)

    async def async_set_volume_level(self, volume: float) -> None:
        """Forward volume_set."""
        await self._async_forward(SERVICE_VOLUME_SET, volume_level=volume)

    async def async_mute_volume(self, mute: bool) -> None:
        """Forward volume_mute."""
        await self._async_forward(SERVICE_VOLUME_MUTE, is_volume_muted=mute)

    async def async_media_play(self) -> None:
        """Forward media_play."""
        await self._async_forward(SERVICE_MEDIA_PLAY)

    async def async_media_pause(self) -> None:
        """Forward media_pause."""
        await self._async_forward(SERVICE_MEDIA_PAUSE)

    async def async_media_play_pause(self) -> None:
        """Forward media_play_pause."""
        await self._async_forward(SERVICE_MEDIA_PLAY_PAUSE)

    async def async_media_stop(self) -> None:
        """Forward media_stop."""
        await self._async_forward(SERVICE_MEDIA_STOP)

    async def async_media_next_track(self) -> None:
        """Forward media_next_track."""
        await self._async_forward(SERVICE_MEDIA_NEXT_TRACK)

    async def async_media_previous_track(self) -> None:
        """Forward media_previous_track."""
        await self._async_forward(SERVICE_MEDIA_PREVIOUS_TRACK)

    async def async_media_seek(self, position: float) -> None:
        """Forward media_seek."""
        await self._async_forward(SERVICE_MEDIA_SEEK, seek_position=position)

    async def async_select_source(self, source: str) -> None:
        """Forward select_source."""
        await self._async_forward("select_source", source=source)

    async def async_select_sound_mode(self, sound_mode: str) -> None:
        """Forward select_sound_mode."""
        await self._async_forward("select_sound_mode", sound_mode=sound_mode)

    async def async_clear_playlist(self) -> None:
        """Forward clear_playlist."""
        await self._async_forward("clear_playlist")

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Forward shuffle_set."""
        await self._async_forward(SERVICE_SHUFFLE_SET, shuffle=shuffle)

    async def async_set_repeat(self, repeat: Any) -> None:
        """Forward repeat_set."""
        await self._async_forward(SERVICE_REPEAT_SET, repeat=repeat)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Forward play_media."""
        data: dict[str, Any] = {
            "media_content_type": media_type,
            "media_content_id": media_id,
        }
        for key in ("enqueue", "announce", "extra"):
            if kwargs.get(key) is not None:
                data[key] = kwargs[key]
        await self._async_forward("play_media", **data)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Delegate media browsing to the source player."""
        component = self.hass.data.get(DATA_COMPONENT)
        source = (
            component.get_entity(self._player.source_entity_id) if component else None
        )
        if source is None:
            raise BrowseError("Source media player is not available")
        return await source.async_browse_media(media_content_type, media_content_id)
