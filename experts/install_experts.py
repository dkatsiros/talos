#!/usr/bin/env python3
"""Install (or remove) the Talos expert roster into a project.

Additive + non-breaking: this only adds files. With experts installed, the
per-project CTO (Talos) can delegate slices of a task to focused Claude Code
sub-agents and is told to enforce the verify-by-running and adversarial-review
gates. With experts NOT installed (or removed), Talos behaves exactly as before.

What it installs into <project_root>:
  .claude/agents/*.md          the 9 expert sub-agent definitions
  docs/CONVENTIONS.md          shared conventions (only if absent; never clobbered)
  .openclaw/claude-loop/experts/ROUTER.md   router doc the wiring appends to the
                                            CTO system prompt (see experts_wiring.py)

Usage:
  python3 install_experts.py --project-root /path/to/project           # install
  python3 install_experts.py --project-root /path/to/project --status  # show state
  python3 install_experts.py --project-root /path/to/project --remove   # uninstall
  python3 install_experts.py --project-root /path/to/project --force    # overwrite agents
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parent
AGENTS_SRC = PACK_DIR / "agents"
CONVENTIONS_SRC = PACK_DIR / "CONVENTIONS.template.md"
ROUTER_SRC = PACK_DIR / "ROUTER.md"

EXPERT_NAMES = [
    "creative-3d-frontend",
    "design-ux",
    "backend-payments",
    "qa-verify",
    "security-reviewer",
    "devops-deploy",
    "growth-copy",
    "spec-keeper",
    "test-engineer",
]


def agents_dir(root: Path) -> Path:
    return root / ".claude" / "agents"


def conventions_path(root: Path) -> Path:
    return root / "docs" / "CONVENTIONS.md"


def router_dest(root: Path) -> Path:
    return root / ".openclaw" / "claude-loop" / "experts" / "ROUTER.md"


def install(root: Path, force: bool) -> int:
    dest = agents_dir(root)
    dest.mkdir(parents=True, exist_ok=True)
    installed = []
    for src in sorted(AGENTS_SRC.glob("*.md")):
        target = dest / src.name
        if target.exists() and not force:
            print(f"  skip (exists): .claude/agents/{src.name}  (use --force to overwrite)")
            continue
        shutil.copy2(src, target)
        installed.append(src.name)
        print(f"  installed: .claude/agents/{src.name}")

    # Router doc — always refresh (it's our managed wiring, not user content).
    rd = router_dest(root)
    rd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROUTER_SRC, rd)
    print(f"  installed: {rd.relative_to(root)}")

    # CONVENTIONS — never clobber a project's filled-in version.
    conv = conventions_path(root)
    if conv.exists():
        print(f"  skip (exists): docs/CONVENTIONS.md  (keeping your version)")
    else:
        conv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONVENTIONS_SRC, conv)
        print(f"  installed: docs/CONVENTIONS.md  (TEMPLATE — fill it in for this project)")

    print(
        "\nExperts installed. Next:\n"
        "  1. Fill in docs/CONVENTIONS.md for this project.\n"
        "  2. Enable router wiring so the CTO is told to delegate + enforce gates:\n"
        "     set experts.enabled=true (see experts_wiring.py / README), or pass\n"
        "     experts_enabled=True to ProjectLoop.run(...).\n"
        "  3. (Optional) wire per-expert MCPs — see experts/README.md."
    )
    return 0


def remove(root: Path) -> int:
    for name in EXPERT_NAMES:
        target = agents_dir(root) / f"{name}.md"
        if target.exists():
            target.unlink()
            print(f"  removed: .claude/agents/{name}.md")
    rd = router_dest(root)
    if rd.exists():
        rd.unlink()
        print(f"  removed: {rd.relative_to(root)}")
    print("Experts removed. docs/CONVENTIONS.md left in place (yours to keep/delete).")
    return 0


def status(root: Path) -> int:
    print(f"Project: {root}")
    present = [n for n in EXPERT_NAMES if (agents_dir(root) / f"{n}.md").exists()]
    print(f"  experts installed: {len(present)}/{len(EXPERT_NAMES)}")
    for n in EXPERT_NAMES:
        mark = "x" if n in present else " "
        print(f"    [{mark}] {n}")
    print(f"  router doc: {'present' if router_dest(root).exists() else 'absent'}")
    print(f"  CONVENTIONS: {'present' if conventions_path(root).exists() else 'absent'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the Talos expert roster into a project.")
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--force", action="store_true", help="Overwrite existing agent files.")
    ap.add_argument("--remove", action="store_true", help="Uninstall the experts.")
    ap.add_argument("--status", action="store_true", help="Show install state and exit.")
    args = ap.parse_args(argv)
    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    if args.status:
        return status(root)
    if args.remove:
        return remove(root)
    return install(root, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
