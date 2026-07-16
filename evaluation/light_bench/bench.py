#!/usr/bin/env python3
"""Self-contained, deterministic regression runner for RDO worker workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


BENCH_ROOT = Path(__file__).resolve().parent
CASES_ROOT = BENCH_ROOT / "cases"
PRESETS_PATH = BENCH_ROOT / "presets.json"
PROFILES = {"direct", "delegated", "full"}
TASK_INPUTS = ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md", "EXECUTION_POLICY.json")
RESULT_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError(f"short JSONL append: {written}/{len(data)} bytes")
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    elapsed_seconds: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def terminate_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_capture(
    argv: Sequence[str], *, cwd: Path, timeout: float = 60, env: dict[str, str] | None = None
) -> CommandResult:
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
        returncode = int(process.returncode)
    except subprocess.TimeoutExpired:
        terminate_group(process)
        stdout, stderr = process.communicate()
        timed_out = True
        returncode = 124
    return CommandResult(
        tuple(str(item) for item in argv),
        returncode,
        round(time.monotonic() - started, 6),
        stdout,
        stderr,
        timed_out,
    )


def run_logged(
    argv: Sequence[str], *, cwd: Path, timeout: float, log_path: Path, env: dict[str, str] | None = None
) -> CommandResult:
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
            timed_out = False
            returncode = int(process.returncode)
        except subprocess.TimeoutExpired:
            terminate_group(process)
            timed_out = True
            returncode = 124
    return CommandResult(
        tuple(str(item) for item in argv),
        returncode,
        round(time.monotonic() - started, 6),
        timed_out=timed_out,
    )


def require_success(result: CommandResult, label: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{label} failed with {result.returncode}: {detail}")


def git(repo: Path, *args: str, timeout: float = 30) -> CommandResult:
    return run_capture(["git", *args], cwd=repo, timeout=timeout)


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", value).strip("-") or "case"


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_identity(rdo_root: Path) -> dict[str, Any]:
    commit_result = git(rdo_root, "rev-parse", "HEAD")
    require_success(commit_result, "read RDO revision")
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=rdo_root
    )
    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=rdo_root)
    digest = hashlib.sha256(status + b"\0" + diff)
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=rdo_root
    )
    for encoded in untracked.split(b"\0"):
        if not encoded:
            continue
        relative = encoded.decode("utf-8", errors="surrogateescape")
        path = rdo_root / relative
        if path.is_file():
            digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "root": str(rdo_root),
        "commit": commit_result.stdout.strip(),
        "dirty": bool(status),
        "dirty_sha256": digest.hexdigest() if status else None,
    }


@dataclass(frozen=True)
class BenchCase:
    path: Path
    payload: dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.payload["id"])

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.payload["profiles"])

    def manifest_path(self, field: str, fallback: str) -> Path:
        raw = Path(str(self.payload.get(field) or fallback))
        return (raw if raw.is_absolute() else BENCH_ROOT / raw).resolve()

    @property
    def fixture(self) -> Path:
        return self.manifest_path("fixture", "fixture/base")

    @property
    def setup_patch(self) -> Path:
        return self.manifest_path("setup_patch", f"cases/{self.path.name}/setup.patch")

    @property
    def task_dir(self) -> Path:
        return self.manifest_path("task_dir", f"cases/{self.path.name}/task")

    @property
    def verifier(self) -> dict[str, Any]:
        value = self.payload.get("verifier") or {}
        if not isinstance(value, dict):
            raise ValueError(f"{self.case_id}: verifier must be an object")
        return value

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for root in (self.fixture, self.path):
            digest.update(str(root.name).encode("utf-8") + b"\0")
            digest.update(hash_tree(root).encode("ascii"))
        return digest.hexdigest()


def load_case(path: Path) -> BenchCase:
    payload = json_load(path / "case.json")
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: case.json must be an object")
    required = {"id", "profiles"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"{path}: missing case fields {missing}")
    case = BenchCase(path.resolve(), payload)
    if not case.case_id or any(profile not in PROFILES for profile in case.profiles):
        raise ValueError(f"{path}: invalid id or profiles")
    if len(set(case.profiles)) != len(case.profiles):
        raise ValueError(f"{path}: duplicate profile")
    return case


def discover_cases() -> dict[str, BenchCase]:
    cases: dict[str, BenchCase] = {}
    if not CASES_ROOT.exists():
        return cases
    for manifest in sorted(CASES_ROOT.glob("*/case.json")):
        case = load_case(manifest.parent)
        if case.case_id in cases:
            raise ValueError(f"duplicate case id {case.case_id}")
        cases[case.case_id] = case
    return cases


def verifier_argv(case: BenchCase, worktree: Path) -> list[str]:
    raw = case.verifier.get("argv") or ["{python}", "{case_dir}/verify.py", "{worktree}"]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{case.case_id}: verifier.argv must be a non-empty string array")
    replacements = {
        "{python}": sys.executable,
        "{case_dir}": str(case.path),
        "{bench_root}": str(BENCH_ROOT),
        "{worktree}": str(worktree),
    }
    rendered = [replace_all(item, replacements) for item in raw]
    if rendered[0] == "python3":
        rendered[0] = sys.executable
    for index in range(1, len(rendered)):
        candidate = Path(rendered[index])
        if not candidate.is_absolute() and (BENCH_ROOT / candidate).exists():
            rendered[index] = str((BENCH_ROOT / candidate).resolve())
    return rendered


def replace_all(value: str, replacements: dict[str, str]) -> str:
    for key, replacement in replacements.items():
        value = value.replace(key, replacement)
    return value


def prepare_repo(case: BenchCase, destination: Path) -> tuple[Path, str]:
    repo = destination / "repo"
    shutil.copytree(case.fixture, repo)
    require_success(git(repo, "init", "-b", "main"), "git init")
    require_success(git(repo, "config", "user.email", "light-bench@example.invalid"), "git config")
    require_success(git(repo, "config", "user.name", "RDO Light Bench"), "git config")
    if case.setup_patch.is_file() and case.setup_patch.stat().st_size:
        require_success(git(repo, "apply", "--whitespace=nowarn", str(case.setup_patch)), "apply setup patch")
    require_success(git(repo, "add", "-A"), "git add fixture")
    require_success(git(repo, "commit", "-m", f"light-bench base: {case.case_id}"), "commit fixture")
    head = git(repo, "rev-parse", "HEAD")
    require_success(head, "read fixture commit")
    return repo, head.stdout.strip()


def validate_case_files(case: BenchCase) -> list[str]:
    errors: list[str] = []
    if not case.fixture.is_dir():
        errors.append("fixture directory is missing")
    if not case.setup_patch.is_file():
        errors.append("setup patch is missing")
    for filename in TASK_INPUTS:
        if not (case.task_dir / filename).is_file():
            errors.append(f"task/{filename} is missing")
    try:
        policy = json_load(case.task_dir / "EXECUTION_POLICY.json")
        if not isinstance(policy, dict):
            errors.append("execution policy must be an object")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"execution policy is invalid: {exc}")
    return errors


def command_validate(args: argparse.Namespace) -> int:
    cases = discover_cases()
    selected = [cases[args.case]] if args.case else list(cases.values())
    failures: list[str] = []
    for case in selected:
        errors = validate_case_files(case)
        if errors:
            failures.extend(f"{case.case_id}: {item}" for item in errors)
            continue
        with tempfile.TemporaryDirectory(prefix=f"rdo-light-bench-{case.case_id}-") as temporary:
            try:
                repo, _ = prepare_repo(case, Path(temporary))
                timeout = float(case.verifier.get("timeout_seconds") or 60)
                result = run_capture(verifier_argv(case, repo), cwd=repo, timeout=timeout)
                expected = int(case.payload.get("expected_initial_verifier_exit", 1))
                if result.returncode != expected:
                    failures.append(
                        f"{case.case_id}: initial verifier returned {result.returncode}, expected {expected}; "
                        f"{(result.stderr or result.stdout).strip()}"
                    )
            except Exception as exc:  # validation should report all cases
                failures.append(f"{case.case_id}: {exc}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"validated {len(selected)} light-bench case(s)")
    return 0


class GitChangeWatcher:
    def __init__(self, worktree: Path, base_commit: str):
        self.worktree = worktree
        self.base_commit = base_commit
        self.first_change_at: float | None = None
        self.last_change_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="light-bench-git-watcher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _signature(self) -> tuple[str, str] | None:
        if not self.worktree.exists():
            return None
        head = git(self.worktree, "rev-parse", "HEAD", timeout=5)
        status = git(self.worktree, "status", "--porcelain=v1", "--untracked-files=all", timeout=5)
        if head.returncode != 0 or status.returncode != 0:
            return None
        return head.stdout.strip(), status.stdout

    def _run(self) -> None:
        previous: tuple[str, str] | None = None
        while not self._stop.wait(0.2):
            signature = self._signature()
            if signature is None:
                continue
            if previous is None:
                previous = signature
                if signature != (self.base_commit, ""):
                    now = time.time()
                    self.first_change_at = self.first_change_at or now
                    self.last_change_at = now
                continue
            if signature != previous:
                now = time.time()
                self.first_change_at = self.first_change_at or now
                self.last_change_at = now
                previous = signature


def copy_task_inputs(case: BenchCase, task_dir: Path, profile: str) -> dict[str, Any]:
    for filename in TASK_INPUTS:
        shutil.copy2(case.task_dir / filename, task_dir / filename)
    policy_path = task_dir / "EXECUTION_POLICY.json"
    policy = json_load(policy_path)
    policy["strategy_required"] = profile == "full"
    overrides = case.payload.get("profile_policy_overrides") or {}
    if isinstance(overrides, dict) and isinstance(overrides.get(profile), dict):
        policy.update(overrides[profile])
    atomic_json(policy_path, policy)
    return policy


def task_identifier(case: BenchCase) -> str:
    declared = case.payload.get("task_id")
    if isinstance(declared, str) and re.fullmatch(r"T[0-9]{3}[A-Za-z0-9-]*", declared):
        return declared
    suffix = safe_slug(case.case_id)
    return f"T001-{suffix}"[:80]


def create_run_and_task(
    *, case: BenchCase, profile: str, repo: Path, rdo_root: Path
) -> tuple[str, str, Path, Path]:
    run_id = "light-bench"
    task_id = task_identifier(case)
    init_result = run_capture(
        [
            sys.executable,
            str(rdo_root / "scripts" / "init_run.py"),
            "--run-id", run_id,
            "--project-slug", "rdo-light-bench",
            "--objective", f"Light bench {case.case_id}",
            "--target-branch", "main",
        ],
        cwd=repo,
    )
    require_success(init_result, "initialize RDO run")
    policy = json_load(case.task_dir / "EXECUTION_POLICY.json")
    allowed = list(policy.get("allowed_paths") or [])
    if not allowed:
        raise ValueError(f"{case.case_id}: allowed_paths must be non-empty")
    command = [
        sys.executable,
        str(rdo_root / "scripts" / "create_task.py"),
        "--run-id", run_id,
        "--task-id", task_id,
        "--goal", str(case.payload.get("goal") or case.payload.get("title") or case.case_id),
        "--profile", profile,
        "--allowed-paths", *allowed,
    ]
    read_paths = list(policy.get("read_paths") or allowed)
    if read_paths:
        command.extend(["--read-paths", *read_paths])
    forbidden = list(policy.get("forbidden_paths") or [])
    if forbidden:
        command.extend(["--forbidden-paths", *forbidden])
    sources = list(policy.get("context_sources") or [])
    if sources:
        command.extend(["--context-sources", *sources])
    create_result = run_capture(command, cwd=repo)
    require_success(create_result, "create RDO task")
    task_dir = repo / ".agent-collab" / "runs" / run_id / "tasks" / task_id
    copy_task_inputs(case, task_dir, profile)
    status = json_load(task_dir / "STATUS.json")
    worktree = Path(str(status["worktree"]))
    if not worktree.is_absolute():
        worktree = repo / worktree
    return run_id, task_id, task_dir, worktree.resolve()


def backend_version(backend: str) -> dict[str, Any]:
    binaries = {
        "claude-code": "claude",
        "codex": "codex",
        "opencode": "opencode",
        "kimi-code": "kimi",
    }
    binary = binaries.get(backend, backend)
    resolved = shutil.which(binary)
    if not resolved:
        return {"binary": binary, "path": None, "version": None, "observed": False}
    result = run_capture([resolved, "--version"], cwd=Path.cwd(), timeout=10)
    text = (result.stdout or result.stderr).strip().splitlines()
    return {
        "binary": binary,
        "path": resolved,
        "version": text[0] if text else None,
        "observed": result.returncode == 0 and bool(text),
    }


def latest_strategy_revision(task_dir: Path) -> int:
    strategies = sorted((task_dir / "strategy").glob("STRATEGY-v*.json"))
    if not strategies:
        raise RuntimeError("Full planning produced no strategy revision")
    payload = json_load(strategies[-1])
    revision = payload.get("revision")
    if not isinstance(revision, int):
        raise RuntimeError("strategy revision is missing")
    return revision


def terminate_timed_out_worker(rdo_root: Path, repo: Path, task_dir: Path) -> CommandResult:
    """Ask RDO to clean descendants if the bench-level guard outlives dispatch."""
    return run_capture(
        [
            sys.executable,
            str(rdo_root / "scripts" / "rdo.py"),
            "worker",
            "terminate",
            "--task-dir",
            str(task_dir),
        ],
        cwd=repo,
        timeout=30,
    )


def cleanup_result(result: CommandResult) -> tuple[bool, dict[str, Any]]:
    payload: dict[str, Any] | None = None
    try:
        candidate = json.loads(result.stdout)
        if isinstance(candidate, dict):
            payload = candidate
    except json.JSONDecodeError:
        pass
    survivors = payload.get("surviving_pids") if payload is not None else None
    succeeded = (
        result.returncode == 0
        and not result.timed_out
        and isinstance(survivors, list)
        and not survivors
    )
    return succeeded, {
        "returncode": result.returncode,
        "elapsed_seconds": result.elapsed_seconds,
        "timed_out": result.timed_out,
        "surviving_pids": survivors,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            result.append({"event": "invalid_jsonl", "line": number})
            continue
        if isinstance(payload, dict):
            result.append(payload)
    return result


def parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def collect_attempt_metrics(task_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    totals: dict[str, Any] = {
        "worker_elapsed_seconds": 0,
        "model_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0,
        "max_context_tokens": 0,
    }
    context_records: list[dict[str, Any]] = []
    broker_records: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    ready_times: list[float] = []
    usage_observability: set[str] = set()
    context_attempt_ids: list[str] = []
    context_declared_attempts: list[str] = []
    context_initialized_attempts: list[str] = []
    context_missing_attempts: list[str] = []
    context_unsupported_attempts: list[str] = []
    declared_context_coverage: set[str] = set()
    for attempt_json in sorted((task_dir / "attempts").glob("*/ATTEMPT.json")):
        attempt_dir = attempt_json.parent
        metadata = json_load(attempt_json)
        attempt_id = str(metadata.get("attempt_id") or attempt_dir.name)
        context_attempt_ids.append(attempt_id)
        supervisor_path = attempt_dir / "supervisor-result.json"
        supervisor = json_load(supervisor_path) if supervisor_path.is_file() else {}
        usage = (supervisor.get("usage") or {}).get("totals") or {}
        elapsed = supervisor.get("elapsed_seconds")
        if not isinstance(elapsed, (int, float)):
            start, end = parse_iso(metadata.get("started_at")), parse_iso(metadata.get("ended_at"))
            elapsed = max(0.0, end - start) if start is not None and end is not None else 0.0
        totals["worker_elapsed_seconds"] += float(elapsed)
        for name in ("model_turns", "input_tokens", "output_tokens", "cost_usd"):
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[name] += float(value)
        value = usage.get("max_context_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            totals["max_context_tokens"] = max(totals["max_context_tokens"], float(value))
        runtime = attempt_dir / "runtime"
        backend_profile_path = runtime / "BACKEND_PROFILE.json"
        backend_profile = json_load(backend_profile_path) if backend_profile_path.is_file() else {}
        runtime_mode = str((metadata.get("runtime") or {}).get("io_mode") or "machine")
        observed = (backend_profile.get("usage_observability") or {}).get(runtime_mode) or []
        if isinstance(observed, list):
            usage_observability.update(str(item) for item in observed)
        adapter = (backend_profile.get("context_access") or {}).get("adapter") or {}
        telemetry_declared = (
            isinstance(adapter, dict)
            and adapter.get("request_log") == "CONTEXT_ACCESS.ndjson"
        )
        if telemetry_declared:
            context_declared_attempts.append(attempt_id)
            if adapter.get("telemetry_coverage"):
                declared_context_coverage.add(str(adapter["telemetry_coverage"]))
        else:
            context_unsupported_attempts.append(attempt_id)
        attempt_context_records = read_ndjson(runtime / "CONTEXT_ACCESS.ndjson")
        attempt_access_records = [
            record for record in attempt_context_records
            if record.get("event") == "context_access"
        ]
        telemetry_initialized = any(
            record.get("event") == "context_telemetry_initialized"
            for record in attempt_context_records
        ) or bool(attempt_access_records)
        if telemetry_declared and telemetry_initialized:
            context_initialized_attempts.append(attempt_id)
        elif telemetry_declared:
            context_missing_attempts.append(attempt_id)
        context_records.extend(attempt_access_records)
        broker_records.extend(read_ndjson(runtime / "CONTEXT_REQUESTS.ndjson"))
        commands.extend(read_ndjson(runtime / "COMMANDS.ndjson"))
        workflows.extend(read_ndjson(runtime / "WORKFLOWS.ndjson"))
        violations.extend(read_ndjson(runtime / "VIOLATIONS.ndjson"))
        ready = runtime / "HANDOFF_READY.json"
        if ready.is_file():
            ready_times.append(ready.stat().st_mtime)
        attempts.append({
            "attempt_id": metadata.get("attempt_id"),
            "phase": metadata.get("phase"),
            "state": metadata.get("state"),
            "execution_mode": metadata.get("execution_mode"),
            "worker_id": metadata.get("worker_id"),
            "session_id": metadata.get("session_id"),
            "parent_attempt_id": metadata.get("parent_attempt_id"),
            "started_at": metadata.get("started_at"),
            "ended_at": metadata.get("ended_at"),
            "exit_code": metadata.get("exit_code"),
            "backend_id": metadata.get("backend_id"),
            "model": (metadata.get("runtime") or {}).get("model"),
            "elapsed_seconds": elapsed,
        })
    read_keys = [
        (
            record.get("operation"), record.get("path") or record.get("scope"),
            record.get("offset"), record.get("limit"), record.get("decision"),
        )
        for record in context_records
        if record.get("operation") == "Read"
    ]
    access_paths = {
        str(record.get("path")) for record in context_records if record.get("path")
    }
    record_coverage = {str(record.get("coverage")) for record in context_records if record.get("coverage")}
    coverage = sorted(declared_context_coverage | record_coverage)
    context_available = bool(context_attempt_ids) and not (
        context_missing_attempts or context_unsupported_attempts
    )
    def context_value(value: int) -> int | None:
        return value if context_available else None
    context_metrics = {
        "access_checks": context_value(len(context_records)),
        "unique_paths": context_value(len(access_paths)),
        "repeated_read_requests": context_value(max(0, len(read_keys) - len(set(read_keys)))),
        "denied_requests": context_value(sum(record.get("decision") == "deny" for record in context_records)),
        "repo_wide_searches": context_value(sum(
            record.get("operation") in {"Grep", "Glob"}
            and (record.get("scope") == "." or record.get("path") == ".")
            for record in context_records
        )),
        "unbounded_read_source_bytes": context_value(sum(
            int(record["source_size_bytes"])
            for record in context_records
            if record.get("operation") == "Read"
            and not record.get("bounded")
            and isinstance(record.get("source_size_bytes"), int)
        )),
        "telemetry_declared": bool(context_declared_attempts),
        "telemetry_declared_for_all_attempts": bool(context_attempt_ids)
        and len(context_declared_attempts) == len(context_attempt_ids),
        "telemetry_initialized": context_available,
        "telemetry_complete": context_available,
        "telemetry_attempts": {
            "total": len(context_attempt_ids),
            "declared": len(context_declared_attempts),
            "initialized": len(context_initialized_attempts),
            "missing_attempt_ids": context_missing_attempts,
            "unsupported_attempt_ids": context_unsupported_attempts,
        },
        "request_records_observed": bool(context_records),
        "coverage": coverage,
        "broker_requests": len(broker_records),
        "broker_result_bytes": sum(
            int(record["result_bytes"])
            for record in broker_records
            if isinstance(record.get("result_bytes"), int)
        ),
        "broker_truncated_results": sum(
            record.get("result_truncated") is True for record in broker_records
        ),
        "broker_actions": {
            name: sum(record.get("action") == name for record in broker_records)
            for name in ("index", "search", "get")
        },
    }
    totals = {name: round(value, 6) for name, value in totals.items()}
    usage_name_map = {
        "model_turns": "model_turns",
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cost_usd": "cost_usd",
        "max_context_tokens": "context_tokens",
    }
    for output_name, declared_name in usage_name_map.items():
        if declared_name not in usage_observability:
            totals[output_name] = None
    totals["coverage"] = sorted(usage_observability)
    return attempts, {
        "usage": totals,
        "context": context_metrics,
        "commands": {
            "total": len(commands),
            "acceptance": sum(
                record.get("category") == "required_commands" or record.get("acceptance") is True
                for record in commands
            ),
            "failed": sum(
                record.get("exit_code") not in {None, 0} or record.get("timed_out") is True
                for record in commands
            ),
        },
        "workflows": {
            "events": len(workflows),
            "started": sum(record.get("event") == "workflow_started" for record in workflows),
            "completed": sum(record.get("event") == "workflow_completed" for record in workflows),
            "carried_forward": sum(record.get("event") == "workflow_carried_forward" for record in workflows),
        },
        "violations": {
            "total": len(violations),
            "hard": sum(record.get("hard") is True for record in violations),
        },
        "handoff_ready_at": max(ready_times) if ready_times else None,
    }


def changed_paths(worktree: Path, base_commit: str) -> list[str]:
    paths: set[str] = set()
    committed = git(worktree, "diff", "--name-only", f"{base_commit}..HEAD")
    if committed.returncode == 0:
        paths.update(line.strip() for line in committed.stdout.splitlines() if line.strip())
    dirty = git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty.returncode == 0:
        for line in dirty.stdout.splitlines():
            if len(line) >= 4:
                paths.add(line[3:].split(" -> ")[-1])
    return sorted(paths)


def collect_status(rdo_root: Path, repo: Path, run_id: str) -> tuple[int, dict[str, Any] | None, str]:
    result = run_capture(
        [sys.executable, str(rdo_root / "scripts" / "collect_status.py"), "--run-id", run_id, "--json"],
        cwd=repo,
        timeout=60,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    return result.returncode, payload if isinstance(payload, dict) else None, result.stderr


def run_one(
    *, case: BenchCase, profile: str, backend: str, rdo_root: Path,
    output_root: Path, repetition: int, permission: str, model_label: str,
) -> dict[str, Any]:
    if profile not in case.profiles:
        raise ValueError(f"{case.case_id} does not support profile {profile}")
    run_key = (
        f"{safe_slug(case.case_id)}-{profile}-{safe_slug(backend)}-"
        f"r{repetition:02d}-{time.time_ns()}"
    )
    workspace = output_root / "workspaces" / run_key
    workspace.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()
    runner_started_monotonic = time.monotonic()
    repo, base_commit = prepare_repo(case, workspace)
    run_id, task_id, task_dir, worktree = create_run_and_task(
        case=case, profile=profile, repo=repo, rdo_root=rdo_root
    )
    lifecycle_started_epoch = time.time()
    lifecycle_started_monotonic = time.monotonic()
    fixture_setup_seconds = round(lifecycle_started_monotonic - runner_started_monotonic, 6)
    watcher = GitChangeWatcher(worktree, base_commit)
    watcher.start()
    dispatches: list[dict[str, Any]] = []
    cleanup_ok = True
    strategy_review_mode = "not_applicable"
    timeout = float(case.payload.get("worker_timeout_seconds") or 900)
    # RDO's attempt supervisor owns the real deadline and descendant cleanup.
    # This outer guard is deliberately later and only protects a wedged
    # dispatcher; if it fires, explicitly invoke RDO's recorded process-tree
    # cleanup rather than assuming that killing the shell reached new sessions.
    dispatch_timeout = timeout + 90
    dispatch_command = [
        str(rdo_root / "scripts" / "dispatch_agent.sh"), run_id, task_id,
        "--worker", backend, "--runtime", "plain", "--io", "machine",
        "--permission", permission,
    ]
    try:
        first = run_logged(
            dispatch_command, cwd=repo, timeout=dispatch_timeout,
            log_path=workspace / "dispatch-01.log", env=os.environ.copy(),
        )
        first_cleanup: dict[str, Any] | None = None
        if first.timed_out:
            cleanup = terminate_timed_out_worker(rdo_root, repo, task_dir)
            cleanup_ok, first_cleanup = cleanup_result(cleanup)
        dispatches.append({
            "phase": "planning" if profile == "full" else "execution",
            "returncode": first.returncode,
            "elapsed_seconds": first.elapsed_seconds,
            "timed_out": first.timed_out,
            "cleanup": first_cleanup,
            "log": str(workspace / "dispatch-01.log"),
        })
        if profile == "full" and first.returncode == 0:
            status = json_load(task_dir / "STATUS.json")
            if status.get("state") == "strategy_review":
                revision = latest_strategy_revision(task_dir)
                approval = run_capture(
                    [
                        sys.executable, str(rdo_root / "scripts" / "rdo.py"),
                        "strategy", "approve", "--task-dir", str(task_dir),
                        "--revision", str(revision), "--reviewer", "light-bench-protocol-auto-approve",
                        "--note", "Benchmark automation validates protocol shape; it does not judge strategy quality.",
                    ],
                    cwd=repo,
                    timeout=60,
                )
                require_success(approval, "auto-approve Full strategy")
                strategy_review_mode = "protocol_auto_approve"
                second = run_logged(
                    dispatch_command, cwd=repo, timeout=dispatch_timeout,
                    log_path=workspace / "dispatch-02.log", env=os.environ.copy(),
                )
                second_cleanup: dict[str, Any] | None = None
                if second.timed_out:
                    cleanup = terminate_timed_out_worker(rdo_root, repo, task_dir)
                    second_cleanup_ok, second_cleanup = cleanup_result(cleanup)
                    cleanup_ok = cleanup_ok and second_cleanup_ok
                dispatches.append({
                    "phase": "execution",
                    "returncode": second.returncode,
                    "elapsed_seconds": second.elapsed_seconds,
                    "timed_out": second.timed_out,
                    "cleanup": second_cleanup,
                    "log": str(workspace / "dispatch-02.log"),
                })
            else:
                strategy_review_mode = "planning_did_not_reach_strategy_review"
    finally:
        watcher.stop()
    status_code, status_report, status_error = collect_status(rdo_root, repo, run_id)
    status = json_load(task_dir / "STATUS.json") if (task_dir / "STATUS.json").is_file() else {}
    verifier_timeout = float(case.verifier.get("timeout_seconds") or 60)
    if worktree.is_dir() and cleanup_ok:
        verifier_result = run_capture(verifier_argv(case, worktree), cwd=worktree, timeout=verifier_timeout)
        paths = changed_paths(worktree, base_commit)
    elif worktree.is_dir():
        verifier_result = CommandResult(
            tuple(),
            125,
            0,
            stderr="verifier skipped because timed-out worker cleanup failed",
        )
        paths = changed_paths(worktree, base_commit)
    else:
        verifier_result = CommandResult(tuple(), 125, 0, stderr="task worktree was not created")
        paths = []
    attempts, metrics = collect_attempt_metrics(task_dir)
    configured_models = sorted({
        str(attempt["model"])
        for attempt in attempts
        if isinstance(attempt.get("model"), str) and attempt["model"]
    })
    expected_terminal = "verified" if profile == "direct" else "review"
    expected_paths = sorted(str(item) for item in case.payload.get("expected_changed_paths", []))
    within_expected = not expected_paths or set(paths).issubset(set(expected_paths))
    protocol_valid = bool(status_report and status_report.get("valid") is True)
    terminal_correct = status.get("state") == expected_terminal
    dispatch_ok = (
        bool(dispatches)
        and all(item["returncode"] == 0 for item in dispatches)
        and cleanup_ok
    )
    hard_pass = bool(
        verifier_result.returncode == 0
        and protocol_valid
        and terminal_correct
        and within_expected
        and dispatch_ok
        and metrics["violations"]["hard"] == 0
    )
    lifecycle_elapsed = round(time.monotonic() - lifecycle_started_monotonic, 6)
    runner_elapsed = round(time.monotonic() - runner_started_monotonic, 6)
    first_change_seconds = (
        round(watcher.first_change_at - lifecycle_started_epoch, 6)
        if watcher.first_change_at is not None else None
    )
    handoff_ready = metrics.pop("handoff_ready_at")
    last_change_to_handoff = (
        round(handoff_ready - watcher.last_change_at, 6)
        if (
            handoff_ready is not None
            and watcher.last_change_at is not None
            and handoff_ready >= watcher.last_change_at
        )
        else None
    )
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": "rdo_light_bench_result",
        "run_key": run_key,
        "started_at": started_at,
        "completed_at": utc_now(),
        "case": {
            "id": case.case_id,
            "title": case.payload.get("title") or case.case_id,
            "digest": case.digest,
            "profile": profile,
            "repetition": repetition,
        },
        "provenance": {
            "rdo": source_identity(rdo_root),
            "backend": {"id": backend, **backend_version(backend)},
            "model_label": model_label or None,
            "configured_models": configured_models,
            "configured_model_recorded": bool(configured_models),
            "permission_mode": permission,
            "python": sys.version.split()[0],
            "platform": sys.platform,
        },
        "scope": {
            "worker_execution_measured": True,
            "coordinator_review_measured": False,
            "coordinator_review_mode": "not_run",
            "strategy_review_mode": strategy_review_mode,
            "read_telemetry_is_os_complete": False,
        },
        "outcome": {
            "passed": hard_pass,
            "verifier_passed": verifier_result.returncode == 0,
            "verifier_exit_code": verifier_result.returncode,
            "protocol_valid": protocol_valid,
            "collect_status_exit_code": status_code,
            "terminal_state": status.get("state"),
            "expected_terminal_state": expected_terminal,
            "terminal_state_correct": terminal_correct,
            "dispatch_ok": dispatch_ok,
            "timed_out_worker_cleanup_ok": cleanup_ok,
            "benchmark_abort_required": not cleanup_ok,
            "changed_paths": paths,
            "expected_changed_paths": expected_paths,
            "changed_paths_within_expected": within_expected,
            "status_error": status_error.strip(),
        },
        "timing": {
            "total_seconds": lifecycle_elapsed,
            "runner_total_seconds": runner_elapsed,
            "fixture_setup_seconds": fixture_setup_seconds,
            "worker_elapsed_seconds": metrics["usage"].pop("worker_elapsed_seconds"),
            "time_to_first_change_seconds": first_change_seconds,
            "last_change_to_handoff_seconds": last_change_to_handoff,
        },
        "usage": metrics.pop("usage"),
        "context_access": metrics.pop("context"),
        "protocol_activity": metrics,
        "attempts": attempts,
        "dispatches": dispatches,
        "verifier": {
            "argv": list(verifier_result.argv),
            "elapsed_seconds": verifier_result.elapsed_seconds,
            "timed_out": verifier_result.timed_out,
            "stdout": verifier_result.stdout[-4000:],
            "stderr": verifier_result.stderr[-4000:],
        },
        "artifacts": {
            "workspace": str(workspace),
            "repository": str(repo),
            "task_dir": str(task_dir),
            "task_worktree": str(worktree),
        },
    }
    result_path = output_root / "results" / f"{run_key}.json"
    atomic_json(result_path, result)
    append_jsonl(output_root / "results.jsonl", result)
    return result


def load_presets() -> dict[str, Any]:
    if not PRESETS_PATH.is_file():
        return {}
    payload = json_load(PRESETS_PATH)
    return payload if isinstance(payload, dict) else {}


def command_list(args: argparse.Namespace) -> int:
    cases = discover_cases()
    if args.json:
        print(json.dumps([
            {
                "id": case.case_id,
                "title": case.payload.get("title") or case.case_id,
                "profiles": list(case.profiles),
                "description": case.payload.get("description") or "",
            }
            for case in cases.values()
        ], indent=2))
        return 0
    for case in cases.values():
        print(f"{case.case_id:24} {','.join(case.profiles):24} {case.payload.get('title') or ''}")
    return 0


def selected_matrix(args: argparse.Namespace, cases: dict[str, BenchCase]) -> list[tuple[BenchCase, str]]:
    if args.preset:
        presets = load_presets()
        entries = presets.get(args.preset)
        if not isinstance(entries, list):
            raise ValueError(f"unknown or invalid preset {args.preset!r}")
        result: list[tuple[BenchCase, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("case") not in cases:
                raise ValueError(f"invalid preset entry: {entry!r}")
            profiles = entry.get("profiles") or []
            for profile in profiles:
                case = cases[str(entry["case"])]
                profile = str(profile)
                if profile not in case.profiles:
                    raise ValueError(
                        f"preset requests unsupported profile {profile!r} for {case.case_id}"
                    )
                result.append((case, profile))
        return result
    if not args.case or not args.profile:
        raise ValueError("run requires --case and --profile, or --preset")
    if args.case not in cases:
        raise ValueError(f"unknown case {args.case!r}")
    if args.profile not in cases[args.case].profiles:
        raise ValueError(f"{args.case} does not support profile {args.profile}")
    return [(cases[args.case], args.profile)]


def prepare_output_root(
    raw_output: str, *, prefix: str, forbidden_roots: Sequence[Path]
) -> Path:
    """Return a new or empty output directory outside every measured RDO tree."""
    roots = tuple(root.resolve() for root in forbidden_roots)
    if raw_output:
        output_root = Path(raw_output).resolve()
    else:
        temp_parent = Path(tempfile.gettempdir()).resolve()
        for root in roots:
            try:
                temp_parent.relative_to(root)
            except ValueError:
                continue
            raise ValueError(
                f"temporary output parent must be outside measured RDO root {root}: "
                f"{temp_parent}"
            )
        output_root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()

    for root in roots:
        try:
            output_root.relative_to(root)
        except ValueError:
            continue
        raise ValueError(
            f"output directory must be outside measured RDO root {root}: {output_root}"
        )

    if output_root.exists():
        if not output_root.is_dir():
            raise ValueError(f"output path is not a directory: {output_root}")
        if any(output_root.iterdir()):
            raise ValueError(f"output directory must be new or empty: {output_root}")
    else:
        output_root.mkdir(parents=True)
    return output_root


def command_run(args: argparse.Namespace) -> int:
    cases = discover_cases()
    matrix = selected_matrix(args, cases)
    rdo_root = Path(args.rdo_root).resolve()
    if not (rdo_root / "scripts" / "dispatch_agent.sh").is_file():
        raise ValueError(f"not an RDO root: {rdo_root}")
    output_root = prepare_output_root(
        args.output,
        prefix="rdo-light-bench-results-",
        forbidden_roots=(rdo_root,),
    )
    failures = 0
    print(f"results: {output_root}")
    for case, profile in matrix:
        for repetition in range(1, args.repeat + 1):
            print(f"running {case.case_id} profile={profile} backend={args.backend} repetition={repetition}")
            result = run_one(
                case=case,
                profile=profile,
                backend=args.backend,
                rdo_root=rdo_root,
                output_root=output_root,
                repetition=repetition,
                permission=args.permission,
                model_label=args.model_label,
            )
            turns = result["usage"]["model_turns"]
            reads = result["context_access"]["access_checks"]
            print(
                f"  pass={result['outcome']['passed']} "
                f"wall={result['timing']['total_seconds']:.1f}s "
                f"turns={'n/a' if turns is None else f'{turns:g}'} "
                f"reads={'n/a' if reads is None else reads}"
            )
            failures += int(not result["outcome"]["passed"])
            if result["outcome"].get("benchmark_abort_required") is True:
                print(
                    "fatal light-bench cleanup failure; refusing to run further samples",
                    file=sys.stderr,
                )
                return 2
    return 1 if failures else 0


def command_ab(args: argparse.Namespace) -> int:
    if args.repeat % 2:
        raise ValueError("A/B --repeat must be even so baseline/candidate order is balanced")
    if not args.model_label.strip():
        raise ValueError("A/B requires a non-empty --model-label")
    cases = discover_cases()
    matrix = selected_matrix(args, cases)
    roots = {
        "baseline": Path(args.baseline_rdo).resolve(),
        "candidate": Path(args.candidate_rdo).resolve(),
    }
    if roots["baseline"] == roots["candidate"]:
        raise ValueError("A/B baseline and candidate RDO roots must be distinct")
    for label, root in roots.items():
        if not (root / "scripts" / "dispatch_agent.sh").is_file():
            raise ValueError(f"{label} is not an RDO root: {root}")
    output_root = prepare_output_root(
        args.output,
        prefix="rdo-light-bench-ab-",
        forbidden_roots=tuple(roots.values()),
    )
    print(f"A/B results: {output_root}")
    failures = 0
    for case, profile in matrix:
        for repetition in range(1, args.repeat + 1):
            order = ("baseline", "candidate") if repetition % 2 else ("candidate", "baseline")
            for label in order:
                print(
                    f"running {label} {case.case_id} profile={profile} "
                    f"backend={args.backend} repetition={repetition}"
                )
                result = run_one(
                    case=case,
                    profile=profile,
                    backend=args.backend,
                    rdo_root=roots[label],
                    output_root=output_root / label,
                    repetition=repetition,
                    permission=args.permission,
                    model_label=args.model_label,
                )
                failures += int(not result["outcome"]["passed"])
                if result["outcome"].get("benchmark_abort_required") is True:
                    print(
                        "fatal light-bench cleanup failure; refusing to run further A/B samples",
                        file=sys.stderr,
                    )
                    return 2
    comparison = compare_results(
        load_results(output_root / "baseline"),
        load_results(output_root / "candidate"),
    )
    atomic_json(output_root / "comparison.json", comparison)
    markdown = render_comparison(comparison)
    (output_root / "comparison.md").write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    if comparison_incomplete(comparison):
        return 2
    return 1 if failures or any(
        row["pass_rate"]["candidate"] < row["pass_rate"]["baseline"]
        for row in comparison["rows"]
    ) else 0


def load_results(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        files = sorted((path / "results").glob("*.json")) if (path / "results").is_dir() else sorted(path.glob("*.json"))
        return [payload for file in files if isinstance((payload := json_load(file)), dict)]
    if path.suffix == ".jsonl":
        return [record for record in read_ndjson(path) if record.get("kind") == "rdo_light_bench_result"]
    payload = json_load(path)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return [payload] if isinstance(payload, dict) else []


def nested_number(payload: dict[str, Any], path: str) -> float | None:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


def comparison_key(result: dict[str, Any]) -> tuple[str, ...] | None:
    case = result.get("case") or {}
    provenance = result.get("provenance") or {}
    backend = provenance.get("backend") or {}
    model_label = str(provenance.get("model_label") or "").strip()
    if not model_label:
        return None
    configured_models = provenance.get("configured_models") or []
    configured_identity = (
        ",".join(str(item) for item in configured_models)
        if isinstance(configured_models, list) and configured_models
        else "unrecorded"
    )
    return (
        str(case.get("id")),
        str(case.get("digest")),
        str(case.get("profile")),
        str(backend.get("id")),
        str(backend.get("version")),
        model_label,
        configured_identity,
        str(provenance.get("permission_mode") or "unrecorded"),
    )


def median_metric(
    records: list[dict[str, Any]], path: str, *, passed_only: bool = False
) -> tuple[float | None, int]:
    if passed_only:
        records = [
            record
            for record in records
            if record.get("outcome", {}).get("passed") is True
        ]
    values = [value for record in records if (value := nested_number(record, path)) is not None]
    return (statistics.median(values) if values else None, len(values))


def pct_delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return round((after - before) / before * 100, 2)


COMPARE_METRICS = (
    "timing.total_seconds",
    "timing.worker_elapsed_seconds",
    "timing.time_to_first_change_seconds",
    "timing.last_change_to_handoff_seconds",
    "usage.model_turns",
    "usage.input_tokens",
    "usage.output_tokens",
    "usage.cost_usd",
    "context_access.access_checks",
    "context_access.unique_paths",
    "context_access.repeated_read_requests",
    "context_access.repo_wide_searches",
    "context_access.unbounded_read_source_bytes",
    "context_access.broker_result_bytes",
    "protocol_activity.commands.acceptance",
)


def compare_results(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    left: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    right: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    uncomparable_left = 0
    uncomparable_right = 0
    for record in baseline:
        key = comparison_key(record)
        if key is None:
            uncomparable_left += 1
        else:
            left.setdefault(key, []).append(record)
    for record in candidate:
        key = comparison_key(record)
        if key is None:
            uncomparable_right += 1
        else:
            right.setdefault(key, []).append(record)
    rows: list[dict[str, Any]] = []
    for key in sorted(set(left) & set(right)):
        base, cand = left[key], right[key]
        base_pass = sum(bool(item.get("outcome", {}).get("passed")) for item in base) / len(base)
        cand_pass = sum(bool(item.get("outcome", {}).get("passed")) for item in cand) / len(cand)
        metrics: dict[str, Any] = {}
        alerts: list[str] = []
        for name in COMPARE_METRICS:
            all_before, all_before_n = median_metric(base, name)
            all_after, all_after_n = median_metric(cand, name)
            pass_before, pass_before_n = median_metric(base, name, passed_only=True)
            pass_after, pass_after_n = median_metric(cand, name, passed_only=True)
            all_delta = pct_delta(all_before, all_after)
            pass_delta = pct_delta(pass_before, pass_after)
            metrics[name] = {
                "all_runs": {
                    "baseline_median": all_before,
                    "candidate_median": all_after,
                    "delta_percent": all_delta,
                    "baseline_samples": all_before_n,
                    "candidate_samples": all_after_n,
                },
                "passed_runs": {
                    "baseline_median": pass_before,
                    "candidate_median": pass_after,
                    "delta_percent": pass_delta,
                    "baseline_samples": pass_before_n,
                    "candidate_samples": pass_after_n,
                },
            }
            alert_delta = pass_delta if name.startswith("timing.") else all_delta
            if (
                alert_delta is not None
                and alert_delta > 20
                and name.startswith(("timing.", "usage.", "context_access."))
            ):
                basis = "passed-run" if name.startswith("timing.") else "all-run"
                alerts.append(f"{basis} {name} regressed {alert_delta:.1f}%")
        if cand_pass < base_pass:
            alerts.insert(0, f"correctness pass rate fell from {base_pass:.2f} to {cand_pass:.2f}")
        rows.append({
            "key": {
                "case_id": key[0], "case_digest": key[1], "profile": key[2],
                "backend": key[3], "backend_version": key[4], "model_label": key[5],
                "configured_models": key[6], "permission_mode": key[7],
            },
            "samples": {"baseline": len(base), "candidate": len(cand)},
            "pass_rate": {"baseline": base_pass, "candidate": cand_pass},
            "metrics": metrics,
            "alerts": alerts,
        })
    return {
        "schema_version": 1,
        "kind": "rdo_light_bench_comparison",
        "generated_at": utc_now(),
        "matched_groups": len(rows),
        "unmatched_baseline_groups": len(set(left) - set(right)),
        "unmatched_candidate_groups": len(set(right) - set(left)),
        "uncomparable_baseline_records": uncomparable_left,
        "uncomparable_candidate_records": uncomparable_right,
        "unequal_sample_groups": sum(
            row["samples"]["baseline"] != row["samples"]["candidate"]
            for row in rows
        ),
        "rows": rows,
    }


def render_comparison(payload: dict[str, Any]) -> str:
    lines = [
        "# RDO light-bench comparison",
        "",
        "| Case | Profile | Backend | Pass rate | Wall delta | Turns delta | Read delta | Alerts |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in payload["rows"]:
        key, metrics = row["key"], row["metrics"]
        def delta(name: str, population: str) -> str:
            value = metrics[name][population]["delta_percent"]
            return "n/a" if value is None else f"{value:+.1f}%"
        lines.append(
            f"| {key['case_id']} | {key['profile']} | {key['backend']} | "
            f"{row['pass_rate']['baseline']:.2f} → {row['pass_rate']['candidate']:.2f} | "
            f"{delta('timing.total_seconds', 'passed_runs')} | "
            f"{delta('usage.model_turns', 'all_runs')} | "
            f"{delta('context_access.access_checks', 'all_runs')} | "
            f"{'; '.join(row['alerts']) or ''} |"
        )
    if not payload["rows"]:
        lines.extend([
            "",
            "No comparable groups. Check case digest, backend/model identity, and permission mode.",
        ])
    if comparison_incomplete(payload):
        lines.extend([
            "",
            "Coverage gaps: "
            f"unmatched baseline groups={payload['unmatched_baseline_groups']}, "
            f"unmatched candidate groups={payload['unmatched_candidate_groups']}, "
            f"uncomparable baseline records={payload['uncomparable_baseline_records']}, "
            f"uncomparable candidate records={payload['uncomparable_candidate_records']}, "
            f"unequal sample groups={payload['unequal_sample_groups']}.",
        ])
    return "\n".join(lines) + "\n"


def comparison_incomplete(payload: dict[str, Any]) -> bool:
    return (
        payload.get("matched_groups", 0) == 0
        or payload.get("unmatched_baseline_groups", 0) > 0
        or payload.get("unmatched_candidate_groups", 0) > 0
        or payload.get("uncomparable_baseline_records", 0) > 0
        or payload.get("uncomparable_candidate_records", 0) > 0
        or payload.get("unequal_sample_groups", 0) > 0
    )


def command_compare(args: argparse.Namespace) -> int:
    payload = compare_results(load_results(Path(args.baseline)), load_results(Path(args.candidate)))
    markdown = render_comparison(payload)
    if args.output:
        output = Path(args.output)
        atomic_json(output, payload)
        output.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    if payload["matched_groups"] == 0:
        return 2
    if comparison_incomplete(payload) and not getattr(args, "allow_partial", False):
        return 2
    correctness_regression = any(
        row["pass_rate"]["candidate"] < row["pass_rate"]["baseline"]
        for row in payload["rows"]
    )
    return 1 if correctness_regression else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=command_list)
    validate = actions.add_parser("validate")
    validate.add_argument("--case", default="")
    validate.set_defaults(func=command_validate)
    run = actions.add_parser("run")
    run.add_argument("--case", default="")
    run.add_argument("--profile", choices=sorted(PROFILES), default="")
    run.add_argument("--preset", default="")
    run.add_argument("--backend", choices=("claude-code", "codex", "opencode", "kimi-code"), required=True)
    run.add_argument("--rdo-root", default=str(BENCH_ROOT.parents[1]))
    run.add_argument("--output", default="")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--permission", choices=("default", "auto", "yolo"), default="auto")
    run.add_argument("--model-label", default="")
    run.set_defaults(func=command_run)
    ab = actions.add_parser("ab")
    ab.add_argument("--case", default="")
    ab.add_argument("--profile", choices=sorted(PROFILES), default="")
    ab.add_argument("--preset", default="")
    ab.add_argument("--backend", choices=("claude-code", "codex", "opencode", "kimi-code"), required=True)
    ab.add_argument("--baseline-rdo", required=True)
    ab.add_argument("--candidate-rdo", required=True)
    ab.add_argument("--output", default="")
    ab.add_argument("--repeat", type=int, default=4)
    ab.add_argument("--permission", choices=("default", "auto", "yolo"), default="auto")
    ab.add_argument("--model-label", required=True)
    ab.set_defaults(func=command_ab)
    compare = actions.add_parser("compare")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", default="")
    compare.add_argument("--allow-partial", action="store_true")
    compare.set_defaults(func=command_compare)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if getattr(args, "repeat", 1) <= 0:
        raise SystemExit("--repeat must be positive")
    try:
        return int(args.func(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"light bench error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
