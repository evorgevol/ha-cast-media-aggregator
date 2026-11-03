"""Constants for the Google TV Streamer Media Aggregator integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "google_tv_media_aggregator"
TITLE: Final = "Google TV Streamer Media Aggregator"

SUBENTRY_TYPE_PLAYER: Final = "aggregated_player"

# Hub entry data
CONF_TMDB_API_KEY: Final = "tmdb_api_key"
TMDB_API_KEY_URL: Final = "https://www.themoviedb.org/settings/api"

# Hub entry options
CONF_VIDEO_THRESHOLD: Final = "video_threshold_minutes"
CONF_CACHE_ENABLED: Final = "cache_enabled"
CONF_CACHE_DIR: Final = "cache_dir"
CONF_TMDB_LANGUAGE: Final = "tmdb_language"

# Aggregated player subentry data
CONF_SOURCE_ENTITY_ID: Final = "source_entity_id"
CONF_NAME: Final = "name"
CONF_STREMIO_ENTITY_ID: Final = "stremio_entity_id"
CONF_STREMIO_APP_IDS: Final = "stremio_app_ids"
CONF_APP_FILTER_MODE: Final = "app_filter_mode"
CONF_APP_FILTER_LIST: Final = "app_filter_list"
CONF_PREFER_SOURCE_IMAGE: Final = "prefer_source_image"

APP_FILTER_NONE: Final = "none"
APP_FILTER_ALLOW: Final = "allow"
APP_FILTER_DENY: Final = "deny"
APP_FILTER_MODES: Final = [APP_FILTER_NONE, APP_FILTER_ALLOW, APP_FILTER_DENY]

DEFAULT_VIDEO_THRESHOLD: Final = 20
DEFAULT_CACHE_ENABLED: Final = True
DEFAULT_CACHE_DIR: Final = "/media/tv-streamer-media/"
DEFAULT_STREMIO_APP_IDS: Final = ["com.stremio.one", "Stremio"]
DEFAULT_PREFER_SOURCE_IMAGE: Final = True

AGGREGATED_SUFFIX: Final = "_aggregated"
AGGREGATED_NAME_SUFFIX: Final = "Aggregated"

APP_FALLBACK_DIR: Final = "default_apps"
APP_FALLBACK_DEFAULT: Final = "default"

NEGATIVE_CACHE_TTL: Final = timedelta(hours=6)
LOOKUP_RETRY_DELAY: Final = timedelta(seconds=60)
LOOKUP_MAX_RETRIES: Final = 5

SEEN_APPS_STORAGE_KEY: Final = f"{DOMAIN}.seen_apps"
SEEN_APPS_STORAGE_VERSION: Final = 1

ATTR_SOURCE_ENTITY_ID: Final = "source_entity_id"
ATTR_IMAGE_SOURCE: Final = "image_source"
ATTR_AGGREGATOR: Final = "aggregator"
ATTR_MEDIA_KIND: Final = "media_kind"
ATTR_TMDB_ID: Final = "tmdb_id"
ATTR_IMDB_ID: Final = "imdb_id"

IMAGE_SOURCE_AGGREGATOR: Final = "aggregator"
IMAGE_SOURCE_SOURCE: Final = "source"
IMAGE_SOURCE_APP_FALLBACK: Final = "app_fallback"
IMAGE_SOURCE_NONE: Final = "none"
