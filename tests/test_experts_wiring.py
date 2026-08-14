"""Smoke tests for the optional Talos expert roster (additive, non-breaking).

Covers:
  - enablement precedence (config flag > env var > soft auto-detect)
  - the experts block assembly (router + available list + conventions pointer)
  - integration into build_role_system_prompt: ON for cto when enabled, OFF when
    disabled, NEVER for non-cto roles, and never breaks the legacy prompt
  - install_experts.py install / status / remove end-to-end on a temp project
  - all shipped agent definitions parse (frontmatter name/description/tools/model)
"""
from __future__ import annotations

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from openclaw_claude_loop import ProjectLoop  # noqa: E402
from openclaw_claude_loop.cli import build_role_system_prompt  # noqa: E402
from openclaw_claude_loop.experts_wiring import (  # noqa: E402
    build_experts_block,
    experts_enabled,
)

PACK = MODULE_ROOT / "experts"
INSTALLER = PACK / "install_experts.py"
EXPECTED_EXPERTS = {
    "creative-3d-frontend",
    "design-ux",
    "backend-payments",
    "qa-verify",
    "security-reviewer",
    "devops-deploy",
    "growth-copy",
    "spec-keeper",
    "test-engineer",
}


def _install(project: Path) -> int:
    """Run the installer in-process via runpy (exercises its real main())."""
    argv = ["install_experts.py", "--project-root", str(project)]
    old = sys.argv
    sys.argv = argv
    try:
        try:
            runpy.run_path(str(INSTALLER), run_name="__main__")
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0
    finally:
        sys.argv = old


def _run_installer(project: Path, *extra: str) -> int:
    argv = ["install_experts.py", "--project-root", str(project), *extra]
    old = sys.argv
    sys.argv = argv
    try:
        try:
            runpy.run_path(str(INSTALLER), run_name="__main__")
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0
    finally:
        sys.argv = old


class AgentDefinitionTests(unittest.TestCase):
    def test_all_experts_present_and_parseable(self) -> None:
        agent_files = sorted((PACK / "agents").glob("*.md"))
        names = {f.stem for f in agent_files}
        self.assertEqual(names, EXPECTED_EXPERTS)
        for f in agent_files:
            text = f.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), f"{f.name} missing frontmatter")
            fm = text.split("---", 2)[1]
            self.assertIn(f"name: {f.stem}", fm, f"{f.name} name mismatch")
            self.assertIn("description:", fm)
            self.assertIn("tools:", fm)
            self.assertIn("model:", fm)

    def test_review_and_qa_gates_exist(self) -> None:
        sec = (PACK / "agents" / "security-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("model: opus", sec)  # adversarial gate runs on opus
        self.assertIn("SIGN-OFF", sec)
        self.assertIn("BLOCKED", sec)
        qa = (PACK / "agents" / "qa-verify.md").read_text(encoding="utf-8")
        self.assertIn("WebKit", qa)  # verify-by-running gate, cross-engine

    def test_spec_keeper_anti_drift_gate(self) -> None:
        sk = (PACK / "agents" / "spec-keeper.md").read_text(encoding="utf-8")
        self.assertIn("model: sonnet", sk)  # structured spec work, not deep reasoning
        self.assertIn("SPECS-IN-SYNC", sk)  # in-sync verdict
        self.assertIn("SPEC-DRIFT", sk)  # drift verdict
        self.assertIn("CONTRACTS.md", sk)  # owns the contract index
        # composes with the review gate rather than duplicating it
        self.assertNotIn("SIGN-OFF", sk)

    def test_router_wires_spec_keeper_as_drift_gate(self) -> None:
        router = (PACK / "ROUTER.md").read_text(encoding="utf-8")
        self.assertIn("spec-keeper", router)
        self.assertIn("data contract", router)
        # spec gate is sequenced before verify + review, not in place of them
        self.assertIn("security-reviewer", router)

    def test_test_engineer_coverage_gate(self) -> None:
        te = (PACK / "agents" / "test-engineer.md").read_text(encoding="utf-8")
        self.assertIn("model: sonnet", te)  # test authoring, not deep reasoning
        self.assertIn("Bash", te)  # it must be able to RUN the tests it writes
        self.assertIn("TESTS-GREEN", te)  # green verdict
        self.assertIn("TESTS-RED", te)  # red / untested verdict
        # it AUTHORS tests and explicitly defers running the live app to qa-verify
        self.assertIn("qa-verify", te)
        # composes with the review gate rather than duplicating it
        self.assertNotIn("SIGN-OFF", te)

    def test_router_sequences_test_engineer_between_spec_and_verify(self) -> None:
        router = (PACK / "ROUTER.md").read_text(encoding="utf-8")
        self.assertIn("test-engineer", router)
        # the explicit gate sequence line orders them: spec → test → qa → review
        seq = next(
            ln for ln in router.splitlines()
            if "spec-keeper" in ln and "qa-verify" in ln and "→" in ln
        )
        i_spec = seq.index("spec-keeper")
        i_test = seq.index("test-engineer")
        i_qa = seq.index("qa-verify")
        i_sec = seq.index("review")
        self.assertLess(i_spec, i_test, "spec-keeper should precede test-engineer")
        self.assertLess(i_test, i_qa, "test-engineer should precede qa-verify")
        self.assertLess(i_qa, i_sec, "qa-verify should precede review")

    def test_qa_verify_scope_notes_author_vs_run(self) -> None:
        qa = (PACK / "agents" / "qa-verify.md").read_text(encoding="utf-8")
        self.assertIn("test-engineer", qa)  # boundary spelled out on the qa side too


class EnablementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = os.environ.pop("TALOS_EXPERTS", None)

    def tearDown(self) -> None:
        os.environ.pop("TALOS_EXPERTS", None)
        if self._saved_env is not None:
            os.environ["TALOS_EXPERTS"] = self._saved_env

    def test_disabled_by_default_clean_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            self.assertFalse(experts_enabled(p, {}))
            self.assertFalse(experts_enabled(p, None))

    def test_config_flag_wins_over_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            # explicit false hard-disables even with agents installed + env on
            _install(p)
            os.environ["TALOS_EXPERTS"] = "1"
            self.assertFalse(experts_enabled(p, {"experts": {"enabled": False}}))
            self.assertTrue(experts_enabled(p, {"experts": {"enabled": True}}))

    def test_env_var_enables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            os.environ["TALOS_EXPERTS"] = "yes"
            self.assertTrue(experts_enabled(p, {}))
            os.environ["TALOS_EXPERTS"] = "0"
            self.assertFalse(experts_enabled(p, {}))

    def test_soft_auto_on_after_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            self.assertFalse(experts_enabled(p, {}))
            _install(p)  # installs agents + router doc
            self.assertTrue(experts_enabled(p, {}))


class ExpertsBlockTests(unittest.TestCase):
    def test_block_none_when_nothing_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_experts_block(Path(tmp)))

    def test_block_contains_router_agents_and_conventions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            _install(p)
            block = build_experts_block(p)
            self.assertIsNotNone(block)
            assert block is not None
            self.assertIn("Talos Expert Router", block)
            self.assertIn("security-reviewer", block)
            self.assertIn("qa-verify", block)
            self.assertIn("spec-keeper", block)  # auto-discovered into the roster
            self.assertIn("test-engineer", block)  # auto-discovered into the roster
            self.assertIn("CONVENTIONS.md", block)
            self.assertIn("MANDATORY gates", block)


class PromptIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = os.environ.pop("TALOS_EXPERTS", None)

    def tearDown(self) -> None:
        os.environ.pop("TALOS_EXPERTS", None)
        if self._saved_env is not None:
            os.environ["TALOS_EXPERTS"] = self._saved_env

    def test_cto_prompt_excludes_experts_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "proj"
            p.mkdir()
            loop = ProjectLoop(p)
            loop.bootstrap()
            prompt = build_role_system_prompt(loop.loop_root, p, "cto")
            self.assertIsNotNone(prompt)
            assert prompt is not None
            self.assertIn("project CTO", prompt)  # legacy text intact
            self.assertNotIn("Talos Expert Router", prompt)

    def test_cto_prompt_includes_experts_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "proj"
            p.mkdir()
            loop = ProjectLoop(p)
            loop.bootstrap()
            _install(p)  # soft auto-on
            loop.set_experts_enabled(True)  # belt-and-suspenders explicit flag
            prompt = build_role_system_prompt(loop.loop_root, p, "cto")
            assert prompt is not None
            self.assertIn("project CTO", prompt)  # legacy text STILL there
            self.assertIn("Talos Expert Router", prompt)
            self.assertIn("security-reviewer", prompt)

    def test_non_cto_role_never_gets_experts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "proj"
            p.mkdir()
            loop = ProjectLoop(p)
            loop.bootstrap()
            _install(p)
            loop.set_experts_enabled(True)
            for role in ("builder", "qa", "frontend"):
                prompt = build_role_system_prompt(loop.loop_root, p, role)
                if prompt is not None:
                    self.assertNotIn("Talos Expert Router", prompt, f"leaked into {role}")


class InstallerEndToEndTests(unittest.TestCase):
    def test_install_status_remove_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            # install
            self.assertEqual(_install(p), 0)
            agents_dir = p / ".claude" / "agents"
            installed = {f.stem for f in agents_dir.glob("*.md")}
            self.assertEqual(installed, EXPECTED_EXPERTS)
            self.assertTrue((p / ".openclaw" / "claude-loop" / "experts" / "ROUTER.md").is_file())
            self.assertTrue((p / "docs" / "CONVENTIONS.md").is_file())
            # status exits 0
            self.assertEqual(_run_installer(p, "--status"), 0)
            # remove
            self.assertEqual(_run_installer(p, "--remove"), 0)
            self.assertEqual(list(agents_dir.glob("*.md")), [])
            self.assertFalse((p / ".openclaw" / "claude-loop" / "experts" / "ROUTER.md").is_file())
            # CONVENTIONS is intentionally left behind
            self.assertTrue((p / "docs" / "CONVENTIONS.md").is_file())

    def test_install_does_not_clobber_existing_conventions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            conv = p / "docs" / "CONVENTIONS.md"
            conv.parent.mkdir(parents=True)
            conv.write_text("MY PROJECT RULES\n", encoding="utf-8")
            _install(p)
            self.assertEqual(conv.read_text(encoding="utf-8"), "MY PROJECT RULES\n")

    def test_install_skips_existing_agents_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            _install(p)
            tampered = p / ".claude" / "agents" / "qa-verify.md"
            tampered.write_text("custom\n", encoding="utf-8")
            _install(p)  # no --force
            self.assertEqual(tampered.read_text(encoding="utf-8"), "custom\n")
            self.assertEqual(_run_installer(p, "--force"), 0)
            self.assertNotEqual(tampered.read_text(encoding="utf-8"), "custom\n")


if __name__ == "__main__":
    unittest.main()
