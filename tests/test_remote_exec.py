"""Tests for the shuttle-offload execution_host targeting (Path C).

We drive the real ``run_handoff`` in-process with ``subprocess.run`` and
``shutil.which`` monkeypatched, so no ssh/rsync/screen actually runs. The two
mandated scenarios:

  (a) happy path  — preflight succeeds, dispatch is wrapped in an ssh command
      that spawns ``screen -dmS ... claude -p ...`` in the remote mirror dir.
  (b) fallback    — preflight fails, the local screen path runs instead and one
      JSON line is appended to logs/shuttle-offload-fallback.jsonl.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from openclaw_claude_loop import cli, remote_exec

MODULE_ROOT = Path(__file__).resolve().parents[1]
MIRROR_ROOT = "/home/dimitris/.claude-loop-tasks"


def _cli(project: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), *args],
        cwd=MODULE_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _prepare_shuttle_task(tmp_path: Path) -> tuple[Path, str]:
    """Bootstrap a project and park a cto task in needs_approval with prompt.md."""
    project = tmp_path / "toy"
    project.mkdir()
    (project / "README.md").write_text("# Toy\n", encoding="utf-8")
    _cli(project, "bootstrap")
    task = _cli(
        project,
        "enqueue",
        "Probe host",
        "--role",
        "cto",
        "--instruction",
        "run hostname and echo the pid",
    )
    # subscription-interactive renders prompt.md and moves task -> needs_approval.
    _cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

    loop = project / ".openclaw" / "claude-loop"
    task_json = loop / "tasks" / task / "task.json"
    envelope = json.loads(task_json.read_text(encoding="utf-8"))
    envelope["execution_host"] = "shuttle"
    task_json.write_text(json.dumps(envelope), encoding="utf-8")
    return project, task


class FakeRun:
    """Records every subprocess.run argv; controls preflight success."""

    def __init__(self, preflight_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.preflight_ok = preflight_ok

    def __call__(self, argv, *args, **kwargs):  # noqa: ANN001 - drop-in for subprocess.run
        argv = list(argv)
        self.calls.append(argv)
        returncode = 0
        # Preflight is `ssh -o ... <target> true`.
        if argv[:1] == ["ssh"] and argv[-1] == "true":
            returncode = 0 if self.preflight_ok else 255
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")

    def ssh_screen_cmds(self) -> list[str]:
        return [
            argv[-1]
            for argv in self.calls
            if argv[:1] == ["ssh"] and "screen -dmS" in argv[-1]
        ]

    def local_screen_calls(self) -> list[list[str]]:
        return [
            argv
            for argv in self.calls
            if argv and argv[0].endswith("screen") and "-dmS" in argv
        ]


def _run_handoff(project: Path, task: str, fake: FakeRun, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    args = types.SimpleNamespace(
        project_root=str(project),
        task_id=task,
        permission_mode=None,
        quiet=False,
        detach=True,  # spawn only, skip the polling loop
        timeout=10,
        poll_interval=1,
    )
    assert cli.run_handoff(args) == 0


def test_happy_path_wraps_dispatch_in_ssh_screen(tmp_path, monkeypatch):
    project, task = _prepare_shuttle_task(tmp_path)
    fake = FakeRun(preflight_ok=True)
    _run_handoff(project, task, fake, monkeypatch)

    ssh_screen = fake.ssh_screen_cmds()
    assert len(ssh_screen) == 1, f"expected one ssh screen dispatch, got {fake.calls}"
    remote_cmd = ssh_screen[0]
    assert "screen -dmS" in remote_cmd
    assert "claude -p" in remote_cmd
    assert f"{MIRROR_ROOT}/{task}" in remote_cmd
    # No local screen spawn should have happened on the happy path.
    assert fake.local_screen_calls() == []

    # Fallback log must NOT have been written when preflight succeeds.
    fallback_log = project / ".openclaw" / "claude-loop" / "logs" / "shuttle-offload-fallback.jsonl"
    assert not fallback_log.exists()


def test_preflight_failure_falls_back_to_local_and_logs(tmp_path, monkeypatch):
    project, task = _prepare_shuttle_task(tmp_path)
    fake = FakeRun(preflight_ok=False)
    _run_handoff(project, task, fake, monkeypatch)

    # No remote dispatch; a local screen -dmS was spawned instead.
    assert fake.ssh_screen_cmds() == []
    local = fake.local_screen_calls()
    assert len(local) == 1, f"expected one local screen spawn, got {fake.calls}"
    assert "bash" in local[0]

    # Exactly one JSON fallback line recording the event.
    fallback_log = project / ".openclaw" / "claude-loop" / "logs" / "shuttle-offload-fallback.jsonl"
    assert fallback_log.exists()
    lines = [l for l in fallback_log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["task_id"] == task
    assert entry["reason"]
    assert "ts" in entry and "latency_ms" in entry


def test_default_execution_host_is_local(tmp_path, monkeypatch):
    """No execution_host set -> byte-identical local screen path, no ssh."""
    project = tmp_path / "toy"
    project.mkdir()
    (project / "README.md").write_text("# Toy\n", encoding="utf-8")
    _cli(project, "bootstrap")
    task = _cli(project, "enqueue", "Local one", "--role", "cto", "--instruction", "noop")
    _cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

    fake = FakeRun(preflight_ok=True)
    _run_handoff(project, task, fake, monkeypatch)

    assert fake.ssh_screen_cmds() == []
    assert len(fake.local_screen_calls()) == 1
    # Preflight ssh must never be attempted when execution_host is unset.
    assert not any(a[:1] == ["ssh"] for a in fake.calls)


# --- pure unit tests for remote_exec helpers ---


def test_shuttle_config_defaults():
    cfg = remote_exec.shuttle_config({})
    assert cfg["ssh_target"] == remote_exec.DEFAULT_SSH_TARGET
    assert cfg["mirror_root"] == remote_exec.DEFAULT_MIRROR_ROOT
    assert cfg["preflight_timeout_s"] == remote_exec.DEFAULT_PREFLIGHT_TIMEOUT_S


def test_shuttle_config_overrides():
    cfg = remote_exec.shuttle_config(
        {"shuttle": {"ssh_target": "u@host", "mirror_root": "/m", "preflight_timeout_s": 7}}
    )
    assert cfg == {"ssh_target": "u@host", "mirror_root": "/m", "preflight_timeout_s": 7}


# --- injection-guard tests (Fix F1, ported from public talos-export) ---


def test_shuttle_config_rejects_injected_ssh_target():
    """ssh_target starting with '-' must be rejected (option injection)."""
    with pytest.raises(ValueError, match="ssh_target"):
        remote_exec.shuttle_config({"shuttle": {"ssh_target": "-oProxyCommand=evil", "mirror_root": "/m"}})


def test_shuttle_config_rejects_bare_hostname_ssh_target():
    with pytest.raises(ValueError, match="ssh_target"):
        remote_exec.shuttle_config({"shuttle": {"ssh_target": "hostname-only", "mirror_root": "/m"}})


def test_shuttle_config_rejects_relative_mirror_root():
    with pytest.raises(ValueError, match="mirror_root"):
        remote_exec.shuttle_config({"shuttle": {"ssh_target": "u@host", "mirror_root": "relative/path"}})


def test_shuttle_config_rejects_dash_mirror_root():
    """mirror_root starting with '-' must be rejected (rsync option injection)."""
    with pytest.raises(ValueError, match="mirror_root"):
        remote_exec.shuttle_config({"shuttle": {"ssh_target": "u@host", "mirror_root": "-some-rsync-flag"}})


def test_build_remote_screen_argv_shape():
    inner = remote_exec.build_remote_inner("/m/t123", "claude -p 'hi' --add-dir /m/t123")
    argv = remote_exec.build_remote_screen_argv("u@host", "claude-loop-t123", inner)
    assert argv[:1] == ["ssh"]
    assert argv[-2] == "u@host" or "u@host" in argv
    remote_cmd = argv[-1]
    assert "screen -dmS" in remote_cmd
    assert "claude -p" in remote_cmd
    assert "/m/t123" in remote_cmd


def test_remote_inner_carries_default_model():
    inner = remote_exec.build_remote_inner("/m/t123", "claude -p 'hi'", env={})
    assert "export ANTHROPIC_MODEL=claude-opus-4-8 &&" in inner
    # and it survives the ssh/screen quoting layers
    argv = remote_exec.build_remote_screen_argv("u@host", "claude-loop-t123", inner)
    assert "ANTHROPIC_MODEL=claude-opus-4-8" in argv[-1]


def test_remote_inner_respects_explicit_model_override():
    inner = remote_exec.build_remote_inner(
        "/m/t123", "claude -p 'hi'", env={"ANTHROPIC_MODEL": "claude-fable-5"}
    )
    assert "export ANTHROPIC_MODEL=claude-fable-5 &&" in inner
    assert "claude-opus-4-8" not in inner


def test_resolve_anthropic_model_reads_process_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert remote_exec.resolve_anthropic_model() == "claude-opus-4-8"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-fable-5")
    assert remote_exec.resolve_anthropic_model() == "claude-fable-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "  ")
    assert remote_exec.resolve_anthropic_model() == "claude-opus-4-8"


def test_preflight_nonzero_returns_false(monkeypatch):
    def fake_run(argv, *a, **k):
        return subprocess.CompletedProcess(argv, 255, stdout="", stderr="offline")

    monkeypatch.setattr(remote_exec.subprocess, "run", fake_run)
    ok, latency_ms, reason = remote_exec.preflight("u@host", 3)
    assert ok is False
    assert reason == "offline"
    assert isinstance(latency_ms, int)


def test_log_fallback_appends_one_line(tmp_path):
    logs = tmp_path / "logs"
    remote_exec.log_fallback(logs, "task-1", "unreachable", 42, "2026-07-15T00:00:00Z")
    remote_exec.log_fallback(logs, "task-2", "timeout", 3005, "2026-07-15T00:01:00Z")
    lines = (logs / "shuttle-offload-fallback.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["task_id"] == "task-1"
    assert json.loads(lines[1])["latency_ms"] == 3005
