"""complete-handoff must finish tasks that builders finished.

2026-09-06 audit of the live queue: 21 tasks sat in status "running" for up
to 52 days although a result.json existed. Four root causes, one test each:
  1. improvised result states ("completed_pending_deploy", "partial") were
     refused instead of mapped by intent;
  2. artifacts written as a dict failed the list contract forever;
  3. no queue token at all (orphan) raised instead of recording the outcome;
  4. token already terminal (done/failed) but status.json never updated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from openclaw_claude_loop import cli

MODULE_ROOT = Path(__file__).resolve().parents[1]


def _cli(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), *args],
        cwd=MODULE_ROOT, text=True, capture_output=True, check=check,
    )


def _project_with_task(tmp_path: Path) -> tuple[Path, Path, str]:
    project = tmp_path / "toy"
    project.mkdir()
    (project / "README.md").write_text("# Toy\n", encoding="utf-8")
    _cli(project, "bootstrap")
    task_id = _cli(project, "enqueue", "Do a thing", "--role", "builder",
                   "--instruction", "echo hi").stdout.strip()
    _cli(project, "run-worker", "--backend", "subscription-interactive", "--once")
    root = project / ".openclaw" / "claude-loop"
    return project, root, task_id


def _good_result(task_id: str, state: str = "completed", **extra) -> dict:
    base = {
        "task_id": task_id, "state": state, "summary": "did the thing",
        "changes": {"files_created": [], "files_modified": ["README.md"], "files_deleted": []},
        "verification": {"commands": ["echo hi"], "results": ["hi"]},
        "artifacts": [],
        "deployment_status": {"state": "not_applicable", "details": "docs only"},
    }
    base.update(extra)
    return base


def _token_dir(root: Path, task_id: str) -> str | None:
    for q in ("pending", "claimed", "running", "blocked", "needs_approval", "done", "failed"):
        if (root / "queue" / q / f"{task_id}.json").exists():
            return q
    return None


# ---------------------------------------------------------------- unit ----
@pytest.mark.parametrize("raw,expected", [
    ("completed", "completed"), ("done", "completed"), ("verified", "completed"),
    ("completed_with_caveats", "completed"), ("staged_pending_rollout", "completed"),
    ("completed_pending_deploy", "completed"), ("completed_planning", "completed"),
    ("COMPLETED ", "completed"), (None, "completed"), ("", "completed"),
    ("failed", "failed"), ("error", "failed"), ("failed_tests", "failed"),
    ("blocked", "blocked"), ("needs_approval", "blocked"), ("partial", "blocked"),
    ("completed_with_blocker", "blocked"), ("needs_clarification", "blocked"),
    ("banana", None), ("running", None),
])
def test_normalize_result_state(raw, expected):
    assert cli.normalize_result_state(raw) == expected


def test_flatten_artifacts_keeps_information():
    flat = cli.flatten_artifacts({
        "branch": "talos/x @ abc",
        "new_code": ["a.py", "b.py"],
        "nested": {"k": ["v1"]},
        "none": None,
    })
    assert flat == ["branch: talos/x @ abc", "new_code: a.py", "new_code: b.py",
                    "nested: k: v1"]
    assert cli.flatten_artifacts(["p", ["q"]]) == ["p", "q"]
    assert cli.flatten_artifacts(None) == []


def test_contract_accepts_dict_artifacts():
    result = _good_result("t", artifacts={"branch": "talos/x", "files": ["a", "b"]})
    assert cli.validate_completed_result_contract(result, {"role": "builder"}) == []
    bad = _good_result("t", artifacts="not-a-list")
    assert any("artifacts" in e for e in
               cli.validate_completed_result_contract(bad, {"role": "builder"}))


# ----------------------------------------------------------------- e2e ----
def test_compound_state_and_dict_artifacts_finalize(tmp_path: Path):
    project, root, task_id = _project_with_task(tmp_path)
    task_dir = root / "tasks" / task_id
    (task_dir / "result.json").write_text(json.dumps(_good_result(
        task_id, state="completed_pending_deploy",
        artifacts={"branch": "talos/x @ abc", "docs": ["README.md"]})), encoding="utf-8")
    out = _cli(project, "complete-handoff", task_id).stdout
    assert '"queue": "done"' in out
    assert _token_dir(root, task_id) == "done"
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "completed"


def test_blocker_shaped_state_routes_to_failed_queue_blocked_status(tmp_path: Path):
    project, root, task_id = _project_with_task(tmp_path)
    task_dir = root / "tasks" / task_id
    (task_dir / "result.json").write_text(json.dumps(_good_result(
        task_id, state="completed_with_blocker")), encoding="utf-8")
    _cli(project, "complete-handoff", task_id)
    assert _token_dir(root, task_id) == "failed"
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "blocked"


def test_unknown_state_still_refused(tmp_path: Path):
    project, root, task_id = _project_with_task(tmp_path)
    task_dir = root / "tasks" / task_id
    (task_dir / "result.json").write_text(json.dumps(_good_result(task_id, state="banana")),
                                          encoding="utf-8")
    proc = _cli(project, "complete-handoff", task_id, check=False)
    assert proc.returncode != 0
    assert "unrecognized state" in (proc.stderr + proc.stdout)
    assert _token_dir(root, task_id) == "blocked"


def test_orphan_result_records_terminal_status(tmp_path: Path):
    project, root, task_id = _project_with_task(tmp_path)
    task_dir = root / "tasks" / task_id
    # Lose the token entirely (hand-cancelled / copied project).
    for q in ("pending", "claimed", "running", "blocked", "needs_approval"):
        tok = root / "queue" / q / f"{task_id}.json"
        if tok.exists():
            tok.unlink()
    assert _token_dir(root, task_id) is None
    (task_dir / "result.json").write_text(json.dumps(_good_result(task_id)), encoding="utf-8")
    proc = _cli(project, "complete-handoff", task_id)
    assert '"state": "finalized_orphan"' in proc.stdout
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "completed"
    assert status["phase"] == "finalized_orphan"
    assert _token_dir(root, task_id) is None  # nothing invented

    # A blocked orphan lands as blocked, not done.
    (task_dir / "result.json").write_text(json.dumps(_good_result(task_id, state="partial")),
                                          encoding="utf-8")
    _cli(project, "complete-handoff", task_id)
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "blocked"


def test_terminal_token_repairs_stale_status(tmp_path: Path):
    project, root, task_id = _project_with_task(tmp_path)
    task_dir = root / "tasks" / task_id
    (task_dir / "result.json").write_text(json.dumps(_good_result(task_id)), encoding="utf-8")
    # Simulate an earlier finalize that moved the token but crashed before status.
    src = root / "queue" / _token_dir(root, task_id) / f"{task_id}.json"
    (root / "queue" / "done").mkdir(parents=True, exist_ok=True)
    src.replace(root / "queue" / "done" / src.name)
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    status["state"] = "running"
    (task_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

    proc = _cli(project, "complete-handoff", task_id)
    assert '"state": "already_finalized"' in proc.stdout
    assert '"status_repaired": "completed"' in proc.stdout
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "completed"
    assert status["phase"] == "reconciled_from_queue"
    # Idempotent: second call repairs nothing.
    proc = _cli(project, "complete-handoff", task_id)
    assert "status_repaired" not in proc.stdout
