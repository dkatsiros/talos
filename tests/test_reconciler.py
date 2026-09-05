"""Tests for the Phase 0 auto-delivery reconciler.

Covers the three DoD paths explicitly:
  1. the is-ancestor delivery proof (proven vs unproven);
  2. the reconcile heal path (stranded talos/<id> branch -> merged -> delivered);
  3. the fail-loud crash path (dead session + no result.json -> screamed).

Plus the stranded-branch sweep, finalizer-stall detection, R4 merged->live,
and the delivery-report dead-man.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from openclaw_claude_loop import cli, reconciler


def _git(repo: Path, *argv: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *argv],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _bootstrap_loop(repo: Path) -> Path:
    root = cli.loop_root(repo)
    for state in cli.QUEUE_STATES:
        (root / "queue" / state).mkdir(parents=True, exist_ok=True)
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    cli.write_json(root / "config.json", {"version": 1})
    return root


def _make_task_branch(repo: Path, task_id: str, filename: str) -> str:
    """Create talos/<task_id> with one commit; return its tip sha. Leaves main checked out."""
    _git(repo, "checkout", "-q", "-b", f"talos/{task_id}")
    (repo / filename).write_text(f"work for {task_id}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"work {task_id}")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return tip


def _done_token(root: Path, task_id: str, status: dict) -> None:
    cli.write_json(root / "queue" / "done" / f"{task_id}.json", {"task_id": task_id})
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    cli.write_json(task_dir / "status.json", status)
    cli.write_json(task_dir / "result.json", {
        "task_id": task_id, "state": "completed", "summary": "did work",
        "changes": {"files_created": [f"{task_id}.txt"], "files_modified": [],
                    "files_deleted": []},
    })


class DeliveryProofTests(unittest.TestCase):
    """DoD path 1: the is-ancestor delivery proof."""

    def test_merged_branch_is_proven_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            tip = _make_task_branch(repo, "t1", "t1.txt")
            # Merge it into main (simulating a clean finalize).
            _git(repo, "merge", "--no-ff", "-m", "merge t1", f"talos/t1")

            self.assertTrue(reconciler.is_ancestor(repo, tip, "main"))
            proof = reconciler.delivery_proof(
                repo, "t1", {"merge": {"state": "merged", "tip_sha": tip, "base": "main"}})
            self.assertTrue(proof["proven"])
            self.assertEqual(proof["proof_quality"], "recorded")

    def test_unmerged_branch_is_not_proven(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            _bootstrap_loop(repo)
            tip = _make_task_branch(repo, "t2", "t2.txt")  # NOT merged

            self.assertFalse(reconciler.is_ancestor(repo, tip, "main"))
            proof = reconciler.delivery_proof(
                repo, "t2", {"merge": {"state": "conflict", "tip_sha": tip, "base": "main"}})
            self.assertFalse(proof["proven"])

    def test_missing_sha_is_not_proven(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            self.assertFalse(reconciler.is_ancestor(repo, None, "main"))
            self.assertFalse(reconciler.is_ancestor(repo, "0" * 40, "main"))


class ReconcileHealPathTests(unittest.TestCase):
    """DoD path 2: reconcile heals a stranded done-task by merging it."""

    def test_unmerged_done_task_flagged_readonly_then_healed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            tip = _make_task_branch(repo, "heal1", "heal1.txt")
            # Token says done, but merge never landed -> the interests-class bug.
            _done_token(root, "heal1",
                        {"merge": {"state": "conflict", "tip_sha": tip, "base": "main"},
                         "worktree_branch": "talos/heal1", "worktree_base": "main"})

            # Read-only pass: must flag as STUCK, must NOT merge.
            ro = reconciler.reconcile_project(root, repo, heal=False, base="main")
            self.assertEqual(ro["counts"]["delivered"], 0)
            self.assertEqual(ro["counts"]["stuck"], 1)
            self.assertEqual(ro["stuck"][0]["class"], "unmerged")
            self.assertFalse(reconciler.is_ancestor(repo, tip, "main"))

            # Heal pass: merges under the lock, becomes delivered.
            healed = reconciler.reconcile_project(root, repo, heal=True, base="main")
            self.assertEqual(healed["counts"]["healed"], 1)
            self.assertEqual(healed["counts"]["delivered"], 1)
            self.assertEqual(healed["counts"]["stuck"], 0)
            self.assertTrue(reconciler.is_ancestor(repo, tip, "main"))
            # status.merge now carries a durable proof.
            status = cli.read_json(root / "tasks" / "heal1" / "status.json", {})
            self.assertIn(status["merge"]["state"], cli.MERGE_OK_STATES)

    def test_unmergeable_task_escalates_never_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            # Create a real conflict: same file edited on both main and branch.
            (repo / "conflict.txt").write_text("branch-less base\n", encoding="utf-8")
            _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base edit")
            _git(repo, "checkout", "-q", "-b", "talos/bad")
            (repo / "conflict.txt").write_text("branch side\n", encoding="utf-8")
            _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "branch edit")
            tip = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", "-q", "main")
            (repo / "conflict.txt").write_text("main side\n", encoding="utf-8")
            _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "main edit")
            _done_token(root, "bad",
                        {"merge": {"state": "conflict", "tip_sha": tip, "base": "main"},
                         "worktree_branch": "talos/bad", "worktree_base": "main"})

            healed = reconciler.reconcile_project(root, repo, heal=True, base="main")
            self.assertEqual(healed["counts"]["healed"], 0)
            self.assertEqual(healed["counts"]["stuck"], 1)
            self.assertEqual(healed["stuck"][0]["class"], "unmergeable")
            # SCREAM: a durable ATTENTION entry + handoff alert exist.
            attention = cli.read_json(root / reconciler.ATTENTION_FILE, {})
            self.assertEqual(attention["count"], 1)
            self.assertTrue((root / "tasks" / "bad" / cli.HANDOFF_ALERT_FILE).exists())


class CrashPathTests(unittest.TestCase):
    """DoD path 3: a crashed builder must be screamed about, not read as silence."""

    def test_dead_session_no_result_is_screamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            # Claimed token, task dir exists, NO result.json.
            cli.write_json(root / "queue" / "claimed" / "crashed.json", {"task_id": "crashed"})
            (root / "tasks" / "crashed").mkdir(parents=True, exist_ok=True)
            cli.write_json(root / "tasks" / "crashed" / "status.json", {"state": "running"})

            findings = reconciler.scan_crashed_sessions(root, session_alive=lambda _id: False)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["class"], "session_crashed")
            alert = cli.read_json(root / "tasks" / "crashed" / cli.HANDOFF_ALERT_FILE, {})
            self.assertEqual(alert["outcome"], "session_crashed")

    def test_live_session_or_result_present_is_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            cli.write_json(root / "queue" / "claimed" / "alive.json", {"task_id": "alive"})
            (root / "tasks" / "alive").mkdir(parents=True, exist_ok=True)
            cli.write_json(root / "tasks" / "alive" / "status.json", {"state": "running"})
            # A live session -> not a crash.
            self.assertEqual(
                reconciler.scan_crashed_sessions(root, session_alive=lambda _id: True), [])
            # A written result -> finalizer territory (R3), not a crash.
            cli.write_json(root / "tasks" / "alive" / "result.json", {"state": "completed"})
            self.assertEqual(
                reconciler.scan_crashed_sessions(root, session_alive=lambda _id: False), [])


class SweepAndReportTests(unittest.TestCase):
    def test_stranded_branch_swept_not_healed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            _make_task_branch(repo, "orphan", "orphan.txt")  # branch with no queue token
            report = reconciler.reconcile_project(root, repo, heal=True, base="main")
            self.assertEqual(report["counts"]["stranded"], 1)
            self.assertEqual(report["stranded"][0]["branch"], "talos/orphan")
            # Never auto-merged: still not an ancestor.
            self.assertFalse(reconciler.is_ancestor(
                repo, _git(repo, "rev-parse", "talos/orphan"), "main"))

    def test_r4_merged_but_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            tip = _make_task_branch(repo, "d1", "d1.txt")
            _git(repo, "merge", "--no-ff", "-m", "merge d1", "talos/d1")
            _done_token(root, "d1",
                        {"merge": {"state": "merged", "tip_sha": tip, "base": "main"}})
            # Live sha is the initial commit (before d1) -> merged but not live.
            old_live = _git(repo, "rev-list", "--max-parents=0", "HEAD")
            report = reconciler.reconcile_project(root, repo, base="main", ledger_sha=old_live)
            self.assertEqual(report["counts"]["delivered"], 1)
            self.assertEqual(report["counts"]["delivered_live"], 0)
            self.assertTrue(any(i["class"] == "merged_not_live" for i in report["attention"]))

    def test_delivery_report_deadman(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "p"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            # No report yet -> stale (the alarm-by-absence).
            self.assertTrue(reconciler.delivery_report_stale(root, max_age_seconds=3600))
            report = reconciler.reconcile_project(root, repo, base="main")
            reconciler.write_delivery_report(root, report)
            # Fresh report -> not stale.
            self.assertFalse(reconciler.delivery_report_stale(root, max_age_seconds=3600))
            text = reconciler.render_delivery_report(report)
            self.assertIn("delivery report", text)


if __name__ == "__main__":
    unittest.main()
