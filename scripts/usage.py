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


def _observed_number(mapping: dict[str, Any], *keys: str) -> tuple[float | None, bool]:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value), True
    return None, False


def _event_id(payload: dict[str, Any], info: dict[str, Any]) -> str:
    for mapping in (info, payload.get("message") or {}, payload.get("item") or {}, payload):
        if isinstance(mapping, dict):
            for key in ("id", "message_id", "messageID", "turn_id", "turnId"):
                value = mapping.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def normalize_usage_event(backend: str, payload: Any) -> dict[str, Any] | None:
    """Return one public structured-usage observation.

    Missing metrics remain ``None``. In particular, Codex's public JSONL
    ``turn.completed`` event reports terminal token totals but does not expose
    internal model-call count or context-window occupancy.
    """
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
                "observed_metrics": ["cost_usd"],
                "model_turns": None,
                "input_tokens": None,
                "output_tokens": None,
                "context_tokens": None,
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
    if backend == "codex":
        input_tokens, input_observed = _observed_number(
            usage, "input_tokens", "inputTokens", "prompt_tokens", "input"
        )
        output_tokens, output_observed = _observed_number(
            usage, "output_tokens", "outputTokens", "completion_tokens", "output"
        )
        cached_tokens, cached_observed = _observed_number(
            usage, "cached_input_tokens", "cache_read_input_tokens", "cache_read_tokens"
        )
        observed_metrics = []
        if input_observed:
            observed_metrics.append("input_tokens")
        if output_observed:
            observed_metrics.append("output_tokens")
        return {
            "event_id": _event_id(payload, info) or "terminal-turn",
            "source_event": event_type,
            "observed_metrics": observed_metrics,
            "model_turns": None,
            "input_tokens": int(input_tokens) if input_observed else None,
            "output_tokens": int(output_tokens) if output_observed else None,
            "cached_input_tokens": int(cached_tokens) if cached_observed else None,
            "context_tokens": None,
            "cost_usd": None,
        }
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
        "observed_metrics": [
            "model_turns", "input_tokens", "output_tokens", "context_tokens", "cost_usd"
        ],
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
        self.source_events: set[str] = set()
        # Codex is the first adapter with an explicit public terminal-only
        # observation contract. Preserve the established eager-zero summaries
        # of other adapters until their telemetry contracts are redesigned.
        self.observed_metrics: set[str] = (
            set()
            if backend == "codex"
            else {"model_turns", "input_tokens", "output_tokens", "cost_usd", "context_tokens"}
        )
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
            int(
                (self.runtime / "HANDOFF_READY.json").exists()
                or (self.runtime.parent / "COMPLETION.json").exists()
            ),
        )

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _reported_totals(self) -> dict[str, float | None]:
        return {
            "model_turns": (
                self.totals["model_turns"]
                if "model_turns" in self.observed_metrics else None
            ),
            "input_tokens": (
                self.totals["input_tokens"]
                if "input_tokens" in self.observed_metrics else None
            ),
            "output_tokens": (
                self.totals["output_tokens"]
                if "output_tokens" in self.observed_metrics else None
            ),
            "cost_usd": (
                self.totals["cost_usd"]
                if "cost_usd" in self.observed_metrics else None
            ),
            "max_context_tokens": (
                self.totals["max_context_tokens"]
                if "context_tokens" in self.observed_metrics else None
            ),
        }

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
            self.source_events.add(record["source_event"])
            self.observed_metrics.update(record.get("observed_metrics", []))
            for metric in ("model_turns", "input_tokens", "output_tokens", "cost_usd"):
                value = record.get(metric)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.totals[metric] += value
            context_tokens = record.get("context_tokens")
            if isinstance(context_tokens, (int, float)) and not isinstance(context_tokens, bool):
                self.totals["max_context_tokens"] = max(
                    self.totals["max_context_tokens"], context_tokens
                )
            progress = self._progress_signature()
            if progress != self.last_progress:
                self.last_progress = progress
                self.no_progress_turns = 0
            else:
                turns = record.get("model_turns")
                if isinstance(turns, (int, float)) and not isinstance(turns, bool):
                    self.no_progress_turns += int(turns)
            self._append("USAGE.ndjson", {
                "at": utc_now(), "backend": self.backend, "event": "model_usage",
                **record,
                "totals": self._reported_totals(),
                "no_progress_turns": self.no_progress_turns,
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

    def saw_source_event(self, *event_types: str) -> bool:
        with self.lock:
            return any(event_type in self.source_events for event_type in event_types)

    def require_budget_observations(self) -> str | None:
        """Fail closed when configured usage limits lack a terminal observation."""

        field_metrics = {
            "max_model_turns": "model_turns",
            "max_input_tokens": "input_tokens",
            "max_output_tokens": "output_tokens",
            "max_cost_usd": "cost_usd",
            "max_context_tokens": "context_tokens",
            "max_no_progress_turns": "model_turns",
        }
        with self.lock:
            if self.exceeded:
                return self.exceeded
            missing = sorted({
                metric
                for field, metric in field_metrics.items()
                if field in self.budget and metric not in self.observed_metrics
            })
            if not missing:
                return None
            reason = "required usage observation missing: " + ", ".join(missing)
            self.exceeded = reason
            self._append("VIOLATIONS.ndjson", {
                "at": utc_now(),
                "backend": self.backend,
                "event": "usage_observation_missing",
                "hard": True,
                "reason": reason,
                "metrics": missing,
            })
            return reason

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return {
                "totals": self._reported_totals(),
                "observed_metrics": sorted(self.observed_metrics),
                "source_events": sorted(self.source_events),
                "no_progress_turns": self.no_progress_turns,
                "budget_exceeded": self.exceeded,
            }
