#!/usr/bin/env python3
"""Run one OpenCode attempt behind an RDO permission and session supervisor."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from protocol import load_json, utc_now
from usage import UsageSupervisor


def append_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


class Api:
    def __init__(self, base_url: str, password: str, cwd: str):
        self.base_url = base_url.rstrip("/")
        self.cwd = cwd
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def request(self, method: str, path: str, payload: Any = None, *, timeout: float = 10) -> Any:
        separator = "&" if "?" in path else "?"
        url = f"{self.base_url}{path}{separator}{urllib.parse.urlencode({'directory': self.cwd})}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def event_stream(self):
        url = f"{self.base_url}/event?{urllib.parse.urlencode({'directory': self.cwd})}"
        request = urllib.request.Request(url, headers=self.headers)
        return urllib.request.urlopen(request, timeout=86400)


class Guardian:
    def __init__(
        self,
        *,
        api: Api,
        runtime: Path,
        root_session: str,
        allowed_types: set[str],
        max_parallel: int,
        max_depth: int,
        permission_mode: str,
        emit_events: bool,
        stop_when_root_idle: bool,
        resource_budget: dict[str, Any] | None = None,
    ):
        self.api = api
        self.runtime = runtime
        self.root_session = root_session
        self.allowed_types = allowed_types
        self.max_parallel = max_parallel
        self.max_depth = max_depth
        self.permission_mode = permission_mode
        self.emit_events = emit_events
        self.stop_when_root_idle = stop_when_root_idle
        self.usage = UsageSupervisor(runtime, "opencode", resource_budget or {})
        self.reservations: list[dict[str, Any]] = []
        self.active: dict[str, dict[str, str]] = {}
        self.root_idle = threading.Event()
        self.ready = threading.Event()
        self.failed: Exception | None = None
        self.budget_exceeded: str | None = None
        self.stopped = threading.Event()
        self.budget_lock = threading.Lock()

    def audit(self, event: str, **fields: Any) -> None:
        append_line(self.runtime / "BACKEND_EVENTS.ndjson", {
            "at": utc_now(), "backend": "opencode", "event": event, **fields
        })

    def violation(self, reason: str, *, hard: bool = True, **fields: Any) -> None:
        append_line(self.runtime / "VIOLATIONS.ndjson", {
            "at": utc_now(),
            "backend": "opencode",
            "event": "backend_governance_violation",
            "hard": hard,
            "reason": reason,
            **fields,
        })

    @staticmethod
    def agent_type(properties: dict[str, Any]) -> str:
        metadata = properties.get("metadata") or {}
        for key in ("subagent_type", "agent", "agent_type"):
            if isinstance(metadata.get(key), str):
                return metadata[key]
        values = properties.get("patterns") or properties.get("resources") or []
        return str(values[0]) if values else ""

    def reply(self, request_id: str, allow: bool, message: str = "") -> None:
        payload = {"reply": "once" if allow else "reject"}
        if message:
            payload["message"] = message
        self.api.request("POST", f"/permission/{request_id}/reply", payload)

    def handle_permission(self, properties: dict[str, Any]) -> None:
        request_id = str(properties.get("id") or "")
        session_id = str(properties.get("sessionID") or "")
        permission = str(properties.get("permission") or properties.get("action") or "")
        if permission != "task":
            if self.permission_mode == "auto":
                self.reply(request_id, True)
                self.audit("permission_auto_approved", permission=permission, request_id=request_id)
            elif self.emit_events:
                self.reply(request_id, False, "RDO machine/default mode cannot request interactive permission")
                self.audit("permission_rejected", permission=permission, request_id=request_id)
            return

        now = time.monotonic()
        expired = [item for item in self.reservations if now - float(item["approved_at"]) > 60]
        if expired:
            self.reservations = [item for item in self.reservations if item not in expired]
            for item in expired:
                self.audit(
                    "subagent_reservation_expired",
                    request_id=item["request_id"],
                    agent_type=item["agent_type"],
                )
        agent_type = self.agent_type(properties)
        reason = ""
        if self.max_depth < 1 or session_id != self.root_session:
            reason = "native subagent depth exceeds the approved depth"
        elif agent_type not in self.allowed_types:
            reason = f"subagent type {agent_type!r} is not allowed"
        elif len(self.active) + len(self.reservations) >= self.max_parallel:
            reason = "native subagent parallel budget exhausted"
        if reason:
            self.reply(request_id, False, reason)
            self.audit("subagent_permission_rejected", request_id=request_id, reason=reason)
            return
        self.reservations.append({
            "request_id": request_id,
            "agent_type": agent_type,
            "approved_at": now,
        })
        self.reply(request_id, True)
        self.audit("subagent_permission_approved", request_id=request_id, agent_type=agent_type)

    def handle_session_created(self, properties: dict[str, Any]) -> None:
        info = properties.get("info") or {}
        session_id = str(info.get("id") or properties.get("sessionID") or "")
        parent_id = str(info.get("parentID") or "")
        if not parent_id:
            return
        if parent_id != self.root_session:
            self.violation("nested or foreign child session created", session_id=session_id, parent_id=parent_id)
            self.api.request("POST", f"/session/{session_id}/abort")
            return
        agent_type = str(info.get("agent") or "")
        match = next(
            (index for index, item in enumerate(self.reservations)
             if not agent_type or item["agent_type"] == agent_type),
            None,
        )
        if match is None:
            self.violation("unapproved child session created", session_id=session_id, agent_type=agent_type)
            self.api.request("POST", f"/session/{session_id}/abort")
            return
        reservation = self.reservations.pop(match)
        self.active[session_id] = {
            "agent_type": reservation["agent_type"],
            "request_id": reservation["request_id"],
        }
        self.audit("subagent_started", session_id=session_id, agent_type=reservation["agent_type"])

    def handle(self, event: dict[str, Any]) -> None:
        reason = self.usage.observe(event)
        if reason:
            self.abort_for_budget(reason)
            return
        event_type = event.get("type")
        properties = event.get("properties") or {}
        if self.emit_events:
            print(json.dumps(event, separators=(",", ":")), flush=True)
        if event_type in {"permission.asked", "permission.v2.asked"}:
            self.handle_permission(properties)
        elif event_type == "session.created":
            self.handle_session_created(properties)
        elif event_type == "session.idle":
            session_id = str(properties.get("sessionID") or "")
            if session_id == self.root_session:
                self.root_idle.set()
            elif session_id in self.active:
                details = self.active.pop(session_id)
                self.audit("subagent_stopped", session_id=session_id, **details)

    def abort_for_budget(self, reason: str) -> None:
        with self.budget_lock:
            if self.budget_exceeded:
                return
            self.budget_exceeded = reason
            self.audit("root_session_aborted", reason=reason)
            try:
                self.api.request("POST", f"/session/{self.root_session}/abort")
            finally:
                self.root_idle.set()

    def watch_budget_clock(self) -> None:
        while not self.stopped.wait(0.25):
            reason = self.usage.check_clock()
            if reason:
                self.abort_for_budget(reason)
                return

    def run(self) -> None:
        watchdog = threading.Thread(target=self.watch_budget_clock, name="opencode-budget", daemon=True)
        watchdog.start()
        try:
            with self.api.event_stream() as response:
                self.ready.set()
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload:
                        self.handle(json.loads(payload))
                    if self.stop_when_root_idle and self.root_idle.is_set():
                        return
        except Exception as exc:
            self.failed = exc
            self.ready.set()
            self.root_idle.set()
        finally:
            self.stopped.set()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def opencode_config(settings: dict[str, Any]) -> dict[str, Any]:
    allowed = settings.get("allowed_subagent_types", [])
    task_rules = {"*": "ask"} if settings.get("native_subagents_enabled") else {"*": "deny"}
    agents = {name: {"permission": {"task": "deny"}} for name in allowed}
    return {"permission": {"task": task_rules}, "agent": agents}


def opencode_attach_command(
    base_url: str, cwd: str, session_id: str, password: str
) -> list[str]:
    # OpenCode 1.17 parses the positional URL reliably when it follows options.
    return [
        "opencode",
        "attach",
        "--dir",
        cwd,
        "--session",
        session_id,
        "--password",
        password,
        base_url,
    ]


def wait_for_server(api: Api, process: subprocess.Popen[Any]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"OpenCode server exited before startup with status {process.returncode}")
        try:
            api.request("GET", "/global/health", timeout=1)
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("OpenCode server did not become healthy within 15 seconds")


def stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--io-mode", choices=["machine", "human"], required=True)
    parser.add_argument("--permission-mode", choices=["default", "auto"], required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--execution-mode", choices=["start", "resume", "replace"], default="start")
    parser.add_argument("--session-id", default="")
    args = parser.parse_args()

    runtime = Path(args.runtime_dir).resolve()
    profile = load_json(runtime / "BACKEND_PROFILE.json")
    settings = profile.get("backend_settings", {})
    port = free_port()
    password = secrets.token_urlsafe(24)
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["OPENCODE_SERVER_PASSWORD"] = password
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(opencode_config(settings), separators=(",", ":"))
    command = ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    if settings.get("pure_mode"):
        command.append("--pure")
    server_log = (runtime / "opencode-server.log").open("a", encoding="utf-8")
    server = subprocess.Popen(
        command,
        cwd=args.cwd,
        env=environment,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    try:
        api = Api(base_url, password, args.cwd)
        wait_for_server(api, server)
        if args.execution_mode == "resume":
            if not args.session_id:
                raise RuntimeError("OpenCode resume requires --session-id")
            root_id = args.session_id
        else:
            root = api.request("POST", "/session", {"title": "RDO attempt"})
            root_id = str(root["id"])
        (runtime / "SESSION.json").write_text(
            json.dumps({"backend_id": "opencode", "session_id": root_id}) + "\n",
            encoding="utf-8",
        )
        guardian = Guardian(
            api=api,
            runtime=runtime,
            root_session=root_id,
            allowed_types=set(settings.get("allowed_subagent_types", [])),
            max_parallel=int(settings.get("max_parallel_subagents", 1)),
            max_depth=int(settings.get("max_agent_depth", 0)),
            permission_mode=args.permission_mode,
            emit_events=args.io_mode == "machine",
            stop_when_root_idle=args.io_mode == "machine",
            resource_budget=profile.get("resource_budget", {}),
        )
        thread = threading.Thread(target=guardian.run, name="opencode-guardian", daemon=True)
        thread.start()
        if not guardian.ready.wait(timeout=5) or guardian.failed:
            raise RuntimeError(f"OpenCode event supervisor failed to start: {guardian.failed}")
        api.request("POST", f"/session/{root_id}/prompt_async", {
            "parts": [{"type": "text", "text": args.prompt}]
        })
        if args.io_mode == "human":
            attach_command = opencode_attach_command(
                base_url, args.cwd, root_id, password
            )
            (runtime / "ATTACH.json").write_text(
                json.dumps(
                    {
                        "backend_id": "opencode",
                        "url": base_url,
                        "cwd": args.cwd,
                        "session_id": root_id,
                        "password_redacted": True,
                        "argv_shape": [
                            "opencode",
                            "attach",
                            "--dir",
                            args.cwd,
                            "--session",
                            root_id,
                            "--password",
                            "<redacted>",
                            base_url,
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            attach = subprocess.run(
                attach_command,
                cwd=args.cwd,
                env=environment,
                check=False,
            )
            return 125 if guardian.budget_exceeded else attach.returncode
        guardian.root_idle.wait()
        if guardian.failed:
            raise RuntimeError(f"OpenCode event supervisor failed: {guardian.failed}")
        return 125 if guardian.budget_exceeded else 0
    finally:
        stop_process(server)
        server_log.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RDO OpenCode attempt supervisor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
