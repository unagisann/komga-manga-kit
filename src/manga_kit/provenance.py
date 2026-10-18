"""Multiple source links, compatible with the original single bibliographic link."""
from copy import deepcopy

PROVIDER_LABELS = {"rakuten_books": "楽天Books", "demo": "架空の書誌データ",
                   "rakuten_kobo": "楽天Kobo", "demo_kobo": "架空の電子書籍",
                   "mangaupdates": "MangaUpdates", "demo_mangaupdates": "架空の作品情報",
                   "anilist": "AniList", "demo_anilist": "架空のタグ・ジャンル", "komga": "Komga"}


def provider_key(provider):
    return {"demo": "rakuten_books", "demo_kobo": "rakuten_kobo", "demo_mangaupdates": "mangaupdates", "demo_anilist": "anilist"}.get(provider, provider)


def source_links(value):
    if not value:
        return {}
    if "provider" in value:
        return {provider_key(value["provider"]): deepcopy(value)}
    return deepcopy(value)


def stored_links(links):
    # Keep existing state and prepared-operation snapshots byte-for-byte comparable.
    if set(links) == {"rakuten_books"}:
        return deepcopy(links["rakuten_books"])
    return deepcopy(links)
