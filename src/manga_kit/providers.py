"""Metadata proposals: providers return data, never mutate Komga or archives."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import json
import zipfile
from defusedxml.ElementTree import fromstring

MAX_METADATA_BYTES = 256 * 1024


@dataclass(frozen=True)
class MetadataProposal:
    source: str
    fields: dict[str, str]


class MetadataProvider(Protocol):
    id: str
    label: str
    def inspect(self, path: Path) -> MetadataProposal | None: ...


class ComicInfoProvider:
    id = "comicinfo"
    label = "ComicInfo.xml"

    def inspect(self, path: Path) -> MetadataProposal | None:
        if path.suffix.lower() not in {".cbz", ".zip"}:
            return None
        with zipfile.ZipFile(path) as archive:
            info = next((i for i in archive.infolist() if i.filename.casefold() == "comicinfo.xml"), None)
            if info is None:
                return None
            if info.file_size > MAX_METADATA_BYTES:
                raise ValueError("ComicInfo.xml のサイズが上限を超えています。")
            content = archive.read(info)
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise ValueError("ComicInfo.xml に対応外のXML宣言があります。")
        root = fromstring(content)
        mapping = {"Series": "title", "Writer": "author", "Summary": "summary", "Publisher": "publisher", "Number": "volume"}
        return MetadataProposal(self.id, {target: value for key, target in mapping.items()
                                         if (value := (root.findtext(key) or "").strip())})


def read_sidecar(folder: Path) -> MetadataProposal | None:
    path = folder / "series.json"
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("series.json のサイズが上限を超えています。")
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"title", "author", "reading", "status", "summary", "publisher"}
    if not isinstance(data, dict) or set(data) - allowed or any(not isinstance(v, str) for v in data.values()):
        raise ValueError("series.json はタイトル・著者・読みなどの文字列で指定してください。")
    if data.get("status", "unknown") not in {"ongoing", "completed", "unknown"}:
        raise ValueError("series.json の status が不正です。")
    return MetadataProposal("sidecar", data)
