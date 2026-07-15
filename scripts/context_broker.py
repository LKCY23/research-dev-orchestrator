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

from protocol import load_json, utc_now
from read_policy import resolve_source


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded_text(data: bytes, maximum: int) -> tuple[str, bool]:
    clipped = len(data) > maximum
    return data[:maximum].decode("utf-8", errors="replace"), clipped


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
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def source_payload(policy: dict[str, Any], source: Path) -> dict[str, Any]:
    worktree = Path(policy["worktree"])
    return {
        "source": source.relative_to(worktree).as_posix(),
        "source_sha256": digest(source),
        "size_bytes": source.stat().st_size,
    }


def command_index(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    sources = [args.source] if args.source else policy.get("context_sources", [])
    if not sources:
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
    indexed = []
    maximum_sources = 100
    for raw in sources[:maximum_sources]:
        source = resolve_source(policy, raw)
        indexed.append({**source_payload(policy, source), "heading_count": len(headings(source))})
    return {"sources": indexed, "truncated": len(sources) > maximum_sources}


def command_search(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
    raw_sources = [args.source] if args.source else policy.get("context_sources", [])
    if not raw_sources:
        raise ValueError("search requires --source when EXECUTION_POLICY.json context_sources is empty")
    sources = [resolve_source(policy, raw) for raw in raw_sources]
    command = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-count", str(args.max_matches), args.query]
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


def command_get(args: argparse.Namespace, policy: dict[str, Any]) -> dict[str, Any]:
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
    result = handlers[args.action](args, policy)
    audit(policy_path, {
        "action": args.action,
        "source": getattr(args, "source", ""),
        "query": getattr(args, "query", ""),
        "section": getattr(args, "section", ""),
        "question": getattr(args, "question", ""),
        "result_sources": [item.get("source") for item in result.get("sources", [])]
        if isinstance(result.get("sources"), list) else [result.get("source")],
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"context broker error: {exc}", file=sys.stderr)
        raise SystemExit(2)
