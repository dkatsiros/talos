"""Tests for the shuttle-offload execution_host targeting (Path C).

We drive the real ``run_handoff`` in-process with ``subprocess.run`` and
``shutil.which`` monkeypatched, so no ssh/rsync/screen actually runs. The two
mandated scenarios:

  (a) happy path  — preflight succeeds, dispatch is wrapped in an ssh command
      that spawns ``screen -dmS ... claude -p ...`` running INSIDE the repo
      mirror on the Shuttle, with the task mirror dir as --add-dir.
  (b) fail loud   — preflight fails: NO local screen, one JSON line in
      logs/shuttle-offload-fallback.jsonl, a handoff-alert.json, SystemExit.
  (c) defaults    — no execution_host in task.json resolves to "shuttle"
      (fleet plan 1.2, 2026-09-05); without a repo mapping it fails loud;
      an explicit "local" still runs the local screen path.
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
REPO_ON_SHUTTLE = "/home/dimitris/toy"


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
    envelope["shuttle"] = {"project_root": REPO_ON_SHUTTLE}
    task_json.write_text(json.dumps(envelope), encoding="utf-8")
    return project, task


def _prepare_plain_task(tmp_path: Path, *, config_patch: dict | None = None,
                        execution_host: str | None = None) -> tuple[Path, str]:
    """Bootstrap + park a task WITHOUT touching execution_host (unless given)."""
    project = tmp_path / "toy"
    project.mkdir()
    (project / "README.md").write_text("# Toy\n", encoding="utf-8")
    _cli(project, "bootstrap")
    loop = project / ".openclaw" / "claude-loop"
    if config_patch:
        cfg_path = loop / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.update(config_patch)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    task = _cli(project, "enqueue", "Plain one", "--role", "cto", "--instruction", "noop")
    _cli(project, "run-worker", "--backend", "subscription-interactive", "--once")
    if execution_host:
        task_json = loop / "tasks" / task / "task.json"
        envelope = json.loads(task_json.read_text(encoding="utf-8"))
        envelope["execution_host"] = execution_host
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


def _handoff_args(project: Path, task: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        project_root=str(project),
        task_id=task,
        permission_mode=None,
        quiet=False,
        detach=True,  # spawn only, skip the polling loop
        timeout=10,
        poll_interval=1,
    )


def _install(fake: FakeRun, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")


def _run_handoff(project: Path, task: str, fake: FakeRun, monkeypatch) -> None:
    _install(fake, monkeypatch)
    assert cli.run_handoff(_handoff_args(project, task)) == 0


def _run_handoff_expect_exit(project: Path, task: str, fake: FakeRun, monkeypatch) -> str:
    _install(fake, monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        cli.run_handoff(_handoff_args(project, task))
    return str(excinfo.value)


def _task_json(project: Path, task: str) -> dict:
    return json.loads(
        (project / ".openclaw" / "claude-loop" / "tasks" / task / "task.json").read_text(encoding="utf-8")
    )


def _alert(project: Path, task: str) -> dict | None:
    p = project / ".openclaw" / "claude-loop" / "tasks" / task / cli.HANDOFF_ALERT_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


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
    # The builder runs INSIDE the repo mirror; the task mirror is --add-dir'd
    # and the session log lands in the task mirror (absolute path).
    assert f"cd {REPO_ON_SHUTTLE} &&" in remote_cmd
    assert f"--add-dir {MIRROR_ROOT}/{task}" in remote_cmd
    assert f"tee {MIRROR_ROOT}/{task}/claude-session.log" in remote_cmd
    assert f"Repository: {REPO_ON_SHUTTLE}" in remote_cmd
    # No local screen spawn should have happened on the happy path.
    assert fake.local_screen_calls() == []

    # Fallback log must NOT have been written when preflight succeeds.
    fallback_log = project / ".openclaw" / "claude-loop" / "logs" / "shuttle-offload-fallback.jsonl"
    assert not fallback_log.exists()
    assert _alert(project, task) is None


def test_preflight_failure_fails_loud_no_local_fallback(tmp_path, monkeypatch):
    project, task = _prepare_shuttle_task(tmp_path)
    fake = FakeRun(preflight_ok=False)
    msg = _run_handoff_expect_exit(project, task, fake, monkeypatch)
    assert "local fallback is disabled" in msg

    # No remote dispatch AND no local screen — nothing ran on the AWS head.
    assert fake.ssh_screen_cmds() == []
    assert fake.local_screen_calls() == [], f"local fallback must not run, got {fake.calls}"

    # Exactly one JSON fallback line recording the event (watchers consume it).
    fallback_log = project / ".openclaw" / "claude-loop" / "logs" / "shuttle-offload-fallback.jsonl"
    assert fallback_log.exists()
    lines = [l for l in fallback_log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["task_id"] == task
    assert "NO local fallback" in entry["reason"]
    assert "ts" in entry and "latency_ms" in entry

    # Dead-man alert + task left re-runnable.
    alert = _alert(project, task)
    assert alert and alert["outcome"] == "shuttle_preflight_failed"
    assert alert["suggested_state"] == "needs_approval"
    status = json.loads((project / ".openclaw" / "claude-loop" / "tasks" / task / "status.json").read_text())
    assert status["state"] == "needs_approval"


def test_default_execution_host_is_shuttle_when_project_is_mapped(tmp_path, monkeypatch):
    """No execution_host in task.json + config.json mapping -> Shuttle run, persisted."""
    project, task = _prepare_plain_task(
        tmp_path,
        config_patch={"execution_hosts": {"shuttle": {"project_root": REPO_ON_SHUTTLE}}},
    )
    assert "execution_host" not in _task_json(project, task)

    fake = FakeRun(preflight_ok=True)
    _run_handoff(project, task, fake, monkeypatch)

    ssh_screen = fake.ssh_screen_cmds()
    assert len(ssh_screen) == 1, f"expected a Shuttle dispatch by default, got {fake.calls}"
    assert f"cd {REPO_ON_SHUTTLE} &&" in ssh_screen[0]
    assert fake.local_screen_calls() == []
    # Resolved host + mapping are persisted so every consumer agrees.
    envelope = _task_json(project, task)
    assert envelope["execution_host"] == "shuttle"
    assert envelope["shuttle"]["project_root"] == REPO_ON_SHUTTLE


def test_default_without_mapping_fails_loud(tmp_path, monkeypatch):
    """No execution_host, no mapping -> shuttle_mapping_missing alert, nothing runs."""
    project, task = _prepare_plain_task(tmp_path)
    fake = FakeRun(preflight_ok=True)
    msg = _run_handoff_expect_exit(project, task, fake, monkeypatch)
    assert "no Shuttle repo mapping" in msg

    assert fake.ssh_screen_cmds() == []
    assert fake.local_screen_calls() == []
    alert = _alert(project, task)
    assert alert and alert["outcome"] == "shuttle_mapping_missing"
    assert _task_json(project, task)["execution_host"] == "shuttle"


def test_project_config_default_execution_host_local(tmp_path, monkeypatch):
    """config.json default_execution_host=local -> local screen path, no ssh."""
    project, task = _prepare_plain_task(tmp_path, config_patch={"default_execution_host": "local"})
    fake = FakeRun(preflight_ok=True)
    _run_handoff(project, task, fake, monkeypatch)

    assert fake.ssh_screen_cmds() == []
    assert len(fake.local_screen_calls()) == 1
    assert not any(a[:1] == ["ssh"] for a in fake.calls)
    assert _task_json(project, task)["execution_host"] == "local"


def test_explicit_local_still_runs_local(tmp_path, monkeypatch):
    """task.json execution_host=local is an explicit opt-in for the AWS head."""
    project, task = _prepare_plain_task(tmp_path, execution_host="local")
    fake = FakeRun(preflight_ok=True)
    _run_handoff(project, task, fake, monkeypatch)

    assert fake.ssh_screen_cmds() == []
    assert len(fake.local_screen_calls()) == 1
    assert not any(a[:1] == ["ssh"] for a in fake.calls)


def test_resolve_execution_host_precedence_and_validation():
    assert remote_exec.resolve_execution_host({}, {}) == remote_exec.DEFAULT_EXECUTION_HOST
    assert remote_exec.resolve_execution_host({}, {"default_execution_host": "local"}) == "local"
    assert remote_exec.resolve_execution_host({"execution_host": "shuttle-sandbox"},
                                              {"default_execution_host": "local"}) == "shuttle-sandbox"
    with pytest.raises(ValueError):
        remote_exec.resolve_execution_host({"execution_host": "shutle"}, {})
    assert remote_exec.shuttle_project_root({}, {}) is None
    assert remote_exec.shuttle_project_root(
        {}, {"execution_hosts": {"shuttle": {"project_root": "/home/dimitris/x/"}}}
    ) == "/home/dimitris/x"
    assert remote_exec.shuttle_project_root(
        {"shuttle": {"project_root": "/task/wins"}},
        {"execution_hosts": {"shuttle": {"project_root": "/cfg"}}},
    ) == "/task/wins"
    with pytest.raises(ValueError):
        remote_exec.shuttle_project_root({"shuttle": {"project_root": "relative/path"}}, {})


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
