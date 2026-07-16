"""Persistent queue operations and lease lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from uuid import uuid4

from .clock import Clock, SystemClock
from .errors import ConflictError, InvalidJobError, JobNotFoundError, LeaseError
from .model import Job, JobState, QueueSnapshot, QueueStats, validate_payload
from .retry import RetryPolicy
from .store import MemoryStore, Store


@dataclass(frozen=True)
class QueueConfig:
    """Queue-wide behavior with deliberately conservative limits."""

    default_lease_seconds: float = 30.0
    default_max_attempts: int = 3
    max_payload_bytes: int = 16_384
    mutation_retries: int = 4

    def __post_init__(self) -> None:
        if (
            isinstance(self.default_lease_seconds, bool)
            or not isinstance(self.default_lease_seconds, (int, float))
            or self.default_lease_seconds <= 0
        ):
            raise InvalidJobError("default_lease_seconds must be positive")
        for name, value in (
            ("default_max_attempts", self.default_max_attempts),
            ("max_payload_bytes", self.max_payload_bytes),
            ("mutation_retries", self.mutation_retries),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InvalidJobError(f"{name} must be a positive integer")


T = TypeVar("T")


class Queue:
    """Revision-safe job queue.

    Queue methods load a fresh snapshot for each mutation and retry bounded
    optimistic conflicts. Returned Job objects are clones so callers cannot
    mutate durable state without an explicit operation.
    """

    def __init__(
        self,
        store: Store | None = None,
        *,
        clock: Clock | None = None,
        config: QueueConfig | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.store = store or MemoryStore()
        self.clock = clock or SystemClock()
        self.config = config or QueueConfig()
        self.retry_policy = retry_policy or RetryPolicy()

    def enqueue(
        self,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        priority: int = 0,
        delay_seconds: float = 0,
        max_attempts: int | None = None,
        tags: tuple[str, ...] = (),
    ) -> Job:
        validate_payload(payload, max_bytes=self.config.max_payload_bytes)
        identifier = job_id or uuid4().hex
        if isinstance(delay_seconds, bool) or not isinstance(
            delay_seconds, (int, float)
        ):
            raise InvalidJobError("delay_seconds must be numeric")
        if delay_seconds < 0:
            raise InvalidJobError("delay_seconds cannot be negative")
        attempt_limit = (
            self.config.default_max_attempts
            if max_attempts is None
            else max_attempts
        )
        now = self.clock.now()
        job = Job(
            job_id=identifier,
            payload=dict(payload),
            priority=priority,
            created_at=now,
            available_at=now + float(delay_seconds),
            max_attempts=attempt_limit,
            tags=tuple(tags),
        )

        def mutate(snapshot: QueueSnapshot) -> Job:
            if identifier in snapshot.jobs:
                raise InvalidJobError(f"job id already exists: {identifier!r}")
            snapshot.jobs[identifier] = job.clone()
            return job.clone()

        return self._mutate(mutate)

    def get(self, job_id: str) -> Job:
        job = self.store.load().jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job.clone()

    def list_jobs(self, *, state: JobState | None = None) -> list[Job]:
        jobs = self.store.load().jobs.values()
        selected = [job.clone() for job in jobs if state is None or job.state is state]
        return sorted(selected, key=lambda job: (job.created_at, job.job_id))

    def lease_next(
        self,
        worker_id: str,
        *,
        ttl_seconds: float | None = None,
        required_tags: tuple[str, ...] = (),
    ) -> Job | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise InvalidJobError("worker_id must be a non-empty string")
        ttl = self._resolve_lease_seconds(ttl_seconds)
        requested_tags = set(required_tags)
        now = self.clock.now()

        def mutate(snapshot: QueueSnapshot) -> Job | None:
            self._release_expired(snapshot, now)
            eligible = [
                job
                for job in snapshot.jobs.values()
                if job.state is JobState.QUEUED
                and job.available_at <= now
                and requested_tags.issubset(job.tags)
            ]
            if not eligible:
                return None
            eligible.sort(
                key=lambda job: (-job.priority, job.available_at, job.created_at, job.job_id)
            )
            job = eligible[0]
            job.state = JobState.LEASED
            job.attempts += 1
            job.lease_owner = worker_id
            job.leased_until = now + ttl
            return job.clone()

        return self._mutate(mutate, save_if_unchanged=False)

    def renew(self, job_id: str, worker_id: str, *, ttl_seconds: float | None = None) -> Job:
        ttl = self._resolve_lease_seconds(ttl_seconds)
        now = self.clock.now()

        def mutate(snapshot: QueueSnapshot) -> Job:
            job = self._require_job(snapshot, job_id)
            self._require_lease(job, worker_id, now)
            job.leased_until = now + ttl
            return job.clone()

        return self._mutate(mutate)

    def acknowledge(self, job_id: str, worker_id: str) -> Job:
        now = self.clock.now()

        def mutate(snapshot: QueueSnapshot) -> Job:
            job = self._require_job(snapshot, job_id)
            self._require_lease(job, worker_id, now)
            job.state = JobState.SUCCEEDED
            job.lease_owner = None
            job.leased_until = None
            job.completed_at = now
            job.last_error = None
            return job.clone()

        return self._mutate(mutate)

    def fail(self, job_id: str, worker_id: str, error: str) -> Job:
        if not isinstance(error, str) or not error.strip():
            raise InvalidJobError("error must be a non-empty string")
        now = self.clock.now()

        def mutate(snapshot: QueueSnapshot) -> Job:
            job = self._require_job(snapshot, job_id)
            self._require_lease(job, worker_id, now)
            job.lease_owner = None
            job.leased_until = None
            job.last_error = error.strip()
            if job.attempts >= job.max_attempts:
                job.state = JobState.DEAD
                job.completed_at = now
            else:
                job.state = JobState.QUEUED
                job.available_at = now + self.retry_policy.delay_for_attempt(
                    job.attempts, key=job.job_id
                )
            return job.clone()

        return self._mutate(mutate)

    def release_expired(self) -> int:
        now = self.clock.now()

        def mutate(snapshot: QueueSnapshot) -> int:
            return self._release_expired(snapshot, now)

        return self._mutate(mutate, save_if_unchanged=False)

    def stats(self) -> QueueStats:
        return QueueStats.from_jobs(self.store.load().jobs.values())

    def _resolve_lease_seconds(self, requested: float | None) -> float:
        if requested is None:
            return float(self.config.default_lease_seconds)
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise InvalidJobError("ttl_seconds must be numeric")
        if requested <= 0:
            raise InvalidJobError("ttl_seconds must be positive")
        return float(requested)

    @staticmethod
    def _require_job(snapshot: QueueSnapshot, job_id: str) -> Job:
        job = snapshot.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    @staticmethod
    def _require_lease(job: Job, worker_id: str, now: float) -> None:
        if job.state is not JobState.LEASED:
            raise LeaseError(f"job {job.job_id!r} is not leased")
        if job.lease_owner != worker_id:
            raise LeaseError(f"worker {worker_id!r} does not own job {job.job_id!r}")
        if job.leased_until is None or job.leased_until <= now:
            raise LeaseError(f"lease for job {job.job_id!r} has expired")

    def _release_expired(self, snapshot: QueueSnapshot, now: float) -> int:
        released = 0
        for job in snapshot.jobs.values():
            if (
                job.state is JobState.LEASED
                and job.leased_until is not None
                and job.leased_until <= now
            ):
                job.state = JobState.QUEUED
                job.lease_owner = None
                job.leased_until = None
                job.available_at = now
                released += 1
        return released

    def _mutate(
        self,
        operation: Callable[[QueueSnapshot], T],
        *,
        save_if_unchanged: bool = True,
    ) -> T:
        conflict: ConflictError | None = None
        for _ in range(self.config.mutation_retries):
            snapshot = self.store.load()
            before = snapshot.to_dict()
            result = operation(snapshot)
            if not save_if_unchanged and snapshot.to_dict() == before:
                return result
            expected = snapshot.revision
            snapshot.revision += 1
            try:
                self.store.save(snapshot, expected_revision=expected)
            except ConflictError as exc:
                conflict = exc
                continue
            return result
        assert conflict is not None
        raise conflict
