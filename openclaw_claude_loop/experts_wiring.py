"""Optional Talos expert-roster wiring.

Additive + non-breaking. When the experts pack is enabled for a project, this
module returns an extra block to append to the CTO's `--append-system-prompt`:
the router instructions (how to autonomously delegate to the expert sub-agents)
plus a pointer to the shared conventions doc. When disabled (the default), it
returns None and the CTO behaves exactly as before.

Enablement (any one of these turns it on):
  - config.json -> {"experts": {"enabled": true}}
  - env var      TALOS_EXPERTS=1
  - the project has .claude/agents/ populated AND a router doc present
    (i.e. install_experts.py ran) — soft auto-on so installing == enabling.
The order above is also the precedence: an explicit config flag wins over env,
which wins over auto-detection. Setting config experts.enabled=false hard-disables
even if agents are installed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROUTER_REL = Path(".openclaw") / "claude-loop" / "experts" / "ROUTER.md"
CONVENTIONS_REL = Path("docs") / "CONVENTIONS.md"
AGENTS_REL = Path(".claude") / "agents"


def _agents_installed(project_root: Path) -> bool:
    agents = project_root / AGENTS_REL
    return agents.is_dir() and any(agents.glob("*.md"))


def experts_enabled(project_root: Path, config: dict[str, Any] | None) -> bool:
    """Resolve whether the expert roster is active for this project."""
    cfg = (config or {}).get("experts") if isinstance(config, dict) else None
    if isinstance(cfg, dict) and "enabled" in cfg:
        return bool(cfg["enabled"])  # explicit config wins (true OR false)
    env = os.environ.get("TALOS_EXPERTS")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    # Soft auto-on: installed agents + router doc present == enabled.
    return _agents_installed(project_root) and (project_root / ROUTER_REL).is_file()


def build_experts_block(project_root: Path) -> str | None:
    """Return the router + conventions block for the CTO system prompt, or None.

    Caller is responsible for deciding enablement (see experts_enabled); this just
    assembles the text if the inputs exist.
    """
    parts: list[str] = []
    router = project_root / ROUTER_REL
    if router.is_file():
        try:
            parts.append(router.read_text(encoding="utf-8").rstrip())
        except OSError:
            pass

    available = []
    agents = project_root / AGENTS_REL
    if agents.is_dir():
        available = sorted(p.stem for p in agents.glob("*.md"))
    if available:
        parts.append(
            "## Experts available to you in THIS project\n"
            "Delegate via the Agent tool with subagent_type set to one of:\n- "
            + "\n- ".join(available)
        )

    conv = project_root / CONVENTIONS_REL
    if conv.is_file():
        parts.append(
            "## Shared conventions (you AND every expert obey this)\n"
            f"Read `{CONVENTIONS_REL.as_posix()}` before acting and keep all output "
            "consistent with it. Pass it to experts so their work stays coherent."
        )

    if not parts:
        return None
    return "\n\n---\n\n".join(parts) + "\n"
