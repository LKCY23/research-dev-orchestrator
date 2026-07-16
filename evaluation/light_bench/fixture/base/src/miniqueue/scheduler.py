"""Small pull scheduler built on Queue leases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .clock import Clock, SystemClock
from .errors import InvalidJobError
from .model import Job
from .queue import Queue


Handler = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class RunResult:
    """Outcome of one scheduler polling cycle."""

    status: str
    job_id: str | None = None
    result: Any = None
    error: str | None = None


class Scheduler:
    """Execute at most one eligible job per call.

    Handlers are selected by the payload's required ``kind`` string. The
    scheduler owns only execution and acknowledgement; ordering, leases, and
    retries stay in Queue where they remain independently testable.
    """

    def __init__(
        self,
        queue: Queue,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str,
        clock: Clock | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise InvalidJobError("worker_id must be a non-empty string")
        if not handlers:
            raise InvalidJobError("at least one handler is required")
        for kind, handler in handlers.items():
            if not isinstance(kind, str) or not kind:
                raise InvalidJobError("handler kinds must be non-empty strings")
            if not callable(handler):
                raise InvalidJobError(f"handler for {kind!r} is not callable")
        self.queue = queue
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.clock = clock or SystemClock()
        self.lease_seconds = lease_seconds

    def run_once(self) -> RunResult:
        job = self.queue.lease_next(
            self.worker_id,
            ttl_seconds=self.lease_seconds,
        )
        if job is None:
            return RunResult(status="idle")
        kind = job.payload.get("kind")
        if not isinstance(kind, str) or not kind:
            return self._fail(job, "payload field 'kind' must be a non-empty string")
        handler = self.handlers.get(kind)
        if handler is None:
            return self._fail(job, f"no handler registered for kind {kind!r}")
        try:
            result = handler(dict(job.payload))
        except Exception as exc:  # fixture intentionally turns handler errors into retries
            message = f"{type(exc).__name__}: {exc}"
            return self._fail(job, message)
        self.queue.acknowledge(job.job_id, self.worker_id)
        return RunResult(status="succeeded", job_id=job.job_id, result=result)

    def run_until_idle(self, *, max_jobs: int = 100) -> list[RunResult]:
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs < 1:
            raise InvalidJobError("max_jobs must be a positive integer")
        results: list[RunResult] = []
        for _ in range(max_jobs):
            result = self.run_once()
            if result.status == "idle":
                break
            results.append(result)
        return results

    def _fail(self, job: Job, message: str) -> RunResult:
        self.queue.fail(job.job_id, self.worker_id, message)
        return RunResult(
            status="failed",
            job_id=job.job_id,
            error=message,
        )
