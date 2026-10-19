"""Cover screening and small previews, decoding pages at a fraction of their size with Pillow."""
import io
import math
from pathlib import PurePosixPath

from .archives import IMAGES, MAX_ENTRIES, name_key, reader, safe_name
from .config import ConfigError
from .filesystem import LibraryFilesystem
from .state import timestamp

MAX_IMAGE = 25 * 1024 * 1024
# Colourfulness (Hasler and Süsstrunk) below which a page reads as black and white, and above which it is
# clearly in colour. On a real library, grey pages measured 0-1; pale or dark colour covers 10-25, others above 27;
# covers drawn almost without colour fall between 6 and 10, so they are not reported as black and white.
MONO_LIMIT = 6.0
COLOR_LIMIT = 15.0
# Stored verdicts from another measure are judged again.
METHOD = "colourfulness-1"
BUCKETS = {"mono_color": "1枚目白黒・2枚目カラー（入れ替え候補）", "mono_first": "1枚目が白黒", "broken": "破損・画像なし"}


def colourfulness(data):
    """Hasler and Süsstrunk's colourfulness of a page. It counts the spread of colours as well as their strength,
    so pale covers still read as colour. JPEGs decode at 1/8 scale, so a page costs little more than reading it."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image.draft("RGB", (96, 96))
        small = image.convert("RGB")
        small.thumbnail((64, 64))
        pixels = list(small.getdata())
    count = max(1, len(pixels))
    rg = [red - green for red, green, _ in pixels]
    yb = [(red + green) / 2 - blue for red, green, blue in pixels]
    mean_rg, mean_yb = sum(rg) / count, sum(yb) / count
    spread = math.sqrt(sum((value - mean_rg) ** 2 for value in rg) / count + sum((value - mean_yb) ** 2 for value in yb) / count)
    return spread + 0.3 * math.hypot(mean_rg, mean_yb)


def small_image(data, width=320):
    """A JPEG no wider than `width`, or None when the bytes cannot be decoded (callers then send the original)."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.draft("RGB", (width, width * 2))
            small = image.convert("RGB")
            small.thumbnail((width, width * 2))
            output = io.BytesIO()
            small.save(output, "JPEG", quality=82, optimize=True)
            return output.getvalue()
    except Exception:
        return None


def first_pages(fs, path, count=2):
    """The first `count` page images in name order, as the page editor orders them."""
    suffix = PurePosixPath(path).suffix.lower()
    pages, seen = [], set()
    with fs.open_read(path) as content, reader(content, suffix) as (_, _, entries):
        for index, (raw, size, directory, _) in enumerate(entries, 1):
            if index > MAX_ENTRIES:
                raise ConfigError("書庫内の項目数が上限を超えています。")
            name = safe_name(raw, directory)
            if name_key(name) in seen:
                raise ConfigError("書庫内に重複した名前があります。")
            seen.add(name_key(name))
            if not directory and PurePosixPath(name).suffix.lower() in IMAGES:
                pages.append((name, raw, size))
    chosen = sorted(pages)[:count]
    wanted = {raw: position for position, (_, raw, _) in enumerate(chosen)}
    data = [None] * len(chosen)
    if chosen:
        with fs.open_read(path) as content, reader(content, suffix) as (_, _, entries):
            for raw, size, directory, blocks in entries:
                if raw in wanted and not directory:
                    if not 0 < size <= MAX_IMAGE:
                        raise ConfigError("画像サイズが確認の上限を超えています。")
                    value = bytearray()
                    for block in blocks:
                        value.extend(block)
                        if len(value) > size:
                            raise ConfigError("画像のサイズが書庫の記録と一致しません。")
                    data[wanted[raw]] = bytes(value)
                    if all(item is not None for item in data):
                        break
    return data


