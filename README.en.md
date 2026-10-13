# Komga Manga Kit

A self-hosted foundation for Japanese manga organization and Komga integration.
The UI uses warm paper tones, a clay accent, responsive cards, and light/dark themes.

**Status: 0.1.0.dev4, durability improvements for the third v0.1 milestone.** This is not a production v0.1 release.
The interface is currently Japanese. This document provides English setup instructions.
No manga acquisition or download functionality is included.

## Available

- Demo dashboard, library search, status and Japanese kana-row filters
- Read-only scanning of CBZ, ZIP, CBR, RAR, 7z, and EPUB locations
- ComicInfo.xml reading for CBZ/ZIP and per-series `series.json` overrides
- Configurable status folders, series names, book names, and folder hierarchies
- Organization previews, duplicate-target detection, and existing-file checks
- Opt-in CLI moves with confirmed plans, full backups, and restore without overwriting existing files
- Durable backups and destination names before source removal, previous operation checkpoints, and recovery after process termination
- Completed-folder organization with verified Komga 1.29.0 reading-progress and locked-metadata preservation
- CBZ copies from ZIP/CBZ, RAR/CBR, and 7z with normalized page names/order and preserved ComicInfo.xml
- Background scan/preview jobs with persistent progress, results, and operation history
- Manual metadata and reading edits with diff review, conflict detection, and reset
- Komga connection checks and read-only library/series API calls
- Configurable Komga-to-local path mappings and explicit series matching
- CLI, Docker Compose, tests, and original fictitious sample CBZ generation

Default settings and the web interface provide organization previews. CLI execution requires `execution.enabled: true`.
Moves support POSIX, one filesystem, and complete series only. All selected archives and `series.json` are backed up first.
Persistence ordering and recovery limits are documented in [the durability guide](docs/DURABILITY.md) (Japanese).
Move integration explicitly requests Komga scans. CBZ conversion copies to a separate output root and retains originals.
Replacing original archives, migrating progress to converted copies, and external metadata writes remain unimplemented.
Manual edits are stored in application state and used by search, kana filters, and organization previews.
External metadata enrichment, new-volume checks, automatic completion detection, and portal features are planned.
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
Application state is stored in the `manga-kit-state` named volume at `/state` and survives container replacement.
`docker compose down -v` also deletes that volume.

## Your library

Copy `config.example.yaml` to `config.yaml`, set `app.demo: false`, and set `library.root` to your library path.
Relative paths resolve against the configuration file, independently of the application source.
Choose your status folders and naming templates. Restart after editing the config file.
Rules entered in the UI are used for previews only, not saved.
Use `data.root` or `MANGA_KIT_DATA_ROOT` for private application state (default: `var/state` relative to the config).
This location must not overlap the manga library. Stop the application and back up the entire state directory.

Select a series on the Metadata page to edit its title, author, reading, status, summary, or publisher.
Review the diff before saving. Kana readings are normalized to hiragana. Reset restores scanned metadata.
Edits survive restart and do not modify archives, sidecars, or Komga. Concurrent edits or changed source metadata require a new review.

