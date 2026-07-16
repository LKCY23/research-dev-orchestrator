import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.light_bench import bench


RUN_LIFECYCLE_INTEGRATION = os.environ.get("RDO_RUN_LIGHT_BENCH_INTEGRATION") == "1"


class LightBenchTests(unittest.TestCase):
    def test_run_rejects_output_inside_measured_rdo_root_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            rdo_root = Path(temporary) / "rdo"
            dispatch = rdo_root / "scripts" / "dispatch_agent.sh"
            dispatch.parent.mkdir(parents=True)
            dispatch.write_text("#!/bin/sh\n", encoding="utf-8")
            output = rdo_root / "results"
            args = bench.build_parser().parse_args([
                "run",
                "--case", "L01-located-fix",
                "--profile", "direct",
                "--backend", "codex",
                "--rdo-root", str(rdo_root),
                "--output", str(output),
            ])
            with mock.patch.object(bench, "run_one") as run_one:
                with self.assertRaisesRegex(ValueError, "outside measured RDO root"):
                    bench.command_run(args)
            run_one.assert_not_called()
            self.assertFalse(output.exists())

    def test_ab_rejects_nonempty_output_before_loading_stale_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            candidate = root / "candidate"
            for rdo_root in (baseline, candidate):
                dispatch = rdo_root / "scripts" / "dispatch_agent.sh"
                dispatch.parent.mkdir(parents=True)
                dispatch.write_text("#!/bin/sh\n", encoding="utf-8")
            output = root / "results"
            output.mkdir()
            (output / "stale.json").write_text("{}\n", encoding="utf-8")
            args = bench.build_parser().parse_args([
                "ab",
                "--case", "L01-located-fix",
                "--profile", "direct",
                "--backend", "codex",
                "--baseline-rdo", str(baseline),
                "--candidate-rdo", str(candidate),
                "--model-label", "same-model",
                "--output", str(output),
            ])
            with mock.patch.object(bench, "run_one") as run_one:
                with self.assertRaisesRegex(ValueError, "new or empty"):
                    bench.command_ab(args)
            run_one.assert_not_called()

    def test_ab_rejects_identical_baseline_and_candidate_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            rdo_root = Path(temporary) / "rdo"
            dispatch = rdo_root / "scripts" / "dispatch_agent.sh"
            dispatch.parent.mkdir(parents=True)
            dispatch.write_text("#!/bin/sh\n", encoding="utf-8")
            args = bench.build_parser().parse_args([
                "ab",
                "--case", "L01-located-fix",
                "--profile", "direct",
                "--backend", "codex",
                "--baseline-rdo", str(rdo_root),
                "--candidate-rdo", str(rdo_root),
                "--model-label", "same-model",
                "--output", str(Path(temporary) / "output"),
            ])
            with mock.patch.object(bench, "run_one") as run_one:
                with self.assertRaisesRegex(ValueError, "must be distinct"):
                    bench.command_ab(args)
            run_one.assert_not_called()

    def test_cases_are_discoverable_and_complete(self):
        cases = bench.discover_cases()
        self.assertEqual(
            {"L01-located-fix", "L02-context-needle", "L03-cross-file-feature"},
            set(cases),
        )
        for case in cases.values():
            self.assertEqual([], bench.validate_case_files(case))
            self.assertTrue(case.digest)

    def test_setup_patch_is_the_only_initial_commit_and_verifier_fails(self):
        case = bench.discover_cases()["L01-located-fix"]
        with tempfile.TemporaryDirectory() as temporary:
            repo, _ = bench.prepare_repo(case, Path(temporary))
            count = subprocess.check_output(
                ["git", "rev-list", "--count", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertEqual("1", count)
            result = bench.run_capture(
                bench.verifier_argv(case, repo), cwd=repo, timeout=30
            )
            self.assertEqual(1, result.returncode)

    def test_full_profile_override_is_materialized_without_changing_case(self):
        case = bench.discover_cases()["L03-cross-file-feature"]
        original = json.loads(
            (case.task_dir / "EXECUTION_POLICY.json").read_text(encoding="utf-8")
        )
        self.assertFalse(original["strategy_required"])
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            policy = bench.copy_task_inputs(case, task, "full")
            self.assertTrue(policy["strategy_required"])
        unchanged = json.loads(
            (case.task_dir / "EXECUTION_POLICY.json").read_text(encoding="utf-8")
        )
        self.assertFalse(unchanged["strategy_required"])

    def test_compare_uses_medians_and_does_not_create_a_composite_score(self):
        def result(wall, turns, reads, passed=True):
            return {
                "kind": "rdo_light_bench_result",
                "case": {
                    "id": "L01",
                    "digest": "digest",
                    "profile": "direct",
                },
                "provenance": {
                    "backend": {"id": "claude-code", "version": "1"},
                    "model_label": "model",
                    "configured_models": ["model-id"],
                    "permission_mode": "auto",
                },
                "outcome": {"passed": passed},
                "timing": {
                    "total_seconds": wall,
                    "worker_elapsed_seconds": wall,
                    "time_to_first_change_seconds": wall / 2,
                    "last_change_to_handoff_seconds": 1,
                },
                "usage": {
                    "model_turns": turns,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0,
                },
                "context_access": {
                    "access_checks": reads,
                    "unique_paths": reads,
                    "repeated_read_requests": 0,
                    "repo_wide_searches": 0,
                    "unbounded_read_source_bytes": 0,
                },
                "protocol_activity": {"commands": {"acceptance": 1}},
            }

        payload = bench.compare_results(
            [result(10, 4, 6), result(14, 6, 8)],
            [result(8, 3, 4), result(10, 5, 6)],
        )
        self.assertEqual(1, payload["matched_groups"])
        wall = payload["rows"][0]["metrics"]["timing.total_seconds"]["passed_runs"]
        self.assertEqual(12, wall["baseline_median"])
        self.assertEqual(9, wall["candidate_median"])
        self.assertEqual(2, wall["baseline_samples"])
        self.assertNotIn("score", payload)

    def test_compare_separates_all_run_consumption_from_passed_run_performance(self):
        def result(wall, passed):
            return {
                "case": {"id": "L01", "digest": "d", "profile": "direct"},
                "provenance": {
                    "backend": {"id": "claude-code", "version": "1"},
                    "model_label": "model",
                    "configured_models": ["model-id"],
                    "permission_mode": "auto",
                },
                "outcome": {"passed": passed},
                "timing": {"total_seconds": wall},
            }

        payload = bench.compare_results(
            [result(10, True), result(100, False)],
            [result(8, True), result(1, False)],
        )
        wall = payload["rows"][0]["metrics"]["timing.total_seconds"]
        self.assertEqual(55, wall["all_runs"]["baseline_median"])
        self.assertEqual(4.5, wall["all_runs"]["candidate_median"])
        self.assertEqual(10, wall["passed_runs"]["baseline_median"])
        self.assertEqual(8, wall["passed_runs"]["candidate_median"])
        self.assertEqual(1, wall["passed_runs"]["baseline_samples"])

    def test_compare_rejects_no_comparable_groups(self):
        baseline = {
            "kind": "rdo_light_bench_result",
            "case": {"id": "L01", "digest": "same", "profile": "direct"},
            "provenance": {
                "backend": {"id": "claude-code", "version": "1"},
                "model_label": "model-a",
                "configured_models": ["configured-a"],
                "permission_mode": "auto",
            },
            "outcome": {"passed": True},
        }
        candidate = json.loads(json.dumps(baseline))
        candidate["provenance"]["model_label"] = "model-b"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left.json"
            right = root / "right.json"
            left.write_text(json.dumps(baseline), encoding="utf-8")
            right.write_text(json.dumps(candidate), encoding="utf-8")
            args = type("Args", (), {
                "baseline": str(left), "candidate": str(right), "output": "",
            })()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(2, bench.command_compare(args))
                args.allow_partial = True
                self.assertEqual(2, bench.command_compare(args))

    def test_compare_is_incomplete_when_only_some_groups_match(self):
        def result(case_id):
            return {
                "case": {"id": case_id, "digest": case_id, "profile": "direct"},
                "provenance": {
                    "backend": {"id": "claude-code", "version": "1"},
                    "model_label": "model",
                    "configured_models": ["configured"],
                    "permission_mode": "auto",
                },
                "outcome": {"passed": True},
            }

        payload = bench.compare_results(
            [result("L01"), result("L02")],
            [result("L01")],
        )
        self.assertEqual(1, payload["matched_groups"])
        self.assertEqual(1, payload["unmatched_baseline_groups"])
        self.assertTrue(bench.comparison_incomplete(payload))

    def test_compare_is_incomplete_when_sample_counts_differ(self):
        result = {
            "case": {"id": "L01", "digest": "d", "profile": "direct"},
            "provenance": {
                "backend": {"id": "claude-code", "version": "1"},
                "model_label": "model",
                "configured_models": ["configured"],
                "permission_mode": "auto",
            },
            "outcome": {"passed": True},
        }
        payload = bench.compare_results([result, result], [result])
        self.assertEqual(1, payload["matched_groups"])
        self.assertEqual(1, payload["unequal_sample_groups"])
        self.assertTrue(bench.comparison_incomplete(payload))

    def test_ab_defaults_to_balanced_repetitions(self):
        args = bench.build_parser().parse_args([
            "ab",
            "--case", "L01-located-fix",
            "--profile", "direct",
            "--backend", "claude-code",
            "--baseline-rdo", "/baseline",
            "--candidate-rdo", "/candidate",
            "--model-label", "same-model",
        ])
        self.assertEqual(4, args.repeat)
        with self.assertRaisesRegex(ValueError, "must be even"):
            bench.command_ab(type("Args", (), {"repeat": 3, "model_label": "model"})())

    def test_source_identity_digest_tracks_untracked_file_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "bench@example.invalid"],
                           cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Bench"], cwd=repo, check=True)
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                           stdout=subprocess.DEVNULL)
            untracked = repo / "new" / "payload.txt"
            untracked.parent.mkdir()
            untracked.write_text("one\n", encoding="utf-8")
            first = bench.source_identity(repo)
            untracked.write_text("two\n", encoding="utf-8")
            second = bench.source_identity(repo)
            self.assertTrue(first["dirty"])
            self.assertNotEqual(first["dirty_sha256"], second["dirty_sha256"])

    def test_attempt_metrics_count_explicit_dot_as_repo_wide_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempts" / "A001"
            runtime = attempt / "runtime"
            runtime.mkdir(parents=True)
            (attempt / "ATTEMPT.json").write_text(
                json.dumps({"attempt_id": "A001", "runtime": {"io_mode": "machine"}}),
                encoding="utf-8",
            )
            (runtime / "BACKEND_PROFILE.json").write_text(
                json.dumps({
                    "context_access": {"adapter": {
                        "request_log": "CONTEXT_ACCESS.ndjson",
                        "telemetry_coverage": "native_tool",
                    }},
                }),
                encoding="utf-8",
            )
            (runtime / "CONTEXT_ACCESS.ndjson").write_text(
                json.dumps({
                    "event": "context_access", "operation": "Grep", "path": ".",
                    "decision": "allow", "coverage": "native_tool",
                }) + "\n",
                encoding="utf-8",
            )
            _, metrics = bench.collect_attempt_metrics(Path(temporary))
            self.assertEqual(1, metrics["context"]["repo_wide_searches"])
            self.assertEqual(0, metrics["context"]["repeated_read_requests"])

    def test_declared_but_missing_context_log_is_not_reported_as_zero_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempts" / "A001"
            runtime = attempt / "runtime"
            runtime.mkdir(parents=True)
            (attempt / "ATTEMPT.json").write_text(
                json.dumps({"attempt_id": "A001", "runtime": {"io_mode": "machine"}}),
                encoding="utf-8",
            )
            (runtime / "BACKEND_PROFILE.json").write_text(
                json.dumps({
                    "context_access": {"adapter": {
                        "request_log": "CONTEXT_ACCESS.ndjson",
                        "telemetry_coverage": "native_tool",
                    }},
                }),
                encoding="utf-8",
            )
            _, metrics = bench.collect_attempt_metrics(Path(temporary))
            context = metrics["context"]
            self.assertTrue(context["telemetry_declared"])
            self.assertFalse(context["telemetry_initialized"])
            self.assertIsNone(context["access_checks"])

    def test_one_missing_attempt_makes_context_aggregate_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            for attempt_id in ("A001", "A002"):
                attempt = task / "attempts" / attempt_id
                runtime = attempt / "runtime"
                runtime.mkdir(parents=True)
                (attempt / "ATTEMPT.json").write_text(
                    json.dumps({
                        "attempt_id": attempt_id,
                        "runtime": {"io_mode": "machine"},
                    }),
                    encoding="utf-8",
                )
                (runtime / "BACKEND_PROFILE.json").write_text(
                    json.dumps({
                        "context_access": {"adapter": {
                            "request_log": "CONTEXT_ACCESS.ndjson",
                            "telemetry_coverage": "native_tool",
                        }},
                    }),
                    encoding="utf-8",
                )
            (task / "attempts" / "A001" / "runtime" / "CONTEXT_ACCESS.ndjson").write_text(
                json.dumps({
                    "event": "context_telemetry_initialized",
                    "coverage": "native_tool",
                }) + "\n",
                encoding="utf-8",
            )
            _, metrics = bench.collect_attempt_metrics(task)
            context = metrics["context"]
            self.assertFalse(context["telemetry_complete"])
            self.assertEqual(
                ["A002"], context["telemetry_attempts"]["missing_attempt_ids"]
            )
            self.assertIsNone(context["access_checks"])

    def test_failed_timeout_cleanup_skips_verifier_and_fails_sample(self):
        case = bench.discover_cases()["L01-located-fix"]
        timed_out = bench.CommandResult(("dispatch",), 124, 1.0, timed_out=True)
        cleanup_failed = bench.CommandResult(
            ("terminate",), 0, 0.1,
            stdout=json.dumps({"status": "terminated", "surviving_pids": [123]}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(bench, "run_logged", return_value=timed_out),
                mock.patch.object(
                    bench,
                    "terminate_timed_out_worker",
                    return_value=cleanup_failed,
                ),
                mock.patch.object(
                    bench,
                    "backend_version",
                    return_value={
                        "binary": "claude", "path": "/fake/claude",
                        "version": "fake", "observed": True,
                    },
                ),
            ):
                result = bench.run_one(
                    case=case,
                    profile="direct",
                    backend="claude-code",
                    rdo_root=ROOT,
                    output_root=root / "results",
                    repetition=1,
                    permission="auto",
                    model_label="fake-model",
                )
        self.assertFalse(result["outcome"]["passed"])
        self.assertFalse(result["outcome"]["timed_out_worker_cleanup_ok"])
        self.assertTrue(result["outcome"]["benchmark_abort_required"])
        self.assertEqual(125, result["outcome"]["verifier_exit_code"])
        self.assertEqual(0, result["dispatches"][0]["cleanup"]["returncode"])
        self.assertEqual([123], result["dispatches"][0]["cleanup"]["surviving_pids"])

    @unittest.skipUnless(RUN_LIFECYCLE_INTEGRATION, "light-bench lifecycle integration")
    def test_direct_and_delegated_runs_exercise_public_rdo_lifecycle(self):
        case = bench.discover_cases()["L01-located-fix"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake = binary_dir / "claude"
            fake_source = """#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("fake-claude 1.0")
    raise SystemExit(0)

prompt = sys.argv[-1]
task = Path(re.search(r"^- TASK_DIR: (.+)$", prompt, re.M).group(1))
attempt = Path(re.search(r"^- ATTEMPT_DIR: (.+)$", prompt, re.M).group(1))
print(json.dumps({"type": "system", "subtype": "init", "session_id": "11111111-1111-1111-1111-111111111111"}), flush=True)
target = Path.cwd() / "src" / "miniqueue" / "retry.py"
text = target.read_text(encoding="utf-8")
target.write_text(text.replace("self.multiplier ** attempt", "self.multiplier ** (attempt - 1)"), encoding="utf-8")
subprocess.run(["git", "add", str(target)], check=True)
subprocess.run(["git", "commit", "-m", "fix retry delay"], check=True, stdout=subprocess.DEVNULL)
rdo = [sys.executable, "__RDO_PATH__"]
subprocess.run(rdo + ["check", "--attempt-dir", str(attempt), "--check-id", "visible_tests"], check=True, stdout=subprocess.DEVNULL)
print(json.dumps({"type": "assistant", "message": {"id": "turn-1", "usage": {"input_tokens": 10, "output_tokens": 5}}}), flush=True)
profile = json.loads((task / "STATUS.json").read_text(encoding="utf-8"))["profile"]
finalize = rdo + ["finalize", "--attempt-dir", str(attempt), "--state", "verified" if profile == "direct" else "review", "--summary", "fake worker complete"]
if profile == "direct":
    finalize.append("--self-review-passed")
subprocess.run(finalize, check=True, stdout=subprocess.DEVNULL)
"""
            fake.write_text(
                fake_source.replace("__RDO_PATH__", str(ROOT / "scripts" / "rdo.py")),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(binary_dir) + os.pathsep + old_path
            try:
                results = {
                    profile: bench.run_one(
                        case=case,
                        profile=profile,
                        backend="claude-code",
                        rdo_root=ROOT,
                        output_root=root / "results" / profile,
                        repetition=1,
                        permission="auto",
                        model_label="fake-model",
                    )
                    for profile in ("direct", "delegated")
                }
            finally:
                os.environ["PATH"] = old_path
            for profile, terminal_state in (("direct", "verified"), ("delegated", "review")):
                result = results[profile]
                logs = "\n".join(
                    Path(item["log"]).read_text(encoding="utf-8", errors="replace")
                    for item in result["dispatches"]
                )
                self.assertTrue(result["outcome"]["passed"], logs or result)
                self.assertEqual(terminal_state, result["outcome"]["terminal_state"])
                self.assertEqual(1, result["usage"]["model_turns"])
                self.assertEqual(0, result["context_access"]["access_checks"])
                self.assertTrue(result["context_access"]["telemetry_initialized"])

    @unittest.skipUnless(RUN_LIFECYCLE_INTEGRATION, "light-bench lifecycle integration")
    def test_full_run_records_mechanical_strategy_approval_and_two_attempts(self):
        direct_case = bench.discover_cases()["L01-located-fix"]
        payload = dict(direct_case.payload)
        payload["profiles"] = [*payload["profiles"], "full"]
        case = bench.BenchCase(direct_case.path, payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake = binary_dir / "claude"
            fake_source = """#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("fake-claude 1.0")
    raise SystemExit(0)

prompt = sys.argv[-1]
task = Path(re.search(r"^- TASK_DIR: (.+)$", prompt, re.M).group(1))
attempt = Path(re.search(r"^- ATTEMPT_DIR: (.+)$", prompt, re.M).group(1))
rdo = [sys.executable, "__RDO_PATH__"]
session = "22222222-2222-2222-2222-222222222222"
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)

if "## Planning Phase" in prompt:
    task_id = json.loads((task / "STATUS.json").read_text())["task_id"]
    strategy = {
        "schema_version": 2,
        "backend_id": "claude-code",
        "strategy_id": f"{task_id}-S001",
        "task_id": task_id,
        "revision": 1,
        "supersedes": None,
        "objective": "Fix and verify retry delay",
        "global_budget": {"wall_seconds": 120, "max_workflows": 1, "max_workflow_instances": 1, "max_parallel_workflows": 1, "max_subagents": 1, "max_parallel_subagents": 1},
        "workflows": [{
            "workflow_id": "WF-fix",
            "kind": "implementation",
            "purpose": "Apply the focused retry fix and run acceptance",
            "depends_on": [],
            "required": True,
            "executor": {"mode": "primary_worker", "write_access": True, "max_agents": 0, "max_parallel": 0, "allowed_paths": ["src/miniqueue/retry.py"]},
            "budget": {"wall_seconds": 120, "command_seconds": 30, "max_enumerated_cases": 10, "max_instances": 1},
            "completion": {"evidence": "visible tests pass"},
            "on_timeout": "block"
        }],
        "runtime_change_policy": {"allow_new_instances_of_approved_workflows": True, "require_revision_for_new_workflow_kind": True, "require_revision_for_budget_increase": True, "allow_unbounded_search": False},
        "completion_gate": {"required_workflows_complete": True, "acceptance_commands_pass": True, "optional_workflows_may_timeout": False}
    }
    candidate = attempt / "strategy.json"
    candidate.write_text(json.dumps(strategy), encoding="utf-8")
    subprocess.run(rdo + ["strategy", "submit", "--task-dir", str(task), "--file", str(candidate)], check=True, stdout=subprocess.DEVNULL)
else:
    target = Path.cwd() / "src" / "miniqueue" / "retry.py"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("self.multiplier ** attempt", "self.multiplier ** (attempt - 1)"), encoding="utf-8")
    subprocess.run(["git", "add", str(target)], check=True)
    subprocess.run(["git", "commit", "-m", "fix retry delay"], check=True, stdout=subprocess.DEVNULL)
    instance = "WF-fix-I001"
    subprocess.run(rdo + ["workflow", "start", "--attempt-dir", str(attempt), "--workflow-id", "WF-fix", "--instance-id", instance], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(rdo + ["check", "--attempt-dir", str(attempt), "--check-id", "visible_tests", "--workflow-id", "WF-fix", "--instance-id", instance], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(rdo + ["workflow", "complete", "--attempt-dir", str(attempt), "--workflow-id", "WF-fix", "--instance-id", instance], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(rdo + ["finalize", "--attempt-dir", str(attempt), "--state", "review", "--summary", "fake Full worker complete"], check=True, stdout=subprocess.DEVNULL)

print(json.dumps({"type": "assistant", "message": {"id": "turn", "usage": {"input_tokens": 10, "output_tokens": 5}}}), flush=True)
"""
            fake.write_text(
                fake_source.replace("__RDO_PATH__", str(ROOT / "scripts" / "rdo.py")),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(binary_dir) + os.pathsep + old_path
            try:
                result = bench.run_one(
                    case=case,
                    profile="full",
                    backend="claude-code",
                    rdo_root=ROOT,
                    output_root=root / "results",
                    repetition=1,
                    permission="auto",
                    model_label="fake-model",
                )
            finally:
                os.environ["PATH"] = old_path
            logs = "\n".join(
                Path(item["log"]).read_text(encoding="utf-8", errors="replace")
                for item in result["dispatches"]
            )
            self.assertTrue(result["outcome"]["passed"], logs or result)
            self.assertEqual("protocol_auto_approve", result["scope"]["strategy_review_mode"])
            self.assertEqual(["planning", "execution"], [item["phase"] for item in result["attempts"]])
            self.assertEqual("review", result["outcome"]["terminal_state"])


if __name__ == "__main__":
    unittest.main()
