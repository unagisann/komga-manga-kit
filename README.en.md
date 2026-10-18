# Komga Manga Kit

A self-hosted foundation for Japanese manga organization and Komga integration.
The UI uses warm paper tones, a clay accent, responsive cards, and light/dark themes.

The main workflow is **add books to a series folder → check changes → normalize**.
The Normalize page combines file/folder naming, CBZ conversion, page ordering, and backup-based restore.
See the [normalization guide](docs/NORMALIZATION.md) (Japanese) for setup and limitations.

**Status: 0.1.0rc1, the first release candidate.** Required features and release-preparation checks are complete; publication is pending.
The interface is currently Japanese. This document provides English setup instructions.
No manga acquisition or download functionality is included.

## Scope of the first 0.1.0 release

The first release covers library scanning, organization, reviewed metadata enrichment, and confirmed CLI updates to Komga with undo.
Per-book author and role updates are implemented. Clean installation, dev8 upgrades, backup/recovery, and distribution checks
have been exercised for this candidate. Unreleased changes add opt-in web normalization and restore.
Unreleased work also adds Kobo/MangaUpdates, new releases, anime/discovery/awards, shelves, AI review, per-user reading views, page editing and scheduled post-import workflows. See the [feature coverage](docs/FEATURE_PARITY.md) and [Tools guide](docs/TOOLS.md) for implemented behavior and verification limits. The [roadmap](docs/ROADMAP.md) (Japanese) records the remaining work
and acceptance criteria. Planned features are separate from the implemented features below.

Managed archive rebuilding is verified on Komga 1.29.1 using byte-preserving moves followed by rebuilding at the same path, with book IDs and reading dates checked. Page-count-changing edits in a managed library remain unsupported.

## Available

The shared header links to the portal, library, discovery, new volumes, seasonal anime, rankings, completion moves, cover editing, and management, with library search alongside. The portal opens first with library search and completed-series browsing.
Its organization shortcut leads to normalization. Management groups normalization, enrichment, manual corrections,
organization rules, history, settings, and the roadmap. Enrichment fills missing fields after review.

Organization previews show saved results and their check time without recalculating on each visit. Check the current layout on demand,
review issues first, and search or browse all results in pages of 20. Unchanged archive metadata is reused during preview updates;
collision checks still cover all books, and CLI execution reviews keep their full validation.

- Demo dashboard, library search, status and Japanese kana-row filters
- Read-only scanning of CBZ, ZIP, CBR, RAR, 7z, and EPUB locations
- ComicInfo.xml reading for ZIP/CBZ, RAR/CBR, and 7z, EPUB volume metadata, and per-series `series.json` overrides
- Configurable status folders, series names, book names, and folder hierarchies
- Organization previews, duplicate-target detection, and existing-file checks
- Opt-in CLI moves with confirmed plans, full backups, and restore without overwriting existing files
- Durable backups and destination names before source removal, previous operation checkpoints, and recovery after process termination
- Completed-folder organization with verified Komga 1.29.0 / 1.29.1 reading-progress and locked-metadata preservation
- CBZ copies from ZIP/CBZ, RAR/CBR, and 7z with normalized page names/order and preserved ComicInfo.xml
- Background scan/preview jobs with persistent progress, results, and operation history
- Persistent metadata caching for unchanged archives, 60-series pages, and browsing during explicit rescans
- Manual metadata and reading edits with diff review, conflict detection, and reset
- Rakuten Books bibliographic search, representative-volume links, field-by-field review, and saved source/timestamp information
- Candidate comparison by title, reviewed aliases, authors, publisher, and explicit edition notes; separate imprint display and conservative readership proposals
- AniList manga searches for tags/genres, independent links for both sources, manual classification edits, and library search
- Confirmed CLI series-metadata updates to Komga 1.29.0 / 1.29.1 with per-field locks, saved original values, and conflict-aware undo
- Explicit CLI completion-tag/status updates with undo, retaining unrelated tags and hiatus/abandoned statuses
- Per-book author/role updates and undo, selected books, merging by default, and explicit list/lock replacement
- Library-wide batch updates of series metadata, completion, and per-book authors with reverse-order undo; fills empty Komga values by default and checks archives by file attributes instead of re-reading them
- Batch import of readings from Komga's sort titles, never replacing readings already set, with a one-step undo
- Komga connection checks and read-only library/series API calls
- Configurable Komga-to-local path mappings and explicit series matching
- CLI, Docker Compose, tests, and original fictitious sample CBZ generation

