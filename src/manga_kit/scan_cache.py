"""Disposable, library-scoped archive metadata; never an execution authority."""
import hashlib
import json

from .providers import ComicInfoProvider, EpubProvider, MetadataProposal

# Bump when built-in metadata parsing changes. Custom providers are not cached.
CACHE_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024


def file_signature(info):
    return ':'.join(str(value) for value in (info.st_dev, info.st_ino, info.st_size,
                                            info.st_mtime_ns, info.st_ctime_ns))


class ArchiveMetadataCache:
    def __init__(self, store, providers, *, reuse=True):
        self.store, self.reuse = store, reuse
        self.enabled = store is not None and all(type(p) in {ComicInfoProvider, EpubProvider} for p in providers)
        self.key = hashlib.sha256(json.dumps([CACHE_VERSION, [type(p).__name__ for p in providers]]).encode()).hexdigest()
        self.rows, self.changed, self.seen = {}, {}, set()
        if self.enabled:
            with store.connection() as conn:
                rows = conn.execute('SELECT path,signature,data FROM archive_metadata_cache '
                                    'WHERE namespace=? AND provider_key=?', (store.namespace, self.key)).fetchall()
            self.rows = {row['path']: (row['signature'], row['data']) for row in rows}

    def get(self, path, info):
        self.seen.add(path)
        row = self.rows.get(path)
        if not self.enabled or not self.reuse or row is None or row[0] != file_signature(info):
            return None
        try:
            if len(row[1]) > MAX_PAYLOAD_BYTES:
                return None
            values = json.loads(row[1])
            if not isinstance(values, list) or len(values) > 2:
                return None
            result = []
            for value in values:
                if (not isinstance(value, dict) or set(value) != {'source', 'fields'}
                        or value['source'] not in {'comicinfo', 'epub'} or not isinstance(value['fields'], dict)):
                    return None
                for key, item in value['fields'].items():
                    if key in {'genres', 'tags'}:
                        if not isinstance(item, list) or any(not isinstance(v, str) for v in item):
                            return None
                    elif key not in {'title', 'author', 'summary', 'publisher', 'reading', 'volume'} or not isinstance(item, str):
                        return None
                result.append(MetadataProposal(value['source'], value['fields']))
            return result
        except (TypeError, ValueError, RecursionError):
            return None

    def put(self, path, info, proposals):
        if not self.enabled:
            return
        payload = json.dumps([{'source': p.source, 'fields': p.fields} for p in proposals], ensure_ascii=False)
        value = (file_signature(info), payload)
        if len(payload.encode()) <= MAX_PAYLOAD_BYTES and self.rows.get(path) != value:
            self.changed[path] = value

    def discard(self, path):
        self.changed.pop(path, None)
        # Failed or unstable reads must not resurrect a previous cached value.
        if path in self.rows:
            self.rows[path] = ('', '')

    def save(self, *, complete):
        if not self.enabled:
            return
        removed = {path for path, value in self.rows.items() if not value[0]}
        if complete:
            removed.update(self.rows.keys() - self.seen)
        if not removed and not self.changed:
            return
        with self.store.connection() as conn:
            conn.executemany('DELETE FROM archive_metadata_cache WHERE namespace=? AND provider_key=? AND path=?',
                             [(self.store.namespace, self.key, path) for path in removed])
            conn.executemany('INSERT INTO archive_metadata_cache(namespace,provider_key,path,signature,data) '
                             'VALUES(?,?,?,?,?) ON CONFLICT(namespace,provider_key,path) DO UPDATE SET '
                             'signature=excluded.signature,data=excluded.data',
                             [(self.store.namespace, self.key, path, *value) for path, value in self.changed.items()])
