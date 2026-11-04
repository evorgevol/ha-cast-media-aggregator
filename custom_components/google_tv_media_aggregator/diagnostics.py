"""Diagnostics for the Google TV Streamer Media Aggregator integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_TMDB_API_KEY
from .runtime import AggregatorConfigEntry

TO_REDACT = {CONF_TMDB_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AggregatorConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the hub entry."""
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    players = []
    for subentry_id, subentry in entry.subentries.items():
        entity_id = registry.async_get_entity_id(
            "media_player", entry.domain, subentry_id
        )
        state = hass.states.get(entity_id) if entity_id else None
        players.append(
            {
                "title": subentry.title,
                "data": dict(subentry.data),
                "entity_id": entity_id,
                "state": state.state if state else None,
                "attributes": (
                    {
                        k: v
                        for k, v in state.attributes.items()
                        if k not in ("entity_picture", "entity_picture_local")
                    }
                    if state
                    else None
                ),
                "lookup_history": [
                    lookup.as_dict()
                    for lookup in runtime.lookup_history.get(subentry_id, ())
                ],
            }
        )
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "settings": asdict(runtime.settings),
        "tmdb_configured": runtime.tmdb is not None,
        "cache": {
            "root": str(runtime.cache.root),
            "enabled": runtime.cache.enabled,
            "writable": runtime.cache.writable,
        },
        "aggregators": [agg.name for agg in runtime.registry.aggregators],
        "lookup_memo": runtime.registry.deps.memo.snapshot(),
        "seen_apps": runtime.seen_apps.apps,
        "players": players,
    }
