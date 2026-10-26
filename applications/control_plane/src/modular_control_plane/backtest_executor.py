from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock

from .backtest import BacktestControlService, BacktestJobRecord
from .contracts import JobStatus


class BacktestJobExecutor:
    """Single-process executor for durable Backtest jobs.

    The durable SQLite store remains the source of truth. This executor only owns local worker
    scheduling: API requests enqueue a job after durable creation, and server startup re-enqueues
    persisted QUEUED jobs. Duplicate submissions are collapsed while a job is already scheduled.
    """

    def __init__(self, service: BacktestControlService, *, max_workers: int = 1) -> None:
        if not 1 <= max_workers <= 4:
            raise ValueError("Backtest worker count must be between 1 and 4")
        self._service = service
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="modular-backtest-control",
        )
        self._lock = Lock()
        self._scheduled: dict[str, Future[BacktestJobRecord]] = {}
        self._closed = False

    def submit(self, job_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Backtest job executor is shut down")
            existing = self._scheduled.get(job_id)
            if existing is not None and not existing.done():
                return
            future = self._pool.submit(self._service.run_job, job_id)
            self._scheduled[job_id] = future

        def forget_completed(completed: Future[BacktestJobRecord]) -> None:
            self._forget(job_id, completed)

        future.add_done_callback(forget_completed)

    def recover_queued(self) -> int:
        submitted = 0
        offset = 0
        while True:
            page = self._service.list_jobs(limit=200, offset=offset)
            if not page:
                break
            for record in page:
                if record.job.status is JobStatus.QUEUED:
                    self.submit(record.job.job_id)
                    submitted += 1
            if len(page) < 200:
                break
            offset += len(page)
        return submitted

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=False)

    def _forget(self, job_id: str, completed: Future[BacktestJobRecord]) -> None:
        with self._lock:
            current = self._scheduled.get(job_id)
            if current is completed:
                self._scheduled.pop(job_id, None)
