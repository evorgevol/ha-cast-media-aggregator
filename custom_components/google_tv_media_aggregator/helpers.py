"""Helpers shared by the config flow and the media player platform."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .aggregators import PlayerConfig
from .const import AGGREGATED_NAME_SUFFIX, AGGREGATED_SUFFIX


def default_object_id(source_entity_id: str) -> str:
    """Return '<source object id>_aggregated'."""
    return f"{source_entity_id.split('.', 1)[1]}{AGGREGATED_SUFFIX}"


def suggested_object_id(player: PlayerConfig) -> str:
    """Return the object id to suggest for a new aggregated player."""
    if player.name and (slug := slugify(player.name)):
        return slug
    return default_object_id(player.source_entity_id)


def default_player_name(hass: HomeAssistant, source_entity_id: str) -> str:
    """Return '<source friendly name> Aggregated'."""
    if (state := hass.states.get(source_entity_id)) is not None:
        base = state.name
    elif (entry := er.async_get(hass).async_get(source_entity_id)) is not None and (
        entry.name or entry.original_name
    ):
        base = entry.name or entry.original_name or ""
    else:
        base = source_entity_id.split(".", 1)[1].replace("_", " ").title()
    return f"{base} {AGGREGATED_NAME_SUFFIX}"
