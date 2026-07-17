#!/usr/bin/env python3
"""Bounded, deterministic context projection for merged task dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from artifact_bundle import (
    ArtifactBundleError,
    file_sha256,
    safe_ref,
    validate_artifact_binding,
)
from protocol import EventJournalError, load_json, read_event_journal
from task_contract import (
    DEPENDENCY_CONTEXT_REF,
    ImmutableArtifactError,
    parse_context_markdown,
    parse_task_markdown,
    validate_task_inputs_payload,
    write_json_immutable,
)


DEPENDENCY_CONTEXT_SCHEMA_VERSION = 1
DEPENDENCY_ALIAS_PREFIX = "dependency:"
SUMMARY_EXCERPT_BYTES = 512
PROMPT_MANIFEST_MAX_BYTES = 8 * 1024
PROMPT_LIST_LIMIT = 8
DEPENDENCY_SECTIONS = (
    "summary",
    "limitations",
    "self_review",
    "changed_paths",
    "checks",
    "required_outputs",
    "objective",
    "frozen_decisions",
    "required_interfaces",
    "local_code_map",
    "necessary_background",
)

_TASK_ID_RE = re.compile(r"T[0-9]{3}[A-Za-z0-9-]*")
_ATTEMPT_ID_RE = re.compile(r"A[0-9A-Za-z._-]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class DependencyContextError(ValueError):
    """Dependency context cannot be constructed or trusted."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clip_utf8(value: str, maximum: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, False
    clipped = raw[:maximum]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip(), True
        except UnicodeDecodeError as exc:
            clipped = clipped[: exc.start]
    return "", True


def _changed_roots(paths: list[str]) -> list[str]:
    roots = sorted({path.split("/", 1)[0] for path in paths if path})
    return roots[:PROMPT_LIST_LIMIT]


def _safe_attempt_id(value: Any) -> str:
    if not isinstance(value, str) or _ATTEMPT_ID_RE.fullmatch(value) is None:
        raise DependencyContextError("dependency source attempt id is invalid")
    if value in {".", ".."} or Path(value).name != value:
        raise DependencyContextError("dependency source attempt id is unsafe")
    return value


def _dependency_task_dir(attempt_dir: Path, task_id: str) -> Path:
    task_dir = Path(attempt_dir).resolve().parent.parent
    run_dir = task_dir.parent.parent
    tasks_root = (run_dir / "tasks").resolve()
    candidate = run_dir / "tasks" / task_id
    if (
        candidate.is_symlink()
        or not candidate.is_dir()
        or candidate.resolve().parent != tasks_root
    ):
        raise DependencyContextError("dependency task path escapes the current run")
    return candidate


def _merged_event(run_dir: Path, task_id: str, merged_commit: str) -> Mapping[str, Any]:
    try:
        records, _warning = read_event_journal(
            run_dir,
            tolerate_interrupted_tail=True,
        )
    except EventJournalError as exc:
        raise DependencyContextError(f"cannot read dependency merge event: {exc}") from exc
    matches = [
        record
        for record in records
        if record.get("event") == "task_merged" and record.get("task_id") == task_id
    ]
    if not matches:
        raise DependencyContextError(f"dependency {task_id!r} has no task_merged event")
    event = matches[-1]
    if event.get("commit") != merged_commit:
        raise DependencyContextError(
            f"dependency {task_id!r} merge event commit differs from TASK_INPUTS"
        )
    verification = event.get("verification")
    if not isinstance(verification, dict) or verification.get("passed") is not True:
        raise DependencyContextError(
            f"dependency {task_id!r} merge verification did not pass"
        )
    return event


