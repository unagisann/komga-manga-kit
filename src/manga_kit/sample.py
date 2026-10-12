"""Original fictitious sample books, shared by demo and fixture generation."""
import json
from pathlib import Path
import struct
import zlib
import xml.etree.ElementTree as ET
import zipfile

from .config import Layout
from .models import Book, ScanResult, Series

_SAMPLES = [
    ("雨あがりの喫茶室", "アメアガリノキッサシツ", "森野しおり", "ongoing", 3, "雨の日だけ開く喫茶室で、店主と訪れる人々が紡ぐ小さな物語。", "暮らし"),
    ("かもめ便り", "カモメダヨリ", "海野あお", "completed", 4, "港町の配達員が届けるのは、手紙と、少しだけ前を向くきっかけ。", "日常"),
    ("星図のアトリエ", "セイズノアトリエ", "月見なぎ", "ongoing", 2, "星を描く工房で働く新人と、不思議な注文を持ち込むお客さまたち。", "冒険"),
    ("ちいさな庭の一年", "チイサナニワノイチネン", "若葉みち", "completed", 3, "街のすみの庭を舞台に、季節の移ろいと人のつながりを描く。", "暮らし"),
    ("ノートの向こう側", "ノートノムコウガワ", "紙谷ふみ", "ongoing", 2, "交換ノートから始まる、会ったことのない二人の放課後。", "青春"),
    ("ひだまり工房", "ヒダマリコウボウ", "木下つむぎ", "completed", 5, "古い道具を直す工房に集まる、思い出とものづくりの日々。", "日常"),
    ("月を待つ駅", "", "", "unknown", 1, "", "未設定"),
    ("ゆっくり歩こう", "ユックリアルコウ", "道野すず", "ongoing", 2, "いつもの道を遠回りして見つける、小さな発見の記録。", "散歩"),
]


def demo_library(layout: Layout) -> ScanResult:
    entries = []
    for title, reading, author, status, count, summary, _ in _SAMPLES:
        folder = f"{layout.folder(status)}/[{author or '不明'}] {title}"
        books = [Book(f"{folder}/原稿 第{n:02}巻.cbz", str(n), 4200000 + n * 170000, "comicinfo")
                 for n in range(1, count + 1)]
        entries.append(Series(folder, title, author, reading, status, summary, "サンプル出版", books,
                              sources=["comicinfo", "sidecar"]))
    return ScanResult(entries)


def _png() -> bytes:
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff)
    pixels = (b"\x00" + bytes((240, 238, 230)) * 32) * 48
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 32, 48, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def create_sample_library(root: Path, layout: Layout) -> int:
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("サンプルは空のフォルダに作成してください。")
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    for series in demo_library(layout).series:
        directory = root / series.path
        directory.mkdir(parents=True)
        fields = {key: getattr(series, key) for key in ("title", "author", "reading", "status", "summary", "publisher")}
        (directory / "series.json").write_text(json.dumps(fields, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for book in series.books:
            xml = ET.Element("ComicInfo")
            for key, value in {"Series": series.title, "Writer": series.author, "Summary": series.summary,
                               "Publisher": series.publisher, "Number": book.volume, "LanguageISO": "ja", "Manga": "YesAndRightToLeft"}.items():
                ET.SubElement(xml, key).text = value
            with zipfile.ZipFile(root / book.path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ComicInfo.xml", ET.tostring(xml, encoding="utf-8", xml_declaration=True))
                archive.writestr("0001.png", _png())
            count += 1
    return count
