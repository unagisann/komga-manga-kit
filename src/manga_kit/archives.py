"""Bounded archive reads and byte-preserving CBZ normalization."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile

from defusedxml.ElementTree import fromstring

from .config import ConfigError
from .state import StateConflict

FORMATS = {".zip", ".cbz", ".rar", ".cbr", ".7z"}
IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif", ".jxl", ".heic", ".heif", ".tif", ".tiff"}
MAX_ENTRIES = 10000
MAX_ENTRY_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_XML_BYTES = 256 * 1024
BLOCK = 64 * 1024
MAX_CENTRAL_BYTES = 16 * 1024 * 1024


def name_key(name):
    return unicodedata.normalize("NFKC", name).casefold()


def safe_name(name, directory=False):
    if not isinstance(name, str) or len(name) > 1024 or any(ord(c) < 32 for c in name) or "\\" in name:
        raise ConfigError("書庫内に対応外の名前があります。")
    name = name.rstrip("/") if directory else name
    normalized = name_key(name)
    if (not name or normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized)
            or any(part in {"", ".", ".."} for part in normalized.split("/"))):
        raise ConfigError("書庫内に絶対パス・階層逸脱・曖昧な名前があります。")
    return name


def natural_key(name):
    value = name_key(name)
    return tuple((1, int(part)) if part.isdecimal() else (0, part)
                 for part in re.split(r"([0-9]+)", value)), value


def check_zip_directory(source):
    """Bound the central directory before ZipFile allocates its member list."""
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise ConfigError("ZIPの終端情報を読み取れません。")
    _, disk, directory_disk, on_disk, count, central_size, _, comment = struct.unpack("<4s4H2IH", tail[index:index + 22])
    if disk or directory_disk or on_disk != count or len(tail) - index != 22 + comment:
        raise ConfigError("分割ZIP・不正な終端情報は変換できません。")
    if count == 65535 or central_size == 0xffffffff:
        absolute = size - len(tail) + index
        source.seek(max(0, absolute - 20))
        signature, disk, offset, disks = struct.unpack("<4sIQI", source.read(20))
        if signature != b"PK\x06\x07" or disk or disks != 1:
            raise ConfigError("ZIP64の終端情報が不正です。")
        source.seek(offset)
        fields = struct.unpack("<4sQ2H2I4Q", source.read(56))
        if fields[0] != b"PK\x06\x06" or fields[4] or fields[5] or fields[6] != fields[7]:
            raise ConfigError("分割ZIP64は変換できません。")
        count, central_size = fields[7], fields[8]
    if count > MAX_ENTRIES or central_size > MAX_CENTRAL_BYTES:
        raise ConfigError("ZIPの項目数・中央ディレクトリのサイズが上限を超えています。")
    source.seek(0)


def check_rar_header(source):
    header = source.read(128)
    source.seek(0)
    if header.startswith(b"Rar!\x1a\x07\x00"):
        if len(header) < 14 or header[9] != 0x73 or struct.unpack_from("<H", header, 10)[0] & 0x81:
            raise ConfigError("分割・暗号化RARは変換できません。")
    elif header.startswith(b"Rar!\x1a\x07\x01\x00"):
        position = 12
        def vint():
            nonlocal position
            value = 0
            for shift in range(0, 70, 7):
                byte = header[position]
                position += 1
                value |= (byte & 127) << shift
                if byte < 128:
                    return value
            raise ConfigError("RAR5のヘッダーが不正です。")
        vint()  # Header size.
        if vint() != 1:
            raise ConfigError("暗号化・対応外のRAR5ヘッダーです。")
        flags = vint()
        if flags & 1:
            vint()  # Extra area size.
        if flags & 2:
            vint()  # Data size.
        if vint() & 1:
            raise ConfigError("分割RAR5は変換できません。")
    else:
        raise ConfigError("通常のRAR書庫を指定してください。自己展開形式は未対応です。")


@contextmanager
def reader(source, suffix):
    if suffix in {".zip", ".cbz"}:
        check_zip_directory(source)
        with zipfile.ZipFile(source) as archive:
            def entries():
                for info in archive.infolist():
                    kind = stat.S_IFMT(info.external_attr >> 16)
                    if info.flag_bits & 1 or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ConfigError("暗号化・リンク・特殊ファイルを含む書庫は変換できません。")
                    def blocks(entry=info):
                        with archive.open(entry) as content:
                            while chunk := content.read(BLOCK):
                                yield chunk
                    yield info.orig_filename, info.file_size, info.is_dir(), blocks()
            yield "ZIP", archive.comment.hex(), entries()
    else:
        if suffix in {".rar", ".cbr"}:
            check_rar_header(source)
        try:
            import libarchive
        except (ImportError, OSError) as exc:
            raise ConfigError("RAR/7zの読み取りにはlibarchiveが必要です。導入手順を確認してください。") from exc
        with libarchive.fd_reader(source.fileno(), format_name="7zip" if suffix == ".7z" else "all") as archive:
            def entries():
                for info in archive:
                    if suffix in {".rar", ".cbr"} and not archive.format_name.startswith(b"RAR"):
                        raise ConfigError("RARとして読み取れる書庫を指定してください。")
                    if info.issym or info.islnk or (not info.isdir and not info.isreg):
                        raise ConfigError("リンク・特殊ファイルを含む書庫は変換できません。")
                    yield info.pathname, info.size, info.isdir, info.get_blocks(BLOCK)
            yield "7z" if suffix == ".7z" else "RAR", "", entries()


def _comicinfo(content, page_count):
    try:
        root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if root.tag != "ComicInfo":
            raise ValueError()
        count = root.findtext("PageCount")
        if count is not None and int(count) != page_count:
            raise ValueError()
        seen = set()
        for page in root.findall("./Pages/Page"):
            index = int(page.attrib["Image"])
            if not 0 <= index < page_count or index in seen:
                raise ValueError()
            seen.add(index)
    except Exception as exc:
        raise ConfigError("ComicInfo.xmlのページ数・参照・XMLを確認してください。元の情報は変更しません。") from exc


def inspect_archive(fs, relative, spool=None):
    suffix = PurePosixPath(relative).suffix.lower()
    if suffix not in FORMATS:
        raise ConfigError("変換対象はZIP/CBZ・RAR/CBR・7zの書庫に限ります。EPUBは対象外です。")
    members, names, total, metadata = [], set(), 0, None
    try:
        with fs.open_read(relative) as source, reader(source, suffix) as (format_name, comment, entries):
            for count, (raw, size, directory, blocks) in enumerate(entries, 1):
                if count > MAX_ENTRIES:
                    raise ConfigError("書庫内の項目数が上限を超えています。")
                name = safe_name(raw, directory)
                key = name_key(name)
                if key in names:
                    raise ConfigError("書庫内に同じ名前・表記違いの項目があります。")
                names.add(key)
                if directory:
                    if size not in {0, None}:
                        raise ConfigError("書庫内のフォルダ情報が不正です。")
                    continue
                is_metadata = PurePosixPath(name).name.casefold() == "comicinfo.xml"
                limit = MAX_XML_BYTES if is_metadata else MAX_ENTRY_BYTES
                if size is None or not 0 <= size <= limit or total + size > MAX_TOTAL_BYTES:
                    raise ConfigError("展開サイズが上限を超えている、または不明な書庫は変換できません。")
                if is_metadata and metadata is not None:
                    raise ConfigError("ComicInfo.xmlが複数あります。対応を確認してください。")
                digest, actual, content = hashlib.sha256(), 0, bytearray()
                output = (spool / str(len(members))).open("xb") if spool else None
                try:
                    for chunk in blocks:
                        actual += len(chunk)
                        if actual > size or actual > limit:
                            raise ConfigError("書庫のサイズ宣言と内容が一致しません。")
                        digest.update(chunk)
                        if output:
                            output.write(chunk)
                        if is_metadata:
                            content.extend(chunk)
                finally:
                    if output:
                        output.close()
                if actual != size:
                    raise ConfigError("書庫のサイズ宣言と内容が一致しません。")
                total += actual
                if is_metadata:
                    metadata = bytes(content)
                members.append({"source": name, "size": size, "sha256": digest.hexdigest(),
                                "metadata": is_metadata})
        pages = sorted((m for m in members if PurePosixPath(m["source"]).suffix.lower() in IMAGES),
                       key=lambda member: natural_key(member["source"]))
        if not pages:
            raise ConfigError("書庫に対応する画像ページがありません。")
        if any(part.startswith(".") or part == "__MACOSX" for member in pages for part in PurePosixPath(member["source"]).parts):
            raise ConfigError("隠し画像が含まれています。ページ順を確認してから変換してください。")
        if metadata is not None:
            _comicinfo(metadata, len(pages))
        width = max(4, len(str(len(pages))))
        for page, member in enumerate(pages, 1):
            member.update(page=page, target=f"{page:0{width}d}" + PurePosixPath(member["source"]).suffix.lower())
        targets = set()
        for member in members:
            member.setdefault("target", "ComicInfo.xml" if member["metadata"] else member["source"])
            key = name_key(member["target"])
            if key in targets:
                raise ConfigError("正規化したページ名と補助ファイルが衝突します。")
            targets.add(key)
        if any(name_key(str(parent)) in targets for member in members
               for parent in PurePosixPath(member["target"]).parents if str(parent) != "."):
            raise ConfigError("正規化したページ名と書庫内フォルダが衝突します。")
        estimated = total + sum(2 * len(m["target"].encode("utf-8")) + 256 for m in members) + len(comment) // 2 + 4096
        return {"format": format_name, "comment": comment, "pages": len(pages), "expanded_bytes": total,
                "estimated_output_bytes": estimated,
                "members": members, "order": "natural-path", "recipe": 1}
    except (ConfigError, StateConflict, OSError):
        raise
    except Exception as exc:
        raise ConfigError("書庫を読み取れません。破損・暗号化・対応形式を確認してください。") from exc


def build_cbz(fs, relative, expected, destination):
    with tempfile.TemporaryDirectory(prefix="pages-", dir=destination.parent) as temporary:
        spool = Path(temporary)
        current = inspect_archive(fs, relative, spool)
        if current != expected:
            raise StateConflict("確認後に書庫の内容が変わりました。計画を作り直してください。")
        ordered = sorted(enumerate(current["members"]),
                         key=lambda pair: (0, pair[1]["page"]) if "page" in pair[1] else (1, pair[1]["target"]))
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                archive.comment = bytes.fromhex(current["comment"])
                for index, member in ordered:
                    info = zipfile.ZipInfo(member["target"], (1980, 1, 1, 0, 0, 0))
                    info.create_system, info.external_attr = 3, 0o100644 << 16
                    with archive.open(info, "w", force_zip64=True) as target, (spool / str(index)).open("rb") as source:
                        shutil.copyfileobj(source, target, BLOCK)
            output.flush()
            os.fsync(output.fileno())
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise StateConflict("生成したCBZの検証に失敗しました。")
            for _, member in ordered:
                with archive.open(member["target"]) as page:
                    if hashlib.file_digest(page, "sha256").hexdigest() != member["sha256"]:
                        raise StateConflict("生成したCBZのページ・メタデータが一致しません。")
