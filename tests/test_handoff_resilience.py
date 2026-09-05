"""run_handoff resilience + failure-visibility tests.

Same harness as test_remote_exec.py: the real `run_handoff` runs in-process with
`subprocess.run` and `shutil.which` monkeypatched, so no screen/ssh/rsync/claude
ever runs. Each test simulates one of the failure modes that used to end in a
SILENT stall — status.json frozen at "running", token stranded in blocked/, no
record anywhere — and asserts the failure is now recorded in all four channels:
the dead-man file, status.json, the queue token, and logs/handoff-events.jsonl.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from openclaw_claude_loop import cli, remote_exec, resilience

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


def _prepare_task(tmp_path: Path, *, execution_host: str | None = None) -> tuple[Path, str]:
    """Bootstrap a project and park one task in needs_approval with prompt.md."""
    project = tmp_path / "toy"
    project.mkdir()
    (project / "README.md").write_text("# Toy\n", encoding="utf-8")
    _cli(project, "bootstrap")
    task = _cli(project, "enqueue", "Do a thing", "--role", "builder",
                "--instruction", "echo hi")
    _cli(project, "run-worker", "--backend", "subscription-interactive", "--once")
    # Default execution host is the Shuttle since 2026-09-05; these tests
    # exercise the LOCAL screen path unless a host is given, so pin local.
    task_json = project / ".openclaw" / "claude-loop" / "tasks" / task / "task.json"
    envelope = json.loads(task_json.read_text(encoding="utf-8"))
    envelope["execution_host"] = execution_host or "local"
    if execution_host == "shuttle":
        envelope["shuttle"] = {"project_root": "/home/dimitris/toy"}
    task_json.write_text(json.dumps(envelope), encoding="utf-8")
    return project, task


class FakeHost:
    """Scriptable stand-in for every subprocess the handoff shells out to."""

    def __init__(
        self,
        session_name: str,
        *,
        local_spawn_failures: int = 0,
        remote_spawn_failures: int = 0,
        preflight_ok: bool = True,
        rsync_ok: bool = True,
        alive_script: list[bool] | None = None,
        alive_default: bool = True,
    ) -> None:
        self.session_name = session_name
        self.local_spawn_failures = local_spawn_failures
        self.remote_spawn_failures = remote_spawn_failures
        self.preflight_ok = preflight_ok
        self.rsync_ok = rsync_ok
        # Liveness answers, consumed in order; `alive_default` afterwards.
        self.alive_script = list(alive_script or [])
        self.alive_default = alive_default
        self.spawned = False
        self.calls: list[list[str]] = []

    # -- helpers -----------------------------------------------------------
    def _next_alive(self) -> bool:
        if not self.spawned:
            return False  # pre-spawn "is it already running?" probe
        if self.alive_script:
            return self.alive_script.pop(0)
        return self.alive_default

    def _screen_ls_output(self) -> str:
        return f"\t12345.{self.session_name}\t(Detached)\n" if self._next_alive() else "No Sockets found.\n"

    def local_spawns(self) -> list[list[str]]:
        return [a for a in self.calls if a and str(a[0]).endswith("screen") and "-dmS" in a]

    def remote_spawns(self) -> list[str]:
        return [a[-1] for a in self.calls if a[:1] == ["ssh"] and "screen -dmS" in a[-1]]

    # -- subprocess.run replacement ---------------------------------------
    def __call__(self, argv, *args, **kwargs):  # noqa: ANN001 - drop-in
        argv = list(argv)
        self.calls.append(argv)
        head = str(argv[0])

        if head == "rsync":
            return subprocess.CompletedProcess(argv, 0 if self.rsync_ok else 23)

        if head == "ssh":
            remote_cmd = str(argv[-1])
            if remote_cmd == "true":  # preflight
                return subprocess.CompletedProcess(argv, 0 if self.preflight_ok else 255,
                                                   stdout="", stderr="offline")
            if "screen -dmS" in remote_cmd:
                if self.remote_spawn_failures > 0:
                    self.remote_spawn_failures -= 1
                    raise subprocess.CalledProcessError(255, argv)
                self.spawned = True
                return subprocess.CompletedProcess(argv, 0)
            if "screen -ls" in remote_cmd:
                return subprocess.CompletedProcess(argv, 0, stdout=self._screen_ls_output())
            return subprocess.CompletedProcess(argv, 0, stdout="")  # mkdir -p etc.

        if head.endswith("screen"):
            if "-dmS" in argv:
                if self.local_spawn_failures > 0:
                    self.local_spawn_failures -= 1
                    raise subprocess.CalledProcessError(1, argv)
                self.spawned = True
                return subprocess.CompletedProcess(argv, 0)
            if "-ls" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=self._screen_ls_output())

        # git probes etc. — returncode 0, empty stdout (no worktree is created).
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Neither the poll loop nor the retry backoff may actually sleep."""
    monkeypatch.setattr(cli.time, "sleep", lambda _d: None)
    monkeypatch.setattr(resilience.time, "sleep", lambda _d: None)