def _load_dependency_bundle(
    attempt_dir: Path,
    *,
    task_id: str,
    merged_commit: str,
):
    dependency_dir = _dependency_task_dir(attempt_dir, task_id)
    try:
        status = load_json(dependency_dir / "STATUS.json")
    except Exception as exc:
        raise DependencyContextError(
            f"dependency {task_id!r} STATUS.json is unreadable: {exc}"
        ) from exc
    if not isinstance(status, dict) or status.get("state") != "merged":
        raise DependencyContextError(f"dependency {task_id!r} is not merged")
    if status.get("artifact_protocol_version") != 2:
        raise DependencyContextError(
            f"dependency {task_id!r} does not use artifact protocol v2"
        )
    run_dir = dependency_dir.parent.parent
    event = _merged_event(run_dir, task_id, merged_commit)
    attempt_id = _safe_attempt_id(event.get("attempt_id"))
    expected_binding = event.get("artifact_binding")
    if not isinstance(expected_binding, dict):
        raise DependencyContextError(
            f"dependency {task_id!r} merge event has no artifact binding"
        )
    try:
        bundle = validate_artifact_binding(
            dependency_dir / "attempts" / attempt_id,
            expected_binding,
            expected_task_id=task_id,
            expected_attempt_id=attempt_id,
            expected_source_commit=merged_commit,
        )
    except ArtifactBundleError as exc:
        raise DependencyContextError(
            f"dependency {task_id!r} bundle is invalid: {exc}"
        ) from exc
    return dependency_dir, bundle, event


def _entry_binding(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "binding_sha256"}