Default settings provide previews. CLI execution requires `execution.enabled: true`.
Web normalization additionally requires `execution.web_enabled: true`, reviewed changes, and a local or authenticated session.
Moves support POSIX, one filesystem, and complete series only. All selected archives and `series.json` are backed up first.
Persistence ordering and recovery limits are documented in [the durability guide](docs/DURABILITY.md) (Japanese).
Move integration explicitly requests Komga scans. CBZ conversion copies to a separate output root and retains originals.
Web normalization can replace archives with verified CBZs and backups before they are registered in Komga.
Komga 1.29.1 supports rebuilding at the same path after hash-preserving moves. Book IDs, reading progress and reading dates are verified before completion and during restore. Arbitrary migration to another library and page-count-changing edits are unsupported.
Komga series metadata can be updated separately through `prepare-metadata`, `execute`, and `restore`.
The Komga series is resolved from the path mapping, so `--komga-series` is optional.
`prepare-komga-batch --all` plans many series at once, and `prepare-reading-import --all` imports readings from Komga sort titles.
Choose both local/Komga series IDs and explicit fields; tags/genres merge by default, while replacing lists or locked values needs explicit flags.
See [the metadata update guide](docs/KOMGA_METADATA.md) and the separate [author/role guide](docs/AUTHORS.md) (Japanese).
Author plans support up to 50 books per series. Archives and current reading progress are retained.
Manual edits are stored in application state and used by search, kana filters, and organization previews.
Reviewed bibliographic fields are saved in application state. Demo mode uses fictitious search results without network access.
Live Rakuten Books searches require your own application ID and access key; live authentication has not yet been verified.
See [bibliographic search and enrichment](docs/METADATA_SOURCES.md) (Japanese) for setup and limitations.
AniList classification searches require `anilist.enabled: true`; no API key is needed. Demo mode uses fictitious results.
Reviewed genres/tags are saved locally alongside Rakuten links. Spoiler-marked and unknown-spoiler tags are excluded.
An empty-result query using a fictitious search term verified public API connectivity; real-title matching remains unverified.
Additional portal tools, new-volume checks, AI review, completion management, page editing and scheduling are implemented as unreleased work. Covers can be read from local archives or Komga. See [feature coverage](docs/FEATURE_PARITY.md) for remaining gaps and external-service verification limits.

## Run

Linux, Python 3.11 or newer (tested on 3.11 / 3.13), and system libarchive:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/manga-kit serve
```

Open <http://127.0.0.1:5080>. Demo mode requires no keys or services.
The CLI server is intended for development; the container runs Gunicorn.
Use `-e` for editable development installs. Runtime versions are pinned for Linux, without artifact hashes.
Full upstream license/NOTICE texts are bundled; see [third-party notices](THIRD_PARTY_NOTICES.md).

See [installation, upgrade, backup and recovery](docs/OPERATIONS.md), [tested environments and limitations](docs/SUPPORT.md),
and [release notes](docs/RELEASE_NOTES.md) (Japanese). Before upgrading, stop the app and back up the complete state directory,
configuration, and library together. Keep the same application-visible absolute library path and state volume.
Recover into empty storage, preserve ownership/modes, and verify edits, sources, history, and book counts before resuming.
After physically recreating a library root, old operation plans remain inspectable but must be replaced by newly prepared plans.
The candidate includes a wheel and source distribution with SHA-256 manifests; `scripts/build_release.py` builds and checks them without publishing.

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

Use Automatic enrichment to collect and review missing-field proposals. For information that cannot be retrieved or needs correction,
select a series under Management → Manual corrections to edit its title, author, reading, status, summary, or publisher.
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

For Komga integration, use 1.29.0 or 1.29.1 with an administrator API key and an exact library-root path mapping.
Enable file hashing and complete analysis/hashing before preparing. Disable startup/periodic scans, automatic trash
emptying, CBZ conversion, and extension repair. Stale hashes, duplicate content anywhere on the server, or matching
trash entries prevent execution. Book/series IDs and some internal creation timestamps change after moves;
external tools that retain these IDs are not covered by the tests.
See the [Japanese organization guide](docs/ORGANIZATION.md) for detailed recovery and opt-in integration testing.

## Sync completion information

Mark a series as completed in this tool before planning its Komga update:

```bash
.venv/bin/manga-kit --config /path/to/execute.yaml prepare-completion --series LOCAL_SERIES_ID
# Review plan.changes, including values and locks.
.venv/bin/manga-kit --config /path/to/execute.yaml execute OPERATION_ID --confirm DIGEST
.venv/bin/manga-kit --config /path/to/execute.yaml restore OPERATION_ID --confirm DIGEST
```

The default tags are `連載中` (removed) and `完結` (added); customize them with `--ongoing-tag` and `--completed-tag`.
Other tags remain. `ONGOING` becomes `ENDED` and is locked; existing `ENDED`, `HIATUS`, and `ABANDONED` values and locks remain.
Changing a locked value requires `--replace-locked`. This is a separate CLI operation and does not run when saving web edits.
When also moving a series, move first and plan the update using its new IDs. Undo the metadata update before undoing the move.
See the [Japanese completion guide](docs/COMPLETION.md) for conditions and recovery behavior.

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
Use `--page-order lexicographic-path` to retain KomgaTool's lexical page-order policy.
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
Isolated ARM64 Komga 1.29.0 and 1.29.1 servers were tested with fictitious archives (1.29.1 only changes the web UI): connection/matching,
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
