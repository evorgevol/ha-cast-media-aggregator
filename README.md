# Google TV Streamer Media Aggregator

A Home Assistant custom integration (installable through HACS) that adds artwork to Google TV Streamer media players.

When something plays on a Google TV Streamer, Home Assistant usually only gets `media_title`, `media_artist` and `app_id`, with no image. This integration wraps that media player in a new **aggregated** media player. The new player:

- mirrors the state and attributes of the original player,
- forwards all controls (play/pause, volume, source, …) to the original player,
- looks up a poster for what is playing and exposes it as `entity_picture`.

You can create as many aggregated players as you like, one per Google TV Streamer.

## Installation

1. In HACS, open **⋮ → Custom repositories**, add `https://github.com/evorgevol/ha-cast-media-aggregator` with the type **Integration**.
2. Install **Google TV Streamer Media Aggregator** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration** and pick **Google TV Streamer Media Aggregator**.
4. Optionally enter a [TMDb](https://www.themoviedb.org/settings/api) **API key (v3)** or **API Read Access Token (v4)**. Without one, only cached and per-app fallback images are used.

The minimum Home Assistant version is 2025.4.

The TMDb key is stored in the config entry data, not in the options. It is redacted from diagnostics and never logged. To change or remove it later, use **⋮ → Reconfigure** on the integration. If TMDb rejects the key, Home Assistant shows a re-authentication prompt.

## Adding aggregated players

On the integration page, click **Add aggregated player**, then fill in:

| Field | Description |
| --- | --- |
| Source media player | The Google TV Streamer's media player, e.g. `media_player.pablo`. |
| Name | Optional. When blank, the name is the source name plus "Aggregated", and the entity ID is `media_player.<source>_aggregated` (e.g. `media_player.pablo_aggregated`). |
| Stremio media player | Optional. A media player from the [Stremio integration](https://github.com/tamaygz/hacs-stremio). See [Stremio](#stremio-aggregator). |
| Stremio app IDs | App IDs or names that identify Stremio on the source. Default: `com.stremio.one`, `Stremio`. |
| App filter / Apps | Restricts which apps the default aggregator looks up artwork for: all apps, only the listed apps (allow list), or all except the listed apps (deny list). Apps seen on your players are offered as suggestions, and you can type your own (comma separated is fine). |
| Prefer the source's own image | When the source already provides an image, use it instead of looking one up. Default: on. |

To edit an existing player, open the **⋮** menu on the player's entry and choose **Reconfigure**. Changing the name updates the device name but keeps the entity ID. Deleting the entry removes the aggregated player.

## Settings

Open **Configure** on the integration to change the settings shared by all players:

| Option | Default | Description |
| --- | --- | --- |
| Video threshold | `20` minutes | Media longer than this (or with an unknown length) is treated as video. Shorter media is treated as music and skipped. |
| Cache images locally | on | Saves downloaded artwork to disk and serves it from there. When off, the TMDb image URL is used directly. |
| Cache directory | `/media/tv-streamer-media/` | Must be an absolute path. When caching is on, it must also be inside a media directory or [`allowlist_external_dirs`](https://www.home-assistant.io/integrations/homeassistant/#allowlist_external_dirs). |
| TMDb language | Home Assistant language | Language for TMDb lookups, e.g. `en-AU`. |

## How artwork is found

Each time the source's title, artist, app or state changes, the aggregated player resolves an image in this order:

1. **Source image.** If "prefer the source's own image" is on and the source has an image, that image is used.
2. **Aggregators.** These are chosen per app. The first matching aggregator that returns a result wins:
   1. [Stremio aggregator](#stremio-aggregator)
   2. [Default aggregator](#default-aggregator)
3. **Source image.** The source's own image, if it has one (this applies when the preference above is off).
4. **Per-app fallback image.** See [below](#per-app-fallback-images).
5. No image.

Every aggregator checks the local cache **before** making any API call. Failed lookups are remembered for 6 hours. Temporary TMDb errors are retried in the background.

### Default aggregator

Used for any app allowed by the app filter. It first decides what is playing:

- If `media_content_type` is set, it decides. Music types are skipped; `tvshow`/`episode`/`season` mean a TV show; `movie` means a movie.
- If the length is known and shorter than the video threshold, the media is assumed to be **music**, which is skipped (out of scope for now).
- **Title only** means a **movie**: TMDb movie search, using the year if the title contains one.
- **Title + artist** means a **TV show**: `media_title` is taken as the show name, and a TMDb TV search is made.
- If the specific search finds nothing, a TMDb multi-search is tried.

### Stremio aggregator

Used when a **Stremio media player** is configured and the source's `app_id`/`app_name` matches one of the **Stremio app IDs**. It reads the IMDb ID exposed by the Stremio integration and uses TMDb's `/find` endpoint for an exact match. If TMDb has no poster, it uses Stremio's own poster.

Stremio's state is per account, so the aggregator only uses it when its title loosely matches the source's title. That way, playback on another device doesn't affect this one. If the Stremio player isn't configured, isn't playing, or doesn't match, the default aggregator is used instead.

## Image cache

When caching is on, artwork is stored under the cache directory:

```text
/media/tv-streamer-media/
├── default_apps/   # your per-app fallback images (see below)
├── movie/          # default aggregator, movies
├── tv/             # default aggregator, TV shows
├── imdb/           # Stremio aggregator, keyed by IMDb ID
└── stremio/        # Stremio's own posters
```

Images are served through Home Assistant's media player image proxy, so `entity_picture` is a `/api/media_player_proxy/...` URL. You can delete any file or folder to force a fresh lookup.

### Per-app fallback images

Put an image in `<cache directory>/default_apps/` to use it whenever nothing better is found for an app. The first file found is used, in this order:

1. The app name, slugified, e.g. `default_apps/stremio.jpg`, `default_apps/netflix.png`.
2. The app ID, slugified, e.g. `default_apps/com_stremio_one.jpg`.
3. `default_apps/default.jpg`, used for any app.

Supported extensions are `.jpg`, `.jpeg`, `.png` and `.webp`. Fallback images are also used when caching is off; in that case the folder is not created automatically.

## Extra attributes

Each aggregated player has these extra state attributes:

| Attribute | Description |
| --- | --- |
| `source_entity_id` | The wrapped media player. |
| `image_source` | `aggregator`, `source`, `app_fallback` or `none`. |
| `aggregator` | The aggregator that found the image (`stremio` or `default`). |
| `media_kind` | `movie` or `tv`, when known. |
| `tmdb_id` / `imdb_id` | IDs of the matched title, when known. |

## Out of scope (for now)

- Music artwork, e.g. YouTube Music via a PO token generator or Music Assistant metadata.
- Grouping and media search on the aggregated player.

## Development

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements_test.txt
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
.venv\Scripts\pytest tests
```

The tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component). They run on Linux and macOS; Home Assistant's test harness doesn't support Windows natively, so use WSL or the CI. CI runs ruff, plus the tests against both the latest Home Assistant and the minimum supported version, and validates with hassfest and HACS.

### Testing on your own Home Assistant

- **Released version:** install through HACS as described in [Installation](#installation).
- **Work in progress:** copy `custom_components/google_tv_media_aggregator` into `/config/custom_components/` (for example with the Samba or Studio Code Server add-on) and restart Home Assistant. Remove the HACS-installed copy first, otherwise HACS overwrites it. Manual copies report version `0.0.0`.

## Releases

Every push to `main` that changes `custom_components/` or `hacs.json` publishes a [GitHub release](https://github.com/evorgevol/ha-cast-media-aggregator/releases) once the tests and validation pass. HACS offers it as an update, and the update dialog shows the release notes of every release between the installed version and the latest one.

The [release workflow](.github/workflows/release.yml) works out the version and the notes from the commit messages since the previous release, using [Conventional Commits](https://www.conventionalcommits.org):

| Commit message | Version bump | Release notes section |
| --- | --- | --- |
| `feat!: …` or a `BREAKING CHANGE:` footer | major (`1.4.2` → `2.0.0`) | ⚠️ Breaking changes |
| `feat: …` / `feat(scope): …` | minor (`1.4.2` → `1.5.0`) | Features |
| `fix: …` | patch (`1.4.2` → `1.4.3`) | Fixes |
| `perf: …` | patch | Performance |
| anything else (`refactor:`, `docs:`, plain messages) | patch | Other changes |

Write commit messages for users, since they become the release notes. Only commits that touch `custom_components/` or `hacs.json` are included.

- To push several commits without releasing each one, put `[skip release]` in the last commit's message. The next release includes all of them.
- To release a specific version (for example `1.0.0`), run the **Release** workflow manually from the Actions tab and enter the version.

The `version` in `manifest.json` is a `0.0.0` placeholder. The workflow writes the real version into the `google_tv_media_aggregator.zip` attached to each release, which is what HACS installs.

## Attribution

This product uses the TMDb API but is not endorsed or certified by TMDb.
