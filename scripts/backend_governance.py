#!/usr/bin/env python3
"""Compile durable backend governance and approved task intent for one attempt."""

from __future__ import annotations

import json
import os
import shlex
import sys
import hashlib
from pathlib import Path
from typing import Any

from agent_backends import load_backend, validate_backend, validate_project_governance
from config import load_config
from protocol import load_json
from read_policy import compile_read_policy
from strategy import canonical_digest, validate_execution_policy, validate_strategy


class BackendGovernanceError(ValueError):
    """Raised when backend governance cannot be compiled without weakening policy."""


RESOURCE_METRICS = {
    "max_model_turns": "model_turns",
    "max_input_tokens": "input_tokens",
    "max_output_tokens": "output_tokens",
    "max_cost_usd": "cost_usd",
    "max_context_tokens": "context_tokens",
    "max_no_progress_turns": "model_turns",
}


def require_resource_observability(profile: dict[str, Any], io_mode: str) -> None:
    if io_mode not in {"machine", "human"}:
        raise BackendGovernanceError(f"invalid io mode {io_mode!r}")
    budget = profile.get("resource_budget", {})
    observed = set(profile.get("usage_observability", {}).get(io_mode, []))
    missing = sorted({metric for field, metric in RESOURCE_METRICS.items() if field in budget and metric not in observed})
    if missing:
        raise BackendGovernanceError(
            f"hard resource budget is not observable for {profile.get('backend_id')} + {io_mode}: {missing}"
        )


