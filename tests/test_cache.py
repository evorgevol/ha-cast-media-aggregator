"""Tests for the image cache."""

from __future__ import annotations

from pathlib import Path

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.google_tv_media_aggregator.cache import (
    ImageCache,
    async_download_image,
    cache_key,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def test_cache_key() -> None:
    assert cache_key("The Office (US)") == "the_office_us"
    assert cache_key("../../etc/passwd") == "etc_passwd"
    assert cache_key("!!!") == cache_key("!!!")
    assert cache_key("!!!").startswith("h_")
    assert cache_key("???") != cache_key("!!!")
    assert cache_key("  ") is None


async def test_setup_creates_default_apps(hass: HomeAssistant, tmp_path: Path) -> None:
    cache = ImageCache(hass, tmp_path / "cache", enabled=True)
    await cache.async_setup()
    assert (tmp_path / "cache" / "default_apps").is_dir()
    assert cache.writable


async def test_put_and_get(hass: HomeAssistant, tmp_path: Path) -> None:
    cache = ImageCache(hass, tmp_path, enabled=True)
    await cache.async_setup()

    assert await cache.async_get("movie", "Inception") is None
    stored = await cache.async_put("movie", "Inception", JPEG, "image/jpeg")
    assert stored is not None
    assert stored.path == tmp_path / "movie" / "inception.jpg"
    assert stored.content_type == "image/jpeg"

    found = await cache.async_get("movie", "Inception")
    assert found == stored
    assert await cache.async_read(found) == JPEG
    assert not list((tmp_path / "movie").glob(".tmp_*"))


async def test_disabled_cache(hass: HomeAssistant, tmp_path: Path) -> None:
    (tmp_path / "movie").mkdir()
    (tmp_path / "movie" / "inception.jpg").write_bytes(JPEG)
    cache = ImageCache(hass, tmp_path, enabled=False)
    await cache.async_setup()

    assert not cache.writable
    assert await cache.async_get("movie", "Inception") is None
    assert await cache.async_put("movie", "Other", JPEG, "image/jpeg") is None


async def test_app_fallback_order(hass: HomeAssistant, tmp_path: Path) -> None:
    cache = ImageCache(hass, tmp_path, enabled=False)
    await cache.async_setup()
    folder = tmp_path / "default_apps"
    # A disabled cache never creates folders; fallbacks are still read.
    assert not folder.exists()

    assert await cache.async_find_app_fallback("Stremio", "com.stremio.one") is None

    folder.mkdir()
    (folder / "default.webp").write_bytes(b"default")
    found = await cache.async_find_app_fallback("Stremio", "com.stremio.one")
    assert found is not None
    assert found.path.name == "default.webp"
    assert found.content_type == "image/webp"

    (folder / "com_stremio_one.png").write_bytes(b"by-id")
    found = await cache.async_find_app_fallback("Stremio", "com.stremio.one")
    assert found is not None
    assert found.path.name == "com_stremio_one.png"

    (folder / "stremio.jpg").write_bytes(b"by-name")
    found = await cache.async_find_app_fallback("Stremio", "com.stremio.one")
    assert found is not None
    assert found.path.name == "stremio.jpg"

    found = await cache.async_find_app_fallback(None, None)
    assert found is not None
    assert found.path.name == "default.webp"


async def test_hash_changes_when_file_changes(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    cache = ImageCache(hass, tmp_path, enabled=True)
    await cache.async_setup()
    first = await cache.async_put("tv", "show", JPEG, "image/jpeg")
    second = await cache.async_put("tv", "show", JPEG + b"more", "image/jpeg")
    assert first is not None
    assert second is not None
    assert first.image_hash != second.image_hash


async def test_download_image(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    session = async_get_clientsession(hass)
    aioclient_mock.get(
        "https://img.test/ok.jpg",
        content=JPEG,
        headers={"Content-Type": "image/jpeg; charset=binary"},
    )
    aioclient_mock.get(
        "https://img.test/page.html",
        text="<html>",
        headers={"Content-Type": "text/html"},
    )
    aioclient_mock.get("https://img.test/missing.jpg", status=404)

    assert await async_download_image(session, "https://img.test/ok.jpg") == (
        JPEG,
        "image/jpeg",
    )
    assert await async_download_image(session, "https://img.test/page.html") is None
    assert await async_download_image(session, "https://img.test/missing.jpg") is None
    assert await async_download_image(session, "file:///etc/passwd") is None
