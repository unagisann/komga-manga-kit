"""A bounded FIFO worker with shared admission, deduplication and progress."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import logging
from threading import Lock
import time

from .config import ConfigError
from .catalogs import CatalogError
from .komga import KomgaError
from .state import StateConflict
from .organizer import plan_organization

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, library, store):
        self.library, self.store = library, store
        self._executor, self._lock, self._pending = None, Lock(), 0
        self._futures = {}
        self._refresh_future, self._next_refresh, self._closed = None, 0.0, False

    def refresh_cached(self):
        """One incremental refresh; browsing keeps the last published snapshot."""
        with self._lock:
            now = time.monotonic()
            if (self._closed or self._pending >= 20 or now < self._next_refresh
                    or self._refresh_future is not None and not self._refresh_future.done()):
                return
            self._next_refresh = now + 30
            try:
                job_id = self._submit_locked("scan_refresh", self._refresh_cached, key="library")
                self._refresh_future = self._futures.get(job_id)
            except ConfigError:
                return

    def _refresh_cached(self, progress):
        try:
            self.library.scan(background=False)
        except Exception:
            logger.warning("蔵書の再読み取りに失敗しました。前回の結果を表示しています。保存先を確認してください。")
            raise ConfigError("蔵書の再読み取りに失敗しました。前回の結果を表示しています。") from None
        return {"refreshed": True}

    def submit(self, kind, layout=None):
        if not isinstance(kind, str) or kind not in {"scan", "preview"}:
            raise ConfigError("処理は scan または preview を指定してください。")
        if layout is not None and not isinstance(layout, dict):
            raise ConfigError("layout はマッピングで指定してください。")
        rules = self.library.settings.layout.with_overrides(layout or {})
        return self.submit_task(kind, lambda progress: self._scan(kind, rules, progress),
                                key=asdict(rules) if kind == "preview" else "library")

    def _scan(self, kind, rules, report):
        last_update = 0.0
        def progress(current):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= 0.2:
                report(current, None)
                last_update = now
        # Preview checks every current file/sidecar without reopening unchanged
        # archives. Explicit scans and execution reviews keep the strict scan.
        scanned = self.library.scan(refresh=True, progress=progress, **({"reuse_metadata": True} if kind == "preview" else {}))
        settings = self.library.settings
        total = scanned.stats()["books"]
        report(total, total)
        if kind == "scan":
            return {"stats": scanned.stats(), "warnings": scanned.warnings,
                    "truncated": scanned.truncated, "demo": settings.demo}
        result = plan_organization(scanned.series, rules, None if settings.demo else settings.library_root)
        result.update(truncated=scanned.truncated, warnings=scanned.warnings, layout=asdict(rules))
        return result

    def submit_task(self, kind, task, *, key=None, child_kind=None):
        """Queue application-owned work; equal active keys share one job.

        A compound task may reserve a child history record in the same admission
        transaction. Its callable receives (progress, child_id).
        """
        with self._lock:
            return self._submit_locked(kind, task, key=key, child_kind=child_kind)

    def _submit_locked(self, kind, task, *, key=None, child_kind=None):
        if self._closed:
            raise ConfigError("処理の受付は終了しています。")
        self.store.recover()
        task_key = hashlib.sha256(json.dumps([kind, key, child_kind], sort_keys=True,
                                            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        job_id, child_id, created = self.store.enqueue_job(kind, task_key, child_kind=child_kind)
        if created:
            try:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manga-kit")
                work = (lambda progress: task(progress, child_id)) if child_id else task
                self._futures[job_id] = self._executor.submit(self._run_task, job_id, work, child_id)
            except Exception:
                self.store.update_job(job_id, status="failed", error="処理を受け付けられませんでした。再実行してください。")
                if child_id:
                    self.store.update_job(child_id, status="failed", error="処理を受け付けられませんでした。再実行してください。")
                raise
            self._pending += 1
        return job_id

    def _run_task(self, job_id, task, child_id=None):
        try:
            while True:
                status = self.store.start_queued_job(job_id)
                if status == "started":
                    break
                if status != "queued":
                    return
                time.sleep(0.1)
                self.store.recover()
            result = task(lambda current, total: self.store.update_job(job_id, current=current, total=total))
            self.store.update_job(job_id, status="succeeded", result=result)
        except Exception as error:
            message = str(error) if isinstance(error, (ConfigError, StateConflict, CatalogError, KomgaError)) else "処理を完了できませんでした。処理履歴を確認して再実行してください。"
            self.store.update_job(job_id, status="failed", error=message)
        finally:
            try:
                if child_id:
                    child = self.store.job(child_id)
                    if child and child["status"] in {"queued", "running"}:
                        self.store.update_job(child_id, status="interrupted", error="親の処理が終了しました。処理履歴から確認してください。")
            finally:
                with self._lock:
                    self._pending -= 1
                    self._futures.pop(job_id, None)

    def shutdown(self):
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True)