def _merge_governance(backend_id: str, shipped: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    errors = validate_project_governance(backend_id, project)
    if errors:
        raise BackendGovernanceError("; ".join(errors))
    merged = dict(shipped)
    if backend_id == "claude-code":
        merged["disabled_plugins"] = sorted(
            set(shipped.get("disabled_plugins", [])) | set(project.get("disabled_plugins", []))
        )
        if "enable_agent_teams" in project:
            merged["enable_agent_teams"] = bool(project["enable_agent_teams"])
        if "max_tool_use_concurrency" in project:
            shipped_limit = int(shipped.get("max_tool_use_concurrency", project["max_tool_use_concurrency"]))
            merged["max_tool_use_concurrency"] = min(shipped_limit, int(project["max_tool_use_concurrency"]))
        if "enforce_spawn_limit" in project:
            merged["enforce_spawn_limit"] = bool(shipped.get("enforce_spawn_limit", False)) or bool(
                project["enforce_spawn_limit"]
            )
    elif backend_id == "codex":
        if "enable_multi_agent" in project:
            merged["enable_multi_agent"] = bool(shipped.get("enable_multi_agent", True)) and bool(
                project["enable_multi_agent"]
            )
        for field in ("max_agent_threads", "max_agent_depth"):
            if field in project:
                shipped_limit = int(shipped.get(field, project[field]))
                merged[field] = min(shipped_limit, int(project[field]))
        if "enforce_spawn_limit" in project:
            merged["enforce_spawn_limit"] = bool(shipped.get("enforce_spawn_limit", False)) or bool(
                project["enforce_spawn_limit"]
            )
    elif backend_id == "kimi-code":
        for field in ("enable_native_subagents", "enable_agent_swarm"):
            if field in project:
                merged[field] = bool(shipped.get(field, True)) and bool(project[field])
        for field in ("max_parallel_subagents", "max_agent_depth"):
            if field in project:
                merged[field] = min(int(shipped.get(field, project[field])), int(project[field]))
    elif backend_id == "opencode":
        if "enable_native_subagents" in project:
            merged["enable_native_subagents"] = bool(shipped.get("enable_native_subagents", True)) and bool(
                project["enable_native_subagents"]
            )
        if "allowed_subagent_types" in project:
            shipped_types = set(shipped.get("allowed_subagent_types", []))
            merged["allowed_subagent_types"] = sorted(shipped_types & set(project["allowed_subagent_types"]))
        for field in ("max_parallel_subagents", "max_agent_depth"):
            if field in project:
                merged[field] = min(int(shipped.get(field, project[field])), int(project[field]))
        if "pure_mode" in project:
            merged["pure_mode"] = bool(shipped.get("pure_mode", False)) or bool(project["pure_mode"])
    return merged


def compile_backend_profile(
    *,
    repo_root: Path,
    task_dir: Path,
    backend_id: str,
    phase: str,
    strategy_path: Path | None = None,
    io_mode: str | None = None,
) -> dict[str, Any]:
    if phase not in {"planning", "execution"}:
        raise BackendGovernanceError("phase must be planning or execution")
    backend = load_backend(backend_id)
    backend_errors = validate_backend(backend)
    if backend_errors:
        raise BackendGovernanceError("; ".join(backend_errors))
    config_result = load_config(repo_root, use_env=False)
    if config_result.errors:
        raise BackendGovernanceError("; ".join(config_result.errors))
    project_policy = config_result.config.backend_policies.get(backend_id, {})
    governance = _merge_governance(backend_id, backend.get("governance", {}), project_policy)
    policy = validate_execution_policy(load_json(task_dir / "EXECUTION_POLICY.json"))
    status_path = task_dir / "STATUS.json"
    status = load_json(status_path) if status_path.exists() else {}
    task_profile = status.get("profile", "full") if isinstance(status, dict) else "full"
    if task_profile not in {"direct", "delegated", "full"}:
        raise BackendGovernanceError(f"invalid task execution profile {task_profile!r}")

    strategy: dict[str, Any] | None = None
    strategy_sha256: str | None = None
    if phase == "execution":
        if task_profile == "full" and (strategy_path is None or not strategy_path.exists()):
            raise BackendGovernanceError("execution profile requires an approved strategy path")
        if strategy_path is not None and strategy_path.exists():
            strategy = validate_strategy(load_json(strategy_path), policy)
            if strategy["backend_id"] != backend_id:
                raise BackendGovernanceError(
                    f"approved strategy backend {strategy['backend_id']!r} does not match dispatch backend {backend_id!r}"
                )
            strategy_sha256 = canonical_digest(strategy)

    global_budget = strategy["global_budget"] if strategy else policy
    agent_limits = {"max_parallel": int(global_budget["max_parallel_subagents"])}
    if backend_id in {"claude-code", "codex"}:
        agent_limits["max_spawns"] = int(global_budget["max_subagents"])
        agent_limits["enforce_max_spawns"] = bool(governance.get("enforce_spawn_limit", False))
    controls: list[dict[str, Any]] = []
    backend_settings: dict[str, Any] = {}
    environment: dict[str, str] = {}
    generated_files: list[str] = []
    native_agents_declared = any(
        workflow.get("executor", {}).get("mode") == "native_subagents"
        and int(workflow.get("executor", {}).get("max_agents", 0)) > 0
        for workflow in (strategy.get("workflows", []) if strategy else [])
    )
    independent_review_declared = any(
        workflow.get("review", {}).get("mode") == "independent"
        for workflow in (strategy.get("workflows", []) if strategy else [])
    )
    codex_stream_required = False
    read_policy = compile_read_policy(
        repo_root=repo_root,
        task_dir=task_dir,
        status=status if isinstance(status, dict) else {},
        execution_policy=policy,
    )
    generated_files.append("READ_POLICY.json")

    if backend_id == "claude-code":
        disabled = governance.get("disabled_plugins", [])
        backend_settings["enabledPlugins"] = {plugin: False for plugin in disabled}
        generated_files.append("claude-settings.json")
        controls.append({
            "name": "disabled_plugins",
            "value": disabled,
            "enforcement": "backend_settings",
            "hard": True,
        })
        teams_enabled = bool(governance.get("enable_agent_teams", False))
        requested_team = any(
            workflow.get("executor", {})
            .get("backend_options", {})
            .get("claude-code", {})
            .get("coordination") == "agent_team"
            for workflow in (strategy.get("workflows", []) if strategy else [])
        )
        if requested_team and not teams_enabled:
            raise BackendGovernanceError("approved strategy requires an agent team but Claude Agent Teams are disabled")
        environment["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1" if teams_enabled else "0"
        controls.append({
            "name": "agent_teams_enabled",
            "value": teams_enabled,
            "enforcement": "backend_native",
            "hard": False,
        })
        concurrency = int(governance.get("max_tool_use_concurrency", 1))
        environment["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"] = str(concurrency)
        controls.append({
            "name": "max_tool_use_concurrency",
            "value": concurrency,
            "enforcement": "backend_native",
            "hard": True,
        })
    elif backend_id == "codex":
        governance_enabled = bool(governance.get("enable_multi_agent", False))
        depth_limit = int(governance.get("max_agent_depth", 1))
        if phase == "execution" and native_agents_declared and not governance_enabled:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but Codex multi-agent is disabled"
            )
        if phase == "execution" and native_agents_declared and depth_limit < 1:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but Codex max_agent_depth is zero"
            )
        multi_agent_enabled = governance_enabled and depth_limit >= 1 and (
            phase == "planning" or native_agents_declared
        )
        parallel_limit = min(
            int(governance.get("max_agent_threads", agent_limits["max_parallel"])),
            agent_limits["max_parallel"],
        )
        codex_stream_required = multi_agent_enabled and (
            bool(agent_limits["enforce_max_spawns"]) or independent_review_declared
        )
        backend_settings["config_overrides"] = [
            f"features.multi_agent={'true' if multi_agent_enabled else 'false'}",
            "features.multi_agent_v2=false",
            "features.enable_fanout=false",
            f"agents.max_threads={parallel_limit}",
            f"agents.max_depth={depth_limit}",
        ]
        codex_read_hook = Path(__file__).resolve().parent / "codex_read_policy_hook.py"
        codex_read_command = shlex.join([os.fspath(Path(sys.executable)), os.fspath(codex_read_hook)])
        backend_settings["config_overrides"].append(
            "hooks.PreToolUse=[{ matcher = \"^Bash$\", hooks = [{ type = \"command\", command = "
            + json.dumps(codex_read_command)
            + ", timeout = 5 }] }]"
        )
        backend_settings["stream_monitor_required"] = codex_stream_required
        controls.extend([
            {
                "name": "multi_agent_enabled",
                "value": multi_agent_enabled,
                "enforcement": "backend_settings",
                "hard": True,
            },
            {
                "name": "max_agent_threads",
                "value": parallel_limit,
                "enforcement": "backend_native",
                "hard": True,
            },
            {
                "name": "max_agent_depth",
                "value": depth_limit,
                "enforcement": "backend_native",
                "hard": True,
            },
            {
                "name": "unobservable_agent_fanout",
                "value": False,
                "enforcement": "backend_settings",
                "hard": True,
            },
        ])
    elif backend_id == "kimi-code":
        governance_enabled = bool(governance.get("enable_native_subagents", False))
        depth_limit = int(governance.get("max_agent_depth", 1))
        if phase == "execution" and native_agents_declared and not governance_enabled:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but Kimi native subagents are disabled"
            )
        if phase == "execution" and native_agents_declared and depth_limit < 1:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but Kimi max_agent_depth is zero"
            )
        native_enabled = governance_enabled and depth_limit >= 1 and (
            phase == "planning" or native_agents_declared
        )
        swarm_enabled = native_enabled and bool(governance.get("enable_agent_swarm", False))
        parallel_limit = min(
            int(governance.get("max_parallel_subagents", agent_limits["max_parallel"])),
            agent_limits["max_parallel"],
        )
        environment["KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY"] = str(parallel_limit)
        backend_settings.update({
            "native_subagents_enabled": native_enabled,
            "agent_swarm_enabled": swarm_enabled,
            "max_parallel_subagents": parallel_limit,
            "max_agent_depth": min(depth_limit, 1),
        })
        generated_files.append("kimi-governance.toml")
        controls.extend([
            {"name": "native_subagents_enabled", "value": native_enabled,
             "enforcement": "backend_permission", "hard": True},
            {"name": "agent_swarm_enabled", "value": swarm_enabled,
             "enforcement": "backend_permission", "hard": True},
            {"name": "max_parallel_subagents", "value": parallel_limit,
             "enforcement": "backend_native_and_rdo_hook", "hard": False},
            {"name": "max_agent_depth", "value": min(depth_limit, 1),
             "enforcement": "backend_native", "hard": True},
        ])
    elif backend_id == "opencode":
        governance_enabled = bool(governance.get("enable_native_subagents", False))
        depth_limit = int(governance.get("max_agent_depth", 1))
        allowed_types = sorted(set(governance.get("allowed_subagent_types", [])))
        if phase == "execution" and native_agents_declared and not governance_enabled:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but OpenCode native subagents are disabled"
            )
        if phase == "execution" and native_agents_declared and depth_limit < 1:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but OpenCode max_agent_depth is zero"
            )
        if phase == "execution" and native_agents_declared and not allowed_types:
            raise BackendGovernanceError(
                "approved strategy requires native subagents but OpenCode has no allowed subagent types"
            )
        native_enabled = governance_enabled and depth_limit >= 1 and bool(allowed_types) and (
            phase == "planning" or native_agents_declared
        )
        parallel_limit = min(
            int(governance.get("max_parallel_subagents", agent_limits["max_parallel"])),
            agent_limits["max_parallel"],
        )
        backend_settings.update({
            "native_subagents_enabled": native_enabled,
            "allowed_subagent_types": allowed_types if native_enabled else [],
            "max_parallel_subagents": parallel_limit,
            "max_agent_depth": min(depth_limit, 1),
            "pure_mode": bool(governance.get("pure_mode", False)),
            "attempt_supervisor_required": True,
            "context_plugin_enabled": not bool(governance.get("pure_mode", False)),
        })
        if backend_settings["context_plugin_enabled"]:
            generated_files.append("opencode-config/plugins/rdo-context.js")
        controls.extend([
            {"name": "native_subagents_enabled", "value": native_enabled,
             "enforcement": "rdo_permission_supervisor", "hard": True},
            {"name": "allowed_subagent_types", "value": allowed_types if native_enabled else [],
             "enforcement": "rdo_permission_supervisor", "hard": True},
            {"name": "max_parallel_subagents", "value": parallel_limit,
             "enforcement": "rdo_permission_supervisor", "hard": True},
            {"name": "max_agent_depth", "value": min(depth_limit, 1),
             "enforcement": "rdo_session_supervisor", "hard": True},
        ])

    controls.append({
        "name": "native_agent_limits",
        "value": agent_limits,
        "enforcement": (
            "rdo_hook" if backend_id == "claude-code"
            else "rdo_stream_supervisor" if backend_id == "codex" and codex_stream_required
            else "rdo_permission_supervisor" if backend_id == "opencode"
            else "backend_native_and_rdo_hook" if backend_id == "kimi-code"
            else "observed"
        ),
        "hard": backend_id in {"claude-code", "opencode"}
        or (backend_id == "codex" and codex_stream_required),
    })
    if backend_id == "claude-code":
        context_adapter = {"id": "claude-pretooluse", "version": 1,
                           "enforcement_level": "tool_blocking", "enforced_tools": ["Read", "Grep", "Glob"]}
    elif backend_id == "kimi-code":
        context_adapter = {"id": "kimi-pretooluse", "version": 1,
                           "enforcement_level": "fail_open_tool_blocking", "enforced_tools": ["Read", "Grep", "Glob"]}
    elif backend_id == "opencode" and backend_settings.get("context_plugin_enabled"):
        context_adapter = {"id": "opencode-tool-execute-before", "version": 1,
                           "enforcement_level": "tool_blocking", "enforced_tools": ["read", "grep", "glob"]}
    elif backend_id == "codex":
        context_adapter = {"id": "codex-pretooluse", "version": 1,
                           "enforcement_level": "best_effort", "enforced_tools": ["Bash"],
                           "known_gaps": ["unified_exec interception is incomplete", "indirect script reads are unclassified"]}
    else:
        context_adapter = {"id": "prompt-and-cli", "version": 1,
                           "enforcement_level": "advisory", "enforced_tools": []}

    profile = {
        "schema_version": 1,
        "backend_id": backend_id,
        "phase": phase,
        "task_profile": task_profile,
        "strategy_id": strategy.get("strategy_id") if strategy else None,
        "strategy_revision": strategy.get("revision") if strategy else None,
        "strategy_sha256": strategy_sha256,
        "capabilities": backend.get("capabilities", {}),
        "context_access": {
            "policy": read_policy,
            "capability_contract_version": 1,
            "adapter": context_adapter,
            "broker": {"id": "context-broker-cli", "version": 1},
        },
        "usage_observability": backend.get("usage_observability", {}),
        "resource_budget": strategy.get("resource_budget", {}) if strategy else {},
        "governance": governance,
        "controls": controls,
        "native_agent_limits": agent_limits,
        "backend_settings": backend_settings,
        "environment": environment,
        "generated_files": generated_files,
        "unsupported_requests": [],
        "external_limitations": (
            ["organization-managed Claude settings may override ordinary plugin settings"]
            if backend_id == "claude-code" and governance.get("disabled_plugins")
            else ["Kimi hooks are fail-open on timeout or hook failure; native swarm and background limits remain independent"]
            if backend_id == "kimi-code"
            else []
        ),
    }
    profile["profile_sha256"] = canonical_digest(profile)
    if io_mode is not None:
        require_resource_observability(profile, io_mode)
    return profile


