"""Tests for the Talos Sandbox (execution_host="shuttle-sandbox") M2 gap fixes.

These cover the four unattended-safety fixes landed 2026-09-04, WITHOUT running a
real sandbox (no ssh / docker / Shuttle):

  1. SAFETY — a failed sandbox dispatch must NEVER fall back to a local claude-p
     screen on the AWS head; it fails loud (dead-man alert + SystemExit) and
     leaves the task re-runnable.
  2. Result schema — the in-container agent directive forces deployment_status to
     be a structured object (state="not_deployed"), not a bare string.
  3. Merge-back cleanup — prune_stale_local_refs() removes a stale local worktree
     + talos/<id> branch that would otherwise make the sandbox fetch refuse.
  4. Project-name normalisation — a dict project ({name,root}) reaches
     spawn_sandbox as the bare name string, never a dict (the TypeError that used
     to trigger the local fallback).
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from openclaw_claude_loop import cli, sandbox_exec

MODULE_ROOT = Path(__file__).resolve().parents[1]


def _cli(project: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), *args],
        cwd=MODULE_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _prepare_sandbox_task(tmp_path: Path, project_field=None) -> tuple[Path, str]:
    """Bootstrap a project and park a cto task in needs_approval with prompt.md,
    then flip its envelope to execution_host="shuttle-sandbox"."""
    project = tmp_path / "toy"
    project.mkdir()
    (project / "README.md").write_text("# Toy\n", encoding="utf-8")
    _cli(project, "bootstrap")
    task = _cli(
        project,
        "enqueue",
        "Build a feature in the sandbox",
        "--role",
        "cto",
        "--instruction",
        "add a greeter and a test",
    )
    _cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

    loop = project / ".openclaw" / "claude-loop"
    task_json = loop / "tasks" / task / "task.json"
    envelope = json.loads(task_json.read_text(encoding="utf-8"))
    envelope["execution_host"] = "shuttle-sandbox"
    if project_field is not None:
        envelope["project"] = project_field
    task_json.write_text(json.dumps(envelope), encoding="utf-8")
    return project, task


class FakeRun:
    """Records every subprocess.run argv (drop-in for subprocess.run)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):  # noqa: ANN001
        argv = list(argv)
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def local_screen_calls(self) -> list[list[str]]:
        return [
            argv
            for argv in self.calls
            if argv and str(argv[0]).endswith("screen") and "-dmS" in argv
        ]


def _sandbox_args(project: Path, task: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        project_root=str(project),
        task_id=task,
        permission_mode=None,
        quiet=False,
        detach=True,  # spawn only; skip the polling loop
        timeout=10,
        poll_interval=1,
    )


