"""Tests for the Phase 1 auto-delivery wiring.

DoD coverage:
  1. finalize-SHA-record  -> cli.record_merge_proof stamps a durable tip_sha.
  2. heal-under-lock      -> the gate blocks heal when off; when on, heal merges
                             a stranded done-task under the merge lock.
  3. EOD report + LOOPS.md ingestion -> loops parsing + cross-referencing the
                             delivery report (stuck / closeable), via cmd_reconcile.
  4. deploy watcher       -> commit -> deploy -> verify -> record live sha, with
                             fail-loud deploy/verify paths.
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path

from openclaw_claude_loop import cli, deploy_watch, loops, reconciler


# --------------------------------------------------------------------------- #
# Shared git / loop fixtures (mirrors tests/test_reconciler.py)
# --------------------------------------------------------------------------- #
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
    # Real projects keep the loop dir out of git; without this, a later
    # `git add -A` on a task branch would sweep .openclaw/ into the commit and
    # `checkout` would then delete config.json from the working tree.
    (repo / ".gitignore").write_text(".openclaw/\n", encoding="utf-8")
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
    _git(repo, "checkout", "-q", "-b", f"talos/{task_id}")
    (repo / filename).write_text(f"work for {task_id}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"work {task_id}")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return tip


def _stranded_done_token(root: Path, task_id: str) -> None:
    cli.write_json(root / "queue" / "done" / f"{task_id}.json", {"task_id": task_id})
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    # done token but never merged: worktree_branch present, no merge proof.
    cli.write_json(task_dir / "status.json", {
        "worktree_branch": f"talos/{task_id}", "worktree_base": "main",
    })
    cli.write_json(task_dir / "result.json", {
        "task_id": task_id, "state": "completed", "summary": "did work",
        "changes": {"files_created": [f"{task_id}.txt"], "files_modified": [],
                    "files_deleted": []},
    })


# --------------------------------------------------------------------------- #
# DoD 1 — finalize records a durable merge-proof tip_sha
# --------------------------------------------------------------------------- #
class RecordMergeProofTests(unittest.TestCase):
    def test_records_branch_tip_while_branch_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            tip = _make_task_branch(repo, "t1", "a.txt")
            outcome = {"state": "skipped_dirty", "branch": "talos/t1", "base": "main"}
            cli.record_merge_proof(repo, outcome)
            self.assertEqual(outcome["tip_sha"], tip)
            self.assertEqual(outcome["base_sha"], _git(repo, "rev-parse", "main"))

    def test_falls_back_to_base_tip_when_branch_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            # merged+deleted: branch no longer resolves -> proof is the base tip,
            # which now contains the work (is-ancestor passes).
            outcome = {"state": "merged", "branch": "talos/gone", "base": "main"}
            cli.record_merge_proof(repo, outcome)
            base_tip = _git(repo, "rev-parse", "main")
            self.assertEqual(outcome["tip_sha"], base_tip)
            self.assertTrue(reconciler.is_ancestor(repo, outcome["tip_sha"], "main"))

    def test_never_raises_and_returns_noop_for_non_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            self.assertIsNone(cli.record_merge_proof(repo, None))


# --------------------------------------------------------------------------- #
# DoD 2 — heal is gated, and when enabled merges under the merge lock
# --------------------------------------------------------------------------- #
class HealGateTests(unittest.TestCase):
    def test_gate_defaults_off(self) -> None:
        self.assertFalse(cli.reconcile_heal_enabled({}))
        self.assertFalse(cli.reconcile_heal_enabled(None))

    def test_gate_explicit_config_wins(self) -> None:
        self.assertTrue(cli.reconcile_heal_enabled({"reconciler": {"heal_enabled": True}}))
        self.assertFalse(cli.reconcile_heal_enabled({"reconciler": {"heal_enabled": False}}))

    def _args(self, repo: Path, **over: object) -> argparse.Namespace:
        ns = argparse.Namespace(
            project_root=str(repo), base=None, ledger_sha=None, heal=False,
            report=False, scan_crashes=False, loops_file=None, no_loops=True,
        )
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    def test_heal_refused_when_gate_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            _bootstrap_loop(repo)
            with self.assertRaises(SystemExit):
                cli.cmd_reconcile(self._args(repo, heal=True))

    def test_heal_when_gate_on_merges_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            cli.write_json(root / "config.json",
                           {"version": 1, "reconciler": {"heal_enabled": True}})
            task_id = "20260905T135939Z-heal-me"
            tip = _make_task_branch(repo, task_id, f"{task_id}.txt")
            _stranded_done_token(root, task_id)

            # Precondition: not yet an ancestor of main.
            self.assertFalse(reconciler.is_ancestor(repo, tip, "main"))

            rc = cli.cmd_reconcile(self._args(repo, heal=True))

            # Post: the branch tip is now merged into main (delivered).
            self.assertTrue(reconciler.is_ancestor(repo, tip, "main"))
            # The merge lock file was created by project_lock during heal.
            self.assertTrue((root / "locks" / "merge.lock").exists())
            self.assertEqual(rc, 0)


# --------------------------------------------------------------------------- #
# DoD 3 — EOD delivery report + LOOPS.md ingestion
# --------------------------------------------------------------------------- #
SAMPLE_LOOPS = """# LOOPS.md

