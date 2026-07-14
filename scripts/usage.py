#!/usr/bin/env python3
"""Normalize structured backend usage and enforce attempt-local hard budgets."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from protocol import utc_now


def _number(mapping: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return 0.0


def _event_id(payload: dict[str, Any], info: dict[str, Any]) -> str:
    for mapping in (info, payload.get("message") or {}, payload.get("item") or {}, payload):
        if isinstance(mapping, dict):
            for key in ("id", "message_id", "messageID", "turn_id", "turnId"):
                value = mapping.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def normalize_usage_event(backend: str, payload: Any) -> dict[str, Any] | None:
    """Return one completed model-turn usage record, or a cost-only terminal record."""
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("type") or "")
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    info = properties.get("info") if isinstance(properties.get("info"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}

    completed = False
    if backend == "opencode":
        role = str(info.get("role") or "")
        timing = info.get("time") if isinstance(info.get("time"), dict) else {}
        completed = role == "assistant" and bool(timing.get("completed"))
    elif backend == "claude-code":
        completed = event_type == "assistant" and isinstance(message, dict)
    elif backend == "codex":
        completed = event_type in {"turn.completed", "turn_completed"}
    elif backend == "kimi-code":
        completed = event_type in {"assistant", "message.completed", "turn.completed", "turn_completed"}

    # Claude reports total cost on the terminal result rather than assistant events.
    if backend == "claude-code" and event_type == "result":
        cost = _number(payload, "total_cost_usd", "cost_usd")
        if cost:
            return {
                "event_id": _event_id(payload, info) or "terminal-result",
                "source_event": event_type,
                "model_turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "context_tokens": 0,
                "cost_usd": cost,
            }
        return None
    if not completed:
        return None

    usage: dict[str, Any] = {}
    for candidate in (
        info.get("tokens"), info.get("usage"), message.get("usage"), item.get("usage"),
        payload.get("usage"), properties.get("usage"),
    ):
        if isinstance(candidate, dict):
            usage = candidate
            break
    cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
    input_tokens = _number(usage, "input_tokens", "inputTokens", "prompt_tokens", "input")
    output_tokens = _number(usage, "output_tokens", "outputTokens", "completion_tokens", "output")
    cache_tokens = _number(
        usage, "cache_read_input_tokens", "cache_read_tokens", "cached_input_tokens"
    ) + _number(cache, "read")
    context_tokens = _number(usage, "context_tokens", "contextTokens")
    if not context_tokens:
        context_tokens = input_tokens + output_tokens + cache_tokens
    cost = _number(info, "cost", "cost_usd") or _number(usage, "cost", "cost_usd")
    return {
        "event_id": _event_id(payload, info),
        "source_event": event_type,
        "model_turns": 1,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "context_tokens": int(context_tokens),
        "cost_usd": cost,
    }


class UsageSupervisor:
    """Thread-safe attempt usage ledger and hard-budget gate."""

    def __init__(self, runtime: Path, backend: str, budget: dict[str, Any]):
        self.runtime = runtime
        self.backend = backend
        self.budget = dict(budget)
        self.started = time.monotonic()
        self.seen: set[str] = set()
        self.totals: dict[str, float] = {
            "model_turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0,
            "max_context_tokens": 0,
        }
        self.last_progress = self._progress_signature()
        self.no_progress_turns = 0
        self.exceeded: str | None = None
        self.lock = threading.Lock()

    def _line_count(self, name: str) -> int:
        path = self.runtime / name
        if not path.exists():
            return 0
        try:
            with path.open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def _progress_signature(self) -> tuple[int, int, int]:
        return (
            self._line_count("WORKFLOWS.ndjson"),
            self._line_count("COMMANDS.ndjson"),
            int((self.runtime.parent / "COMPLETION.json").exists()),
        )

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _violate(self, field: str, observed: float, limit: float) -> str:
        reason = f"resource budget exceeded: {field} observed={observed:g} limit={limit:g}"
        if self.exceeded is None:
            self.exceeded = reason
            self._append("VIOLATIONS.ndjson", {
                "at": utc_now(), "backend": self.backend, "event": "resource_budget_exceeded",
                "hard": True, "reason": reason, "field": field, "observed": observed, "limit": limit,
            })
        return self.exceeded

    def check_clock(self) -> str | None:
        with self.lock:
            if self.exceeded:
                return self.exceeded
            limit = self.budget.get("first_workflow_start_seconds")
            if limit and self._line_count("WORKFLOWS.ndjson") == 0:
                elapsed = time.monotonic() - self.started
                if elapsed >= float(limit):
                    return self._violate("first_workflow_start_seconds", elapsed, float(limit))
            return None

    def observe(self, payload: Any) -> str | None:
        record = normalize_usage_event(self.backend, payload)
        if record is None:
            return self.check_clock()
        with self.lock:
            if self.exceeded:
                return self.exceeded
            identity = f"{record['source_event']}:{record['event_id']}"
            if record["event_id"] and identity in self.seen:
                return None
            if record["event_id"]:
                self.seen.add(identity)
            self.totals["model_turns"] += record["model_turns"]
            self.totals["input_tokens"] += record["input_tokens"]
            self.totals["output_tokens"] += record["output_tokens"]
            self.totals["cost_usd"] += record["cost_usd"]
            self.totals["max_context_tokens"] = max(
                self.totals["max_context_tokens"], record["context_tokens"]
            )
            progress = self._progress_signature()
            if progress != self.last_progress:
                self.last_progress = progress
                self.no_progress_turns = 0
            else:
                self.no_progress_turns += int(record["model_turns"])
            self._append("USAGE.ndjson", {
                "at": utc_now(), "backend": self.backend, "event": "model_usage",
                **record, "totals": dict(self.totals), "no_progress_turns": self.no_progress_turns,
            })
            checks = {
                "max_model_turns": self.totals["model_turns"],
                "max_input_tokens": self.totals["input_tokens"],
                "max_output_tokens": self.totals["output_tokens"],
                "max_cost_usd": self.totals["cost_usd"],
                "max_context_tokens": self.totals["max_context_tokens"],
                "max_no_progress_turns": self.no_progress_turns,
            }
            for field, observed in checks.items():
                limit = self.budget.get(field)
                if limit is not None and observed > float(limit):
                    return self._violate(field, observed, float(limit))
            return None

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return {"totals": dict(self.totals), "no_progress_turns": self.no_progress_turns,
                    "budget_exceeded": self.exceeded}
