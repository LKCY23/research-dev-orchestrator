#!/usr/bin/env python3
"""Minimal CI validator for a Codex skill repository.

This intentionally avoids depending on the local Codex skill-creator package.
It checks the metadata surface that CI needs to protect before smoke tests run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_simple_yaml_mapping(text: str) -> dict[str, str]:
    """Parse the simple top-level YAML mappings used by skill metadata.

    This is not a general YAML parser. It is deliberately small so CI has no
    PyYAML dependency. Values may be quoted or unquoted scalar strings.
    """

    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


def split_frontmatter(skill_path: Path) -> tuple[dict[str, str], str]:
    text = skill_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :]).strip()
            return parse_simple_yaml_mapping(frontmatter), body

    raise ValueError("SKILL.md frontmatter is missing closing ---")


def validate_skill(root: Path) -> list[str]:
    errors: list[str] = []
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        return ["SKILL.md does not exist"]

    try:
        metadata, body = split_frontmatter(skill_path)
    except ValueError as exc:
        return [str(exc)]

    name = metadata.get("name", "").strip()
    description = metadata.get("description", "").strip()

    if not name:
        errors.append("SKILL.md frontmatter is missing name")
    elif not NAME_RE.fullmatch(name):
        errors.append("SKILL.md name must be lowercase hyphen-case")

    if not description:
        errors.append("SKILL.md frontmatter is missing description")
    elif len(description.split()) < 8:
        errors.append("SKILL.md description is too short to be useful")

    if not body:
        errors.append("SKILL.md body is empty")

    agents_path = root / "agents" / "openai.yaml"
    if agents_path.exists():
        agents_text = agents_path.read_text(encoding="utf-8")
        for required in ("interface:", "display_name:", "short_description:", "default_prompt:"):
            if required not in agents_text:
                errors.append(f"agents/openai.yaml is missing {required}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".", help="Skill repository root")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    errors = validate_skill(root)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    print("Skill metadata is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
