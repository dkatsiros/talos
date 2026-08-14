"""Phase-2 hook tests.

Covers the byte-identical no-op guarantee when the hook is disabled (the
Phase-2 safety contract) plus one unit test per new guard: G1 opt-in,
G2 success-only, G3 code-changed-only, G4 anti-recursion, G11 kill-switch,
G13 daily cap.

The updater is never spawned — every enabled-path test either short-circuits
before the updater call or uses `dry_run_stub` via a monkey-patched
`run_doc_updater`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from openclaw_claude_loop.docsync import hook as hook_mod
from openclaw_claude_loop.docsync.hook import (
    DEFAULT_DAILY_TOKEN_CAP,
    DOCSYNC_MARKER,
    maybe_run_docsync,
)


MODULE_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openclaw_claude_loop",
         "--project-root", str(project), *args],
        cwd=MODULE_ROOT, text=True, capture_output=True, check=True,
    )


COMPLETED_RESULT = {
    "state": "completed",
    "summary": "Wrote hello.txt as instructed.",
    "changes": {
        "files_created": ["src/foo.py"],
        "files_modified": [],
        "files_deleted": [],
    },
    "verification": {"commands": ["python -c 'import foo'"], "results": ["ok"]},
    "artifacts": ["src/foo.py"],
    "deployment_status": {
        "state": "not_deployed",
        "details": "Local file change only.",
    },
    "next_actions": [],
}


def _bootstrap_completed(project: Path) -> tuple[Path, str]:
    """Bootstrap a project, run one task through subscription-interactive,
    write result.json — but do NOT call complete-handoff yet. Return the
    project's loop root + task_id."""
    project.mkdir(parents=True, exist_ok=True)
    run_cli(project, "bootstrap")
    task = run_cli(project, "enqueue", "Tiny scoped write",
                   "--instruction", "Create src/foo.py").stdout.strip()
    run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")
    loop = project / ".openclaw" / "claude-loop"
    payload = dict(COMPLETED_RESULT)
    payload["task_id"] = task
    (loop / "tasks" / task / "result.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    return loop, task


# --------------------------------------------------------------------------- #
# Byte-identical no-op contract when disabled                                 #
# --------------------------------------------------------------------------- #

class HookDisabledIsNoOpTests(unittest.TestCase):
    """Phase-2 safety contract: with docsync disabled (the default for every
    project), a Talos completion must be a pure no-op:
        - stdout JSON must not contain a `docsync` key
        - the `.openclaw/claude-loop/docsync/` scratch dir must NOT exist
        - stdout JSON must have exactly the pre-Phase-2 key set
    """

    def test_complete_handoff_default_off_produces_pre_phase2_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            proc = run_cli(project, "complete-handoff", task)

            # No side-effect scratch dir was created.
            self.assertFalse((loop / "docsync").exists(),
                             "docsync/ directory was created when hook is disabled")

            # stdout is valid JSON, has the expected keys (merge added in Phase-3),
            # no docsync (docsync hook is disabled for this test).
            parsed = json.loads(proc.stdout.strip().splitlines()[-1] if False else proc.stdout)
            self.assertEqual(set(parsed.keys()), {"task_id", "state", "queue", "merge"})
            self.assertNotIn("docsync", parsed)
            self.assertEqual(parsed["state"], "completed")
            self.assertEqual(parsed["queue"], "done")

            # The task ended up in queue/done/ — completion path is otherwise
            # unchanged.
            self.assertTrue((loop / "queue" / "done" / f"{task}.json").exists())

    def test_bytewise_identical_payload_shape(self) -> None:
        """The exact bytes of the printed payload must match the current shape.
        Includes the merge key (added when the worktree path is absent, the
        outcome is no_worktree). If a future change ever accidentally leaks a
        docsync=None into the payload, this test catches it."""
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            proc = run_cli(project, "complete-handoff", task)
            expected = json.dumps(
                {
                    "merge": {"detail": "task ran directly in project_root", "state": "no_worktree"},
                    "queue": "done",
                    "state": "completed",
                    "task_id": task,
                },
                indent=2, sort_keys=True,
            )
            self.assertEqual(proc.stdout.strip(), expected)


# --------------------------------------------------------------------------- #
# Unit tests for each new guard                                               #
# --------------------------------------------------------------------------- #

def _write_config(project: Path, docsync_cfg: dict) -> None:
    cfg_path = project / ".openclaw" / "claude-loop" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["docsync"] = docsync_cfg
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


class GuardG1OptInTests(unittest.TestCase):
    def test_hook_returns_none_when_not_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="completed",
            )
            self.assertIsNone(out)
            self.assertFalse((loop / "docsync").exists())

    def test_hook_returns_none_when_enabled_flag_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"mode": "propose"})  # no `enabled` key
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="completed",
            )
            self.assertIsNone(out)


class GuardG2SuccessOnlyTests(unittest.TestCase):
    def test_skips_on_failed_state(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True})
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="failed",
            )
            self.assertEqual(out, {"state": "skipped", "reason": "task_not_completed"})

    def test_skips_on_blocked_state(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True})
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="blocked",
            )
            self.assertEqual(out, {"state": "skipped", "reason": "task_not_completed"})