def screen(fs, path):
    """One book's verdict: mono_color, mono_first, broken or ok, with the colour shares behind it."""
    try:
        pages = first_pages(fs, path)
    except Exception:
        # Any unreadable archive belongs in the broken group; one bad file must not stop the whole screening.
        return {"bucket": "broken", "reason": "書庫を読み取れません"}
    if not pages:
        return {"bucket": "broken", "reason": "画像がありません"}
    try:
        values = [colourfulness(page) for page in pages]
    except Exception:
        return {"bucket": "broken", "reason": "1枚目または2枚目の画像を読み取れません"}
    first, second = values[0], values[1] if len(values) > 1 else None
    result = {"first": round(first, 1), "second": None if second is None else round(second, 1)}
    if first < MONO_LIMIT and second is not None and second >= COLOR_LIMIT:
        return {**result, "bucket": "mono_color", "reason": "1枚目が白黒で、2枚目がカラー"}
    if first < MONO_LIMIT:
        return {**result, "bucket": "mono_first", "reason": "1枚目が白黒"}
    return {**result, "bucket": "ok", "reason": ""}


class CoverScreening:
    """Library-wide screening, kept per book and repeated only for archives whose size or time changed."""
    KEY = "covers:screening"
    SAVE_EVERY = 500

    def __init__(self, settings, store, inventory, jobs):
        self.settings, self.store, self.inventory, self.jobs = settings, store, inventory, jobs

    def results(self):
        cached = self.store.cached(self.KEY)
        return cached["data"] if cached else {"books": {}, "updated": None}

    def submit(self, *, force=False):
        if type(force) is not bool:
            raise ConfigError("確認の方法を指定してください。")
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise ConfigError("表紙の確認には Pillow が必要です。依存パッケージを入れ直してください。") from None
        return self.jobs.submit_task("cover_screening", lambda progress: self.run(progress, force=force), key=force)

    def books(self):
        return [book for row in self.inventory.cached()["series"] for book in row["books"] if book.get("path")]

    def run(self, progress=lambda *_: None, *, force=False):
        stored = self.results()
        books, previous = self.books(), stored["books"] if stored.get("method") == METHOD else {}
        if self.settings.demo:
            # Sample archives hold placeholder pages, so show one book of each kind instead of judging them.
            kinds = ["mono_color", "mono_first", "broken"]
            verdicts = {book["id"]: {"bucket": kinds[index] if index < 3 else "ok", "reason": BUCKETS.get(kinds[index], "") if index < 3 else "",
                                     "fingerprint": "demo"} for index, book in enumerate(books)}
            self.store.cache(self.KEY, {"books": verdicts, "updated": timestamp(), "demo": True, "method": METHOD})
            progress(len(books), len(books))
            return self.summary(verdicts)
        verdicts, checked = {}, 0
        with LibraryFilesystem(self.settings.library_root) as fs:
            for index, book in enumerate(books, 1):
                try:
                    signature = fs.signature(book["path"])
                    fingerprint = f"{signature['size']}:{signature['mtime_ns']}"
                except (ConfigError, OSError):
                    verdicts[book["id"]] = {"bucket": "broken", "reason": "書庫を開けません", "fingerprint": ""}
                    continue
                old = previous.get(book["id"])
                if old and old.get("fingerprint") == fingerprint and not force:
                    verdicts[book["id"]] = old
                else:
                    verdicts[book["id"]] = {**screen(fs, book["path"]), "fingerprint": fingerprint}
                    checked += 1
                if index % self.SAVE_EVERY == 0:
                    self.store.cache(self.KEY, {"books": {**previous, **verdicts}, "updated": timestamp(), "method": METHOD})
                progress(index, len(books))
        self.store.cache(self.KEY, {"books": verdicts, "updated": timestamp(), "method": METHOD})
        return {**self.summary(verdicts), "checked": checked}

    @staticmethod
    def summary(verdicts):
        counts = {key: 0 for key in BUCKETS}
        for verdict in verdicts.values():
            if verdict.get("bucket") in counts:
                counts[verdict["bucket"]] += 1
        return {"books": len(verdicts), "flagged": sum(counts.values()), "counts": counts}
