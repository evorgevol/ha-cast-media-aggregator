"""Config flow for the Google TV Streamer Media Aggregator integration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .aggregators.base import split_app_list
from .cache import prepare_cache_dir
from .const import (
    APP_FILTER_MODES,
    APP_FILTER_NONE,
    CONF_APP_FILTER_LIST,
    CONF_APP_FILTER_MODE,
    CONF_CACHE_DIR,
    CONF_CACHE_ENABLED,
    CONF_NAME,
    CONF_PREFER_SOURCE_IMAGE,
    CONF_SOURCE_ENTITY_ID,
    CONF_STREMIO_APP_IDS,
    CONF_STREMIO_ENTITY_ID,
    CONF_TMDB_API_KEY,
    CONF_TMDB_LANGUAGE,
    CONF_VIDEO_THRESHOLD,
    DEFAULT_PREFER_SOURCE_IMAGE,
    DEFAULT_STREMIO_APP_IDS,
    DOMAIN,
    SUBENTRY_TYPE_PLAYER,
    TITLE,
    TMDB_API_KEY_URL,
)
from .helpers import default_player_name
from .runtime import SeenApps, settings_from_options
from .tmdb import TmdbAuthError, TmdbClient, TmdbError

STREMIO_DOMAIN = "stremio"

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


async def _async_validate_tmdb_key(hass: HomeAssistant, api_key: str) -> str | None:
    """Return an error key, or None if the key works."""
    client = TmdbClient(async_get_clientsession(hass), api_key)
    try:
        await client.async_validate()
    except TmdbAuthError:
        return "invalid_auth"
    except TmdbError:
        return "cannot_connect"
    return None


async def _async_validate_cache_dir(
    hass: HomeAssistant, path: str, cache_enabled: bool
) -> str | None:
    """Return an error key, or None if the directory is usable.

    When caching is off the directory is only read for app fallback images,
    so it just has to be an absolute path.
    """
    if not os.path.isabs(path):
        return "cache_dir_not_absolute"
    if not cache_enabled:
        return None
    if not await hass.async_add_executor_job(hass.config.is_allowed_path, path):
        return "cache_dir_not_allowed"
    try:
        await hass.async_add_executor_job(prepare_cache_dir, Path(path))
    except OSError:
        return "cache_dir_not_writable"
    return None


class GoogleTvMediaAggregatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Hub config flow: holds the TMDb credentials."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return AggregatorOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry types: one per aggregated player."""
        return {SUBENTRY_TYPE_PLAYER: AggregatedPlayerSubentryFlow}

    async def _async_handle_key(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        api_key = (user_input.get(CONF_TMDB_API_KEY) or "").strip()
        if api_key and (error := await _async_validate_tmdb_key(self.hass, api_key)):
            errors["base"] = error
            return None
        return {CONF_TMDB_API_KEY: api_key} if api_key else {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the hub. The TMDb key is optional."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_handle_key(user_input, errors)
            if data is not None:
                return self.async_create_entry(title=TITLE, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Optional(CONF_TMDB_API_KEY): PASSWORD_SELECTOR}
            ),
            errors=errors,
            description_placeholders={"tmdb_api_url": TMDB_API_KEY_URL},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change or remove the TMDb key."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_handle_key(user_input, errors)
            if data is not None:
                # The update listener reloads the entry.
                self.hass.config_entries.async_update_entry(entry, data=data)
                return self.async_abort(reason="reconfigure_successful")
        schema = self.add_suggested_values_to_schema(
            vol.Schema({vol.Optional(CONF_TMDB_API_KEY): PASSWORD_SELECTOR}),
            user_input or dict(entry.data),
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """TMDb rejected the stored key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new TMDb key (blank removes it)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await self._async_handle_key(user_input, errors)
            if data is not None:
                self.hass.config_entries.async_update_entry(
                    self._get_reauth_entry(), data=data
                )
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_TMDB_API_KEY): PASSWORD_SELECTOR}
            ),
            errors=errors,
        )


class AggregatorOptionsFlow(OptionsFlow):
    """Shared settings: video threshold, image cache, TMDb language."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit hub options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = dict(user_input)
            options[CONF_VIDEO_THRESHOLD] = int(options[CONF_VIDEO_THRESHOLD])
            options[CONF_CACHE_DIR] = options[CONF_CACHE_DIR].strip()
            options[CONF_TMDB_LANGUAGE] = (
                options.get(CONF_TMDB_LANGUAGE) or ""
            ).strip()
            if error := await _async_validate_cache_dir(
                self.hass, options[CONF_CACHE_DIR], options[CONF_CACHE_ENABLED]
            ):
                errors[CONF_CACHE_DIR] = error
            else:
                return self.async_create_entry(data=options)

        current = settings_from_options(self.hass, dict(self.config_entry.options))
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VIDEO_THRESHOLD, default=current.video_threshold_minutes
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=600,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(
                    CONF_CACHE_ENABLED, default=current.cache_enabled
                ): BooleanSelector(),
                vol.Required(CONF_CACHE_DIR, default=current.cache_dir): TextSelector(),
                vol.Optional(CONF_TMDB_LANGUAGE): TextSelector(),
            }
        )
        suggested = {CONF_TMDB_LANGUAGE: current.language or ""}
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


class AggregatedPlayerSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure an aggregated media player."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add an aggregated player."""
        return await self._async_step_player("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an aggregated player."""
        return await self._async_step_player(
            "reconfigure", user_input, self._get_reconfigure_subentry()
        )

    async def _async_step_player(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        subentry: ConfigSubentry | None,
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        errors: dict[str, str] = {}
        own_entities = {
            ent.entity_id
            for ent in er.async_entries_for_config_entry(
                er.async_get(self.hass), entry.entry_id
            )
        }

        if user_input is not None:
            data = _clean_player_input(user_input)
            source = data[CONF_SOURCE_ENTITY_ID]
            current_id = subentry.subentry_id if subentry else None
            if source in own_entities:
                errors[CONF_SOURCE_ENTITY_ID] = "source_is_aggregated"
            elif any(
                other.data.get(CONF_SOURCE_ENTITY_ID) == source
                for other in entry.subentries.values()
                if other.subentry_id != current_id
                and other.subentry_type == SUBENTRY_TYPE_PLAYER
            ):
                errors[CONF_SOURCE_ENTITY_ID] = "source_already_configured"
            elif data.get(CONF_STREMIO_ENTITY_ID) == source:
                errors[CONF_STREMIO_ENTITY_ID] = "stremio_is_source"
            else:
                title = data.get(CONF_NAME) or default_player_name(self.hass, source)
                if self.source == SOURCE_RECONFIGURE and subentry is not None:
                    return self.async_update_and_abort(
                        entry, subentry, title=title, data=data, unique_id=source
                    )
                return self.async_create_entry(title=title, data=data, unique_id=source)

        app_options = await self._async_app_options(entry)
        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_ENTITY_ID): EntitySelector(
                    EntitySelectorConfig(
                        domain="media_player", exclude_entities=sorted(own_entities)
                    )
                ),
                vol.Optional(CONF_NAME): TextSelector(),
                vol.Optional(CONF_STREMIO_ENTITY_ID): EntitySelector(
                    EntitySelectorConfig(
                        domain="media_player", integration=STREMIO_DOMAIN
                    )
                ),
                vol.Optional(CONF_STREMIO_APP_IDS): SelectSelector(
                    SelectSelectorConfig(
                        options=_merge_options(DEFAULT_STREMIO_APP_IDS, app_options),
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_APP_FILTER_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=APP_FILTER_MODES,
                        translation_key=CONF_APP_FILTER_MODE,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_APP_FILTER_LIST): SelectSelector(
                    SelectSelectorConfig(
                        options=app_options,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_PREFER_SOURCE_IMAGE): BooleanSelector(),
            }
        )
        defaults: dict[str, Any] = {
            CONF_STREMIO_APP_IDS: list(DEFAULT_STREMIO_APP_IDS),
            CONF_APP_FILTER_MODE: APP_FILTER_NONE,
            CONF_PREFER_SOURCE_IMAGE: DEFAULT_PREFER_SOURCE_IMAGE,
        }
        if subentry is not None:
            defaults.update(subentry.data)
        if user_input is not None:
            defaults.update(user_input)
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors,
        )

    async def _async_app_options(self, entry: ConfigEntry) -> list[SelectOptionDict]:
        """Apps seen previously plus apps currently showing on any media player."""
        if entry.state is ConfigEntryState.LOADED:
            apps = entry.runtime_data.seen_apps.apps
        else:
            seen = SeenApps(self.hass)
            await seen.async_load()
            apps = seen.apps
        for state in self.hass.states.async_all("media_player"):
            app_id = state.attributes.get("app_id")
            app_name = state.attributes.get("app_name")
            if key := app_id or app_name:
                apps.setdefault(key, app_name or key)
        return [
            SelectOptionDict(
                value=key, label=key if label == key else f"{label} ({key})"
            )
            for key, label in sorted(apps.items(), key=lambda item: item[1].casefold())
        ]


def _merge_options(
    values: list[str], options: list[SelectOptionDict]
) -> list[SelectOptionDict]:
    merged = [SelectOptionDict(value=v, label=v) for v in values]
    seen = set(values)
    merged.extend(opt for opt in options if opt["value"] not in seen)
    return merged


def _clean_player_input(user_input: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_SOURCE_ENTITY_ID: user_input[CONF_SOURCE_ENTITY_ID],
        CONF_STREMIO_APP_IDS: split_app_list(
            user_input.get(CONF_STREMIO_APP_IDS, DEFAULT_STREMIO_APP_IDS)
        ),
        CONF_APP_FILTER_MODE: user_input.get(CONF_APP_FILTER_MODE) or APP_FILTER_NONE,
        CONF_APP_FILTER_LIST: split_app_list(user_input.get(CONF_APP_FILTER_LIST)),
        CONF_PREFER_SOURCE_IMAGE: user_input.get(
            CONF_PREFER_SOURCE_IMAGE, DEFAULT_PREFER_SOURCE_IMAGE
        ),
    }
    if name := (user_input.get(CONF_NAME) or "").strip():
        data[CONF_NAME] = name
    if stremio := user_input.get(CONF_STREMIO_ENTITY_ID):
        data[CONF_STREMIO_ENTITY_ID] = stremio
    return data