def materialize_backend_profile(profile: dict[str, Any], runtime_dir: Path) -> dict[str, Any]:
    expected = profile.get("profile_sha256")
    unsigned = dict(profile)
    unsigned.pop("profile_sha256", None)
    if expected != canonical_digest(unsigned):
        raise BackendGovernanceError("compiled backend profile digest is invalid")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    read_policy = profile.get("context_access", {}).get("policy")
    if not isinstance(read_policy, dict):
        raise BackendGovernanceError("compiled backend profile is missing context access policy")
    read_policy_path = runtime_dir / "READ_POLICY.json"
    _atomic_json(read_policy_path, read_policy)
    settings_path: Path | None = None
    if profile["backend_id"] == "claude-code":
        settings_path = runtime_dir / "claude-settings.json"
        settings = dict(profile.get("backend_settings", {}))
        hook_script = Path(__file__).resolve().parent / "claude_governance_hook.py"
        read_hook_script = Path(__file__).resolve().parent / "read_policy_hook.py"
        hook_command = lambda event: shlex.join([
            os.fspath(Path(sys.executable)),
            os.fspath(hook_script),
            "--runtime-dir",
            os.fspath(runtime_dir),
            "--event",
            event,
        ])
        settings["hooks"] = {
            "PreToolUse": [
                {
                    "matcher": "Agent|Task",
                    "hooks": [{"type": "command", "command": hook_command("pre-tool-use"), "timeout": 5}],
                },
                {
                    "matcher": "Read|Grep|Glob",
                    "hooks": [{
                        "type": "command",
                        "command": shlex.join([
                            os.fspath(Path(sys.executable)),
                            os.fspath(read_hook_script),
                            "--runtime-dir",
                            os.fspath(runtime_dir),
                            "--backend",
                            "claude-code",
                        ]),
                        "timeout": 5,
                    }],
                },
            ],
            "PostToolUse": [{
                "matcher": "Agent|Task",
                "hooks": [{"type": "command", "command": hook_command("post-tool-use"), "timeout": 5}],
            }],
            "PostToolUseFailure": [{
                "matcher": "Agent|Task",
                "hooks": [{"type": "command", "command": hook_command("post-tool-use"), "timeout": 5}],
            }],
            "SubagentStart": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": hook_command("subagent-start"), "timeout": 5}],
            }],
            "SubagentStop": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": hook_command("subagent-stop"), "timeout": 5}],
            }],
        }
        _atomic_json(settings_path, settings)
    elif profile["backend_id"] == "opencode":
        if profile.get("backend_settings", {}).get("context_plugin_enabled"):
            config_dir = runtime_dir / "opencode-config"
            plugin_path = config_dir / "plugins" / "rdo-context.js"
            plugin_path.parent.mkdir(parents=True, exist_ok=True)
            adapter_script = Path(__file__).resolve().parent / "read_policy_hook.py"
            plugin = f'''import {{ spawnSync }} from "node:child_process"

const python = {json.dumps(os.fspath(Path(sys.executable)))}
const adapter = {json.dumps(os.fspath(adapter_script))}
const runtime = {json.dumps(os.fspath(runtime_dir))}

export const RdoContextPolicy = async () => ({{
  "tool.execute.before": async (input, output) => {{
    if (!["read", "grep", "glob"].includes(input.tool)) return
    const payload = JSON.stringify({{ tool_name: input.tool, tool_input: output.args ?? {{}} }})
    const result = spawnSync(python, [adapter, "--runtime-dir", runtime, "--backend", "opencode", "--format", "decision"], {{
      input: payload,
      encoding: "utf8",
    }})
    if (result.status !== 0) throw new Error(result.stderr || "RDO context policy adapter failed")
    const decision = JSON.parse(result.stdout)
    if (decision.decision === "deny") throw new Error(decision.reason)
  }},
}})
'''
            _atomic_text(plugin_path, plugin)
            settings_path = plugin_path
    elif profile["backend_id"] == "kimi-code":
        settings_path = runtime_dir / "kimi-governance.toml"
        settings = profile.get("backend_settings", {})
        hook_script = Path(__file__).resolve().parent / "kimi_governance_hook.py"

        def hook_command(event: str) -> str:
            return shlex.join([
                os.fspath(Path(sys.executable)),
                os.fspath(hook_script),
                "--runtime-dir",
                os.fspath(runtime_dir),
                "--event",
                event,
            ])

        read_hook_script = Path(__file__).resolve().parent / "read_policy_hook.py"
        read_hook_command = shlex.join([
            os.fspath(Path(sys.executable)), os.fspath(read_hook_script),
            "--runtime-dir", os.fspath(runtime_dir), "--backend", "kimi-code",
        ])

        native_enabled = bool(settings.get("native_subagents_enabled", False))
        swarm_enabled = bool(settings.get("agent_swarm_enabled", False))
        fragment = [
            "# Generated by RDO for this attempt. Appended after the user configuration.",
            "[[permission.rules]]",
            f'decision = {json.dumps("allow" if native_enabled else "deny")}',
            'pattern = "Agent"',
            'reason = "RDO approved execution strategy"',
            "",
            "[[permission.rules]]",
            f'decision = {json.dumps("allow" if swarm_enabled else "deny")}',
            'pattern = "AgentSwarm"',
            'reason = "RDO approved execution strategy"',
            "",
            "[[hooks]]",
            'event = "PreToolUse"',
            'matcher = "Read|Grep|Glob"',
            f"command = {json.dumps(read_hook_command)}",
            "timeout = 5",
            "",
        ]
        for kimi_event, internal_event in (
            ("PreToolUse", "pre-tool-use"),
            ("PostToolUse", "post-tool-use"),
            ("PostToolUseFailure", "post-tool-use"),
            ("SubagentStart", "subagent-start"),
            ("SubagentStop", "subagent-stop"),
        ):
            fragment.extend([
                "[[hooks]]",
                f"event = {json.dumps(kimi_event)}",
                'matcher = "Agent|AgentSwarm"' if "ToolUse" in kimi_event else 'matcher = "*"',
                f"command = {json.dumps(hook_command(internal_event))}",
                "timeout = 5",
                "",
            ])
        _atomic_text(settings_path, "\n".join(fragment))
    profile_path = runtime_dir / "BACKEND_PROFILE.json"
    _atomic_json(profile_path, profile)
    return {
        "profile_path": str(profile_path),
        "profile_sha256": expected,
        "settings_path": str(settings_path) if settings_path else "",
        "settings_sha256": _file_sha256(settings_path) if settings_path else "",
        "read_policy_path": str(read_policy_path),
        "read_policy_sha256": _file_sha256(read_policy_path),
        "environment": profile.get("environment", {}),
    }


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
