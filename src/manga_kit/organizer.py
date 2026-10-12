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


def plan_organization(series: list[Series], layout: Layout, root: Path | None = None) -> dict:
    layout.validate()
    items: list[PlanItem] = []
    targets: dict[str, list[PlanItem]] = {}
    for entry in series:
        fields = {"title": component(entry.title), "author": component(entry.author or "著者未設定"),
                  "kana_row": entry.row, "status": layout.folder(entry.status)}
        folder = component(layout.series.format(**fields))
        fields["series"] = folder
        rendered = getattr(layout, entry.status).format(**fields)
        if PurePosixPath(rendered).is_absolute() or any(p in {"", ".", ".."} for p in rendered.split("/")):
            raise ConfigError("整理先がライブラリの外に出ています。")
        directory = PurePosixPath(*(component(p) for p in rendered.split("/")))
        for book in entry.books:
            issues = []
            target = None
            if book.volume is None and "{volume" in layout.book:
                issues.append("巻数を確認してください。")
            else:
                filename = component(layout.book.format(title=fields["title"], volume=Decimal(book.volume or "0"),
                                                        extension=PurePosixPath(book.path).suffix.lower()))
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
