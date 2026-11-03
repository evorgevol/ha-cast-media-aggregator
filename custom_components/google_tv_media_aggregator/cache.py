"""Local on-disk image cache and per-app fallback images."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import tempfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import APP_FALLBACK_DEFAULT, APP_FALLBACK_DIR

_LOGGER = logging.getLogger(__name__)

EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_KEY_LENGTH = 120


@dataclass(frozen=True, slots=True)
class CachedImage:
    """An image file on disk."""

    path: Path
    content_type: str
    image_hash: str


def cache_key(value: str) -> str | None:
    """Turn an arbitrary string into a safe file name stem."""
    if not value.strip():
        return None
    slug = slugify(value, separator="_")[:MAX_KEY_LENGTH].strip("_")
    # slugify returns "unknown" when nothing survives; don't let titles collide.
    if not slug or (slug == "unknown" and "unknown" not in value.casefold()):
        return f"h_{hashlib.sha256(value.encode()).hexdigest()[:16]}"
    return slug


def _find_image(base: Path) -> CachedImage | None:
    """Find base.<ext> for any supported extension (runs in executor)."""
    for ext, content_type in EXTENSION_CONTENT_TYPES.items():
        path = base.parent / f"{base.name}{ext}"
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256(
            f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode()
        ).hexdigest()[:16]
        return CachedImage(path=path, content_type=content_type, image_hash=digest)
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    """Write data to path atomically (runs in executor)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def prepare_cache_dir(root: Path) -> None:
    """Create the cache root and the default_apps folder (runs in executor)."""
    (root / APP_FALLBACK_DIR).mkdir(parents=True, exist_ok=True)
    if not os.access(root, os.W_OK):
        raise PermissionError(f"{root} is not writable")


class ImageCache:
    """Cache downloaded artwork under a media directory.

    Layout: <root>/<category>/<key>.<ext>, plus user supplied images in
    <root>/default_apps/.
    """

    def __init__(self, hass: HomeAssistant, root: Path, enabled: bool) -> None:
        """Initialise the cache."""
        self._hass = hass
        self.root = root
        self.enabled = enabled
        self._writable = False

    @property
    def writable(self) -> bool:
        """Return True if new images will be written to disk."""
        return self.enabled and self._writable

    async def async_setup(self) -> None:
        """Create directories; disable writing if that fails."""
        if not self.enabled:
            # Only app fallback images are read; never create folders.
            self._writable = False
            return
        try:
            await self._hass.async_add_executor_job(prepare_cache_dir, self.root)
        except OSError as err:
            self._writable = False
            _LOGGER.warning(
                "Image cache directory %s is not usable (%s); images will not be"
                " cached",
                self.root,
                err,
            )
            return
        self._writable = True

    def _base(self, category: str, key: str) -> Path | None:
        if (stem := cache_key(key)) is None:
            return None
        return self.root / category / stem

    async def async_get(self, category: str, key: str) -> CachedImage | None:
        """Return a cached image, or None."""
        if not self.enabled or (base := self._base(category, key)) is None:
            return None
        return await self._hass.async_add_executor_job(_find_image, base)

    async def async_put(
        self, category: str, key: str, data: bytes, content_type: str
    ) -> CachedImage | None:
        """Store an image and return it, or None if caching is unavailable."""
        if not self.writable or (base := self._base(category, key)) is None:
            return None
        ext = CONTENT_TYPE_EXTENSIONS.get(content_type, ".jpg")
        path = base.parent / f"{base.name}{ext}"
        try:
            await self._hass.async_add_executor_job(_write_atomic, path, data)
        except OSError as err:
            _LOGGER.warning("Unable to write cached image %s: %s", path, err)
            return None
        return await self._hass.async_add_executor_job(_find_image, base)

    async def async_find_app_fallback(
        self, app_name: str | None, app_id: str | None
    ) -> CachedImage | None:
        """Find a user supplied fallback image for an app.

        Looks for default_apps/<app_name>, default_apps/<app_id>, then
        default_apps/default (any supported extension). Works even when caching
        of downloaded images is disabled.
        """
        folder = self.root / APP_FALLBACK_DIR
        stems: list[str] = []
        for candidate in (app_name, app_id):
            if candidate and (stem := cache_key(candidate)) and stem not in stems:
                stems.append(stem)
        stems.append(APP_FALLBACK_DEFAULT)

        def _lookup() -> CachedImage | None:
            for stem in stems:
                if (found := _find_image(folder / stem)) is not None:
                    return found
            return None

        return await self._hass.async_add_executor_job(_lookup)

    async def async_read(self, image: CachedImage) -> bytes | None:
        """Read an image from disk."""
        return await self._hass.async_add_executor_job(_read, image.path)


async def async_download_image(
    session: aiohttp.ClientSession, url: str
) -> tuple[bytes, str] | None:
    """Download an image. Returns (data, content_type) or None."""
    if not url.startswith(("https://", "http://")):
        return None
    try:
        async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                _LOGGER.debug("Image download failed (HTTP %s): %s", resp.status, url)
                return None
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
            content_type = content_type.strip().lower()
            if content_type not in CONTENT_TYPE_EXTENSIONS:
                _LOGGER.debug("Ignoring non-image content (%s): %s", content_type, url)
                return None
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    _LOGGER.debug("Image too large, ignoring: %s", url)
                    return None
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Image download failed (%s): %s", type(err).__name__, url)
        return None
    if not data:
        return None
    return bytes(data), content_type
