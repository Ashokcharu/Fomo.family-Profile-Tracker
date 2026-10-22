from __future__ import annotations

from datetime import datetime, timedelta


class RefreshScheduler:
    """Supports a regular refresh cadence and concurrency limits for background sync jobs."""

    def __init__(self, refresh_interval_seconds: int = 60, max_concurrent_jobs: int = 10):
        self.refresh_interval_seconds = max(1, int(refresh_interval_seconds))
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self._running_jobs = 0

    def next_run_at(self, last_run_at: datetime | None = None) -> datetime:
        reference = last_run_at or datetime.utcnow()
        return reference + timedelta(seconds=self.refresh_interval_seconds)

    def is_due(self, last_run_at: datetime | None = None, now: datetime | None = None) -> bool:
        now = now or datetime.utcnow()
        if last_run_at is None:
            return True
        return now >= self.next_run_at(last_run_at)

    def can_start(self) -> bool:
        return self._running_jobs < self.max_concurrent_jobs

    def acquire(self) -> bool:
        if not self.can_start():
            return False
        self._running_jobs += 1
        return True

    def release(self) -> None:
        self._running_jobs = max(0, self._running_jobs - 1)

    def mark_started(self) -> bool:
        return self.acquire()

    def mark_finished(self) -> None:
        self.release()

    def __enter__(self) -> "RefreshScheduler":
        if not self.acquire():
            raise RuntimeError("refresh scheduler exceeded max_concurrent_jobs")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
