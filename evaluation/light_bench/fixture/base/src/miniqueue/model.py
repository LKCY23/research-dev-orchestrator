"""Serializable domain models for MiniQueue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from .errors import InvalidJobError


class JobState(str, Enum):
    """Durable lifecycle states.

    A lease is represented explicitly so a process restart cannot accidentally
    make an in-flight job available before its deadline.
    """

    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


def _require_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidJobError(f"{name} must be numeric")
    number = float(value)
    if minimum is not None and number < minimum:
        raise InvalidJobError(f"{name} must be at least {minimum}")
    return number


def _require_integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidJobError(f"{name} must be an integer")
    if value < minimum:
        raise InvalidJobError(f"{name} must be at least {minimum}")
    return value


def validate_payload(payload: Any, *, max_bytes: int) -> None:
    """Require a JSON object within the configured encoded-size boundary."""

    if not isinstance(payload, dict):
        raise InvalidJobError("payload must be a JSON object")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidJobError(f"payload is not JSON serializable: {exc}") from exc
    if len(encoded) > max_bytes:
        raise InvalidJobError(
            f"payload is {len(encoded)} bytes; maximum is {max_bytes}"
        )


@dataclass
class Job:
    """One durable unit of work."""

    job_id: str
    payload: dict[str, Any]
    priority: int
    created_at: float
    available_at: float
    max_attempts: int
    state: JobState = JobState.QUEUED
    attempts: int = 0
    lease_owner: str | None = None
    leased_until: float | None = None
    last_error: str | None = None
    completed_at: float | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise InvalidJobError("job_id must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise InvalidJobError("payload must be a mapping")
        self.priority = _require_integer(self.priority, "priority", minimum=-100)
        if self.priority > 100:
            raise InvalidJobError("priority must be at most 100")
        self.created_at = _require_number(self.created_at, "created_at", minimum=0)
        self.available_at = _require_number(
            self.available_at, "available_at", minimum=0
        )
        self.max_attempts = _require_integer(
            self.max_attempts, "max_attempts", minimum=1
        )
        self.attempts = _require_integer(self.attempts, "attempts", minimum=0)
        if self.attempts > self.max_attempts:
            raise InvalidJobError("attempts cannot exceed max_attempts")
        if not isinstance(self.state, JobState):
            try:
                self.state = JobState(self.state)
            except ValueError as exc:
                raise InvalidJobError(f"unknown job state: {self.state!r}") from exc
        if not isinstance(self.tags, tuple):
            self.tags = tuple(self.tags)
        if any(not isinstance(tag, str) or not tag for tag in self.tags):
            raise InvalidJobError("tags must be non-empty strings")
        if len(set(self.tags)) != len(self.tags):
            raise InvalidJobError("tags must be unique")
        self._validate_state_fields()

    def _validate_state_fields(self) -> None:
        if self.state is JobState.LEASED:
            if not self.lease_owner or self.leased_until is None:
                raise InvalidJobError("leased jobs require an owner and deadline")
        elif self.lease_owner is not None or self.leased_until is not None:
            raise InvalidJobError("only leased jobs may carry lease fields")
        if self.state in (JobState.SUCCEEDED, JobState.DEAD):
            if self.completed_at is None:
                raise InvalidJobError("terminal jobs require completed_at")
        elif self.completed_at is not None:
            raise InvalidJobError("non-terminal jobs cannot have completed_at")

    @property
    def terminal(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.DEAD)

    def clone(self) -> "Job":
        return Job.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "payload": dict(self.payload),
            "priority": self.priority,
            "created_at": self.created_at,
            "available_at": self.available_at,
            "max_attempts": self.max_attempts,
            "state": self.state.value,
            "attempts": self.attempts,
            "lease_owner": self.lease_owner,
            "leased_until": self.leased_until,
            "last_error": self.last_error,
            "completed_at": self.completed_at,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Job":
        required = {
            "job_id",
            "payload",
            "priority",
            "created_at",
            "available_at",
            "max_attempts",
            "state",
            "attempts",
            "lease_owner",
            "leased_until",
            "last_error",
            "completed_at",
            "tags",
        }
        missing = required - set(value)
        unknown = set(value) - required
        if missing or unknown:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(sorted(missing)))
            if unknown:
                detail.append("unknown " + ", ".join(sorted(unknown)))
            raise InvalidJobError("invalid serialized job: " + "; ".join(detail))
        return cls(
            job_id=value["job_id"],
            payload=dict(value["payload"]),
            priority=value["priority"],
            created_at=value["created_at"],
            available_at=value["available_at"],
            max_attempts=value["max_attempts"],
            state=JobState(value["state"]),
            attempts=value["attempts"],
            lease_owner=value["lease_owner"],
            leased_until=value["leased_until"],
            last_error=value["last_error"],
            completed_at=value["completed_at"],
            tags=tuple(value["tags"]),
        )


@dataclass
class QueueSnapshot:
    """Revisioned store value."""

    revision: int = 0
    jobs: dict[str, Job] = field(default_factory=dict)

    def clone(self) -> "QueueSnapshot":
        return QueueSnapshot(
            revision=self.revision,
            jobs={job_id: job.clone() for job_id, job in self.jobs.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "jobs": [
                self.jobs[job_id].to_dict() for job_id in sorted(self.jobs)
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QueueSnapshot":
        if value.get("schema_version") != 1:
            raise InvalidJobError("unsupported queue snapshot schema")
        revision = _require_integer(value.get("revision"), "revision", minimum=0)
        raw_jobs = value.get("jobs")
        if not isinstance(raw_jobs, list):
            raise InvalidJobError("snapshot jobs must be an array")
        jobs: dict[str, Job] = {}
        for raw_job in raw_jobs:
            if not isinstance(raw_job, dict):
                raise InvalidJobError("serialized job must be an object")
            job = Job.from_dict(raw_job)
            if job.job_id in jobs:
                raise InvalidJobError(f"duplicate job id {job.job_id!r}")
            jobs[job.job_id] = job
        return cls(revision=revision, jobs=jobs)


@dataclass(frozen=True)
class QueueStats:
    """Point-in-time counts returned by Queue.stats()."""

    queued: int
    leased: int
    succeeded: int
    dead: int

    @property
    def total(self) -> int:
        return self.queued + self.leased + self.succeeded + self.dead

    @classmethod
    def from_jobs(cls, jobs: Iterable[Job]) -> "QueueStats":
        counts = {state: 0 for state in JobState}
        for job in jobs:
            counts[job.state] += 1
        return cls(
            queued=counts[JobState.QUEUED],
            leased=counts[JobState.LEASED],
            succeeded=counts[JobState.SUCCEEDED],
            dead=counts[JobState.DEAD],
        )
