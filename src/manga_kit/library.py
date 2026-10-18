"""Read-only scanning. Each directory containing books is one series."""
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
from threading import Lock
import time
import unicodedata

from .config import Settings
from .japanese import search_key, split_folder_name
from .models import Book, ScanResult, Series
from .providers import ComicInfoProvider, EpubProvider, read_sidecar
from .scan_cache import ArchiveMetadataCache, file_signature

EXTENSIONS = {".cbz", ".zip", ".cbr", ".rar", ".epub", ".7z"}
_VOLUME = re.compile(r"第(\d+(?:\.\d+)?)[巻話]|(?<![a-zA-Z0-9])[vV](\d+(?:\.\d+)?)")
_PAREN_VOLUME = re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*\)(?:\s*-\s*.+)?$")
_PADDED_VOLUME = re.compile(r"(?<![0-9])_([0-9]{2,3})$")


def volume_number(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        normalized = unicodedata.normalize("NFKC", str(value))
        if len(normalized) > 64:
            return None
        number = Decimal(normalized)
        if (not number.is_finite() or number < 0 or number > 99999
                or number.as_tuple().exponent < -32):
            return None
        # Decimal.normalize() rounds using the active context (usually 28
        # digits). Keep every accepted fractional digit when naming books.
        text = format(number, "f") if number else "0"
        return text.rstrip("0").rstrip(".") if "." in text else text
    except InvalidOperation:
        return None


def filename_volume(name: str, series_title: str | None = None) -> str | None:
    stem = unicodedata.normalize("NFKC", Path(name).stem)
    # Remove the known title first: digits and volume-like text in a title
    # must never become a book's volume. Only the remaining suffix is parsed.
    if series_title:
        title = unicodedata.normalize("NFKC", series_title).strip()
        prefix = re.match(re.escape(title), stem, re.IGNORECASE) if title else None
        if prefix:
            stem = stem[prefix.end():]
            suffix = re.fullmatch(r"[\s_]*(\d+(?:\.\d+)?)\s*(?:-\s*.+)?", stem)
            if suffix:
                return volume_number(suffix[1])
    # Prefer explicit volume markers. Bare suffixes are limited to _NN/_NNN;
    # a preceding digit could indicate a date or a multipart numeric identifier.
    match = _VOLUME.search(stem) or _PAREN_VOLUME.search(stem) or _PADDED_VOLUME.search(stem)
    return volume_number(next((g for g in match.groups() if g is not None), None)) if match else None


def scan_library(settings: Settings, providers=None, progress=None, *, metadata_cache=None) -> ScanResult:
    root = settings.library_root
    result = ScanResult([])
    if not root.is_dir():
        result.warnings.append("ライブラリが見つかりません。設定の保存先を確認してください。")
        return result
    providers = providers if providers is not None else [ComicInfoProvider(), EpubProvider()]
    count = 0
    def walk_error(error):
        result.warnings.append("読み取れないフォルダがあります。アクセス権を確認してください。")
    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        directory = Path(current)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__MACOSX"
                         and not (directory / d).is_symlink())
        books = sorted(f for f in files if not f.startswith(".") and Path(f).suffix.lower() in EXTENSIONS)
        if not books:
            continue
        relative = directory.relative_to(root)
        title, author = split_folder_name(directory.name)
        folder_title = title
        status = next((s for s in ("ongoing", "completed", "unknown")
                       if relative.parts and relative.parts[0] == settings.layout.folder(s)), "unknown")
        series = Series(relative.as_posix(), title, author, status=status)
        metadata_fields = set()
        sidecar = None
        try:
            sidecar = read_sidecar(directory)
        except (ValueError, OSError) as exc:
            series.warnings.append(f"series.json を読み取れません（{type(exc).__name__}）。")
        for filename in books:
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                continue
            if count >= settings.max_files:
                result.truncated = True
                break
            count += 1
            book = Book(path.relative_to(root).as_posix(), filename_volume(filename, folder_title))
            try:
                info = path.stat()
                book.size = info.st_size
                proposals = metadata_cache.get(book.path, info) if metadata_cache is not None else None
                if proposals is None:
                    proposals = []
                    # Apply each proposal immediately to retain the old partial-error behavior.
                    def read_proposals():
                        for provider in providers:
                            proposal = provider.inspect(path)
                            if proposal is not None:
                                proposals.append(proposal)
                                yield proposal
                    values = read_proposals()
                else:
                    values = proposals
                for proposal in values:
                    if proposal.source not in series.sources:
                        series.sources.append(proposal.source)
                    for key in ("title", "author", "summary", "publisher", "reading", "genres", "tags"):
                        if proposal.fields.get(key) and (key == "title" or key not in metadata_fields):
                            setattr(series, key, proposal.fields[key])
                            metadata_fields.add(key)
                    proposed_volume = volume_number(proposal.fields.get("volume"))
                    if proposal.fields.get("volume") is not None and proposed_volume is None:
                        series.warnings.append(f"{filename}: 書庫内の巻数が不正または対応範囲外です。ファイル名を確認してください。")
                    if proposed_volume is not None:
                        if book.volume is not None and book.volume != proposed_volume:
                            series.warnings.append(
                                f"{filename}: 巻数が一致しません（{book.source}: {book.volume} / "
                                f"{proposal.source}: {proposed_volume}）。書庫内の巻数を優先します。")
                        book.volume, book.source = proposed_volume, proposal.source
                if metadata_cache is not None:
                    if file_signature(path.stat()) == file_signature(info):
                        metadata_cache.put(book.path, info, proposals)
                    else:
                        metadata_cache.discard(book.path)
                        series.warnings.append(f"{filename}: 読み取り中に書庫が更新されました。再読み取りしてください。")
            except Exception as exc:
                if metadata_cache is not None:
                    metadata_cache.discard(book.path)
                # One corrupt archive or provider must not hide the rest of the library.
                series.warnings.append(f"{filename}: メタデータを読み取れません（{type(exc).__name__}）。")
            series.books.append(book)
            if progress is not None:
                progress(count)
        if sidecar:
            for key, value in sidecar.fields.items():
                setattr(series, key, value)
            series.sources.append(sidecar.source)
        if series.books:
            result.series.append(series)
        if result.truncated:
            break
    if metadata_cache is not None:
        metadata_cache.save(complete=not result.truncated and not result.warnings)
    if not result.truncated and count >= settings.max_files * 0.9:
        # Added after the cache is saved: approaching the limit is not a read failure.
        result.warnings.append(f"書庫の数（{count}冊）がスキャン上限 {settings.max_files} の90%を超えました。"
                               "上限に達すると整理・反映を実行できません。library.max_files を引き上げてください。")
    result.series.sort(key=lambda s: (search_key(s.reading or s.title), s.path))
    return result


