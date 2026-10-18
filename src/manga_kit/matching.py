"""Explain candidate identity without automatic selection or fuzzy title matches."""
from pathlib import PurePosixPath

from .authors import author_candidates
from .bibliography import edition_markers, edition_title
from .japanese import search_key


def candidate_titles(candidate):
    names = [candidate.get("fields", {}).get("title") or candidate["title"]]
    return list(dict.fromkeys(names + candidate.get("alternate_titles", [])))


def known_titles(current):
    """Only explicitly linked source titles count as known aliases."""
    names = [current["series"]["title"]]
    for link in current["provenances"].values():
        names.extend(link.get("identity", {}).get("titles", []))
    return list(dict.fromkeys(names))


def _names(text):
    return {search_key(item["name"]) for item in author_candidates(text)}


def compare_candidate(current, candidate, candidates=()):
    series, fields = current["series"], candidate["fields"]
    titles = candidate_titles(candidate)
    keys = {search_key(edition_title(name)) for name in titles}
    own = {search_key(edition_title(series["title"]))}
    aliases = {search_key(edition_title(name)) for name in known_titles(current)} - own
    evidence, warnings = [], []
    title_match = bool(keys & own)
    alias_match = bool(keys & aliases)
    if title_match:
        evidence.append("作品名が一致")
    elif alias_match:
        evidence.append("確認済みの別名が一致")
    else:
        warnings.append("作品名・確認済みの別名が一致しません。副題や関連作品を確認してください。")
    if min(len(key) for key in keys) <= 2:
        warnings.append("短い作品名です。著者や情報源の作品ページも確認してください。")
    if series["author"] and fields.get("author"):
        authors, proposed = _names(series["author"]), _names(fields["author"])
        if authors and authors == proposed:
            evidence.append("著者が一致")
        else:
            warnings.append("著者の一覧が異なります。同名の別作品・役割・共著者を確認してください。")
    else:
        warnings.append("比較できる著者情報がありません。情報源の作品ページを確認してください。")
    if series["publisher"] and fields.get("publisher"):
        if search_key(series["publisher"]) == search_key(fields["publisher"]):
            evidence.append("出版社が一致")
        else:
            warnings.append("出版社が異なります。再刊・版違い・同名作品を確認してください。")
    local_editions = set(edition_markers(series["title"]) + edition_markers(PurePosixPath(series["path"]).name))
    proposed_editions = set(candidate.get("editions", []))
    if local_editions != proposed_editions and (local_editions or proposed_editions):
        warnings.append("版の表記が異なるか、片方にありません。通常・文庫・再刊・特装巻を確認してください。")
    elif local_editions:
        evidence.append("版の表記が一致")
    if candidate.get("readership_source") == "conflict":
        warnings.append("ジャンルとレーベルの読者層が一致しないため、読者層は提案しません。")
    # Search order and matching names cannot resolve distinct editions/creators.
    for other in candidates:
        if other["id"] == candidate["id"]:
            continue
        other_keys = {search_key(edition_title(name)) for name in candidate_titles(other)}
        if not keys & other_keys:
            continue
        other_fields = other["fields"]
        if (any(search_key(fields.get(key, "")) != search_key(other_fields.get(key, ""))
                for key in ("author", "publisher"))
                or candidate.get("label", "") != other.get("label", "")
                or proposed_editions != set(other.get("editions", []))):
            warnings.append("同名で著者・出版社・レーベル・版が異なる候補があります。代表巻を選んでください。")
            break
    return {"status": "review" if warnings else "consistent", "evidence": evidence, "warnings": warnings}


def annotate_results(current, result):
    return {**result, "known_titles": known_titles(current), "candidates": [
        {**candidate, "match": compare_candidate(current, candidate, result["candidates"])}
        for candidate in result["candidates"]]}


def candidate_identity(candidate):
    return {"titles": candidate_titles(candidate), "product_title": candidate["title"],
            "author": candidate["fields"].get("author", ""), "publisher": candidate["fields"].get("publisher", ""),
            "label": candidate.get("label", ""), "editions": candidate.get("editions", []),
            "volume": candidate.get("volume"), "size": candidate.get("size", ""),
            "readership_source": candidate.get("readership_source", ""),
            **({"facts": candidate["facts"]} if candidate.get("facts") else {})}
