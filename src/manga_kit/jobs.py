"""A bounded local worker for scan/preview jobs with durable progress and results."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time

from .config import ConfigError
from .organizer import plan_organization


class JobService:
    def __init__(self, library, store):
        self.library, self.store = library, store
        self._executor, self._lock, self._pending = None, Lock(), 0
        self._futures = {}

    def submit(self, kind, layout=None):
        if not isinstance(kind, str) or kind not in {"scan", "preview"}:
            raise ConfigError("処理は scan または preview を指定してください。")
        if layout is not None and not isinstance(layout, dict):
            raise ConfigError("layout はマッピングで指定してください。")
        rules = self.library.settings.layout.with_overrides(layout or {})
        with self._lock:
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
            scanned = self.library.scan(refresh=True, progress=progress)
            settings = self.library.settings
            if kind == "scan":
                result = {"stats": scanned.stats(), "warnings": scanned.warnings,
                          "truncated": scanned.truncated, "demo": settings.demo}
            else:
                result = plan_organization(scanned.series, rules, None if settings.demo else settings.library_root)
                result.update(truncated=scanned.truncated, warnings=scanned.warnings, layout=rules.__dict__)
            total = scanned.stats()["books"]
            self.store.update_job(job_id, status="succeeded", current=total, total=total, result=result)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ConfigError) else "処理に失敗しました。保存先のアクセス権と設定を確認してください。"
            self.store.update_job(job_id, status="failed", error=error)
        finally:
            with self._lock:
                self._pending -= 1
                self._futures.pop(job_id, None)

    def shutdown(self):
        with self._lock:
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True)