class LibraryService:
    def __init__(self, settings: Settings, providers=None, store=None):
        self.settings, self.providers, self.store = settings, providers, store
        self._lock, self._updated, self._cache = Lock(), 0.0, None
        self.background_refresh = None

    def scan(self, refresh=False, *, edited=True, progress=None, background=True, reuse_metadata=False) -> ScanResult:
        if not refresh and background and self._cache is not None and self.background_refresh is not None:
            if time.monotonic() - self._updated > 30:
                self.background_refresh()
            return self._with_edits(self._cache, edited)
        # Keep the previous snapshot available while an explicit scan is running.
        if refresh or self._cache is None:
            self._lock.acquire()
        elif not self._lock.acquire(blocking=False):
            return self._with_edits(self._cache, edited)
        try:
            if refresh or self._cache is None or time.monotonic() - self._updated > 30:
                if self.settings.demo:
                    from .sample import demo_library
                    self._cache = demo_library(self.settings.layout)
                else:
                    providers = self.providers if self.providers is not None else [ComicInfoProvider(), EpubProvider()]
                    cache = ArchiveMetadataCache(self.store, providers, reuse=not refresh or reuse_metadata)
                    self._cache = scan_library(self.settings, providers, progress, metadata_cache=cache)
                self._updated = time.monotonic()
            result = self._cache
        finally:
            self._lock.release()
        return self._with_edits(result, edited)

    def _with_edits(self, result, edited):
        if edited and self.store is not None:
            from .metadata import apply_edits
            result = apply_edits(result, self.store.edits())
            result.series.sort(key=lambda s: (search_key(s.reading or s.title), s.path))
        return result