# --------------------------------------------------------------------------- #
# Fix 1: SAFETY — a failed sandbox dispatch must NOT fall back to local screen.
# --------------------------------------------------------------------------- #
def test_sandbox_dispatch_failure_does_not_fall_back_to_local(tmp_path, monkeypatch):
    project, task = _prepare_sandbox_task(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Preflight passes (Shuttle reachable) but the actual dispatch blows up.
    monkeypatch.setattr(cli.sandbox_exec, "preflight", lambda cfg: (True, 5, "ok"))
    monkeypatch.setattr(cli.sandbox_exec, "is_alive", lambda cfg, tid: False)

    def _boom(**kwargs):
        raise RuntimeError("talos-sandbox up failed: container refused")

    monkeypatch.setattr(cli.sandbox_exec, "spawn_sandbox", _boom)

    # It must FAIL LOUD, not silently degrade to a local claude-p.
    with pytest.raises(SystemExit):
        cli.run_handoff(_sandbox_args(project, task))

    # The one thing that must never happen: a local screen spawn on this host.
    assert fake.local_screen_calls() == [], (
        f"sandbox dispatch failure fell back to a LOCAL screen: {fake.calls}"
    )

    # A dead-man alert must exist so a human/cron re-dispatches.
    loop = project / ".openclaw" / "claude-loop"
    alert_path = loop / "tasks" / task / "handoff-alert.json"
    assert alert_path.exists(), "no dead-man handoff-alert.json written"
    alert = json.loads(alert_path.read_text(encoding="utf-8"))
    assert alert["outcome"] == "sandbox_dispatch_failed"
    assert "local" in alert["reason"].lower()

    # The task stays re-runnable: the queue token is still in blocked/ (parked at
    # needs_approval), NOT moved to done/ or failed/.
    assert (loop / "queue" / "blocked" / f"{task}.json").exists()
    assert not (loop / "queue" / "done" / f"{task}.json").exists()
    assert not (loop / "queue" / "failed" / f"{task}.json").exists()


# --------------------------------------------------------------------------- #
# Fix 5: SAFETY — preflight failure must NOT fall back to local screen.
#
# This is the preflight arm of the fail-loud policy. The spawn-exception arm
# (Fix 1) was closed in cd5a5aa3b; the preflight arm is the remaining gap:
# if the Shuttle is offline or the kill switch is active, preflight returns
# ok=False and the old code fell through to `if not remote and not sandboxed`
# — spawning a local claude-p on the 3.7GB AWS head (OOM risk). The fix
# (committed 2026-09-04) records a dead-man alert and raises SystemExit.
# --------------------------------------------------------------------------- #
def test_sandbox_preflight_failure_does_not_fall_back_to_local(tmp_path, monkeypatch):
    project, task = _prepare_sandbox_task(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Shuttle is offline: preflight returns (False, ...)
    monkeypatch.setattr(
        cli.sandbox_exec, "preflight",
        lambda cfg: (False, 4321, "Connection timed out (simulated offline Shuttle)"),
    )
    # spawn_sandbox must never be called when preflight fails.
    spawn_called = []

    def _should_not_be_called(**kwargs):
        spawn_called.append(kwargs)
        return {"worktree_branch": f"talos/{task}", "sandboxed": True}

    monkeypatch.setattr(cli.sandbox_exec, "spawn_sandbox", _should_not_be_called)

    # Must FAIL LOUD, not degrade to a local claude-p.
    with pytest.raises(SystemExit):
        cli.run_handoff(_sandbox_args(project, task))

    # spawn_sandbox must never have been called (Shuttle was offline).
    assert spawn_called == [], f"spawn_sandbox called despite preflight failure: {spawn_called}"

    # The one thing that must never happen: a local screen spawn on this host.
    assert fake.local_screen_calls() == [], (
        f"sandbox preflight failure fell back to a LOCAL screen: {fake.calls}"
    )

    # A dead-man alert must be written so a human or cron can re-dispatch.
    loop = project / ".openclaw" / "claude-loop"
    alert_path = loop / "tasks" / task / "handoff-alert.json"
    assert alert_path.exists(), "no dead-man handoff-alert.json written after preflight failure"
    alert = json.loads(alert_path.read_text(encoding="utf-8"))
    assert alert["outcome"] == "sandbox_preflight_failed", (
        f"unexpected outcome: {alert['outcome']!r}"
    )
    assert "local" in alert["reason"].lower(), (
        f"alert reason does not mention local fallback refusal: {alert['reason']!r}"
    )
    assert "needs_approval" in alert.get("suggested_state", ""), (
        f"task should be left re-runnable at needs_approval: {alert!r}"
    )

    # Task queue token stays in blocked/ (re-runnable), NOT done/ or failed/.
    assert (loop / "queue" / "blocked" / f"{task}.json").exists()
    assert not (loop / "queue" / "done" / f"{task}.json").exists()
    assert not (loop / "queue" / "failed" / f"{task}.json").exists()

    # The fallback log entry must record the preflight reason.
    fallback_log = loop / "logs" / "shuttle-offload-fallback.jsonl"
    assert fallback_log.exists(), "no shuttle-offload-fallback.jsonl written"
    last_line = fallback_log.read_text(encoding="utf-8").strip().split("\n")[-1]
    entry = json.loads(last_line)
    assert entry["task_id"] == task
    assert "preflight" in entry["reason"].lower()
    assert "NO local fallback" in entry["reason"]


def test_sandbox_preflight_success_routes_to_sandbox_not_local(tmp_path, monkeypatch):
    """When preflight succeeds, spawn_sandbox is called — never a local screen."""
    project, task = _prepare_sandbox_task(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Preflight passes.
    monkeypatch.setattr(cli.sandbox_exec, "preflight", lambda cfg: (True, 12, "ok"))
    monkeypatch.setattr(cli.sandbox_exec, "is_alive", lambda cfg, tid: False)

    spawn_called = []

    def _capture(**kwargs):
        spawn_called.append(kwargs)
        return {"worktree_branch": f"talos/{task}", "sandboxed": True}

    monkeypatch.setattr(cli.sandbox_exec, "spawn_sandbox", _capture)

    result = cli.run_handoff(_sandbox_args(project, task))
    assert result == 0, f"run_handoff returned non-zero: {result}"

    # spawn_sandbox was called (task went to the Shuttle, not local).
    assert len(spawn_called) == 1, f"spawn_sandbox not called: {spawn_called}"
    # No local screen spawn happened.
    assert fake.local_screen_calls() == [], (
        f"preflight-OK path spawned a LOCAL screen instead of sandbox: {fake.calls}"
    )


# --------------------------------------------------------------------------- #
# Fix 4: project-name normalisation — dict project reaches spawn as a str name.
# --------------------------------------------------------------------------- #
def test_dict_project_normalised_to_name_for_spawn(tmp_path, monkeypatch):
    project, task = _prepare_sandbox_task(
        tmp_path, project_field={"name": "toy-sandbox", "root": str(tmp_path / "toy")}
    )
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.sandbox_exec, "preflight", lambda cfg: (True, 5, "ok"))
    monkeypatch.setattr(cli.sandbox_exec, "is_alive", lambda cfg, tid: False)

    captured: dict[str, object] = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"worktree_branch": f"talos/{task}", "sandboxed": True}

    monkeypatch.setattr(cli.sandbox_exec, "spawn_sandbox", _capture)

    assert cli.run_handoff(_sandbox_args(project, task)) == 0
    # The bare name string, never the {name,root} dict (which used to TypeError
    # inside shlex.quote and silently fall the task back to a local claude-p).
    assert captured["project"] == "toy-sandbox"
    assert isinstance(captured["project"], str)


# --------------------------------------------------------------------------- #
# Fix 2: result schema — the in-container agent directive forces a structured
# deployment_status object, not a bare string.
# --------------------------------------------------------------------------- #
def test_sandbox_agent_cmd_forces_structured_deployment_status():
    cmd = sandbox_exec.build_agent_container_cmd(
        handoff_prompt="Read /work/prompt.md and do the task.",
        role_prompt="You are the CTO.",
        permission_mode="bypassPermissions",
        model="claude-opus-4-8",
        quiet=False,
    )
    # It must explicitly require the OBJECT form and the honest not_deployed value.
    assert "deployment_status" in cmd
    assert "MUST be a JSON OBJECT" in cmd
    assert '"state": "not_deployed"' in cmd
    assert "not_applicable" in cmd  # the full allowed enum is spelled out
    assert "never a string" in cmd.lower() or "never write deployment_status as" in cmd


# --------------------------------------------------------------------------- #
# Fix 3: merge-back cleanup — prune a stale local worktree + talos/<id> branch
# so the forced sandbox fetch does not refuse.
# --------------------------------------------------------------------------- #
def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *argv],
        capture_output=True, text=True, check=True,
    )


def test_prune_stale_local_refs_removes_worktree_and_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    task_id = "20260904T000000Z-example"
    branch = sandbox_exec.task_branch(task_id)  # talos/<id>
    wt = repo / ".worktrees" / task_id
    # Simulate a leftover local worktree from a prior local fallback.
    _git(repo, "worktree", "add", str(wt), "-b", branch)
    assert wt.exists()
    listed = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True, text=True, check=True,
    )
    assert branch in listed.stdout

    actions = sandbox_exec.prune_stale_local_refs(repo, task_id)

    # Worktree directory and its admin entry are gone.
    assert not wt.exists()
    wt_list = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    )
    assert task_id not in wt_list.stdout
    # The stale branch is deleted, so a forced fetch of talos/<id> can land.
    branch_list = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True, text=True, check=True,
    )
    assert branch_list.stdout.strip() == ""
    assert any("worktree" in a for a in actions)
    assert any(branch in a for a in actions)


def test_prune_stale_local_refs_noop_when_clean(tmp_path):
    """No stale state -> no actions, never raises (idempotent)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    actions = sandbox_exec.prune_stale_local_refs(repo, "20260904T000000Z-nothing")
    assert actions == []