def build_dependency_context(
    *,
    attempt_dir: Path,
    task_inputs: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build a compact catalog from exact v2 dependency bundles."""

    attempt_dir = Path(attempt_dir).resolve()
    try:
        task_inputs = validate_task_inputs_payload(dict(task_inputs))
    except Exception as exc:
        raise DependencyContextError(f"current task input contract is invalid: {exc}") from exc
    dependencies = task_inputs.get("resolved_dependencies", [])
    if not dependencies:
        return None

    entries: list[dict[str, Any]] = []
    for dependency in dependencies:
        task_id = str(dependency["task_id"])
        merged_commit = str(dependency["commit"])
        dependency_dir = _dependency_task_dir(attempt_dir, task_id)
        try:
            dependency_status = load_json(dependency_dir / "STATUS.json")
        except Exception as exc:
            raise DependencyContextError(
                f"dependency {task_id!r} STATUS.json is unreadable: {exc}"
            ) from exc
        if not isinstance(dependency_status, dict):
            raise DependencyContextError(
                f"dependency {task_id!r} STATUS.json must be an object"
            )
        # Legacy dependencies remain valid contract dependencies, but they do
        # not expose a virtual context source because they lack a v2 digest
        # closure suitable for Broker retrieval.
        if dependency_status.get("artifact_protocol_version") != 2:
            continue
        _dependency_dir, bundle, merge_event = _load_dependency_bundle(
            attempt_dir,
            task_id=task_id,
            merged_commit=merged_commit,
        )
        summary = " ".join(str(bundle.handoff.get("summary") or "").split())
        summary_excerpt, _summary_truncated = _clip_utf8(
            summary,
            SUMMARY_EXCERPT_BYTES,
        )
        changed_paths = bundle.evidence.get("changed_paths")
        changed_paths = changed_paths if isinstance(changed_paths, list) else []
        roots = _changed_roots(
            [str(path) for path in changed_paths if isinstance(path, str)]
        )
        all_check_ids = sorted(
            {
                str(record["check_id"])
                for record in bundle.evidence.get("command_records", [])
                if isinstance(record, dict)
                and isinstance(record.get("check_id"), str)
                and record["check_id"]
            }
        )
        prompt = {
            "summary_excerpt": summary_excerpt,
            "changed_path_count": len(changed_paths),
            "changed_roots": roots,
            "check_ids": all_check_ids[:PROMPT_LIST_LIMIT],
        }
        entry: dict[str, Any] = {
            "alias": f"{DEPENDENCY_ALIAS_PREFIX}{task_id}",
            "task_id": task_id,
            "merged_commit": merged_commit,
            "artifact_binding": dict(merge_event["artifact_binding"]),
            "available_sections": list(DEPENDENCY_SECTIONS),
            "prompt": prompt,
        }
        entry["binding_sha256"] = _digest(entry)
        entries.append(entry)

    if not entries:
        return None
    return {
        "schema_version": DEPENDENCY_CONTEXT_SCHEMA_VERSION,
        "artifact_protocol_version": 2,
        "task_id": task_inputs["task_id"],
        "task_contract_sha256": task_inputs["contract_sha256"],
        "dependencies": entries,
    }


def validate_dependency_context(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DependencyContextError("DEPENDENCY_CONTEXT.json must be an object")
    expected_root = {
        "schema_version",
        "artifact_protocol_version",
        "task_id",
        "task_contract_sha256",
        "dependencies",
    }
    if set(payload) != expected_root:
        raise DependencyContextError("DEPENDENCY_CONTEXT.json root fields are invalid")
    if payload.get("schema_version") != DEPENDENCY_CONTEXT_SCHEMA_VERSION:
        raise DependencyContextError("DEPENDENCY_CONTEXT.json schema_version is invalid")
    if payload.get("artifact_protocol_version") != 2:
        raise DependencyContextError("dependency context requires artifact protocol v2")
    if not isinstance(payload.get("task_id"), str) or _TASK_ID_RE.fullmatch(
        payload["task_id"]
    ) is None:
        raise DependencyContextError("dependency context task_id is invalid")
    if not isinstance(payload.get("task_contract_sha256"), str) or _SHA256_RE.fullmatch(
        payload["task_contract_sha256"]
    ) is None:
        raise DependencyContextError("dependency context task_contract_sha256 is invalid")
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise DependencyContextError("dependency context dependencies must be non-empty")

    expected_entry = {
        "alias",
        "task_id",
        "merged_commit",
        "artifact_binding",
        "available_sections",
        "prompt",
        "binding_sha256",
    }
    previous = ""
    for entry in dependencies:
        if not isinstance(entry, dict) or set(entry) != expected_entry:
            raise DependencyContextError("dependency context entry fields are invalid")
        task_id = entry.get("task_id")
        if (
            not isinstance(task_id, str)
            or _TASK_ID_RE.fullmatch(task_id) is None
            or task_id <= previous
        ):
            raise DependencyContextError("dependency entries must be sorted and unique")
        if entry.get("alias") != f"{DEPENDENCY_ALIAS_PREFIX}{task_id}":
            raise DependencyContextError("dependency alias does not match task_id")
        if not isinstance(entry.get("merged_commit"), str) or _COMMIT_RE.fullmatch(
            entry["merged_commit"]
        ) is None:
            raise DependencyContextError("dependency merged_commit is invalid")
        artifact_binding = entry.get("artifact_binding")
        if (
            not isinstance(artifact_binding, dict)
            or artifact_binding.get("task_id") != task_id
            or artifact_binding.get("source_commit") != entry.get("merged_commit")
            or not isinstance(artifact_binding.get("binding_sha256"), str)
        ):
            raise DependencyContextError("dependency artifact_binding is invalid")
        _safe_attempt_id(artifact_binding.get("attempt_id"))
        if not isinstance(entry.get("binding_sha256"), str) or _SHA256_RE.fullmatch(
            entry["binding_sha256"]
        ) is None:
            raise DependencyContextError("dependency binding_sha256 is invalid")
        if entry.get("available_sections") != list(DEPENDENCY_SECTIONS):
            raise DependencyContextError("dependency available_sections is invalid")
        prompt = entry.get("prompt")
        expected_prompt = {
            "summary_excerpt",
            "changed_path_count",
            "changed_roots",
            "check_ids",
        }
        if not isinstance(prompt, dict) or set(prompt) != expected_prompt:
            raise DependencyContextError("dependency prompt projection is invalid")
        summary = prompt.get("summary_excerpt")
        if (
            not isinstance(summary, str)
            or len(summary.encode("utf-8")) > SUMMARY_EXCERPT_BYTES
            or summary != " ".join(summary.split())
        ):
            raise DependencyContextError("dependency prompt summary is invalid")
        if (
            type(prompt.get("changed_path_count")) is not int
            or prompt["changed_path_count"] < 0
        ):
            raise DependencyContextError(
                "dependency prompt changed_path_count is invalid"
            )
        for field in ("changed_roots", "check_ids"):
            values = prompt.get(field)
            if (
                not isinstance(values, list)
                or len(values) > PROMPT_LIST_LIMIT
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise DependencyContextError(f"dependency prompt {field} is invalid")
        if entry["binding_sha256"] != _digest(_entry_binding(entry)):
            raise DependencyContextError("dependency entry digest is invalid")
        previous = task_id
    return dict(payload)


def write_dependency_context_immutable(
    attempt_dir: Path,
    payload: Mapping[str, Any],
) -> tuple[Path, str]:
    validated = validate_dependency_context(dict(payload))
    path = Path(attempt_dir) / DEPENDENCY_CONTEXT_REF
    expected_digest = dependency_context_file_sha256(validated)
    try:
        digest = write_json_immutable(path, validated)
    except ImmutableArtifactError as exc:
        if path.is_symlink() or not path.is_file():
            raise
        if file_sha256(path) != expected_digest:
            raise DependencyContextError(
                "existing dependency context differs from the deterministic payload"
            ) from exc
        digest = expected_digest
    return path, digest


def dependency_context_file_sha256(payload: Mapping[str, Any]) -> str:
    """Return the exact digest that write_json_immutable will publish."""

    validated = validate_dependency_context(dict(payload))
    raw = (
        json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_frozen_dependency_context(attempt_dir: Path) -> dict[str, Any] | None:
    """Load TASK_INPUTS and its optional manifest before ATTEMPT publication."""

    attempt_dir = Path(attempt_dir).resolve()
    inputs_path = attempt_dir / "TASK_INPUTS.json"
    if not inputs_path.is_file() or inputs_path.is_symlink():
        return None
    try:
        task_inputs = validate_task_inputs_payload(load_json(inputs_path))
        binding = task_inputs.get("dependency_context")
        if binding is None:
            return None
        ref = binding.get("ref") if isinstance(binding, dict) else None
        declared = binding.get("sha256") if isinstance(binding, dict) else None
        if ref != DEPENDENCY_CONTEXT_REF or not isinstance(declared, str):
            raise DependencyContextError("TASK_INPUTS dependency context binding is invalid")
        path = safe_ref(attempt_dir, ref)
        actual = file_sha256(path)
        payload = validate_dependency_context(load_json(path))
    except Exception as exc:
        raise DependencyContextError(f"dependency context is unreadable: {exc}") from exc
    if actual != declared:
        raise DependencyContextError("dependency context digest differs from TASK_INPUTS")
    if payload["task_id"] != task_inputs.get("task_id"):
        raise DependencyContextError("dependency context task_id differs from TASK_INPUTS")
    if payload["task_contract_sha256"] != task_inputs.get("contract_sha256"):
        raise DependencyContextError("dependency context differs from the frozen task contract")
    _assert_manifest_matches_task_inputs(attempt_dir, payload, task_inputs)
    return payload


def load_bound_dependency_context(attempt_dir: Path) -> dict[str, Any] | None:
    """Load the manifest through ATTEMPT -> TASK_INPUTS -> manifest closure."""

    attempt_dir = Path(attempt_dir).resolve()
    try:
        attempt = load_json(attempt_dir / "ATTEMPT.json")
    except Exception as exc:
        raise DependencyContextError(f"ATTEMPT.json is unreadable: {exc}") from exc
    if not isinstance(attempt, dict):
        raise DependencyContextError("ATTEMPT.json must be an object")
    if attempt.get("artifact_protocol_version") != 2:
        return None
    inputs_ref = attempt.get("task_inputs_ref")
    inputs_digest = attempt.get("task_inputs_sha256")
    if inputs_ref != "TASK_INPUTS.json" or not isinstance(inputs_digest, str):
        raise DependencyContextError("ATTEMPT TASK_INPUTS binding is invalid")
    try:
        inputs_path = safe_ref(attempt_dir, inputs_ref)
        actual_inputs_digest = file_sha256(inputs_path)
    except Exception as exc:
        raise DependencyContextError(f"TASK_INPUTS is unreadable: {exc}") from exc
    if actual_inputs_digest != inputs_digest:
        raise DependencyContextError("TASK_INPUTS digest differs from ATTEMPT")
    payload = load_frozen_dependency_context(attempt_dir)
    if payload is None:
        return None
    if payload["task_id"] != attempt.get("task_id"):
        raise DependencyContextError("dependency context task_id differs from ATTEMPT")
    return payload


def _assert_manifest_matches_task_inputs(
    attempt_dir: Path,
    payload: Mapping[str, Any],
    task_inputs: Mapping[str, Any],
) -> None:
    declared = {
        dependency["task_id"]: dependency["commit"]
        for dependency in task_inputs.get("resolved_dependencies", [])
    }
    expected_v2: dict[str, str] = {}
    for task_id, commit in declared.items():
        dependency_dir = _dependency_task_dir(attempt_dir, task_id)
        try:
            status = load_json(dependency_dir / "STATUS.json")
        except Exception as exc:
            raise DependencyContextError(
                f"dependency {task_id!r} STATUS.json is unreadable: {exc}"
            ) from exc
        if isinstance(status, dict) and status.get("artifact_protocol_version") == 2:
            expected_v2[task_id] = commit
    actual = {
        entry["task_id"]: entry["merged_commit"]
        for entry in payload.get("dependencies", [])
    }
    if actual != expected_v2:
        raise DependencyContextError(
            "dependency context aliases differ from TASK_INPUTS resolved dependencies"
        )


def dependency_entry(payload: Mapping[str, Any], alias: str) -> dict[str, Any]:
    matches = [
        entry
        for entry in payload.get("dependencies", [])
        if isinstance(entry, dict) and entry.get("alias") == alias
    ]
    if len(matches) != 1:
        raise DependencyContextError(f"unknown dependency source: {alias}")
    return dict(matches[0])


def dependency_source_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": entry["alias"],
        "source_sha256": entry["binding_sha256"],
        "task_id": entry["task_id"],
        "merged_commit": entry["merged_commit"],
    }


def _read_bound_input(
    dependency_dir: Path,
    source_inputs: Mapping[str, Any],
    name: str,
    filename: str,
) -> str:
    binding = source_inputs.get("inputs", {}).get(name)
    if not isinstance(binding, dict) or binding.get("ref") != filename:
        raise DependencyContextError(f"dependency {filename} binding is invalid")
    path = dependency_dir / filename
    if path.is_symlink() or path.resolve(strict=False).parent != dependency_dir.resolve():
        raise DependencyContextError(f"dependency {filename} path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DependencyContextError(f"dependency {filename} is unreadable: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != binding.get("sha256"):
        raise DependencyContextError(f"dependency {filename} differs from its frozen input")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DependencyContextError(f"dependency {filename} is not UTF-8") from exc


def _load_dependency_material(
    attempt_dir: Path,
    entry: Mapping[str, Any],
    *,
    read_task: bool,
    read_context: bool,
):
    dependency_dir, bundle, merge_event = _load_dependency_bundle(
        attempt_dir,
        task_id=str(entry["task_id"]),
        merged_commit=str(entry["merged_commit"]),
    )
    if entry["artifact_binding"].get("attempt_id") != bundle.attempt_dir.name:
        raise DependencyContextError(
            f"dependency {entry['task_id']} source attempt differs from the frozen manifest"
        )
    if entry.get("artifact_binding") != merge_event.get("artifact_binding"):
        raise DependencyContextError(
            f"dependency {entry['task_id']} merge artifact binding changed"
        )
    source_inputs = bundle.task_inputs_binding.task_inputs
    task = None
    context = None
    try:
        if read_task:
            task_text = _read_bound_input(
                dependency_dir,
                source_inputs,
                "task",
                "TASK.md",
            )
            task = parse_task_markdown(task_text, task_id=str(entry["task_id"]))
        if read_context:
            context_text = _read_bound_input(
                dependency_dir,
                source_inputs,
                "context",
                "CONTEXT.md",
            )
            context = parse_context_markdown(context_text)
    except Exception as exc:
        raise DependencyContextError(
            f"dependency task/context input is invalid: {exc}"
        ) from exc
    return bundle, task, context


def dependency_section_values(
    attempt_dir: Path,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    bundle, task, context = _load_dependency_material(
        attempt_dir,
        entry,
        read_task=True,
        read_context=True,
    )
    assert task is not None and context is not None
    return {
        "summary": bundle.handoff.get("summary", ""),
        "limitations": bundle.handoff.get("known_limitations", []),
        "self_review": bundle.handoff.get("direct_self_review", {}),
        "changed_paths": bundle.evidence.get("changed_paths", []),
        "checks": _check_records(bundle),
        "required_outputs": bundle.evidence.get("required_outputs", []),
        "objective": task["sections"]["Objective"],
        "frozen_decisions": context["sections"]["Frozen Decisions"],
        "required_interfaces": context["sections"]["Required Interfaces"],
        "local_code_map": context["sections"]["Local Code Map"],
        "necessary_background": context["sections"]["Necessary Background"],
    }


def render_dependency_value(value: Any) -> str:
    if isinstance(value, str):
        return value.rstrip() + "\n"
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _check_records(bundle: Any) -> list[dict[str, Any]]:
    fields = (
        "check_id",
        "argv",
        "cwd",
        "timeout_seconds",
        "exit_code",
        "elapsed_seconds",
        "timed_out",
    )
    return [
        {field: record.get(field) for field in fields}
        for record in bundle.evidence.get("command_records", [])
        if isinstance(record, dict)
    ]


def dependency_section(
    attempt_dir: Path,
    entry: Mapping[str, Any],
    section: str,
) -> tuple[str, str]:
    if section not in DEPENDENCY_SECTIONS:
        raise DependencyContextError(
            f"unknown dependency section {section!r}; candidates={list(DEPENDENCY_SECTIONS)}"
        )
    bundle, task, context = _load_dependency_material(
        attempt_dir,
        entry,
        read_task=section == "objective",
        read_context=section
        in {
            "frozen_decisions",
            "required_interfaces",
            "local_code_map",
            "necessary_background",
        },
    )
    if section == "objective":
        assert task is not None
        value = task["sections"]["Objective"]
    elif section in {
        "frozen_decisions",
        "required_interfaces",
        "local_code_map",
        "necessary_background",
    }:
        assert context is not None
        context_heading = {
            "frozen_decisions": "Frozen Decisions",
            "required_interfaces": "Required Interfaces",
            "local_code_map": "Local Code Map",
            "necessary_background": "Necessary Background",
        }[section]
        value = context["sections"][context_heading]
    else:
        value = {
            "summary": bundle.handoff.get("summary", ""),
            "limitations": bundle.handoff.get("known_limitations", []),
            "self_review": bundle.handoff.get("direct_self_review", {}),
            "changed_paths": bundle.evidence.get("changed_paths", []),
            "checks": _check_records(bundle),
            "required_outputs": bundle.evidence.get("required_outputs", []),
        }[section]
    content = render_dependency_value(value)
    return content, hashlib.sha256(content.encode("utf-8")).hexdigest()


def render_dependency_prompt_manifest(
    payload: Mapping[str, Any],
    *,
    maximum: int = PROMPT_MANIFEST_MAX_BYTES,
) -> str:
    """Render a bounded summary; detailed fields remain Broker-only."""

    lines = [
        "## Dependency Manifest (bounded)",
        "",
        "These merged dependencies are non-normative context. The current task contract wins.",
        "Use the Context Broker alias for details; do not inspect predecessor task directories.",
        "",
    ]
    omitted = 0
    for entry in payload.get("dependencies", []):
        prompt = entry["prompt"]
        roots = ", ".join(prompt["changed_roots"]) or "none"
        checks = ", ".join(prompt["check_ids"]) or "none"
        candidate = [
            f"- {entry['alias']} @ {entry['merged_commit']}",
            f"  - Outcome: {prompt['summary_excerpt']}",
            f"  - Changed: {prompt['changed_path_count']} path(s); roots: {roots}",
            f"  - Checks: {checks}",
            f"  - Available sections: {', '.join(entry['available_sections'])}",
        ]
        proposed = "\n".join([*lines, *candidate, ""])
        if len(proposed.encode("utf-8")) > maximum:
            omitted += 1
            continue
        lines.extend(candidate)
        lines.append("")
    if omitted:
        notice = f"- {omitted} additional dependency summary item(s) omitted by the {maximum}-byte prompt cap. Use context index to enumerate aliases."
        if len("\n".join([*lines, notice]).encode("utf-8")) <= maximum:
            lines.append(notice)
    rendered, _truncated = _clip_utf8("\n".join(lines).rstrip() + "\n", maximum)
    return rendered.rstrip()
