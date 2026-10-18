"""Conservative product identity hints; never infer a publisher from an imprint."""
import re
import unicodedata

EDITIONS = ("通常版", "文庫版", "新装版", "愛蔵版", "完全版", "特装版", "限定版", "カラー版", "合本版")
_EDITION = "|".join(EDITIONS)
_EDITION_SUFFIX = re.compile(rf"(?:\s+|\s*[(（【])({_EDITION})(?:[)）】])?\s*$")
_EDITION_MARK = re.compile(rf"(?:^|[\s(（【])({_EDITION})(?=$|[\s)）】0-9第])")
_LABEL_AUDIENCE = re.compile(r"(少年|少女|青年|レディース)(?:コミックス?|まんが|漫画|マンガ)")


def edition_title(title):
    """Remove only an explicit terminal edition note, retaining unknown subtitles."""
    text = title.strip()
    while match := _EDITION_SUFFIX.search(text):
        prefix = text[:match.start()].strip()
        if not prefix:
            break
        text = prefix
    return text


def edition_markers(title, label="", size=""):
    text = unicodedata.normalize("NFKC", title)
    found = {match[1] for match in _EDITION_MARK.finditer(text)}
    # Rakuten's size=2 is paperback (文庫). Unknown formats remain unknown.
    if size == "文庫" or re.search(r"文庫(?:版)?$", unicodedata.normalize("NFKC", label).strip()):
        found.add("文庫版")
    return [name for name in EDITIONS if name in found]


def label_readership(label):
    """Only explicit audience words, not a private table of imprint guesses."""
    found = set(_LABEL_AUDIENCE.findall(unicodedata.normalize("NFKC", label)))
    return [found.pop()] if len(found) == 1 else []


def product_readership(genre_ids, label, demographics):
    genres = {name for genre in genre_ids.split("/") for prefix, name in demographics.items()
              if genre.startswith(prefix)}
    labels = label_readership(label)
    label_audiences = set(_LABEL_AUDIENCE.findall(unicodedata.normalize("NFKC", label)))
    if len(genres) > 1 or len(label_audiences) > 1 or genres and labels and genres != set(labels):
        return [], "conflict"
    if genres:
        return sorted(genres), "genre"
    return labels, "label" if labels else ""