## Section
1. ✅ **Already done thing** — committed.
16. 🔄 **Reconciler merge-heal + Phase 1** (APPROVED) — task `20260905T135939Z-do-it` (local).
7. ⏳ **Hotel Concierge progress** — queued, no task yet.
9. ❌ **Broken loop** — task `20260101T000000Z-broken` failed.
just prose, not a loop, ✅ ignore me
"""


class LoopsIngestionTests(unittest.TestCase):
    def test_parse_extracts_status_and_task_ids(self) -> None:
        parsed = loops.parse_loops(SAMPLE_LOOPS)
        # prose line (no list marker) is skipped even though it has an emoji.
        self.assertEqual(len(parsed), 4)
        by_status = {p["status"] for p in parsed}
        self.assertEqual(by_status, {"done", "in_flight", "pending", "failed"})
        inflight = next(p for p in parsed if p["status"] == "in_flight")
        self.assertEqual(inflight["task_ids"], ["20260905T135939Z-do-it"])
        self.assertEqual(inflight["title"], "Reconciler merge-heal + Phase 1")

    def test_sweep_flags_stuck_and_closeable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loops_path = Path(tmp) / "LOOPS.md"
            loops_path.write_text(SAMPLE_LOOPS, encoding="utf-8")
            report = {
                "delivered": [{"task_id": "20260905T135939Z-do-it"}],
                "stuck": [{"task_id": "20260101T000000Z-broken"}],
                "stranded": [], "stalled": [],
            }
            findings = loops.sweep_loops(loops_path, report)
            # done loop excluded; 3 open loops remain.
            self.assertEqual(len(findings), 3)
            broken = next(f for f in findings if "20260101T000000Z-broken" in f["task_ids"])
            self.assertTrue(broken["stuck"])
            closeable = next(f for f in findings if "20260905T135939Z-do-it" in f["task_ids"])
            self.assertFalse(closeable["stuck"])
            self.assertIn("DELIVERED", closeable["note"])

    def test_cmd_reconcile_writes_report_and_ingests_loops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            root = _bootstrap_loop(repo)
            loops_path = repo / "LOOPS.md"
            loops_path.write_text(SAMPLE_LOOPS, encoding="utf-8")
            ns = argparse.Namespace(
                project_root=str(repo), base=None, ledger_sha=None, heal=False,
                report=True, scan_crashes=False,
                loops_file=str(loops_path), no_loops=False,
            )
            rc = cli.cmd_reconcile(ns)
            # EOD ledger line was appended (its absence is the dead-man alarm).
            self.assertTrue((root / "logs" / reconciler.DELIVERY_REPORT_LOG).exists())
            # A ❌ loop referencing a task with no reconciler signal is open but
            # not stuck -> rc 0 (no reconciler-side stuck items in an empty repo).
            self.assertEqual(rc, 0)

    def test_resolve_loops_path_walks_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "LOOPS.md").write_text("# loops\n", encoding="utf-8")
            project = workspace / "projects" / "p"
            project.mkdir(parents=True)
            found = loops.resolve_loops_path(project)
            self.assertEqual(found, workspace / "LOOPS.md")


# --------------------------------------------------------------------------- #
# DoD 4 — commit -> auto-redeploy -> verify-live watcher
# --------------------------------------------------------------------------- #
class _FakeRunner:
    """Scripted argv -> (rc, out). rev-parse always resolves to ``tip``."""

    def __init__(self, tip: str, deploy_rc: int = 0, verify_rc: int = 0) -> None:
        self.tip = tip
        self.deploy_rc = deploy_rc
        self.verify_rc = verify_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> "tuple[int, str]":
        self.calls.append(argv)
        if "rev-parse" in argv:
            return 0, self.tip + "\n"
        if argv and argv[0] == "deploy":
            return self.deploy_rc, "deploy ran"
        if argv and argv[0] == "verify":
            return self.verify_rc, "verify ran"
        return 0, ""


class DeployWatcherTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp) / "loop"
        (root / "logs").mkdir(parents=True, exist_ok=True)
        return root

    def test_new_commit_deploys_verifies_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            run = _FakeRunner(tip="a" * 40)
            res = deploy_watch.watch_once(
                root, Path(tmp) / "proj", base="main",
                deploy_cmd=["deploy"], verify_cmd=["verify"], run=run)
            self.assertEqual(res["action"], "deployed")
            state = cli.read_json(root / deploy_watch.DEPLOY_STATE, {})
            self.assertEqual(state["deployed_sha"], "a" * 40)
            self.assertTrue((root / "logs" / deploy_watch.DEPLOY_LOG).exists())
            self.assertIn(["deploy"], run.calls)
            self.assertIn(["verify"], run.calls)

    def test_same_tip_is_noop_and_does_not_redeploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            cli.write_json(root / deploy_watch.DEPLOY_STATE, {"deployed_sha": "b" * 40})
            run = _FakeRunner(tip="b" * 40)
            res = deploy_watch.watch_once(
                root, Path(tmp) / "proj", base="main",
                deploy_cmd=["deploy"], run=run)
            self.assertEqual(res["action"], "noop")
            self.assertNotIn(["deploy"], run.calls)

    def test_deploy_failure_is_fail_loud_and_does_not_advance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            run = _FakeRunner(tip="c" * 40, deploy_rc=1)
            res = deploy_watch.watch_once(
                root, Path(tmp) / "proj", base="main",
                deploy_cmd=["deploy"], verify_cmd=["verify"], run=run)
            self.assertEqual(res["action"], "deploy_failed")
            self.assertFalse((root / deploy_watch.DEPLOY_STATE).exists())
            self.assertNotIn(["verify"], run.calls)  # verify skipped on deploy fail

    def test_verify_failure_is_fail_loud_and_does_not_advance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            run = _FakeRunner(tip="d" * 40, verify_rc=1)
            res = deploy_watch.watch_once(
                root, Path(tmp) / "proj", base="main",
                deploy_cmd=["deploy"], verify_cmd=["verify"], run=run)
            self.assertEqual(res["action"], "verify_failed")
            self.assertFalse((root / deploy_watch.DEPLOY_STATE).exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
