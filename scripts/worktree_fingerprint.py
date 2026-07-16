#!/usr/bin/env python3
"""Create a deterministic content fingerprint for tracked and untracked worktree files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def git_paths(root: Path, *args: str) -> list[str]:
    output = subprocess.check_output(["git", "-C", str(root), *args, "-z"])
    return sorted(item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item)


def fingerprint(root: Path) -> dict[str, object]:
    paths = sorted(set(git_paths(root, "ls-files") + git_paths(root, "ls-files", "--others", "--exclude-standard")))
    digest = hashlib.sha256()
    entries: list[dict[str, object]] = []
    for relative in paths:
        path = root / relative
        kind = "symlink" if path.is_symlink() else "file"
        try:
            raw_mode = path.lstat().st_mode
            mode = f"{stat.S_IFMT(raw_mode) | stat.S_IMODE(raw_mode):06o}"
            content = (
                os.readlink(path).encode("utf-8", errors="surrogateescape")
                if kind == "symlink"
                else path.read_bytes()
            )
        except FileNotFoundError:
            content = b"<missing>"
            kind = "missing"
            mode = None
        file_digest = hashlib.sha256(content).hexdigest()
        encoded_path = relative.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(bytes.fromhex(file_digest))
        entries.append({"path": relative, "kind": kind, "mode": mode, "sha256": file_digest})
    semantic = hashlib.sha256(
        json.dumps(
            entries,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "sha256": digest.hexdigest(),
        "semantic_sha256": semantic,
        "file_count": len(entries),
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fingerprint a Git worktree without mutating it.")
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = fingerprint(Path(args.worktree).resolve())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
