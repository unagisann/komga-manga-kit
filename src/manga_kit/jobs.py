"""A bounded local worker for scan/preview jobs with durable progress and results."""
from concurrent.futures import ThreadPoolExecutor
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
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manga-kit")
            self._refresh_future = self._executor.submit(self._refresh_cached)

    def _refresh_cached(self):
        try:
            self.library.scan(background=False)
        except Exception:
            logger.warning("蔵書の再読み取りに失敗しました。前回の結果を表示しています。保存先を確認してください。")

    def submit(self, kind, layout=None):
        if not isinstance(kind, str) or kind not in {"scan", "preview"}:
            raise ConfigError("処理は scan または preview を指定してください。")
        if layout is not None and not isinstance(layout, dict):
            raise ConfigError("layout はマッピングで指定してください。")
        rules = self.library.settings.layout.with_overrides(layout or {})
        with self._lock:
            if self._closed:
                raise ConfigError("処理の受付は終了しています。")
            if self._pending >= 20:
                raise ConfigError("処理待ちが20件に達しています。完了後に再実行してください。")
            if self._executor is None:
                self.store.recover()
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manga-kit")
            job_id = self.store.create_job(kind)
            self._pending += 1
            self._futures[job_id] = self._executor.submit(self._run, job_id, kind, rules)
        return job_id

    def _run(self, job_id, kind, rules):
        last_update = 0.0
        def progress(current):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= 0.2:
                self.store.update_job(job_id, current=current)
                last_update = now
        try:
            self.store.update_job(job_id, status="running")
            # Preview still checks every current file and sidecar, but unchanged
            # archives need not be opened. Execution reviews keep the strict scan.
            if kind == "preview":
                scanned = self.library.scan(refresh=True, progress=progress, reuse_metadata=True)
            else:
                scanned = self.library.scan(refresh=True, progress=progress)
            settings = self.library.settings
            total = scanned.stats()["books"]
            self.store.update_job(job_id, current=total, total=total)
            if kind == "scan":
                result = {"stats": scanned.stats(), "warnings": scanned.warnings,
                          "truncated": scanned.truncated, "demo": settings.demo}
            else:
                result = plan_organization(scanned.series, rules, None if settings.demo else settings.library_root)
                result.update(truncated=scanned.truncated, warnings=scanned.warnings, layout=rules.__dict__)
            self.store.update_job(job_id, status="succeeded", current=total, total=total, result=result)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ConfigError) else "処理に失敗しました。保存先のアクセス権と設定を確認してください。"
            self.store.update_job(job_id, status="failed", error=error)
        finally:
            with self._lock:
                self._pending -= 1
                self._futures.pop(job_id, None)

    def submit_task(self, kind, task):
        """Queue an application-owned task; HTTP callers cannot supply callables."""
        with self._lock:
            if self._closed or self._pending >= 20:
                raise ConfigError("処理を受け付けられません。処理の完了後に再実行してください。")
            if self._executor is None:
                self.store.recover()
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manga-kit")
            job_id = self.store.create_job(kind)
            self._pending += 1
            self._futures[job_id] = self._executor.submit(self._run_task, job_id, task)
        return job_id

    def _run_task(self, job_id, task):
        try:
            self.store.update_job(job_id, status="running")
            result = task(lambda current, total: self.store.update_job(job_id, current=current, total=total))
            self.store.update_job(job_id, status="succeeded", result=result)
        except Exception as error:
            message = str(error) if isinstance(error, (ConfigError, StateConflict, CatalogError, KomgaError)) else "処理を完了できませんでした。処理履歴を確認して再実行してください。"
            self.store.update_job(job_id, status="failed", error=message)
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
