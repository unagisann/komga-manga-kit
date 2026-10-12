"""Japanese text normalization and kana-row classification."""
import re
import unicodedata

ROWS = "あかさたなはまやらわ"
_GROUPS = ("あいうえおぁぃぅぇぉゔ", "かきくけこがぎぐげご", "さしすせそざじずぜぞ",
           "たちつてとだぢづでどっ", "なにぬねの", "はひふへほばびぶべぼぱぴぷぺぽ",
           "まみむめも", "やゆよゃゅょ", "らりるれろ", "わをんゐゑゎ")
_ROW_MAP = {char: row for row, group in zip(ROWS, _GROUPS) for char in group}


def hiragana(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)


def kana_row(reading: str) -> str:
    return _ROW_MAP.get(hiragana(reading).strip()[:1], "その他")


def search_key(text: str) -> str:
    return re.sub(r"\s+", "", hiragana(text).casefold())


def split_folder_name(name: str) -> tuple[str, str]:
    match = re.match(r"^\[([^\]]+)\]\s*(.+)$", name)
    return (match[2].strip(), match[1].strip()) if match else (name, "")