For Docker, copy `compose.local.example.yaml` to `compose.local.yaml`, customize its host paths,
and set the configured root to `/library`. Copy `.env.example` to `.env` for environment secrets.
The library mount is read-only. Do not commit private configuration or override files.

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build -d
```

Set `komga.base_url` or `KOMGA_URL` and the `KOMGA_API_KEY` environment variable to test your server from Settings.
There is no automatic connection on startup. Inside Docker, `localhost` refers to this application container;
use a hostname or service name reachable from that container.
Configure `komga.path_mappings` with `komga` (server-side absolute path) and `local` (path within this tool's library).
Settings can explicitly match series; POSIX paths, Windows drive paths, and local file URLs are supported.
Matching never writes to Komga and does not expose server filesystem paths in its response.

```bash
.venv/bin/manga-kit --config /path/to/config.yaml serve
.venv/bin/manga-kit scan
.venv/bin/manga-kit plan
.venv/bin/manga-kit plan --record
.venv/bin/manga-kit jobs
.venv/bin/manga-kit job JOB_ID
.venv/bin/manga-kit sample --output var/sample-library
.venv/bin/python -m unittest discover -s tests -v
```

Sample generation writes 22 original simple books across eight fictitious series to an empty folder.
Disable demo mode to scan those generated books. The UI shows the first 200 preview items; `plan` prints every item.
The History page and organization preview updates save jobs and results. `scan --record` / `plan --record`
return the saved job including its ID; `job JOB_ID` retrieves its full result. CLI commands also use saved metadata edits.

## Execute and restore

Use a dedicated test library first. Set `app.demo: false`, a writable `library.root`, separate `data.root`,
and `execution.enabled: true`. The regular Compose library mount remains read-only; execute from a host-side
CLI configuration using the same application state, or explicitly configure a dedicated writable environment.
Rules entered on the web page are previews only; save execution rules in YAML.

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml scan
.venv/bin/manga-kit --config /path/to/execute.yaml prepare --series SERIES_ID
# Review plan.entries and plan.groups; copy plan.id and digest from the result.
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml operation OPERATION_ID
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

Omitting `--series` selects all series, including unchanged ones. A blocked item prevents execution.
No series splitting/merging, symlinks, cross-filesystem moves, or overwrite are supported.
Other sidecar files and empty folders remain in place. File bytes, permissions, and modification times are preserved.
Backups and journals are stored under `data.root/operations`; retain this directory and the state database for recovery.
Changed destinations, conflicting original paths, or corrupt backups stop restore before any file changes.
Stop other writers and readers during execution/restore; incomplete operations are recovered explicitly with `restore`.
After a completed move, restore preserves reading progress recorded since that move.

For Komga integration, use 1.29.0 with an administrator API key and an exact library-root path mapping.
Enable file hashing and complete analysis/hashing before preparing. Disable startup/periodic scans, automatic trash
emptying, CBZ conversion, and extension repair. Stale hashes, duplicate content anywhere on the server, or matching
trash entries prevent execution. Book/series IDs and some internal creation timestamps change after moves;
external tools that retain these IDs are not covered by the tests.
See the [Japanese organization guide](docs/ORGANIZATION.md) for detailed recovery and opt-in integration testing.

## Generate normalized CBZ copies

Use the same explicitly enabled CLI configuration. Create an output directory that overlaps neither the source library
nor application state. `--book` selects a source path relative to `library.root`; repeat it to select multiple archives.

```bash
mkdir -p /path/to/converted-library
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-conversion \
  --book 'Series/Book 1.zip' --output /path/to/converted-library
# Review plan.entries, including each original-page/normalized-page mapping.
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

ZIP/CBZ, RAR/CBR, and 7z are supported. RAR/7z additionally require the libarchive shared library
(`sudo apt-get install libarchive-dev` on Debian-based hosts); the Docker image includes it.
EPUB, encrypted or split archives, links, duplicate names, unsafe paths, inconsistent ComicInfo page references,
and hidden image entries are outside the conversion scope. Limits per archive: 10,000 entries, 256 MiB per file,
4 GiB expanded data, 256 KiB of ComicInfo.xml, and a 16 MiB ZIP central directory.
Images sort naturally by their full internal paths and become sequential names such as `0001.png`.
Images are copied without re-encoding; ComicInfo.xml is moved to the archive root with its bytes intact.
Other file bytes and ZIP comments are preserved. Empty directory entries and archive entry timestamps are not retained.
Outputs use ZIP stored entries, so they can be larger than compressed inputs. Staging and backups require additional space.

All outputs are verified before publishing the first CBZ. Existing output files are never overwritten.
Undo validates every generated file and deletes only unchanged outputs from that operation; originals and later source edits stay intact.
Conversion never contacts Komga or transfers local manual metadata edits into archives. Existing source-library IDs and progress stay intact.
Converted copies can be imported separately as new unread books. Replacing managed sources with converted copies and migrating
their reading state remains a later milestone. See the [Japanese conversion guide](docs/CONVERSION.md).

## Compatibility and extension

The client uses `X-API-Key`, `GET /api/v1/libraries`, and `GET /api/v1/series`, following the
[official Komga 1.29.0 OpenAPI](https://github.com/gotson/komga/blob/1.29.0/komga/docs/openapi.json).
An isolated ARM64 Komga 1.29.0 server was tested with fictitious archives: connection/matching,
renaming, completed-folder moves, and restore including deleted-target backup recovery.
Two users' unread/partial/completed states, reading dates, latest progress, and locked series metadata were checked.
Other versions, production-scale libraries, and external tools referencing Komga IDs remain unverified.
The tool does not open Komga's database directly.

Features register a Flask blueprint and navigation entry. Metadata providers return proposals without writes.
See [architecture](docs/ARCHITECTURE.md), [configuration](docs/CONFIGURATION.md), and [roadmap](docs/ROADMAP.md).

For access protection, set `MANGA_KIT_ADMIN_TOKEN`; use HTTP Basic username `admin` and the token as password.
Use an HTTPS reverse proxy when exposing the service beyond localhost.
No private runtime data, existing Git history, or real manga is bundled.
The source code is available on [GitHub](https://github.com/unagisann/komga-manga-kit).

## License

[MIT](LICENSE).
