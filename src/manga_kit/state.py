"""Private SQLite state, separated by library and independent of the source tree."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from threading import Lock
from uuid import uuid4

from .config import ConfigError
from .durability import durable_mkdir, sync_directory


class StateConflict(RuntimeError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def namespace(settings):
    identity = ("demo:" if settings.demo else "local:") + str(settings.library_root.resolve())
    return hashlib.sha256(identity.encode()).hexdigest()


def process_start(pid):
    # Linux process start time also detects PID reuse after a container restart.
    try:
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + start
    except (OSError, IndexError):
        return None


class StateStore:
    def __init__(self, settings):
        settings.validate_storage()
        self.path = settings.data_root / "state.sqlite3"
        self.namespace = namespace(settings)
        self._lock, self._initialized = Lock(), False

    @contextmanager
    def connection(self):
        with self._lock:
            if not self._initialized:
                durable_mkdir(self.path.parent)
                if self.path.is_symlink():
                    raise ConfigError("実行データのDBにシンボリックリンクは使えません。")
                conn = sqlite3.connect(self.path, timeout=15)
                try:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version not in {0, 1, 2, 3}:
                        raise ConfigError("実行データの形式が対応バージョンと異なります。")
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=FULL")
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS jobs (
                            id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
                            status TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 0, total INTEGER,
                            created TEXT NOT NULL, updated TEXT NOT NULL, owner TEXT NOT NULL,
                            result TEXT, error TEXT);
                        CREATE INDEX IF NOT EXISTS jobs_by_library ON jobs(namespace, created);
                        CREATE TABLE IF NOT EXISTS job_queue (
                            position INTEGER PRIMARY KEY AUTOINCREMENT,
                            namespace TEXT NOT NULL, task_key TEXT NOT NULL,
                            job_id TEXT NOT NULL UNIQUE, child_id TEXT);
                        CREATE INDEX IF NOT EXISTS queue_by_library ON job_queue(namespace, task_key);
                        CREATE UNIQUE INDEX IF NOT EXISTS queue_child ON job_queue(child_id);
                        CREATE TABLE IF NOT EXISTS edits (
                            namespace TEXT NOT NULL, series_id TEXT NOT NULL, fields TEXT NOT NULL,
                            revision INTEGER NOT NULL, updated TEXT NOT NULL,
                            PRIMARY KEY(namespace, series_id));
                        CREATE TABLE IF NOT EXISTS events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
                            kind TEXT NOT NULL, subject TEXT NOT NULL, created TEXT NOT NULL,
                            detail TEXT NOT NULL);
                        CREATE INDEX IF NOT EXISTS events_by_library ON events(namespace, id);
                        CREATE TABLE IF NOT EXISTS operation_edits (
                            namespace TEXT NOT NULL, operation_id TEXT NOT NULL, direction TEXT NOT NULL,
                            edits TEXT NOT NULL, PRIMARY KEY(namespace,operation_id,direction));
                        CREATE TABLE IF NOT EXISTS metadata_searches (
                            id TEXT PRIMARY KEY, namespace TEXT NOT NULL, series_id TEXT NOT NULL,
                            cache_key TEXT NOT NULL, expires REAL NOT NULL, data TEXT NOT NULL);
                        CREATE INDEX IF NOT EXISTS searches_by_library ON metadata_searches(namespace,cache_key);
                        CREATE TABLE IF NOT EXISTS metadata_requests (
                            provider TEXT PRIMARY KEY, next_allowed REAL NOT NULL);
                        CREATE TABLE IF NOT EXISTS archive_metadata_cache (
                            namespace TEXT NOT NULL, provider_key TEXT NOT NULL, path TEXT NOT NULL,
                            signature TEXT NOT NULL, data TEXT NOT NULL,
                            PRIMARY KEY(namespace,provider_key,path));
                        CREATE TABLE IF NOT EXISTS normalization_files (
                            namespace TEXT NOT NULL, path TEXT NOT NULL, data TEXT NOT NULL,
                            PRIMARY KEY(namespace,path));
                        CREATE TABLE IF NOT EXISTS tool_cache (
                            namespace TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
                            updated TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                            PRIMARY KEY(namespace,key));
                    """)
                    conn.execute("BEGIN IMMEDIATE")
                    if "provenance" not in {row[1] for row in conn.execute("PRAGMA table_info(edits)")}:
                        conn.execute("ALTER TABLE edits ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}'")
                    conn.execute("PRAGMA user_version=3")
                    conn.execute("DELETE FROM metadata_searches WHERE expires<=?", (time.time(),))
                    conn.commit()
                finally:
                    conn.close()
                self.path.chmod(0o600)
                sync_directory(self.path.parent)
                self._initialized = True
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def edits(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM edits WHERE namespace=?", (self.namespace,)).fetchall()
        return {row["series_id"]: self._edit(row) for row in rows}

    @staticmethod
    def _edit(row):
        result = {"fields": json.loads(row["fields"]), "revision": row["revision"]}
        provenance = json.loads(row["provenance"])
        if provenance:
            result["provenance"] = provenance
        return result

    def save_edit(self, series_id, fields, revision, detail, *, provenance=None):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision FROM edits WHERE namespace=? AND series_id=?",
                               (self.namespace, series_id)).fetchone()
            actual = row["revision"] if row else 0
            if revision != actual:
                raise StateConflict("別の操作で作品情報が更新されました。差分を再確認してください。")
            conn.execute("INSERT INTO edits(namespace,series_id,fields,revision,updated,provenance) VALUES(?,?,?,?,?,?) "
                         "ON CONFLICT(namespace,series_id) DO UPDATE SET fields=excluded.fields,revision=excluded.revision,"
                         "updated=excluded.updated,provenance=excluded.provenance",
                         (self.namespace, series_id, json.dumps(fields, ensure_ascii=False), actual + 1, timestamp(),
                          json.dumps(provenance or {}, ensure_ascii=False)))
            self._event(conn, "metadata_edit", series_id, detail)
        return actual + 1

    def verify_edits(self, expected):
        actual = self.edits()
        for series_id, value in expected.items():
            if actual.get(series_id, {"fields": {}, "revision": 0}) != value:
                raise StateConflict("確認後に作品情報が編集されました。計画を作り直してください。")

    def relocate_edits(self, groups, expected, operation_id, *, restore=False):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            direction = "restore" if restore else "move"
            saved = conn.execute("SELECT edits FROM operation_edits WHERE namespace=? AND operation_id=? AND direction=?",
                                 (self.namespace, operation_id, direction)).fetchone()
            if saved:
                return json.loads(saved["edits"])
            result = {key: dict(value) for key, value in expected.items()}
            rows = conn.execute("SELECT * FROM edits WHERE namespace=?", (self.namespace,)).fetchall()
            actual = {row["series_id"]: self._edit(row) for row in rows}
            for series_id, value in expected.items():
                if actual.get(series_id, {"fields": {}, "revision": 0}) != value:
                    raise StateConflict("整理中に作品情報が編集されました。バックアップから復元してください。")
            for group in groups:
                source, target = group["source_id"], group["target_id"]
                if source == target:
                    continue
                if restore:
                    source, target = target, source
                fields = expected[source]["fields"]
                for key, value in ((source, {}), (target, fields)):
                    provenance = expected[source].get("provenance", {}) if key == target else {}
                    conn.execute("INSERT INTO edits(namespace,series_id,fields,revision,updated,provenance) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(namespace,series_id) DO UPDATE SET fields=excluded.fields,revision=excluded.revision,"
                        "updated=excluded.updated,provenance=excluded.provenance",
                        (self.namespace, key, json.dumps(value, ensure_ascii=False), expected[key]["revision"] + 1, timestamp(),
                         json.dumps(provenance, ensure_ascii=False)))
                    result[key] = {"fields": value, "revision": expected[key]["revision"] + 1}
                    if provenance:
                        result[key]["provenance"] = provenance
                self._event(conn, "metadata_relocated", source, {"target": target, "restore": restore})
            conn.execute("INSERT INTO operation_edits VALUES(?,?,?,?)",
                         (self.namespace, operation_id, direction, json.dumps(result, ensure_ascii=False)))
        return result

    def apply_operation_edits(self, operation_id, direction, changes, *, event_kind="metadata_import"):
        """Replace several series' edits in one transaction, once per operation and direction.

        Each change names the edit it expects; any later edit stops the whole batch.
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = conn.execute("SELECT edits FROM operation_edits WHERE namespace=? AND operation_id=? AND direction=?",
                                 (self.namespace, operation_id, direction)).fetchone()
            if saved:
                return json.loads(saved["edits"])
            rows = conn.execute("SELECT * FROM edits WHERE namespace=?", (self.namespace,)).fetchall()
            actual = {row["series_id"]: self._edit(row) for row in rows}
            changed = [change for change in changes
                       if actual.get(change["series_id"], {"fields": {}, "revision": 0}) != change["expected"]]
            if changed:
                raise StateConflict(f"計画後に{len(changed)}作品の作品情報が編集されました。上書きせず中止しました。")
            result = {}
            for change in changes:
                fields, provenance = change["edit"]["fields"], change["edit"].get("provenance", {})
                revision = change["expected"]["revision"] + 1
                conn.execute("INSERT INTO edits(namespace,series_id,fields,revision,updated,provenance) VALUES(?,?,?,?,?,?) "
                             "ON CONFLICT(namespace,series_id) DO UPDATE SET fields=excluded.fields,revision=excluded.revision,"
                             "updated=excluded.updated,provenance=excluded.provenance",
                             (self.namespace, change["series_id"], json.dumps(fields, ensure_ascii=False), revision,
                              timestamp(), json.dumps(provenance, ensure_ascii=False)))
                result[change["series_id"]] = {"fields": fields, "revision": revision, **({"provenance": provenance} if provenance else {})}
            self._event(conn, event_kind, operation_id, {"series": len(changes), "direction": direction})
            conn.execute("INSERT INTO operation_edits VALUES(?,?,?,?)",
                         (self.namespace, operation_id, direction, json.dumps(result, ensure_ascii=False)))
        return result

    def operation_edits(self, operation_id, direction="move"):
        with self.connection() as conn:
            row = conn.execute("SELECT edits FROM operation_edits WHERE namespace=? AND operation_id=? AND direction=?",
                               (self.namespace, operation_id, direction)).fetchone()
        return json.loads(row["edits"]) if row else None

    @contextmanager
    def editing(self):
        if os.name != "posix":
            yield
            return
        import fcntl
        directory = self.path.parent / "operations" / self.namespace
        durable_mkdir(directory)
        if not directory.resolve().is_relative_to(self.path.parent.resolve()):
            raise ConfigError("編集のロック保存先が実行データ外を指しています。")
        fd = os.open(directory / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateConflict("整理・復元の実行中は作品情報を保存できません。完了後に再確認してください。") from exc
            yield
        finally:
            os.close(fd)

    @contextmanager
    def tool_lock(self, name):
        if name not in {"smart-shelves", "notifications", "inventory", "managed-normalization"}:
            raise ConfigError("処理ロックの種類が不正です。")
        if os.name != "posix":
            raise ConfigError("この更新処理はPOSIX環境で利用してください。")
        import fcntl
        directory = self.path.parent / "tool-locks"
        durable_mkdir(directory)
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.path.parent.resolve()):
            raise ConfigError("処理ロックの保存先が不正です。")
        descriptor = os.open(directory / (self.namespace + "-" + name), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateConflict("同じ更新処理が実行中です。完了後に再実行してください。") from None
            yield
        finally:
            os.close(descriptor)

    def _event(self, conn, kind, subject, detail):
        conn.execute("INSERT INTO events(namespace,kind,subject,created,detail) VALUES(?,?,?,?,?)",
                     (self.namespace, kind, subject, timestamp(), json.dumps(detail, ensure_ascii=False)))

    def events(self, limit=50, offset=0):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE namespace=? ORDER BY id DESC LIMIT ? OFFSET ?",
                                (self.namespace, limit, offset)).fetchall()
        return [{**{key: row[key] for key in ("id", "kind", "subject", "created")},
                 "detail": json.loads(row["detail"])} for row in rows]

    def create_job(self, kind):
        with self.connection() as conn:
            job_id = self._create_job(conn, kind)
        return job_id

    def _create_job(self, conn, kind):
        job_id, now = uuid4().hex, timestamp()
        conn.execute("INSERT INTO jobs(id,namespace,kind,status,created,updated,owner) VALUES(?,?,?,?,?,?,?)",
                     (job_id, self.namespace, kind, "queued", now, now,
                      json.dumps({"pid": os.getpid(), "start": process_start(os.getpid())})))
        self._event(conn, "job_created", job_id, {"kind": kind})
        return job_id

    def enqueue_job(self, kind, task_key, *, child_kind=None):
        """Reserve one FIFO slot atomically, including duplicate/capacity checks."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute("SELECT q.* FROM job_queue q JOIN jobs j ON j.id=q.job_id "
                                  "WHERE q.namespace=? AND j.status IN ('queued','running') ORDER BY q.position",
                                  (self.namespace,)).fetchall()
            duplicate = next((row for row in active if row["task_key"] == task_key), None)
            if duplicate:
                return duplicate["job_id"], duplicate["child_id"], False
            if len(active) >= 20:
                raise ConfigError("処理待ちが20件に達しています。完了後に再実行してください。")
            job_id = self._create_job(conn, kind)
            child_id = self._create_job(conn, child_kind) if child_kind else None
            conn.execute("INSERT INTO job_queue(namespace,task_key,job_id,child_id) VALUES(?,?,?,?)",
                         (self.namespace, task_key, job_id, child_id))
        return job_id, child_id, True

    def start_queued_job(self, job_id):
        """Only the first active queue entry may start, across local processes."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT status FROM jobs WHERE namespace=? AND id=?",
                               (self.namespace, job_id)).fetchone()
            if job is None or job["status"] != "queued":
                return job["status"] if job else "interrupted"
            first = conn.execute("SELECT j.id FROM job_queue q JOIN jobs j ON j.id=q.job_id "
                                 "WHERE q.namespace=? AND j.status IN ('queued','running') ORDER BY q.position LIMIT 1",
                                 (self.namespace,)).fetchone()
            if first and first["id"] == job_id:
                conn.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (timestamp(), job_id))
                return "started"
        return "queued"

    def queued_child(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT child_id FROM job_queue WHERE namespace=? AND job_id=?",
                               (self.namespace, job_id)).fetchone()
        return row["child_id"] if row else None

    def update_job(self, job_id, *, status=None, current=None, total=None, result=None, error=None):
        values = {"updated": timestamp()}
        for key, value in (("status", status), ("current", current), ("total", total), ("error", error)):
            if value is not None:
                values[key] = value
        if result is not None:
            values["result"] = json.dumps(result, ensure_ascii=False)
        with self.connection() as conn:
            conn.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=? AND namespace=?",
                         (*values.values(), job_id, self.namespace))
            if status in {"succeeded", "failed", "interrupted"}:
                self._event(conn, "job_finished", job_id, {"status": status})

    @staticmethod
    def _job(row, include_result=False):
        job = {key: row[key] for key in ("id", "kind", "status", "current", "total", "created", "updated", "error")}
        job["queue_position"] = row["queue_position"] if "queue_position" in row.keys() else None
        if include_result:
            job["result"] = json.loads(row["result"]) if row["result"] else None
        return job

    def job(self, job_id, include_result=False):
        with self.connection() as conn:
            row = conn.execute(self._queued_jobs + " WHERE j.namespace=? AND j.id=?", (self.namespace, job_id)).fetchone()
        return self._job(row, include_result) if row else None

    def jobs(self, limit=50, offset=0):
        with self.connection() as conn:
            rows = conn.execute(self._queued_jobs + " WHERE j.namespace=? ORDER BY j.created DESC,j.id DESC LIMIT ? OFFSET ?",
                                (self.namespace, limit, offset)).fetchall()
        return [self._job(row) for row in rows]

    _queued_jobs = """SELECT j.*, CASE WHEN j.status='queued' AND q.position IS NOT NULL THEN
        (SELECT COUNT(*) FROM job_queue preceding JOIN jobs active ON active.id=preceding.job_id
         WHERE preceding.namespace=j.namespace AND preceding.position<=q.position
         AND active.status IN ('queued','running')) ELSE NULL END AS queue_position
        FROM jobs j LEFT JOIN job_queue q ON q.job_id=j.id OR q.child_id=j.id"""

    def latest_preview(self):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE namespace=? AND kind='preview' AND status='succeeded' "
                               "ORDER BY created DESC,id DESC LIMIT 1", (self.namespace,)).fetchone()
        return self._job(row, include_result=True) if row else None

    def normalization_files(self):
        with self.connection() as connection:
            rows = connection.execute("SELECT path,data FROM normalization_files WHERE namespace=?", (self.namespace,)).fetchall()
        return {row["path"]: json.loads(row["data"]) for row in rows}

    def cached(self, key):
        with self.connection() as connection:
            row = connection.execute("SELECT data,updated,revision FROM tool_cache WHERE namespace=? AND key=?",
                                     (self.namespace, key)).fetchone()
        return {"data": json.loads(row["data"]), "updated": row["updated"], "revision": row["revision"]} if row else None

    def cache(self, key, data, *, revision=None):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM tool_cache WHERE namespace=? AND key=?", (self.namespace, key)).fetchone()
            actual = row[0] if row else 0
            if revision is not None and revision != actual:
                raise StateConflict("別の操作で更新されました。画面を読み直してください。")
            connection.execute("INSERT INTO tool_cache VALUES(?,?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET "
                               "data=excluded.data,updated=excluded.updated,revision=excluded.revision",
                               (self.namespace, key, json.dumps(data, ensure_ascii=False), timestamp(), actual + 1))
        return actual + 1

    def cached_keys(self, prefix):
        with self.connection() as connection:
            rows = connection.execute("SELECT key FROM tool_cache WHERE namespace=? AND substr(key,1,?)=? ORDER BY key",
                                      (self.namespace, len(prefix), prefix)).fetchall()
        return [row[0] for row in rows]

    def cached_revision(self, key):
        with self.connection() as connection:
            row = connection.execute("SELECT revision FROM tool_cache WHERE namespace=? AND key=?", (self.namespace, key)).fetchone()
        return row[0] if row else 0

    def cached_prefix(self, prefix):
        with self.connection() as connection:
            rows = connection.execute("SELECT key,data,updated,revision FROM tool_cache WHERE namespace=? AND substr(key,1,?)=? ORDER BY key",
                                      (self.namespace, len(prefix), prefix)).fetchall()
        return {row["key"]: {"data": json.loads(row["data"]), "updated": row["updated"], "revision": row["revision"]} for row in rows}

    def remember_normalization(self, values, removed=()):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("INSERT INTO normalization_files VALUES(?,?,?) ON CONFLICT(namespace,path) DO UPDATE SET data=excluded.data",
                                   [(self.namespace, path, json.dumps(value, sort_keys=True)) for path, value in values.items()])
            connection.executemany("DELETE FROM normalization_files WHERE namespace=? AND path=?",
                                   [(self.namespace, path) for path in removed])

    def latest_normalization(self):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE namespace=? AND kind='normalization_check' "
                                     "ORDER BY created DESC,id DESC LIMIT 1", (self.namespace,)).fetchone()
        return self._job(row, include_result=True) if row else None

    def recover(self):
        # A single application process owns workers. Never interrupt a living process.
        if os.name != "posix":
            # Signal 0 is an existence probe only on POSIX. Do not signal Windows processes.
            return
        with self.connection() as conn:
            rows = conn.execute("SELECT id,owner FROM jobs WHERE namespace=? AND status IN ('queued','running')",
                                (self.namespace,)).fetchall()
        for row in rows:
            owner = json.loads(row["owner"])
            interrupted = False
            try:
                os.kill(owner["pid"], 0)
                actual_start = process_start(owner["pid"])
                interrupted = bool(owner["start"] and actual_start and owner["start"] != actual_start)
            except ProcessLookupError:
                interrupted = True
            except PermissionError:
                pass
            if interrupted:
                self.update_job(row["id"], status="interrupted", error="処理中にサーバーが停止しました。再実行してください。")
