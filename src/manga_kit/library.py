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
from .providers import ComicInfoProvider, read_sidecar

EXTENSIONS = {".cbz", ".zip", ".cbr", ".rar", ".epub", ".7z"}
_VOLUME = re.compile(r"第(\d+(?:\.\d+)?)[巻話]|\b[vV](\d+(?:\.\d+)?)|[（(](\d+(?:\.\d+)?)[）)]")


def volume_number(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(unicodedata.normalize("NFKC", str(value)))
        if not number.is_finite() or number < 0 or number > 99999:
            return None
        return format(number.normalize(), "f")
    except InvalidOperation:
        return None


def filename_volume(name: str) -> str | None:
    match = _VOLUME.search(unicodedata.normalize("NFKC", Path(name).stem))
    return volume_number(next((g for g in match.groups() if g is not None), None)) if match else None


def scan_library(settings: Settings, providers=None) -> ScanResult:
    root = settings.library_root
    result = ScanResult([])
    if not root.is_dir():
        result.warnings.append("ライブラリが見つかりません。設定の保存先を確認してください。")
        return result
    providers = providers if providers is not None else [ComicInfoProvider()]
    count = 0
    def walk_error(error):
        result.warnings.append("読み取れないフォルダがあります。アクセス権を確認してください。")
    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        directory = Path(current)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (directory / d).is_symlink())
        books = sorted(f for f in files if Path(f).suffix.lower() in EXTENSIONS)
        if not books:
            continue
        relative = directory.relative_to(root)
        title, author = split_folder_name(directory.name)
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
            if path.is_symlink():
                continue
            if count >= settings.max_files:
                result.truncated = True
                break
            count += 1
            book = Book(path.relative_to(root).as_posix(), filename_volume(filename))
            try:
                book.size = path.stat().st_size
                for provider in providers:
                    proposal = provider.inspect(path)
                    if proposal is None:
                        continue
                    if proposal.source not in series.sources:
                        series.sources.append(proposal.source)
                    for key in ("title", "author", "summary", "publisher", "reading"):
                        if proposal.fields.get(key) and (key == "title" or key not in metadata_fields):
                            setattr(series, key, proposal.fields[key])
                            metadata_fields.add(key)
                    proposed_volume = volume_number(proposal.fields.get("volume"))
                    if proposed_volume is not None:
                        book.volume, book.source = proposed_volume, proposal.source
            except Exception as exc:
                # One corrupt archive or provider must not hide the rest of the library.
                series.warnings.append(f"{filename}: メタデータを読み取れません（{type(exc).__name__}）。")
            series.books.append(book)
        if sidecar:
            for key, value in sidecar.fields.items():
                setattr(series, key, value)
            series.sources.append(sidecar.source)
        if series.books:
            result.series.append(series)
        if result.truncated:
            break
    result.series.sort(key=lambda s: (search_key(s.reading or s.title), s.path))
    return result


class LibraryService:
    def __init__(self, settings: Settings, providers=None):
        self.settings, self.providers = settings, providers
        self._lock, self._updated, self._cache = Lock(), 0.0, None

    def scan(self, refresh=False) -> ScanResult:
        with self._lock:
            if refresh or self._cache is None or time.monotonic() - self._updated > 30:
                if self.settings.demo:
                    from .sample import demo_library
                    self._cache = demo_library(self.settings.layout)
                else:
                    self._cache = scan_library(self.settings, self.providers)
                self._updated = time.monotonic()
            return self._cache
