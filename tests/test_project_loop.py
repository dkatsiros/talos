from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure the module under test is importable when running from the package root.
MODULE_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from openclaw_claude_loop import ProjectLoop, ProjectLoopError  # noqa: E402


class ProjectLoopTests(unittest.TestCase):
    def test_bootstrap_and_delegate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            loop = ProjectLoop(project)
            self.assertFalse(loop.is_bootstrapped)
            loop.bootstrap()
            self.assertTrue(loop.is_bootstrapped)

            task_id = loop.delegate(
                title="Inspect README",
                instructions="Read README.md and summarize.",
                role="builder",
            )
            self.assertTrue(task_id)
            task_json = loop.loop_root / "tasks" / task_id / "task.json"
            self.assertTrue(task_json.exists())
            data = json.loads(task_json.read_text(encoding="utf-8"))
            self.assertEqual(data["role"], "builder")
            self.assertEqual(data["title"], "Inspect README")

    def test_delegate_without_bootstrap_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            loop = ProjectLoop(project)
            with self.assertRaises(ProjectLoopError):
                loop.delegate(title="x", instructions="y")

    def test_result_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            loop = ProjectLoop(project)
            loop.bootstrap()
            task_id = loop.delegate(title="x", instructions="y", role="builder")
            self.assertIsNone(loop.result(task_id))

    def test_discover_project_context_prefers_claude_md(self) -> None:
        from openclaw_claude_loop.cli import discover_project_context

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"
            project.mkdir()
            (project / "CLAUDE.md").write_text("# from CLAUDE\n", encoding="utf-8")
            (project / "context.md").write_text("# from context\n", encoding="utf-8")
            found = discover_project_context(project)
            self.assertIsNotNone(found)
            assert found is not None
            name, content = found
            self.assertEqual(name, "CLAUDE.md")
            self.assertIn("from CLAUDE", content)

    def test_discover_project_context_falls_back_to_context_md(self) -> None:
        from openclaw_claude_loop.cli import discover_project_context

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"
            project.mkdir()
            (project / "context.md").write_text("# project context\n", encoding="utf-8")
            found = discover_project_context(project)
            self.assertIsNotNone(found)
            assert found is not None
            name, content = found
            self.assertEqual(name, "context.md")
            self.assertIn("project context", content)

    def test_discover_project_context_returns_none_when_missing(self) -> None:
        from openclaw_claude_loop.cli import discover_project_context

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"
            project.mkdir()
            self.assertIsNone(discover_project_context(project))

    def test_build_role_system_prompt_includes_context(self) -> None:
        from openclaw_claude_loop.cli import build_role_system_prompt

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "p"
            project.mkdir()
            loop = ProjectLoop(project)
            loop.bootstrap()
            (project / "context.md").write_text("# uses Django + Postgres\n", encoding="utf-8")
            prompt = build_role_system_prompt(loop.loop_root, project, "cto")
            self.assertIsNotNone(prompt)
            assert prompt is not None
            self.assertIn("project CTO", prompt)
            self.assertIn("Project standing context (from `context.md`)", prompt)
            self.assertIn("Django + Postgres", prompt)

    def test_cto_role_text_present_after_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "toy"
            project.mkdir()
            loop = ProjectLoop(project)
            loop.bootstrap()
            cto_role = (loop.loop_root / "roles" / "cto.md").read_text(encoding="utf-8")
            self.assertIn("project CTO", cto_role)
            self.assertIn("clarification.json", cto_role)


if __name__ == "__main__":
    unittest.main()
