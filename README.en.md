# Komga Manga Kit

A self-hosted foundation for Japanese manga organization and Komga integration.
The UI uses warm paper tones, a clay accent, responsive cards, and light/dark themes.

**Status: 0.1.0.dev0, a foundation for v0.1.** This is not a production v0.1 release.
The interface is currently Japanese. This document provides English setup instructions.
No manga acquisition or download functionality is included.

## Available

- Demo dashboard, library search, status and Japanese kana-row filters
- Read-only scanning of CBZ, ZIP, CBR, RAR, 7z, and EPUB locations
- ComicInfo.xml reading for CBZ/ZIP and per-series `series.json` overrides
- Configurable status folders, series names, book names, and folder hierarchies
- Organization previews, duplicate-target detection, and existing-file checks
- Komga connection checks and read-only library/series API calls
- CLI, Docker Compose, tests, and original fictitious sample CBZ generation

Organization is preview-only. No books are moved, converted, deleted, or written back to Komga.
External metadata enrichment, new-volume checks, safe execution, and portal features are planned.
Cover artwork currently uses generic visual placeholders rather than extracted archive covers.

## Run

Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/manga-kit serve
```

Open <http://127.0.0.1:5080>. Demo mode requires no keys or services.
The CLI server is intended for development; the container runs Gunicorn.

```bash
docker compose up --build -d
```

Compose binds to `127.0.0.1:5080` and starts only this application. It does not modify an existing Komga deployment.

## Your library

Copy `config.example.yaml` to `config.yaml`, set `app.demo: false`, and set `library.root` to your library path.
Relative paths resolve against the configuration file, independently of the application source.
Choose your status folders and naming templates. Restart after editing the config file.
Rules entered in the UI are used for previews only, not saved.

For Docker, copy `compose.local.example.yaml` to `compose.local.yaml`, customize its host paths,
and set the configured root to `/library`. Copy `.env.example` to `.env` for environment secrets.
The library mount is read-only. Do not commit private configuration or override files.

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build -d
```

Set `komga.base_url` or `KOMGA_URL` and the `KOMGA_API_KEY` environment variable to test your server from Settings.
There is no automatic connection on startup. Inside Docker, `localhost` refers to this application container;
use a hostname or service name reachable from that container.

```bash
.venv/bin/manga-kit --config /path/to/config.yaml serve
.venv/bin/manga-kit scan
.venv/bin/manga-kit plan
.venv/bin/manga-kit sample --output var/sample-library
.venv/bin/python -m unittest discover -s tests -v
```

Sample generation writes 22 original simple books across eight fictitious series to an empty folder.
Disable demo mode to scan those generated books. The UI shows the first 200 preview items; `plan` prints every item.

## Compatibility and extension

The client uses `X-API-Key`, `GET /api/v1/libraries`, and `GET /api/v1/series`, following the
[official Komga OpenAPI](https://github.com/gotson/komga/blob/master/komga/docs/openapi.json).
Tests use representative mock responses. No real Komga release has been compatibility-tested yet.
The tool does not open Komga's database directly.

Features register a Flask blueprint and navigation entry. Metadata providers return proposals without writes.
See [architecture](docs/ARCHITECTURE.md), [configuration](docs/CONFIGURATION.md), and [roadmap](docs/ROADMAP.md).

For access protection, set `MANGA_KIT_ADMIN_TOKEN`; use HTTP Basic username `admin` and the token as password.
Use an HTTPS reverse proxy when exposing the service beyond localhost.
No private runtime data, existing Git history, or real manga is bundled.
The source code is available on [GitHub](https://github.com/unagisann/komga-manga-kit).

## License

[MIT](LICENSE).
