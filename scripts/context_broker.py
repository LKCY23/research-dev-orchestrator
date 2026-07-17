#!/usr/bin/env python3
"""Deterministic, bounded repository context retrieval for workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from dependency_context import (
    DEPENDENCY_ALIAS_PREFIX,
    DependencyContextError,
    dependency_entry,
    dependency_section,
    dependency_section_values,
    dependency_source_payload,
    load_bound_dependency_context,
    render_dependency_value,
)
from protocol import load_json, utc_now
from read_policy import resolve_source


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded_text(data: bytes, maximum: int) -> tuple[str, bool]:
    clipped = len(data) > maximum
    return data[:maximum].decode("utf-8", errors="ignore"), clipped


def headings(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        match = None if fenced else HEADING_RE.match(line)
        if match:
            result.append({"level": len(match.group(1)), "title": match.group(2).strip(), "line": number})
    return result


def select_section(path: Path, query: str) -> tuple[int, int, str]:
    entries = headings(path)
    needle = query.strip().casefold()
    matches = [entry for entry in entries if entry["title"].casefold() == needle]
    if not matches:
        matches = [entry for entry in entries if entry["title"].casefold().startswith(needle)]
    if not matches:
        matches = [entry for entry in entries if needle in entry["title"].casefold()]
    if len(matches) != 1:
        choices = [entry["title"] for entry in matches or entries]
        raise ValueError(f"section must identify exactly one heading; candidates={choices[:30]}")
    selected = matches[0]
    start = int(selected["line"])
    end = len(path.read_text(encoding="utf-8").splitlines())
    for entry in entries:
        if int(entry["line"]) > start and int(entry["level"]) <= int(selected["level"]):
            end = int(entry["line"]) - 1
            break
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return start, end, "".join(lines[start - 1:end])


def audit(policy_path: Path, payload: dict[str, Any]) -> None:
    record = {"at": utc_now(), **payload}
    target = policy_path.parent / "CONTEXT_REQUESTS.ndjson"
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError(f"short context request log write: {written}/{len(data)} bytes")
    finally:
        os.close(descriptor)


def source_payload(policy: dict[str, Any], source: Path) -> dict[str, Any]:
    worktree = Path(policy["worktree"])
    return {
        "source": source.relative_to(worktree).as_posix(),
        "source_sha256": digest(source),
        "size_bytes": source.stat().st_size,
    }


def _dependency_manifest(policy_path: Path) -> dict[str, Any] | None:
    attempt_dir = policy_path.parent.parent
    if not (attempt_dir / "ATTEMPT.json").is_file():
        return None
    return load_bound_dependency_context(attempt_dir)


def _is_dependency_source(value: str) -> bool:
    return value.startswith(DEPENDENCY_ALIAS_PREFIX)


def command_index(
    args: argparse.Namespace,
    policy: dict[str, Any],
    policy_path: Path,
) -> dict[str, Any]:
    manifest = _dependency_manifest(policy_path)
    if args.source and _is_dependency_source(args.source):
        if manifest is None:
            raise ValueError("this attempt has no dependency context sources")
        entry = dependency_entry(manifest, args.source)
        sections = entry["available_sections"]
        start = args.offset
        selected = sections[start:start + args.limit]
        return {
            "sources": [{
                **dependency_source_payload(entry),
                "source_kind": "dependency",
                "sections": selected,
                "section_offset": start,
                "section_limit": args.limit,
                "section_count": len(sections),
                "truncated": start + len(selected) < len(sections),
            }]
        }
    sources = [args.source] if args.source else policy.get("context_sources", [])
    dependency_entries = (
        manifest.get("dependencies", []) if manifest is not None and not args.source else []
    )
    if not sources and not dependency_entries:
        raise ValueError("context_sources is empty; pass --source or declare it in EXECUTION_POLICY.json")
    if args.source:
        source = resolve_source(policy, args.source)
        entries = headings(source)
        start = args.offset
        selected = entries[start:start + args.limit]
        return {
            "sources": [{
                **source_payload(policy, source),
                "headings": selected,
                "heading_offset": start,
                "heading_limit": args.limit,
                "heading_count": len(entries),
                "truncated": start + len(selected) < len(entries),
            }]
        }
    indexed: list[dict[str, Any]] = []
    maximum_sources = 100
    for raw in sources[:maximum_sources]:
        source = resolve_source(policy, raw)
        indexed.append({**source_payload(policy, source), "heading_count": len(headings(source))})
    remaining = maximum_sources - len(indexed)
    for entry in dependency_entries[:max(0, remaining)]:
        indexed.append({
            **dependency_source_payload(entry),
            "source_kind": "dependency",
            "section_count": len(entry["available_sections"]),
        })
    total = len(sources) + len(dependency_entries)
    return {"sources": indexed, "truncated": total > maximum_sources}


def command_search(
    args: argparse.Namespace,
    policy: dict[str, Any],
    policy_path: Path,
) -> dict[str, Any]:
    if args.source and _is_dependency_source(args.source):
        manifest = _dependency_manifest(policy_path)
        if manifest is None:
            raise ValueError("this attempt has no dependency context sources")
        entry = dependency_entry(manifest, args.source)
        values = dependency_section_values(policy_path.parent.parent, entry)
        searchable = "\n".join(
            f"## {section}\n{render_dependency_value(values[section]).rstrip()}"
            for section in entry["available_sections"]
        ) + "\n"
        command = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(args.max_matches),
            "--",
            args.query,
        ]
        completed = subprocess.run(
            command,
            input=searchable.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        if completed.returncode not in {0, 1}:
            raise RuntimeError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or "rg failed"
            )
        maximum = int(policy.get("section_max_bytes", 16 * 1024))
        content, clipped = bounded_text(completed.stdout, maximum)
        return {
            "query": args.query,
            "sources": [{
                **dependency_source_payload(entry),
                "source_kind": "dependency",
            }],
            "content": content,
            "truncated": clipped,
            "max_bytes": maximum,
        }
    raw_sources = [args.source] if args.source else policy.get("context_sources", [])
    if not raw_sources:
        raise ValueError("search requires --source when EXECUTION_POLICY.json context_sources is empty")
    sources = [resolve_source(policy, raw) for raw in raw_sources]
    command = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-count", str(args.max_matches), "--", args.query]
    command.extend(str(path) for path in sources)
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode not in {0, 1}:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip() or "rg failed")
    maximum = int(policy.get("section_max_bytes", 16 * 1024))
    content, clipped = bounded_text(completed.stdout, maximum)
    return {
        "query": args.query,
        "sources": [source_payload(policy, path) for path in sources],
        "content": content,
        "truncated": clipped,
        "max_bytes": maximum,
    }


def command_get(
    args: argparse.Namespace,
    policy: dict[str, Any],
    policy_path: Path,
) -> dict[str, Any]:
    if _is_dependency_source(args.source):
        manifest = _dependency_manifest(policy_path)
        if manifest is None:
            raise ValueError("this attempt has no dependency context sources")
        entry = dependency_entry(manifest, args.source)
        content, field_sha256 = dependency_section(
            policy_path.parent.parent,
            entry,
            args.section,
        )
        maximum = int(policy.get("section_max_bytes", 16 * 1024))
        rendered, clipped = bounded_text(content.encode("utf-8"), maximum)
        return {
            **dependency_source_payload(entry),
            "source_kind": "dependency",
            "section": args.section,
            "question": args.question,
            "field_sha256": field_sha256,
            "content": rendered,
            "truncated": clipped,
            "max_bytes": maximum,
        }
    source = resolve_source(policy, args.source)
    start, end, content = select_section(source, args.section)
    maximum = int(policy.get("section_max_bytes", 16 * 1024))
    rendered, clipped = bounded_text(content.encode("utf-8"), maximum)
    return {
        **source_payload(policy, source),
        "section": args.section,
        "start_line": start,
        "end_line": end,
        "question": args.question,
        "content": rendered,
        "truncated": clipped,
        "max_bytes": maximum,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, help="attempt runtime/READ_POLICY.json")
    sub = parser.add_subparsers(dest="action", required=True)
    index = sub.add_parser("index")
    index.add_argument("--source", default="")
    index.add_argument("--offset", type=int, default=0)
    index.add_argument("--limit", type=int, default=50)
    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--source", default="")
    search.add_argument("--max-matches", type=int, default=50)
    get = sub.add_parser("get")
    get.add_argument("--source", required=True)
    get.add_argument("--section", required=True)
    get.add_argument("--question", required=True)
    args = parser.parse_args()
    if args.action == "index" and (args.offset < 0 or args.limit <= 0 or args.limit > 200):
        parser.error("index requires offset >= 0 and 1 <= limit <= 200")

    policy_path = Path(args.policy).resolve()
    policy = load_json(policy_path)
    handlers = {"index": command_index, "search": command_search, "get": command_get}
    try:
        result = handlers[args.action](args, policy, policy_path)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        try:
            audit(policy_path, {
                "schema_version": 1,
                "action": args.action,
                "source": getattr(args, "source", ""),
                "source_kind": (
                    "dependency"
                    if _is_dependency_source(getattr(args, "source", ""))
                    else "path"
                ),
                "query": getattr(args, "query", ""),
                "section": getattr(args, "section", ""),
                "question": getattr(args, "question", ""),
                "decision": "deny",
                "error_code": type(exc).__name__,
                "result_sources": [],
                "result_bytes": 0,
                "result_content_bytes": 0,
                "result_truncated": False,
            })
        except OSError:
            pass
        raise
    content = result.get("content")
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    nested_sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    result_truncated = bool(result.get("truncated", False)) or any(
        isinstance(item, dict) and item.get("truncated") is True
        for item in nested_sources
    )
    audit(policy_path, {
        "schema_version": 1,
        "action": args.action,
        "source": getattr(args, "source", ""),
        "query": getattr(args, "query", ""),
        "section": getattr(args, "section", ""),
        "question": getattr(args, "question", ""),
        "source_kind": (
            "dependency"
            if _is_dependency_source(getattr(args, "source", ""))
            else "path"
        ),
        "decision": "allow",
        "result_sources": [item.get("source") for item in result.get("sources", [])]
        if isinstance(result.get("sources"), list) else [result.get("source")],
        "result_bytes": len(rendered.encode("utf-8")),
        "result_content_bytes": len(content.encode("utf-8")) if isinstance(content, str) else 0,
        "result_truncated": result_truncated,
    })
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"context broker error: {exc}", file=sys.stderr)
        raise SystemExit(2)
