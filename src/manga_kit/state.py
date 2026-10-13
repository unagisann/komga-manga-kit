"""Private SQLite state, separated by library and independent of the source tree."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4

from .config import ConfigError


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
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.is_symlink():
                    raise ConfigError("実行データのDBにシンボリックリンクは使えません。")
                conn = sqlite3.connect(self.path, timeout=15)
                try:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version not in {0, 1, 2}:
                        raise ConfigError("実行データの形式が対応バージョンと異なります。")
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS jobs (
                            id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
                            status TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 0, total INTEGER,
                            created TEXT NOT NULL, updated TEXT NOT NULL, owner TEXT NOT NULL,
                            result TEXT, error TEXT);
                        CREATE INDEX IF NOT EXISTS jobs_by_library ON jobs(namespace, created);
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
                        PRAGMA user_version=2;
                    """)
                    conn.commit()
                finally:
                    conn.close()
                self.path.chmod(0o600)
                self._initialized = True
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def edits(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM edits WHERE namespace=?", (self.namespace,)).fetchall()
        return {row["series_id"]: {"fields": json.loads(row["fields"]), "revision": row["revision"]} for row in rows}

    def save_edit(self, series_id, fields, revision, detail):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision FROM edits WHERE namespace=? AND series_id=?",
                               (self.namespace, series_id)).fetchone()
            actual = row["revision"] if row else 0
            if revision != actual:
                raise StateConflict("別の操作で作品情報が更新されました。差分を再確認してください。")
            conn.execute("INSERT INTO edits VALUES(?,?,?,?,?) ON CONFLICT(namespace,series_id) DO UPDATE SET "
                         "fields=excluded.fields,revision=excluded.revision,updated=excluded.updated",
                         (self.namespace, series_id, json.dumps(fields, ensure_ascii=False), actual + 1, timestamp()))
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
            actual = {row["series_id"]: {"fields": json.loads(row["fields"]), "revision": row["revision"]} for row in rows}
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
                    conn.execute("INSERT INTO edits VALUES(?,?,?,?,?) ON CONFLICT(namespace,series_id) DO UPDATE SET "
                        "fields=excluded.fields,revision=excluded.revision,updated=excluded.updated",
                        (self.namespace, key, json.dumps(value, ensure_ascii=False), expected[key]["revision"] + 1, timestamp()))
                    result[key] = {"fields": value, "revision": expected[key]["revision"] + 1}
                self._event(conn, "metadata_relocated", source, {"target": target, "restore": restore})
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
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        job_id, now = uuid4().hex, timestamp()
        with self.connection() as conn:
            conn.execute("INSERT INTO jobs(id,namespace,kind,status,created,updated,owner) VALUES(?,?,?,?,?,?,?)",
                         (job_id, self.namespace, kind, "queued", now, now,
                          json.dumps({"pid": os.getpid(), "start": process_start(os.getpid())})))
            self._event(conn, "job_created", job_id, {"kind": kind})
        return job_id

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
        if include_result:
            job["result"] = json.loads(row["result"]) if row["result"] else None
        return job

    def job(self, job_id, include_result=False):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE namespace=? AND id=?", (self.namespace, job_id)).fetchone()
        return self._job(row, include_result) if row else None

    def jobs(self, limit=50, offset=0):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE namespace=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                                (self.namespace, limit, offset)).fetchall()
        return [self._job(row) for row in rows]

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
