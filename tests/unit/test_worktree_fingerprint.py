from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from worktree_fingerprint import fingerprint


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments],
        cwd=cwd,
        text=True,
    ).strip()


class WorktreeFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        git(self.root, "config", "user.email", "fingerprint@example.com")
        git(self.root, "config", "user.name", "Fingerprint Test")
        (self.root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(self.root, "add", "tracked.txt")
        git(self.root, "commit", "-m", "base")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_gitlink_uses_index_commit_for_initialized_and_absent_directory(self) -> None:
        submodule = self.root / "vendor" / "dependency"
        submodule.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=submodule,
            check=True,
            capture_output=True,
        )
        git(submodule, "config", "user.email", "dependency@example.com")
        git(submodule, "config", "user.name", "Dependency Test")
        (submodule / "dependency.txt").write_text("dependency\n", encoding="utf-8")
        git(submodule, "add", "dependency.txt")
        git(submodule, "commit", "-m", "dependency")
        commit = git(submodule, "rev-parse", "HEAD")
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},vendor/dependency",
        )

        initialized = fingerprint(self.root)
        entry = next(
            item
            for item in initialized["entries"]
            if item["path"] == "vendor/dependency"
        )
        self.assertEqual(entry["kind"], "gitlink")
        self.assertEqual(entry["mode"], "160000")
        self.assertEqual(entry["git_oid"], commit)

        shutil.rmtree(submodule)
        uninitialized = fingerprint(self.root)
        self.assertEqual(initialized, uninitialized)

    def test_regular_file_fingerprint_remains_content_sensitive(self) -> None:
        before = fingerprint(self.root)
        (self.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        after = fingerprint(self.root)

        self.assertNotEqual(before["sha256"], after["sha256"])
        entry = next(
            item for item in after["entries"] if item["path"] == "tracked.txt"
        )
        self.assertEqual(entry["kind"], "file")
        self.assertNotIn("git_oid", entry)


if __name__ == "__main__":
    unittest.main()
