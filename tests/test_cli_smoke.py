from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), *args],
        cwd=MODULE_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )


class CliSmokeTests(unittest.TestCase):
    def test_bootstrap_enqueue_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            (project / "README.md").write_text("# Toy\n", encoding="utf-8")

            run_cli(project, "bootstrap")
            task = run_cli(
                project,
                "enqueue",
                "Inspect README",
                "--role",
                "builder",
                "--instruction",
                "Read README.md and summarize it.",
            ).stdout.strip()
            run_cli(project, "dry-run")

            loop = project / ".openclaw" / "claude-loop"
            status = json.loads((loop / "tasks" / task / "status.json").read_text(encoding="utf-8"))
            result = json.loads((loop / "tasks" / task / "result.json").read_text(encoding="utf-8"))

            self.assertEqual(status["state"], "completed")
            self.assertEqual(result["state"], "completed")
            self.assertTrue((loop / "queue" / "done" / f"{task}.json").exists())
            prompt = (loop / "tasks" / task / "prompt.md").read_text(encoding="utf-8")
            self.assertIn("Inspect README", prompt)

    def test_subscription_interactive_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Tiny scoped write", "--instruction", "Create hello.txt").stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            loop = project / ".openclaw" / "claude-loop"
            status = json.loads((loop / "tasks" / task / "status.json").read_text(encoding="utf-8"))
            approval = json.loads((loop / "tasks" / task / "approval.json").read_text(encoding="utf-8"))

            self.assertEqual(status["state"], "needs_approval")
            self.assertEqual(approval["state"], "ready_for_handoff")
            self.assertTrue((loop / "queue" / "blocked" / f"{task}.json").exists())

    def test_complete_handoff_marks_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Tiny scoped write", "--instruction", "Create hello.txt").stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            loop = project / ".openclaw" / "claude-loop"
            result_payload = {
                "task_id": task,
                "state": "completed",
                "summary": "Created hello.txt as instructed.",
                "changes": {"files_created": ["hello.txt"], "files_modified": [], "files_deleted": []},
                "verification": {"commands": ["cat hello.txt"], "results": ["hello"]},
                "artifacts": ["hello.txt"],
                "deployment_status": {
                    "state": "not_deployed",
                    "details": "Local file change only; no deploy target configured.",
                },
                "next_actions": [],
            }
            (loop / "tasks" / task / "result.json").write_text(
                json.dumps(result_payload), encoding="utf-8"
            )

            run_cli(project, "complete-handoff", task)

            status = json.loads((loop / "tasks" / task / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["phase"], "done")
            self.assertTrue((loop / "queue" / "done" / f"{task}.json").exists())
            self.assertFalse((loop / "queue" / "blocked" / f"{task}.json").exists())

    def test_complete_handoff_missing_result_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Tiny scoped write", "--instruction", "Create hello.txt").stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            proc = subprocess.run(
                [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), "complete-handoff", task],
                cwd=MODULE_ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("No result.json", proc.stderr)

    def test_run_handoff_requires_blocked_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Pending one", "--instruction", "noop").stdout.strip()

            proc = subprocess.run(
                [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), "run-handoff", task],
                cwd=MODULE_ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("needs_approval", proc.stderr)

    def test_run_handoff_requires_prompt_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Unknown one", "--instruction", "noop").stdout.strip()

            proc = subprocess.run(
                [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), "run-handoff", "no-such-task"],
                cwd=MODULE_ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Unknown task", proc.stderr)

    def test_complete_handoff_accepts_verified_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Verify task", "--instruction", "noop").stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            loop = project / ".openclaw" / "claude-loop"
            (loop / "tasks" / task / "result.json").write_text(
                json.dumps(
                    {
                        "task_id": task,
                        "state": "verified",
                        "summary": "All good.",
                        "changes": {"files_created": [], "files_modified": ["README.md"], "files_deleted": []},
                        "verification": {"commands": ["true"], "results": ["ok"]},
                        "artifacts": ["README.md"],
                        "deployment_status": {
                            "state": "not_applicable",
                            "details": "Verification-only task.",
                        },
                        "next_actions": [],
                    }
                ),
                encoding="utf-8",
            )

            run_cli(project, "complete-handoff", task)

            status = json.loads((loop / "tasks" / task / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "completed")
            self.assertTrue((loop / "queue" / "done" / f"{task}.json").exists())

    def test_complete_handoff_rejects_completed_cto_without_artifact_or_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(
                project,
                "enqueue",
                "Plan feature",
                "--role",
                "cto",
                "--instruction",
                "Produce an implementation plan.",
            ).stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            loop = project / ".openclaw" / "claude-loop"
            (loop / "tasks" / task / "result.json").write_text(
                json.dumps({"task_id": task, "state": "completed", "summary": "Done."}),
                encoding="utf-8",
            )

            proc = subprocess.run(
                [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(project), "complete-handoff", task],
                cwd=MODULE_ROOT,
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing required completion evidence", proc.stderr)
            self.assertIn("deployment_status", proc.stderr)
            self.assertTrue((loop / "queue" / "blocked" / f"{task}.json").exists())

    def test_rendered_prompt_requires_artifacts_and_deployment_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Plan feature", "--role", "cto", "--instruction", "Plan it.").stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            prompt = (
                project
                / ".openclaw"
                / "claude-loop"
                / "tasks"
                / task
                / "prompt.md"
            ).read_text(encoding="utf-8")

            self.assertIn("Required result.json contract", prompt)
            self.assertIn("artifacts", prompt)
            self.assertIn("deployment_status.state", prompt)

    def test_complete_handoff_routes_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            run_cli(project, "bootstrap")
            task = run_cli(project, "enqueue", "Tiny scoped write", "--instruction", "Create hello.txt").stdout.strip()
            run_cli(project, "run-worker", "--backend", "subscription-interactive", "--once")

            loop = project / ".openclaw" / "claude-loop"
            (loop / "tasks" / task / "result.json").write_text(
                json.dumps({"task_id": task, "state": "failed", "summary": "Could not complete."}),
                encoding="utf-8",
            )

            run_cli(project, "complete-handoff", task)

            status = json.loads((loop / "tasks" / task / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "failed")
            self.assertTrue((loop / "queue" / "failed" / f"{task}.json").exists())


if __name__ == "__main__":
    unittest.main()