class GuardG3CodeChangedOnlyTests(unittest.TestCase):
    def test_doc_only_diff_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True})
            doc_only = dict(COMPLETED_RESULT)
            doc_only["changes"] = {
                "files_created": [],
                "files_modified": ["README.md", "docs/api.md", "CHANGELOG.md"],
                "files_deleted": ["tests/test_x.py"],
            }
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=doc_only,
                result_state="completed",
            )
            self.assertEqual(out, {"state": "skipped", "reason": "no_code_change"})


class GuardG4AntiRecursionTests(unittest.TestCase):
    def test_head_commit_with_docsync_marker_is_skipped(self, ) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True})

            # Init a git repo with one commit whose message carries the marker.
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
            (project / "seed.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=project, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m",
                 f"📝 docsync\n\n{DOCSYNC_MARKER}: true"],
                cwd=project, check=True,
            )
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="completed",
            )
            self.assertEqual(out, {"state": "skipped", "reason": "docsync_marker_on_head"})


class GuardG11KillSwitchTests(unittest.TestCase):
    def test_project_kill_switch_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True})
            (loop / "docsync.DISABLED").write_text("", encoding="utf-8")
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="completed",
            )
            self.assertIsNone(out)


class GuardG13DailyCapTests(unittest.TestCase):
    def test_over_cap_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True, "daily_token_cap": 1000})
            budget_dir = loop / "docsync"
            budget_dir.mkdir(parents=True, exist_ok=True)
            today = hook_mod._today_utc()
            (budget_dir / "budget.json").write_text(
                json.dumps({today: 5000}), encoding="utf-8",
            )
            out = maybe_run_docsync(
                project_root=project, root=loop,
                task_dir=loop / "tasks" / task,
                task={"id": task}, result=dict(COMPLETED_RESULT),
                result_state="completed",
            )
            self.assertIsNotNone(out)
            self.assertEqual(out["state"], "skipped")
            self.assertEqual(out["reason"], "daily_cap_exceeded")
            self.assertEqual(out["cap"], 1000)
            self.assertEqual(out["spent"], 5000)

    def test_default_cap_is_applied_when_not_configured(self) -> None:
        """The default cap should be `DEFAULT_DAILY_TOKEN_CAP`, not zero."""
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True})  # no daily_token_cap set
            budget_dir = loop / "docsync"
            budget_dir.mkdir(parents=True, exist_ok=True)
            today = hook_mod._today_utc()
            # Spend just under default cap → NOT over.
            (budget_dir / "budget.json").write_text(
                json.dumps({today: DEFAULT_DAILY_TOKEN_CAP - 1}), encoding="utf-8",
            )
            # This will pass G13 and then try to actually run — stub the updater
            # so the test never spawns claude.
            original = hook_mod.run_doc_updater
            try:
                def stub(**_kw):
                    from openclaw_claude_loop.docsync.updater import UpdaterResult
                    return UpdaterResult(state="skipped", reason="stubbed", diff_loc=0)
                hook_mod.run_doc_updater = stub  # type: ignore[assignment]
                out = maybe_run_docsync(
                    project_root=project, root=loop,
                    task_dir=loop / "tasks" / task,
                    task={"id": task}, result=dict(COMPLETED_RESULT),
                    result_state="completed",
                )
            finally:
                hook_mod.run_doc_updater = original  # type: ignore[assignment]
            self.assertIsNotNone(out)
            self.assertNotEqual(out.get("reason"), "daily_cap_exceeded")


# --------------------------------------------------------------------------- #
# End-to-end: enabled path reaches the updater (with stubbed subagent)        #
# --------------------------------------------------------------------------- #

class EnabledPathReachesUpdaterTests(unittest.TestCase):
    def test_enabled_project_invokes_updater_and_attaches_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            loop, task = _bootstrap_completed(project)
            _write_config(project, {"enabled": True, "mode": "propose"})

            # Stub the updater — never spawn claude.
            from openclaw_claude_loop.docsync.updater import Proposal, UpdaterResult
            original = hook_mod.run_doc_updater
            try:
                def stub(**_kw):
                    return UpdaterResult(
                        state="completed",
                        proposals=[Proposal(
                            doc_path="README.md",
                            mode="ADD",
                            fence_id="docsync-notes",
                            patch="- from stub",
                            rationale="test",
                            confidence=0.9,
                        )],
                        raw_output="stubbed",
                        diff_loc=42,
                    )
                hook_mod.run_doc_updater = stub  # type: ignore[assignment]

                out = maybe_run_docsync(
                    project_root=project, root=loop,
                    task_dir=loop / "tasks" / task,
                    task={"id": task}, result=dict(COMPLETED_RESULT),
                    result_state="completed",
                )
            finally:
                hook_mod.run_doc_updater = original  # type: ignore[assignment]

            self.assertIsNotNone(out)
            self.assertEqual(out["state"], "completed")
            self.assertEqual(out["proposals"], 1)
            self.assertEqual(out["mode"], "propose")
            # Propose mode never live-writes.
            self.assertEqual(out["written"], 0)
            # Artifact was written.
            self.assertTrue(Path(out["artifact"]).exists())
            # Budget was incremented (some non-zero spend for today).
            today = hook_mod._today_utc()
            b = json.loads((loop / "docsync" / "budget.json").read_text(encoding="utf-8"))
            self.assertGreater(int(b.get(today, 0)), 0)


if __name__ == "__main__":
    unittest.main()
