"""Metadata proposals: providers return data, never mutate Komga or archives."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
import json
from defusedxml.ElementTree import fromstring

from .archives import FORMATS, read_metadata, safe_name
from .metadata import validate_fields
MAX_METADATA_BYTES = 256 * 1024


@dataclass(frozen=True)
class MetadataProposal:
    source: str
    fields: dict[str, str | list[str]]


class MetadataProvider(Protocol):
    id: str
    label: str
    def inspect(self, path: Path) -> MetadataProposal | None: ...


class ComicInfoProvider:
    id = "comicinfo"
    label = "ComicInfo.xml"

    def inspect(self, path: Path) -> MetadataProposal | None:
        if path.suffix.lower() not in FORMATS:
            return None
        # libarchive reads the file descriptor directly; avoid buffered offsets.
        with path.open("rb", buffering=0) as source:
            candidates = read_metadata(source, path.suffix.lower(),
                                       lambda name: PurePosixPath(name).name.casefold() == "comicinfo.xml")
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("ComicInfo.xml が複数あります。巻数と情報源を確認してください。")
        root = metadata_xml(next(iter(candidates.values())))
        if local_tag(root) != "comicinfo":
            raise ValueError("ComicInfo.xml のルート要素が不正です。")
        values = {}
        for child in root:
            key, value = local_tag(child), (child.text or "").strip()
            if key in values and values[key] != value:
                raise ValueError("ComicInfo.xml に同じ項目の異なる値があります。")
            values[key] = value
        mapping = {"Series": "title", "Writer": "author", "Summary": "summary", "Publisher": "publisher", "Number": "volume"}
        fields = {target: value for key, target in mapping.items()
                  if (value := values.get(key.casefold()))}
        for tag, name in (("Genre", "genres"), ("Tags", "tags")):
            value = values.get(tag.casefold())
            if value:
                fields.update(validate_fields({name: [item.strip() for item in value.split(",") if item.strip()]}))
        return MetadataProposal(self.id, fields)


def local_tag(element):
    return str(element.tag).rsplit("}", 1)[-1].casefold()


def metadata_xml(content):
    return fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)


class EpubProvider:
    id = "epub"
    label = "EPUB OPF"

    def inspect(self, path: Path) -> MetadataProposal | None:
        if path.suffix.lower() != ".epub":
            return None
        with path.open("rb", buffering=0) as source:
            container = read_metadata(source, ".epub", lambda name: name == "META-INF/container.xml")
            if not container:
                raise ValueError("EPUBのcontainer.xmlがありません。")
            root = metadata_xml(container["META-INF/container.xml"])
            paths = {element.attrib.get("full-path", "") for element in root.iter()
                     if local_tag(element) == "rootfile"}
            if len(paths) != 1:
                raise ValueError("EPUBのOPFファイルの対応が不明確です。")
            opf_path = safe_name(next(iter(paths)))
            metadata = read_metadata(source, ".epub", lambda name: name == opf_path)
            if not metadata:
                raise ValueError("EPUBのOPFファイルがありません。")
        package = metadata_xml(metadata[opf_path])
        positions = {(element.text or "").strip() for element in package.iter()
                     if local_tag(element) == "meta"
                     and element.attrib.get("property", "").casefold() == "group-position"
                     and (element.text or "").strip()}
        if not positions:
            return None
        if len(positions) != 1:
            raise ValueError("EPUBに異なるgroup-positionがあります。巻数を確認してください。")
        return MetadataProposal(self.id, {"volume": next(iter(positions))})


def mylar_fields(metadata) -> dict[str, str]:
    """Read Komga's Mylar series.json like Komga 1.29 does, without its status.

    Ended/Continuing is not imported: here the status chooses the destination
    folder, and an existing sidecar must not silently re-plan the library.
    """
    if not isinstance(metadata, dict):
        raise ValueError("series.json の metadata が不正です。")
    name, volume, year = metadata.get("name"), metadata.get("volume"), metadata.get("year")
    summary = metadata.get("description_formatted") or metadata.get("description_text") or ""
    publisher = metadata.get("publisher") or ""
    if (not isinstance(name, str) or not name.strip() or not isinstance(summary, str) or not isinstance(publisher, str)
            or volume is not None and type(volume) is not int or year is not None and type(year) is not int):
        raise ValueError("series.json のMylar形式の項目が不正です。")
    # Komga appends the year when a series has a later volume number.
    title = name.strip() if volume in (None, 1) or year is None else f"{name.strip()} ({year})"
    fields = {"title": title, "summary": summary, "publisher": publisher}
    return validate_fields({key: value for key, value in fields.items() if value.strip()})


def read_sidecar(folder: Path) -> MetadataProposal | None:
    path = folder / "series.json"
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("series.json のサイズが上限を超えています。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "metadata" in data and set(data) <= {"version", "metadata"}:
        return MetadataProposal("sidecar", mylar_fields(data["metadata"]))
    allowed = {"title", "author", "reading", "status", "summary", "publisher", "genres", "tags"}
    if (not isinstance(data, dict) or set(data) - allowed
            or any(not isinstance(v, str) for k, v in data.items() if k not in {"genres", "tags"})):
        raise ValueError("series.json はタイトル・著者・読みなどの文字列で指定してください。")
    if data.get("status", "unknown") not in {"ongoing", "completed", "unknown"}:
        raise ValueError("series.json の status が不正です。")
    classifications = {key: value for key, value in data.items() if key in {"genres", "tags"}}
    if classifications:
        data.update(validate_fields(classifications))
    return MetadataProposal("sidecar", data)