def _args(project: Path, task: str, **overrides):
    base = dict(
        project_root=str(project),
        task_id=task,
        permission_mode=None,
        quiet=False,
        detach=False,
        timeout=3,
        poll_interval=1,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _loop(project: Path) -> Path:
    return project / ".openclaw" / "claude-loop"


def _events(project: Path) -> list[dict]:
    return resilience.tail_jsonl(_loop(project) / "logs" / cli.HANDOFF_EVENTS_LOG, limit=100)


def _install(monkeypatch, fake: FakeHost) -> None:
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")


# --------------------------------------------------------------------------- #
# The headline failure: a poll timeout used to be completely silent            #
# --------------------------------------------------------------------------- #

def test_timeout_records_a_visible_alert_everywhere(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path)
    fake = FakeHost(cli.screen_session_name(task), alive_default=True)
    _install(monkeypatch, fake)

    with pytest.raises(SystemExit) as excinfo:
        cli.run_handoff(_args(project, task))
    assert "Timeout" in str(excinfo.value)

    loop = _loop(project)
    task_dir = loop / "tasks" / task

    # 1. dead-man file
    alert = json.loads((task_dir / cli.HANDOFF_ALERT_FILE).read_text(encoding="utf-8"))
    assert alert["outcome"] == "timeout"
    assert "no result.json" in alert["reason"]
    assert alert["session_name"] == cli.screen_session_name(task)
    # The session is still alive, so we must NOT claim the task failed.
    assert alert["session_alive"] is True
    assert alert.get("suggested_state") is None

    # 2. status.json — but the state itself is untouched, so max-talos-reaper
    #    still recognises the task as a reapable ghost.
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["handoff_alert"]["outcome"] == "timeout"
    assert status["state"] == "running"
    assert "handoff timeout" in status["heartbeat"]["message"]

    # 3. queue token, wherever it sits
    token = json.loads((loop / "queue" / "blocked" / f"{task}.json").read_text(encoding="utf-8"))
    assert token["handoff_alert"]["outcome"] == "timeout"

    # 4. durable event log
    assert any(e["event"] == "handoff_timeout" and e["task_id"] == task
               for e in _events(project))


def test_status_command_surfaces_outstanding_alerts(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path)

    # Clean project: payload shape is unchanged (no alerts key at all).
    clean = json.loads(_cli(project, "status"))
    assert "handoff_alerts" not in clean

    fake = FakeHost(cli.screen_session_name(task), alive_default=True)
    _install(monkeypatch, fake)
    with pytest.raises(SystemExit):
        cli.run_handoff(_args(project, task))

    monkeypatch.undo()  # _cli shells out for real; restore subprocess.run first
    surfaced = json.loads(_cli(project, "status"))
    assert len(surfaced["handoff_alerts"]) == 1
    assert surfaced["handoff_alerts"][0]["task_id"] == task
    assert surfaced["handoff_alerts"][0]["outcome"] == "timeout"


# --------------------------------------------------------------------------- #
# Session liveness: one bad probe is not a death                               #
# --------------------------------------------------------------------------- #

def test_single_missed_liveness_probe_does_not_abort_a_healthy_run(tmp_path, monkeypatch):
    """`screen -ls` missing the session once used to kill the whole handoff."""
    project, task = _prepare_task(tmp_path)
    task_dir = _loop(project) / "tasks" / task
    fake = FakeHost(cli.screen_session_name(task), alive_script=[False, True])
    _install(monkeypatch, fake)

    # The session writes its result while we are still polling.
    real_exists = Path.exists
    state = {"polls": 0}

    def fake_exists(self):
        if self.name == "result.json" and self.parent == task_dir:
            state["polls"] += 1
            if state["polls"] > 2:
                _write_result(task_dir, task)
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert cli.run_handoff(_args(project, task, timeout=10)) == 0
    # No alert: a transient probe miss is not a failure.
    assert not (task_dir / cli.HANDOFF_ALERT_FILE).exists()


def test_confirmed_session_death_alerts_with_suggested_state(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path)
    fake = FakeHost(cli.screen_session_name(task), alive_default=False)
    _install(monkeypatch, fake)

    with pytest.raises(SystemExit) as excinfo:
        cli.run_handoff(_args(project, task, timeout=30))
    assert "without writing result.json" in str(excinfo.value)

    task_dir = _loop(project) / "tasks" / task
    alert = json.loads((task_dir / cli.HANDOFF_ALERT_FILE).read_text(encoding="utf-8"))
    assert alert["outcome"] == "session_died"
    assert alert["suggested_state"] == "failed"
    # Death was confirmed, not assumed on the first miss.
    assert str(cli.SESSION_DEATH_CONFIRMATIONS) in alert["reason"]
    assert any(e["event"] == "handoff_session_died" for e in _events(project))


# --------------------------------------------------------------------------- #
# Spawn: retry the blip, alert on the wall                                     #
# --------------------------------------------------------------------------- #

def test_transient_local_spawn_failure_is_retried(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path)
    fake = FakeHost(cli.screen_session_name(task), local_spawn_failures=1)
    _install(monkeypatch, fake)

    assert cli.run_handoff(_args(project, task, detach=True)) == 0

    assert len(fake.local_spawns()) == 2  # failed once, then stuck
    task_dir = _loop(project) / "tasks" / task
    assert not (task_dir / cli.HANDOFF_ALERT_FILE).exists()
    assert json.loads((task_dir / "status.json").read_text(encoding="utf-8"))["state"] == "running"
    # The intermediate failure is still logged — a retry that nobody records is
    # how a host slowly degrades without anyone noticing.
    assert any(e["event"] == "spawn_retry" for e in _events(project))


def test_retry_never_spawns_a_second_session(tmp_path, monkeypatch):
    """A spawn can fail AFTER screen started; the retry must adopt, not double up.

    Two Claude sessions racing to write one result.json is a worse outcome than
    the transient error being retried.
    """
    project, task = _prepare_task(tmp_path)
    fake = FakeHost(cli.screen_session_name(task), local_spawn_failures=1)

    # Simulate "screen actually started, then the wrapper reported failure".
    real_call = fake.__call__

    def call(argv, *a, **k):
        argv = list(argv)
        if str(argv[0]).endswith("screen") and "-dmS" in argv and fake.local_spawn_failures > 0:
            fake.spawned = True  # the session IS live despite the error below
        return real_call(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", call)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli.run_handoff(_args(project, task, detach=True)) == 0
    assert len(fake.local_spawns()) == 1  # adopted the live session, did not respawn
    assert any(e["event"] == "spawn_adopted_existing" for e in _events(project))


def test_permanent_spawn_failure_alerts_and_leaves_task_recoverable(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path)
    fake = FakeHost(cli.screen_session_name(task), local_spawn_failures=99)
    _install(monkeypatch, fake)

    with pytest.raises(SystemExit) as excinfo:
        cli.run_handoff(_args(project, task, detach=True))
    assert "Could not spawn" in str(excinfo.value)

    assert len(fake.local_spawns()) == cli.SPAWN_ATTEMPTS
    task_dir = _loop(project) / "tasks" / task
    alert = json.loads((task_dir / cli.HANDOFF_ALERT_FILE).read_text(encoding="utf-8"))
    assert alert["outcome"] == "spawn_failed"
    # Nothing ran, so the task must still be re-runnable exactly as it was.
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "needs_approval"
    assert (_loop(project) / "queue" / "blocked" / f"{task}.json").exists()


# --------------------------------------------------------------------------- #
# Remote path: dispatch failure FAILS LOUD (no local fallback — fleet plan 1.2) #
# --------------------------------------------------------------------------- #

def test_remote_dispatch_failure_fails_loud_no_local_fallback(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path, execution_host="shuttle")
    fake = FakeHost(cli.screen_session_name(task), remote_spawn_failures=99)
    _install(monkeypatch, fake)

    with pytest.raises(SystemExit) as excinfo:
        cli.run_handoff(_args(project, task, detach=True))
    assert "local fallback is disabled" in str(excinfo.value)

    # Retried the shuttle; NEVER ran on the AWS head.
    assert len(fake.remote_spawns()) == cli.SPAWN_ATTEMPTS
    assert fake.local_spawns() == []

    fallback_log = _loop(project) / "logs" / "shuttle-offload-fallback.jsonl"
    entries = resilience.tail_jsonl(fallback_log)
    assert any("dispatch failed" in e["reason"] and "NO local fallback" in e["reason"] for e in entries)
    assert any(e["event"] == "remote_dispatch_failed" for e in _events(project))

    # A human must look at this task: dead-man alert + re-runnable state.
    task_dir = _loop(project) / "tasks" / task
    alert = json.loads((task_dir / cli.HANDOFF_ALERT_FILE).read_text(encoding="utf-8"))
    assert alert["outcome"] == "shuttle_dispatch_failed"
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "needs_approval"
    assert (_loop(project) / "queue" / "blocked" / f"{task}.json").exists()


def test_broken_result_sync_is_alerted_while_polling_continues(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path, execution_host="shuttle")
    fake = FakeHost(cli.screen_session_name(task), rsync_ok=False, alive_default=True)
    _install(monkeypatch, fake)

    with pytest.raises(SystemExit):
        cli.run_handoff(_args(project, task, timeout=6, poll_interval=1))

    events = _events(project)
    # Degradation was reported BEFORE the run timed out, so the log explains
    # why no result ever arrived.
    kinds = [e["event"] for e in events]
    assert "handoff_remote_sync_degraded" in kinds
    assert kinds.index("handoff_remote_sync_degraded") < kinds.index("handoff_timeout")


# --------------------------------------------------------------------------- #
# Alerts must not outlive the problem                                          #
# --------------------------------------------------------------------------- #

def _write_result(task_dir: Path, task_id: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "result.json").write_text(json.dumps({
        "task_id": task_id,
        "state": "completed",
        "summary": "did the thing",
        "changes": {"files_created": ["a.py"], "files_modified": [], "files_deleted": []},
        "verification": {"commands": ["pytest"], "results": [{"exit_code": 0}]},
        "artifacts": ["a.py"],
        "deployment_status": {"state": "not_applicable", "details": "toy project"},
        "next_actions": [],
    }), encoding="utf-8")


def test_complete_handoff_clears_a_stale_alert(tmp_path, monkeypatch):
    project, task = _prepare_task(tmp_path)
    fake = FakeHost(cli.screen_session_name(task), alive_default=True)
    _install(monkeypatch, fake)
    with pytest.raises(SystemExit):
        cli.run_handoff(_args(project, task))

    loop = _loop(project)
    task_dir = loop / "tasks" / task
    assert (task_dir / cli.HANDOFF_ALERT_FILE).exists()

    # The session was slow, not dead: it finishes and someone finalizes.
    _write_result(task_dir, task)
    monkeypatch.undo()
    _cli(project, "complete-handoff", task)

    assert not (task_dir / cli.HANDOFF_ALERT_FILE).exists()
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert "handoff_alert" not in status
    token = json.loads((loop / "queue" / "done" / f"{task}.json").read_text(encoding="utf-8"))
    assert "handoff_alert" not in token
    assert json.loads(_cli(project, "status")).get("handoff_alerts") is None
