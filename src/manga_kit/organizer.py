"""Pure planning: never move, rename, unpack, delete or rewrite media."""
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .config import ConfigError, Layout
from .models import Series


def component(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "＿", value).rstrip(". ")
    if value in {"", ".", ".."}:
        return "未設定"
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        value = "_" + value
    if len(value.encode("utf-8")) > 240:
        raise ConfigError("フォルダ名またはファイル名が長すぎます。")
    return value


@dataclass
class PlanItem:
    source: str
    target: str | None
    title: str
    state: str
    issues: list[str]


class VolumeNumber:
    """Format a decimal volume like 第01.5巻: the width pads only the whole part."""

    def __init__(self, value: str | None):
        self.value = Decimal(value or "0")

    def __format__(self, spec: str) -> str:
        text = format(self.value, "f")
        whole, _, fraction = text.partition(".")
        whole = format(int(whole), spec or "d")
        return f"{whole}.{fraction}" if fraction.rstrip("0") else whole


def nested_parents(series: list[Series]) -> dict[str, str]:
    """Map a series folder inside another series folder to its nearest parent."""
    paths = {entry.path for entry in series}
    parents = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        for size in range(len(parts) - 1, 0, -1):
            candidate = PurePosixPath(*parts[:size]).as_posix()
            if candidate in paths:
                parents[path] = candidate
                break
    return parents


def series_directories(series: list[Series], layout: Layout, context: list[Series] | None = None) -> dict[str, PurePosixPath]:
    """Return each series' destination. Nested series keep their place under the parent."""
    parents = nested_parents(context if context is not None else series)
    planned = {entry.path for entry in series}
    directories: dict[str, PurePosixPath] = {}
    # Parents have fewer path parts, so their destinations are known first.
    for entry in sorted(series, key=lambda item: len(PurePosixPath(item.path).parts)):
        parent = parents.get(entry.path)
        if parent is not None:
            relative = PurePosixPath(entry.path).relative_to(parent)
            base = directories[parent] if parent in planned else PurePosixPath(parent)
            directories[entry.path] = base / relative
            continue
        fields = {"title": component(entry.title), "author": component(entry.author or "著者未設定"),
                  "kana_row": entry.row, "status": layout.folder(entry.status)}
        folder = component(layout.series.format(**fields))
        fields["series"] = folder
        rendered = getattr(layout, entry.status).format(**fields)
        if PurePosixPath(rendered).is_absolute() or any(p in {"", ".", ".."} for p in rendered.split("/")):
            raise ConfigError("整理先がライブラリの外に出ています。")
        directories[entry.path] = PurePosixPath(*(component(p) for p in rendered.split("/")))
    return directories


def plan_organization(series: list[Series], layout: Layout, root: Path | None = None,
                      context: list[Series] | None = None, *, normalize=False) -> dict:
    layout.validate()
    items: list[PlanItem] = []
    targets: dict[str, list[PlanItem]] = {}
    directories = series_directories(series, layout, context)
    parents = nested_parents(context if context is not None else series)
    for entry in series:
        directory = directories[entry.path]
        # A missing reading falls back to the title; a kanji or Latin title then
        # lands in その他. Ask for the reading instead of choosing a row silently.
        unknown_row = (entry.path not in parents and "{kana_row" in getattr(layout, entry.status)
                       and not entry.reading.strip() and entry.row == "その他")
        for book in entry.books:
            issues = []
            target = None
            if unknown_row and PurePosixPath(book.path).parent != directory:
                issues.append("読みが未設定のため五十音の行を判定できません。読みを設定してください。")
            if book.volume is None and "{volume" in layout.book:
                issues.append("巻数を確認してください。")
            else:
                extension = PurePosixPath(book.path).suffix.lower()
                if normalize and extension in {".zip", ".rar", ".cbr", ".7z"}:
                    extension = ".cbz"
                filename = component(layout.book.format(title=component(entry.title), volume=VolumeNumber(book.volume), extension=extension))
                target = str(directory / filename)
                if root is not None:
                    destination = root / target
                    if not destination.resolve().is_relative_to(root.resolve()):
                        issues.append("整理先がライブラリ外を指しています。")
                    elif target != book.path and destination.exists():
                        issues.append("整理先に既存ファイルがあります。")
            item = PlanItem(book.path, target, entry.title,
                            "blocked" if issues else "unchanged" if target == book.path else "ready", issues)
            items.append(item)
            if target:
                key = unicodedata.normalize("NFKC", target).casefold()
                targets.setdefault(key, []).append(item)
    for group in targets.values():
        if len(group) > 1:
            for item in group:
                item.state = "blocked"
                item.issues.append("複数のファイルが同じ整理先になります。")
    return {"items": [asdict(i) for i in items], "counts": {state: sum(i.state == state for i in items)
            for state in ("ready", "unchanged", "blocked")}, "preview_only": True}
