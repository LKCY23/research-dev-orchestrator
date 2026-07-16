"""Revisioned in-memory and JSON stores."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Protocol

from .errors import ConflictError, InvalidJobError
from .model import QueueSnapshot


class Store(Protocol):
    """Storage interface required by Queue."""

    def load(self) -> QueueSnapshot:
        """Return an isolated snapshot."""

    def save(self, snapshot: QueueSnapshot, *, expected_revision: int) -> None:
        """Persist snapshot if the current revision matches."""


class MemoryStore:
    """Thread-safe process-local store used by most tests."""

    def __init__(self, initial: QueueSnapshot | None = None) -> None:
        self._lock = RLock()
        self._snapshot = (initial or QueueSnapshot()).clone()

    def load(self) -> QueueSnapshot:
        with self._lock:
            return self._snapshot.clone()

    def save(self, snapshot: QueueSnapshot, *, expected_revision: int) -> None:
        with self._lock:
            if self._snapshot.revision != expected_revision:
                raise ConflictError(
                    "store revision changed: "
                    f"expected {expected_revision}, found {self._snapshot.revision}"
                )
            if snapshot.revision != expected_revision + 1:
                raise ConflictError("saved snapshot must advance revision by exactly one")
            self._snapshot = snapshot.clone()


class JsonStore:
    """Small durable store using atomic replace.

    The fixture deliberately targets a single process. Revision validation
    catches stale Queue instances within that process, while atomic replace
    prevents readers from observing partially written JSON.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def load(self) -> QueueSnapshot:
        with self._lock:
            if not self.path.exists():
                return QueueSnapshot()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidJobError(f"cannot load queue snapshot: {exc}") from exc
            if not isinstance(raw, dict):
                raise InvalidJobError("queue snapshot must be a JSON object")
            return QueueSnapshot.from_dict(raw)

    def save(self, snapshot: QueueSnapshot, *, expected_revision: int) -> None:
        with self._lock:
            current = self.load()
            if current.revision != expected_revision:
                raise ConflictError(
                    "store revision changed: "
                    f"expected {expected_revision}, found {current.revision}"
                )
            if snapshot.revision != expected_revision + 1:
                raise ConflictError("saved snapshot must advance revision by exactly one")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                snapshot.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ) + "\n"
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
