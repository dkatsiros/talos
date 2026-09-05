from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__
from . import remote_exec
from . import resilience
from . import sandbox_exec
from .docsync.hook import maybe_run_docsync

LOOP_DIR = Path(".openclaw") / "claude-loop"
QUEUE_STATES = ("pending", "claimed", "blocked", "done", "failed")
TASK_STATES = (
    "pending",
    "claimed",
    "running",
    "completed",
    "failed",
    "blocked",
    "needs_approval",
    "cancelled",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    chars = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-") or "task"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def project_root_from(args: argparse.Namespace) -> Path:
    return Path(args.project_root or ".").expanduser().resolve()


def loop_root(project_root: Path) -> Path:
    return project_root / LOOP_DIR


def require_bootstrap(project_root: Path) -> Path:
    root = loop_root(project_root)
    if not (root / "config.json").exists():
        raise SystemExit(f"Not bootstrapped: {project_root}. Run bootstrap first.")
    return root


def default_config(project_root: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "module": "openclaw-claude-loop",
        "created_at": utc_now(),
        "project": {
            "name": project_root.name,
            "root": str(project_root),
        },
        "default_backend": "dry-run",
        "poll_interval_seconds": 5,
        "roles": {
            "cto": {
                "description": "Turns product intent into scoped implementation plans.",
                "instructions_file": "roles/cto.md",
            },
            "builder": {
                "description": "Implements scoped code changes and writes verification notes.",
                "instructions_file": "roles/builder.md",
            },
            "qa": {
                "description": "Reviews behavior, tests, and regression risk.",
                "instructions_file": "roles/qa.md",
            },
            "backend": {
                "description": "Backend-focused implementation worker.",
                "instructions_file": "roles/backend.md",
            },
            "frontend": {
                "description": "Frontend-focused implementation worker.",
                "instructions_file": "roles/frontend.md",
            },
            "content-distribution": {
                "description": "Creates authentic website/blog content and distribution drafts.",
                "instructions_file": "roles/content-distribution.md",
            },
        },
        "policy": {
            "network": "deny-by-default",
            "requires_approval_for": [
                "dependency_changes",
                "delete_files_outside_write_scope",
                "git_push",
                "schema_migrations",
                "external_posts",
            ],
        },
        # Auto-delivery reconciler (Phase 1). heal_enabled is the gate for the
        # highest-blast-radius action (auto-merging stranded branches): OFF by
        # default, flip to true (or env TALOS_RECONCILE_HEAL=1) only once a human
        # has approved automatic merge-heal for this project.
        "reconciler": {
            "base": "main",
            "heal_enabled": False,
        },
    }


CTO_ROLE_TEXT = """# You are the project CTO

You are the technical owner of this project. The orchestrator (the project PM)
sends you task envelopes through a file queue. Your job is to execute each task
end-to-end with senior-engineer judgment.

The project's standing context (its `CLAUDE.md`, `context.md`, or
`AGENTS.md`) is appended below this prompt — read it before acting so
you respect the project's stack, conventions, and current state.

## How to work on a task

1. Read the task envelope (path provided in the user prompt).
2. Assess scope: is the task well-defined? If a critical ambiguity blocks
   execution, write `clarification.json` in the task folder with specific
   questions and stop. For minor ambiguity, document your assumption in the
   result and proceed.
3. Plan briefly. You do not need to write a plan document for trivial
   tasks. For larger work, outline the steps in your head and proceed.
4. Execute end-to-end. That means:
   - Make the code changes
   - Add or update tests when behavior changes
   - Run the verification commands you'd expect a reviewer to run
   - Capture outputs in the result
5. Use the `Agent` tool to delegate parallelizable or research-heavy
   sub-work to subagents (e.g., "explore the codebase for X", "draft Y
   while I implement Z"). Do not delegate the core task itself.
6. Write `result.json` in the task folder when finished. Include:
   - `task_id`, `state` (completed | failed | needs_clarification)
   - `summary` (one-paragraph human-readable outcome)
   - `changes` (`files_created`, `files_modified`, `files_deleted`)
   - `verification` (`commands` you ran, `results` you observed)
   - `artifacts` (durable files/reports/plans produced, relative to the project or task folder)
   - `deployment_status` with `state` (deployed | not_deployed | not_applicable | blocked) and `details`
   - `next_actions` (what you'd recommend the orchestrator queue next, if anything)

If you produce only a textual "done" summary, the handoff is invalid. Leave
the orchestrator concrete artifact paths and a clear deployment status every time.

## Test-driven development + deploy gate (MANDATORY — every project, by default)

This project is test-driven. Silent regressions are the failure mode to prevent
("you did not even understand something broke" — Dimitris, 2026-09-03).

- **Every feature/behaviour you deliver ships WITH tests in the SAME task** —
  concrete assertions for its acceptance criteria (routes present, nav/layout
  structure, key flows work), not just a compile/type check.
- **Deploys are GATED on green.** The deploy path (deploy script / CI) MUST run
  the full suite first and REFUSE to deploy on red. Never push code that fails
  its own tests to a live environment.
- **When a test goes red, decide + act — never ignore:** either (a) a regression
  → fix the code, or (b) an intentional change made the assertion obsolete →
  update the test deliberately and note it in the result. Surfacing the break is
  the whole point.
- **Visual layer for UI:** include Playwright **screenshot capture** (phone +
  desktop) so visual regressions are catchable alongside behavioural assertions.
- If the project has **no suite yet**, scaffold a minimal one covering the
  feature you're touching AND wire the deploy gate — leave it better than found.

## Verify UI changes with Playwright (MANDATORY for any frontend change)

If your task touches the frontend/UI in any way, you MUST verify it with
Playwright before reporting done — type-check + build is NOT enough, it does
not catch layout/responsive/behaviour regressions.

- Drive the built preview (or the deployed URL) with Playwright at a PHONE
  viewport (~390x844) AND a desktop viewport.
- Run the checks on BOTH Chromium AND WebKit (Safari engine), since many bugs
  only reproduce on one engine — e.g. iOS Safari date inputs / `showPicker()`,
  flex/grid quirks, sticky/overflow behaviour. A Chromium-only pass is a FALSE
  GREEN (this rule exists because a date-picker bug shipped after passing on
  Chromium but failing on iOS Safari). If WebKit genuinely cannot be installed
  on the host (disk limits), say so explicitly in the report, prefer
  cross-browser/iOS-safe implementation patterns (don't rely on APIs that are
  unsupported or unreliable on Safari), and flag the item for device confirmation.
- Assert the actual change works AND there are no regressions: in particular
  NO horizontal page overflow at mobile width (document.scrollWidth <=
  viewport + small epsilon), interactive elements reachable/clickable, and the
  specific behaviour the task asked for.
- Capture before/after screenshots into your task `artifacts/` folder as
  evidence and reference them in result.json `verification` + `artifacts`.
- Reuse/extend the project's existing e2e setup (e.g. a project may have an
  `e2e/mobile-overflow-check.mjs`). If none exists, add a small focused check.
- A frontend task whose result.json has no Playwright evidence (on both
  engines, or an explicit note why WebKit was unavailable) is incomplete.

## Deploy what you build (so the operator can see it)

If your change is user-visible (a page, an endpoint, a UI flow), update the
project's RUNNING preview environment so the operator can see it live — do not
stop at "code is in the working tree".

1. Find the deployment method from the project's standing context
   (`CLAUDE.md` / `context.md`), `docker-compose*.yml`, Caddy/nginx config,
   or build/deploy scripts. The context usually states the preview URL and
   how it is served.
2. Apply it:
   - Docker stack: rebuild the affected service and `docker compose up -d <svc>`.
   - Static frontend: run the production build, then deploy the build output
     to the served directory (e.g. `dist/` → the path the web server serves).
     If that path needs elevated permission, use it for the copy only.
   - Long-running dev server: confirm it hot-reloads, or restart it.
3. After deploying, do a quick liveness check (curl the URL / hit the route)
   and record the exact command + result.
4. Set `deployment_status.state = "deployed"` and put the live URL in
   `deployment_status.details`. If you genuinely cannot deploy (no method,
   needs prod access, disk full), set `"blocked"` and say exactly what's needed.

This is local/preview deployment ONLY. Never deploy to production, push to a
git remote, or change DNS without explicit task approval.

## Backend specs (for any backend-dependent feature)

If a feature has — or will have — a backend dependency, you MUST create or
update a backend spec at `docs/backend-specs/<feature>.md` in the SAME change,
using the project's `docs/backend-specs/TEMPLATE.md` if present (if the folder
doesn't exist yet, create it with a short README + TEMPLATE). The spec must let
an LLM/engineer working ONLY on the backend implement the server side without
this conversation: product logic, what the frontend built on mock, the data
model, the exact endpoints (method/path/request/response/auth/validation), and
the mock→real swap. Keep the frontend's seam explicit (mock/encoded-link/flag/
`/resource/:id` route) so the swap is mechanical. Build frontend-first against
mocks; specify the backend, do not build it early. A backend-dependent feature
shipped without its spec is incomplete.

## Leave a report the operator can read

For EVERY task, write `report.md` in your task directory. This is the operator's
inspection surface — make it skimmable:
- What you changed and why (2-4 sentences).
- How to see it live: the exact preview URL and the route/page to open.
- How to verify: the commands you ran and their key results.
- Files touched (grouped: created / modified), and any new artifacts.
- Follow-ups or risks worth knowing.

List `report.md` first in `result.json` `artifacts`. A completed task with no
`report.md` is incomplete.

## Boundaries

- Stay inside the project root passed via `--add-dir`. Do not touch files
  outside it (the served preview directory is the one allowed exception, and
  only for deploying your build output).
- Honor any explicit `write_scope` in the task constraints.
- Do not push to git remotes, run schema migrations, install dependencies,
  or post externally without explicit task approval. Local/preview deploy
  (above) is allowed and expected.
- If a task is destructive (delete-files, drop-tables), pause and write
  `approval.json` with the requested action and rationale.

## Style

Senior engineer. Pragmatic, not theatrical. Three lines of explanation
beats three paragraphs. Prefer editing existing files over creating new
ones. Don't add comments unless they explain non-obvious *why*. No emojis
in code.
"""


BUILDER_ROLE_TEXT = """# You are a project Builder

You receive a scoped implementation task and execute it. Do exactly what
the task asks, no scope expansion. Run the smallest meaningful verification.
Write `result.json` with task_id, state, summary, changes, verification,
and next_actions.

Stay inside the project root. Do not push git, install deps, or post
externally without explicit approval.
"""


ADVISOR_ROLE_TEXT = """# You are the project's domain specialist & product advisor

You are consulted for your OPINION, not to write code. You bring deep,
grounded product judgment for this project's domain — what actually drives
adoption, retention, and revenue for products like this one.

The orchestrator consults you on topics like feature prioritization, UX,
onboarding, recommendation/ranking design, monetization, and competitive
positioning.

## How you work

1. Read the task envelope and the project's standing context.
2. Read whatever project files are relevant (code, docs) so your opinion is
   grounded in what actually exists — never opine in a vacuum.
3. Do LIVE WEB RESEARCH using the WebSearch / WebFetch tools (and any research
   skills available): how do comparable products actually solve this? What do
   real users praise or complain about? Cite what you find with URLs.
4. Take a POSITION. Give a clear recommendation, the reasoning, the tradeoffs,
   and what you'd do first. Name the risks. Disagree with the premise if it is
   wrong. Do NOT hedge into "it depends" mush — give a sharp opinion that can
   be acted on.
5. Write your opinion as a durable artifact at `docs/advisory/<topic-slug>.md`
   (create the folder if needed). Skimmable: TL;DR recommendation, reasoning,
   evidence + sources, tradeoffs, concrete next steps.

## Boundaries — you are ADVISORY ONLY

- DO NOT modify product code, configs, or build/deploy anything.
- The ONLY files you write are under `docs/advisory/` and your task directory.
- If your opinion implies a code change, describe the task to hand to the CTO;
  do not implement it yourself.

## Result contract

Write `result.json` with: `state=completed`; `summary` (your headline opinion
in 2-3 sentences); `changes` (the docs/advisory file created); `verification`
(the sources you consulted — list the URLs); `artifacts` (your
`docs/advisory/<topic>.md` FIRST, then `report.md`); `deployment_status.state`
= `not_applicable` with a one-line reason; `next_actions` (concrete follow-ups,
including any CTO build task your opinion implies).

Also write `report.md` in your task directory: the headline opinion, the
advisory doc path, the sources, and recommended next steps.

## Style

Opinionated, specific, evidence-backed. A sharp advisor, not a yes-man.
"""


ROLE_TEXT = {
    "cto": CTO_ROLE_TEXT,
    "builder": BUILDER_ROLE_TEXT,
    "advisor": ADVISOR_ROLE_TEXT,
    "qa": "# QA role\n\nReview the completed work for behavior regressions, missing tests, and unclear handoff. Write findings in result.json under `verification` and `next_actions`.\n",
    "backend": "# Backend role\n\nOwn backend code, data contracts, scripts, and service behavior.\n",
    "frontend": (
        "# Frontend role\n\n"
        "Own UI code, responsive behavior, accessibility, and browser-facing verification.\n\n"
        "For any task that changes visual design, layout, interaction polish, or responsive behavior, "
        "use the global Claude skill 'design-taste-frontend' from '~/.claude/skills' as design guidance. "
        "Still obey the project's existing design system, dependencies, and product constraints first.\n\n"
        "Before adding frontend dependencies, inspect package.json. Verify browser-facing changes with the "
        "smallest meaningful command available: typecheck, build, unit test, or screenshot/browser check.\n"
    ),
    "content-distribution": (
        "# Content/distribution role\n\n"
        "Produce practical, human-sounding drafts for the project's website/blog workflow. "
        "Do not autopost externally; write drafts and distribution notes as artifacts.\n"
    ),
}


def bootstrap(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = loop_root(project_root)
    for state in QUEUE_STATES:
        (root / "queue" / state).mkdir(parents=True, exist_ok=True)
    for dirname in ("tasks", "context", "locks", "tmp", "roles"):
        (root / dirname).mkdir(parents=True, exist_ok=True)

    config_path = root / "config.json"
    if config_path.exists() and not args.force:
        print(f"Already bootstrapped: {root}")
        return 0

    config = default_config(project_root)
    write_json(config_path, config)
    write_text(root / "context" / "project.md", f"# {project_root.name}\n\nProject-scoped context for Claude Loop workers.\n")
    write_text(
        root / "context" / "policies.md",
        "# Worker policies\n\n"
        "- Stay inside the task write scope.\n"
        "- Do not install dependencies, push git commits, run migrations, or post externally without approval.\n"
        "- Write status, logs, and results back under .openclaw/claude-loop/tasks/.\n",
    )
    write_text(
        root / "CLAUDE.md",
        "# Claude Loop Worker\n\n"
        "You are working from a file-driven task contract. Read the task folder, do only the scoped work, "
        "and write a concise result with verification.\n",
    )
    for role, text in ROLE_TEXT.items():
        write_text(root / "roles" / f"{role}.md", text)

    print(f"Bootstrapped Claude Loop at {root}")
    return 0


def make_task_id(title: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{slugify(title)[:60]}"


def enqueue(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    config = read_json(root / "config.json", {})
    task_id = args.id or make_task_id(args.title)
    task_dir = root / "tasks" / task_id
    if task_dir.exists():
        raise SystemExit(f"Task already exists: {task_id}")
    task_dir.mkdir(parents=True)

    instructions = args.instruction or []
    if args.instructions_file:
        instructions.append(Path(args.instructions_file).read_text(encoding="utf-8"))
    if not instructions:
        instructions = [args.title]

    task = {
        "id": task_id,
        "version": 1,
        "created_at": utc_now(),
        "created_by": getattr(args, "created_by", None) or "openclaw",
        "project": config.get("project", {"name": project_root.name, "root": str(project_root)}),
        "type": args.type,
        "role": args.role,
        "title": args.title,
        "goal": args.goal or args.title,
        "instructions": instructions,
        "context_files": [
            ".openclaw/claude-loop/context/project.md",
            ".openclaw/claude-loop/context/policies.md",
            f".openclaw/claude-loop/roles/{args.role}.md",
        ],
        "inputs": {"files_of_interest": args.file or []},
        "constraints": {
            "network": args.network,
            "write_scope": args.write_scope or [str(project_root)],
            "max_runtime_seconds": args.max_runtime_seconds,
            "requires_approval_for": config.get("policy", {}).get("requires_approval_for", []),
        },
        "expected_outputs": args.expected_output or ["summary", "verification"],
        "handoff": {"on_block": "write_blocker_and_stop", "on_complete": "write_result"},
    }
    status = {
        "task_id": task_id,
        "state": "pending",
        "phase": "queued",
        "created_at": task["created_at"],
        "updated_at": utc_now(),
        "attempt": 0,
        "heartbeat": {"message": "Queued", "progress": 0.0},
        "approval": {"required": False, "reason": None},
        "artifacts": {},
    }
    write_json(task_dir / "task.json", task)
    write_json(task_dir / "status.json", status)
    write_text(task_dir / "stdout.log", "")
    write_text(task_dir / "stderr.log", "")
    (task_dir / "artifacts").mkdir()
    queue_link = root / "queue" / "pending" / f"{task_id}.json"
    write_json(queue_link, {"task_id": task_id, "task_path": str(task_dir.relative_to(root))})
    print(task_id)
    return 0


@dataclass
class ClaimedTask:
    task_id: str
    task_dir: Path
    queue_file: Path


def claim_next(root: Path, task_id: str | None = None) -> ClaimedTask | None:
    """Claim a pending task and move its token pending/ -> claimed/.

    task_id=None  -> FIFO: claim the OLDEST pending token (legacy behaviour,
                     preserved for `run-worker` compatibility).
    task_id=<id>  -> claim THAT token specifically.

    Blind FIFO was the root cause of the 2026-07-18 silent black hole: a caller
    asked to prepare task X, FIFO promoted task Y (or nothing at all), and X
    rotted in pending/ — a directory the autorunner never reads — with zero log
    lines and attempt=0. Targeted claiming makes dispatch deterministic.
    """
    if task_id:
        queue_file = root / "queue" / "pending" / f"{task_id}.json"
        if not queue_file.exists():
            return None
    else:
        pending = sorted((root / "queue" / "pending").glob("*.json"))
        if not pending:
            return None
        queue_file = pending[0]
    payload = read_json(queue_file, {})
    resolved_id = payload.get("task_id") or queue_file.stem

    # SELF-HEAL the destination directory.
    #
    # queue/claimed/ was missing in ALL 16 projects (verified 2026-07-18). The
    # rename below then raised FileNotFoundError -- for the MISSING DESTINATION,
    # not a missing source -- and the bare handler swallowed it and returned
    # None. Result: prepare() reported "processed=0", promoted nothing, and
    # every dispatched task rotted in pending/ where the autorunner never looks.
    # The entire promote step was silently dead everywhere. Never again.
    claimed_dir = root / "queue" / "claimed"
    claimed_dir.mkdir(parents=True, exist_ok=True)
    claimed_file = claimed_dir / queue_file.name
    try:
        queue_file.rename(claimed_file)
    except FileNotFoundError:
        # Destination is guaranteed to exist now, so this can only mean the
        # SOURCE token vanished between the check and the rename -- a genuine
        # race with another worker. Nothing to claim; that is not an error.
        return None

    task_dir = root / "tasks" / resolved_id
    return ClaimedTask(task_id=resolved_id, task_dir=task_dir, queue_file=claimed_file)


def update_status(task_dir: Path, state: str, phase: str, message: str, progress: float | None = None) -> None:
    status = read_json(task_dir / "status.json", {})
    status.update(
        {
            "state": state,
            "phase": phase,
            "updated_at": utc_now(),
            "worker": {"host": socket.gethostname(), "pid": os.getpid(), "version": __version__},
        }
    )
    status["attempt"] = int(status.get("attempt", 0)) + (1 if state == "running" else 0)
    heartbeat = status.setdefault("heartbeat", {})
    heartbeat["message"] = message
    if progress is not None:
        heartbeat["progress"] = progress
    write_json(task_dir / "status.json", status)


def render_prompt(root: Path, task_dir: Path) -> str:
    task = read_json(task_dir / "task.json", {})
    parts = [
        "# Claude Loop Task",
        f"Task ID: {task.get('id')}",
        f"Role: {task.get('role')}",
        f"Title: {task.get('title')}",
        "",
        "## Goal",
        task.get("goal", ""),
        "",
        "## Instructions",
    ]
    parts.extend(f"- {item}" for item in task.get("instructions", []))
    parts.extend(["", "## Constraints", json.dumps(task.get("constraints", {}), indent=2), ""])
    parts.extend(
        [
            "## Expected Outputs",
            json.dumps(task.get("expected_outputs", []), indent=2),
            "",
            "## Required result.json contract",
            "When finished, write `result.json` in this task directory. A completed task must include:",
            "- `task_id` and `state`",
            "- `summary`: concrete outcome, not just \"done\"",
            "- `changes.files_created`, `changes.files_modified`, `changes.files_deleted`",
            "- `verification.commands` and `verification.results`",
            "- `artifacts`: durable artifact paths produced or updated",
            "- `deployment_status.state`: `deployed`, `not_deployed`, `not_applicable`, or `blocked`",
            "- `deployment_status.details`: where it is deployed, why it is not deployed, or why deployment does not apply",
            "- `next_actions`",
            "",
            "For CTO/planning work, put the plan/report path in `artifacts`. For implementation work, include changed files and whether the change was deployed.",
            "Write status/result files under this task directory when finished.",
        ]
    )
    return "\n".join(parts)


def complete_dry_run(root: Path, claimed: ClaimedTask) -> None:
    task = read_json(claimed.task_dir / "task.json", {})
    prompt = render_prompt(root, claimed.task_dir)
    write_text(claimed.task_dir / "prompt.md", prompt)
    update_status(claimed.task_dir, "running", "dry_run", "Creating deterministic dry-run result", 0.5)
    result = {
        "task_id": claimed.task_id,
        "state": "completed",
        "finished_at": utc_now(),
        "summary": f"Dry-run processed task: {task.get('title')}",
        "changes": {"files_modified": [], "files_created": ["prompt.md"], "files_deleted": []},
        "verification": {
            "commands": ["openclaw-claude-loop dry-run"],
            "results": [{"command": "openclaw-claude-loop dry-run", "exit_code": 0, "summary": "Task contract rendered successfully"}],
        },
        "git": {"branch": current_branch(root.parent), "commit_created": False},
        "artifacts": ["prompt.md", "stdout.log", "stderr.log"],
        "deployment_status": {
            "state": "not_applicable",
            "details": "Dry-run only renders the task contract; no project deployment is expected.",
        },
        "next_actions": ["Review prompt.md, then run with subscription-interactive or api-automation backend."],
    }
    write_json(claimed.task_dir / "result.json", result)
    update_status(claimed.task_dir, "completed", "done", "Dry-run completed", 1.0)
    move_queue(root, claimed, "done")


def prepare_subscription_interactive(root: Path, claimed: ClaimedTask) -> None:
    prompt = render_prompt(root, claimed.task_dir)
    write_text(claimed.task_dir / "prompt.md", prompt)
    task_id = claimed.task_id
    approval = {
        "task_id": task_id,
        "state": "ready_for_handoff",
        "created_at": utc_now(),
        "reason": (
            "Prompt rendered and parked. This task is ready to run automatically — "
            "no manual tmux/operator session is required."
        ),
        "how_to_run": [
            "Automatic (recommended): run-handoff spawns a non-interactive Claude "
            "session, streams progress to claude-session.log, and auto-closes the loop.",
            f"  python3 -m openclaw_claude_loop --project-root {root.parent.parent} run-handoff {task_id}",
            "  (or from Python: ProjectLoop(project_root).execute(task_id))",
            "Manual fallback (only if claude/screen are unavailable on this host): "
            "open a Claude Code session, paste prompt.md, and write result.json yourself.",
        ],
        "prompt_path": str(claimed.task_dir / "prompt.md"),
        "result_path": str(claimed.task_dir / "result.json"),
    }
    write_json(claimed.task_dir / "approval.json", approval)
    update_status(
        claimed.task_dir,
        "needs_approval",
        "operator_handoff",
        "Prompt rendered; ready for run-handoff (no manual tmux needed)",
        0.25,
    )
    move_queue(root, claimed, "blocked")


def move_queue(root: Path, claimed: ClaimedTask, state: str) -> None:
    target = root / "queue" / state / claimed.queue_file.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if claimed.queue_file.exists():
        claimed.queue_file.rename(target)


def find_queue_entry(root: Path, task_id: str) -> Path | None:
    for state in ("blocked", "claimed", "pending"):
        candidate = root / "queue" / state / f"{task_id}.json"
        if candidate.exists():
            return candidate
    return None


@contextlib.contextmanager
def project_lock(root: Path, name: str = "handoff", *, blocking: bool = False) -> Iterator[None]:
    """Per-project exclusive lock.

    blocking=False (default): fail fast with SystemExit if held — correct for
    run-handoff, where a second concurrent spawn attempt is an operator error.
    blocking=True: WAIT for the lock — correct for finalize/merge, where the
    caller (watchdog tick, human complete-handoff, run-handoff poller) must
    serialize behind whoever got there first, never die mid-finalize.
    """
    lock_path = root / "locks" / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        if blocking:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        else:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                fh.close()
                raise SystemExit(
                    f"Another {name} session is active on this project (lock: {lock_path}). "
                    "Wait for it to finish or remove the lock file if stale."
                ) from exc
        fh.write(f"{os.getpid()} {utc_now()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def load_role_prompt(root: Path, role: str) -> str | None:
    """Read .openclaw/claude-loop/roles/<role>.md if it exists."""
    role_path = root / "roles" / f"{role}.md"
    if role_path.exists():
        return role_path.read_text(encoding="utf-8")
    return None


# Files we treat as the project's standing context. First match wins.
# CLAUDE.md is Claude Code's native convention (auto-loaded by claude on
# startup) but including it here too means the CTO always sees it in the
# system prompt, regardless of where Claude Code's discovery looked.
PROJECT_CONTEXT_FILES = ("CLAUDE.md", "context.md", "AGENTS.md", "AGENT.md")


def discover_project_context(project_root: Path) -> tuple[str, str] | None:
    """Find the project's standing context file. Returns (filename, content) or None."""
    for name in PROJECT_CONTEXT_FILES:
        candidate = project_root / name
        if candidate.is_file():
            try:
                return name, candidate.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def build_role_system_prompt(root: Path, project_root: Path, role: str) -> str | None:
    """Build the --append-system-prompt content: role text + discovered project context.

    If the optional expert roster is enabled for this project AND the role is the
    orchestrator (`cto`), the router + conventions block is appended so the CTO can
    autonomously delegate slices to expert sub-agents and enforce the verify/review
    gates. Disabled-by-default and fully additive: when off, behavior is unchanged.
    """
    role_text = load_role_prompt(root, role)
    discovered = discover_project_context(project_root)
    parts: list[str] = []
    if role_text:
        parts.append(role_text.rstrip())
    if discovered:
        name, content = discovered
        parts.append(f"---\n\n## Project standing context (from `{name}`)\n\n{content.rstrip()}")

    experts_block = _maybe_experts_block(root, project_root, role)
    if experts_block:
        parts.append(experts_block.rstrip())

    if not parts:
        return None
    return "\n\n".join(parts) + "\n"


def _maybe_experts_block(root: Path, project_root: Path, role: str) -> str | None:
    """Return the experts router/conventions block for the CTO, or None.

    Isolated so a failure in the optional wiring can never break a handoff: any
    error here is swallowed and the loop proceeds with the legacy prompt.
    """
    if role != "cto":
        return None
    try:
        from .experts_wiring import experts_enabled, build_experts_block

        config = read_json(root / "config.json", {})
        if not experts_enabled(project_root, config):
            return None
        return build_experts_block(project_root)
    except Exception:
        return None


def screen_session_name(task_id: str) -> str:
    return f"claude-loop-{task_id[:40]}"


def screen_session_alive(screen_bin: str, session_name: str) -> bool:
    result = subprocess.run([screen_bin, "-ls"], capture_output=True, text=True, check=False)
    # Exact-name match (line-anchored), not substring: task ids truncated to 40
    # chars share prefixes, and a substring check would report a sibling task's
    # session as "alive" for this one. NOTE: the NAME FORMAT itself must stay
    # claude-loop-<id[:40]> — max-talos-reaper derives the same name
    # independently and reaps tasks whose session it cannot find.
    pattern = re.compile(r"^\s*\d+\." + re.escape(session_name) + r"\s", re.MULTILINE)
    return bool(pattern.search(result.stdout))


# ---------------------------------------------------------------------------
# Worktree-per-task helpers
# ---------------------------------------------------------------------------

def _is_own_git_repo(project_root: Path) -> bool:
    """True only when project_root is the top-level of its own git repository.

    Guards against workspace-nested projects (e.g. projects/morpheus inside
    the workspace repo): for those, git would report the workspace root as the
    top-level, and creating a worktree there would pollute the wrong repo.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if r.returncode != 0:
            return False
        return Path(r.stdout.strip()).resolve() == project_root.resolve()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ensure_worktrees_gitignored(project_root: Path) -> None:
    """Add '.worktrees/' to the project's .gitignore if absent. Best-effort."""
    try:
        gitignore = project_root / ".gitignore"
        entry = ".worktrees/"
        if gitignore.exists():
            lines = gitignore.read_text(encoding="utf-8").splitlines()
            if entry in lines or entry.rstrip("/") in lines:
                return
            trailer = "\n" if (lines and lines[-1]) else ""
            gitignore.write_text("\n".join(lines) + trailer + entry + "\n", encoding="utf-8")
        else:
            gitignore.write_text(entry + "\n", encoding="utf-8")
    except OSError:
        pass


def create_task_worktree(project_root: Path, task_id: str) -> "Path | None":
    """Create a git worktree at .worktrees/<task_id> for source-code isolation.

    Each Talos task gets its own working tree so concurrent tasks cannot
    clobber each other's uncommitted source changes. Returns the worktree path
    on success, or None on any failure (caller falls back to project_root).

    Only attempted when project_root is the top-level of its own git repo
    (not a workspace-nested project sharing a parent repo).

    The worktree is created on a new branch talos/<task_id>. If that branch
    already exists (previous failed attempt), the stale branch is RENAMED to
    talos/<task_id>-stale-<ts> — never silently reused (merge-back would merge
    the stale attempt's commits) and never deleted (it may hold unmerged work).
    There is deliberately NO --detach fallback: a detached worktree has no
    branch for merge-back, so a completed task's commits would be unreachable
    after cleanup. .worktrees/ is added to .gitignore so the directory stays
    out of git status.
    """
    if not _is_own_git_repo(project_root):
        return None
    _ensure_worktrees_gitignored(project_root)
    worktree_path = project_root / ".worktrees" / task_id
    # Remove stale worktree from a previous failed attempt.
    if worktree_path.exists():
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force",
             str(worktree_path)],
            capture_output=True, check=False, timeout=15,
        )
    branch_name = f"talos/{task_id}"
    try:
        r = subprocess.run(
            ["git", "-C", str(project_root), "worktree", "add",
             str(worktree_path), "-b", branch_name],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if r.returncode != 0:
            # Branch likely exists from a previous attempt: move it aside, retry.
            stale_name = f"{branch_name}-stale-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            subprocess.run(
                ["git", "-C", str(project_root), "branch", "-m",
                 branch_name, stale_name],
                capture_output=True, check=False, timeout=15,
            )
            r2 = subprocess.run(
                ["git", "-C", str(project_root), "worktree", "add",
                 str(worktree_path), "-b", branch_name],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if r2.returncode != 0:
                return None
        return worktree_path
    except (OSError, subprocess.TimeoutExpired):
        return None


def remove_task_worktree(project_root: Path, worktree_path: Path) -> None:
    """Remove a git worktree directory. Best-effort; never raises."""
    try:
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove",
             "--force", str(worktree_path)],
            capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# Merge outcomes that do NOT block dependent tasks (see autorunner dep gate).
MERGE_OK_STATES = {"merged", "empty", "no_worktree", "already_merged"}


def _git(project_root: Path, *argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project_root), *argv],
        capture_output=True, text=True, check=False, timeout=timeout,
    )


def merge_back_worktree(project_root: Path, task_id: str,
                        status: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Merge a completed task's talos/<task_id> branch back into its base branch.

    Returns an outcome dict {state, detail, ...}; states in MERGE_OK_STATES do
    not block dependents, every other state routes dependents to a human via
    the autorunner dep gate. This function NEVER raises and NEVER rolls back
    the completed task; a failed merge leaves the branch in place for a human.
    Caller must hold the blocking per-project "merge" lock.
    """
    branch = status.get("worktree_branch")
    if not branch and status.get("worktree_path"):
        # Legacy spawn (pre worktree_branch recording): branch name is derived.
        branch = f"talos/{task_id}"
    if not branch:
        return {"state": "no_worktree", "detail": "task ran directly in project_root"}

    exists = _git(project_root, "rev-parse", "--verify", "--quiet", branch)
    if exists.returncode != 0:
        # Branch already gone: merged+deleted by a previous finalize, or the
        # session never created commits and cleanup removed everything.
        return {"state": "already_merged", "branch": branch,
                "detail": "branch does not exist (already merged or never committed)"}

    base = status.get("worktree_base") or current_branch(project_root)
    head = current_branch(project_root)
    if not base or not head:
        return {"state": "skipped_detached", "branch": branch,
                "detail": f"base={base!r} head={head!r}: refusing to merge onto "
                          f"detached/unknown HEAD; branch left for human"}
    if head != base:
        return {"state": "skipped_base_moved", "branch": branch, "base": base,
                "detail": f"project checkout is on {head!r}, expected base {base!r}; "
                          f"branch left for human"}

    # Dirty check: TRACKED changes only (-uno) — untracked task artifacts are
    # routine. The engine's own .gitignore edit (_ensure_worktrees_gitignored)
    # is excluded so it can never wedge merge-back forever.
    porcelain = _git(project_root, "status", "--porcelain", "-uno")
    dirty = [ln for ln in porcelain.stdout.splitlines()
             if ln.strip() and ln[3:].strip() != ".gitignore"]
    if dirty:
        return {"state": "skipped_dirty", "branch": branch, "base": base,
                "detail": f"{len(dirty)} tracked change(s) in project checkout; "
                          f"branch left for human"}

    count_r = _git(project_root, "rev-list", "--count", f"{base}..{branch}")
    try:
        ahead = int(count_r.stdout.strip())
    except ValueError:
        ahead = -1
    if ahead == 0:
        # 0 commits ahead ⟺ the branch tip is already reachable from base, so
        # deleting the branch can never lose commits. The ONLY situation where
        # real work is at risk is uncommitted files still sitting in a dirty
        # worktree (cleanup preserves dirty worktrees exactly for this) — then
        # keep everything and block dependents. Otherwise delete: the work is
        # either in base already (e.g. landed out-of-band by another session)
        # or was never produced; a kept branch would protect nothing.
        changes = result.get("changes") or {}
        claimed = (as_list(changes.get("files_created"))
                   + as_list(changes.get("files_modified"))
                   + as_list(changes.get("files_deleted")))
        wt_str = status.get("worktree_path")
        if claimed and wt_str and Path(wt_str).exists():
            wt_dirty = subprocess.run(
                ["git", "-C", wt_str, "status", "--porcelain"],
                capture_output=True, text=True, check=False, timeout=30)
            if wt_dirty.returncode == 0 and wt_dirty.stdout.strip():
                return {"state": "empty_branch_with_changes", "branch": branch,
                        "base": base,
                        "detail": f"result.json claims {len(claimed)} changed "
                                  f"files, branch has 0 commits, and the DIRTY "
                                  f"worktree at {wt_str} still holds uncommitted "
                                  f"work — salvage it before deleting anything"}
        _git(project_root, "branch", "-D", branch)
        detail = "0 commits ahead of base; branch deleted"
        if claimed:
            detail += (f" ({len(claimed)} claimed file changes are already "
                       f"contained in {base} or were landed out-of-band)")
        return {"state": "empty", "branch": branch, "base": base, "detail": detail}
    if ahead < 0:
        return {"state": "failed", "branch": branch, "base": base,
                "detail": f"rev-list failed: {count_r.stderr.strip()[:200]}"}

    summary_lines = str(result.get("summary") or "").strip().splitlines()
    summary = summary_lines[0][:80] if summary_lines else ""
    merge_msg = f"Merge {branch}" + (f": {summary}" if summary else "")
    merged = _git(project_root, "merge", "--no-ff", "-m", merge_msg, branch,
                  timeout=120)
    if merged.returncode != 0:
        # Distinguish a real conflict (MERGE_HEAD exists -> abort restores) from
        # a refused merge (e.g. untracked files in the way -> nothing to abort).
        git_dir_r = _git(project_root, "rev-parse", "--git-dir")
        git_dir = Path(git_dir_r.stdout.strip() or ".git")
        if not git_dir.is_absolute():
            git_dir = project_root / git_dir
        if (git_dir / "MERGE_HEAD").exists():
            _git(project_root, "merge", "--abort")
            state = "conflict"
        else:
            state = "refused"
        return {"state": state, "branch": branch, "base": base,
                "detail": (merged.stdout + merged.stderr).strip()[:400]}

    _git(project_root, "branch", "-d", branch)
    return {"state": "merged", "branch": branch, "base": base, "commits": ahead}


def record_merge_proof(project_root: Path, outcome: dict[str, Any] | None,
                       base: str | None = None) -> dict[str, Any] | None:
    """Phase 1 (A.1): stamp a durable merge-proof onto a finalize outcome.

    Every finalized task must record the ``tip_sha`` its work landed at so the
    reconciler can prove ``DELIVERED`` via ``git merge-base --is-ancestor`` even
    after the talos/<id> branch is deleted. This is the one wiring the Phase 0
    module deferred to a Dimitris-gated step (see reconciler.compute_merge_proof).

    Resolution: prefer the branch tip while it still exists (exact proof); once
    merge-back has deleted the branch (states merged/empty/already_merged) fall
    back to the base tip — which now contains the work, so is-ancestor still
    passes. Best-effort and NEVER raises: a proof we couldn't compute must not
    roll back a completed task. Mutates and returns ``outcome`` in place.
    """
    if not isinstance(outcome, dict):
        return outcome
    b = outcome.get("base") or base
    branch = outcome.get("branch")
    try:
        tip = ""
        if branch:
            r = _git(project_root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}")
            tip = r.stdout.strip()
        base_sha = ""
        if b:
            rb = _git(project_root, "rev-parse", "--verify", "--quiet", f"{b}^{{commit}}")
            base_sha = rb.stdout.strip()
        if not tip:
            # Branch gone (merged/empty/already_merged) or task ran directly in
            # project_root (no_worktree): the base tip is the best-available proof.
            tip = base_sha
        if tip:
            outcome.setdefault("tip_sha", tip)
        if base_sha:
            outcome.setdefault("base_sha", base_sha)
        if b:
            outcome.setdefault("base", b)
    except Exception:  # noqa: BLE001 — a proof is advisory; never fail finalize
        pass
    return outcome


def reconcile_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """The ``reconciler`` sub-config, or an empty dict."""
    cfg = (config or {}).get("reconciler") if isinstance(config, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def reconcile_heal_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the merge-heal gate. OFF unless explicitly enabled.

    Precedence: config ``reconciler.heal_enabled`` (explicit true/false wins) >
    env ``TALOS_RECONCILE_HEAL`` > default False. Healing auto-merges stranded
    branches — the highest-blast-radius action — so the default is deny.
    """
    cfg = reconcile_config(config)
    if "heal_enabled" in cfg:
        return bool(cfg["heal_enabled"])
    env = os.environ.get("TALOS_RECONCILE_HEAL")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    return False


def build_handoff_prompt(
    project_root: Path,
    task_dir: Path,
    *,
    execution_root: "Path | None" = None,
) -> str:
    """Build the claude -p prompt for a task handoff.

    execution_root: the worktree (or project_root when no worktree is used).
    Task output files (result.json etc.) always live in task_dir under the
    original project_root; the model writes them via absolute paths.
    """
    exec_root = execution_root or project_root
    prompt_path = task_dir / "prompt.md"
    result_path = task_dir / "result.json"
    if exec_root != project_root:
        boundary = (
            f"Do not modify source files outside {exec_root} "
            f"(your git worktree for this task). "
            f"Task output files at {task_dir} are the one allowed exception — "
            f"write them via the absolute paths above.\n"
            f"MANDATORY: commit your work inside the worktree BEFORE writing "
            f"{result_path}: run `git add -A && git commit -m \"<concise message>\"` "
            f"in {exec_root}. Uncommitted changes cannot be merged back to the "
            f"main branch and WILL BE LOST when the worktree is cleaned up."
        )
    else:
        boundary = f"Do not touch files outside {project_root}."
    return (
        f"Read {prompt_path}.\n"
        f"Execute that task in {exec_root}.\n"
        f"When finished, write {result_path} with task_id, state, summary, "
        f"changes.files_created, changes.files_modified, changes.files_deleted, "
        f"verification.commands, verification.results, artifacts, deployment_status, "
        f"and next_actions.\n"
        f"For completed CTO/planning work, artifacts must include the durable plan/report path. "
        f"For implementation work, deployment_status must say where it is deployed or why it is not deployed.\n"
        f"{boundary}"
    )


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def validate_completed_result_contract(result: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Return human-readable contract errors for completed handoffs.

    The Claude Loop is useful only if the orchestrator can inspect durable output
    after a task. A bare summary like "done" is not enough for CTO work.
    """
    errors: list[str] = []

    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")

    changes = result.get("changes")
    if not isinstance(changes, dict):
        errors.append("changes must be an object")
        changes = {}
    for key in ("files_created", "files_modified", "files_deleted"):
        if key not in changes or not isinstance(changes.get(key), list):
            errors.append(f"changes.{key} must be a list")

    verification = result.get("verification")
    if not isinstance(verification, dict):
        errors.append("verification must be an object")
        verification = {}
    for key in ("commands", "results"):
        if key not in verification or not isinstance(verification.get(key), list):
            errors.append(f"verification.{key} must be a list")

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list of durable output paths")
        artifacts = []

    deployment_status = result.get("deployment_status")
    if not isinstance(deployment_status, dict):
        errors.append("deployment_status must be an object")
        deployment_status = {}
    deployment_state = deployment_status.get("state")
    valid_deployment_states = {"deployed", "not_deployed", "not_applicable", "blocked"}
    if deployment_state not in valid_deployment_states:
        errors.append(
            "deployment_status.state must be one of: "
            + ", ".join(sorted(valid_deployment_states))
        )
    details = deployment_status.get("details")
    if not isinstance(details, str) or not details.strip():
        errors.append("deployment_status.details must be a non-empty string")

    role = task.get("role")
    changed_files = (
        as_list(changes.get("files_created"))
        + as_list(changes.get("files_modified"))
        + as_list(changes.get("files_deleted"))
    )
    if role == "cto" and not artifacts and not changed_files:
        errors.append("cto completed results must include at least one artifact or changed file")

    return errors


# ---------------------------------------------------------------------------
# Handoff failure visibility (dead-man signal)
# ---------------------------------------------------------------------------
#
# Every failure path in run_handoff used to end at a bare `raise SystemExit`.
# That is loud for whoever is watching the terminal and INVISIBLE to everyone
# else: status.json still said "running", the queue token still sat in blocked/,
# and no durable record existed anywhere. A detached run that timed out or died
# looked exactly like one still working.
#
# These helpers write the same failure to four places, so no single channel
# being missed can hide it:
#   1. task_dir/handoff-alert.json   — dead-man file, easy to glob for
#   2. status.json["handoff_alert"]  — visible to `status`, watchers, ProjectLoop
#   3. the queue token payload       — visible to anyone scanning the queue
#   4. logs/handoff-events.jsonl     — durable, append-only history
# plus a stderr line for the operator in the loop right now.
#
# DELIBERATE NON-CHANGE: none of this touches status.json's `state`.
# max-talos-reaper only reaps tasks whose state is running/claimed (it writes
# the synthetic result.json and moves the token to queue/failed/). Flipping the
# state to "failed" here would hide a genuinely dead task from the one component
# that closes it out, stranding its token forever — trading a visible stall for
# an invisible one. The alert carries `suggested_state` instead, as advice.

HANDOFF_ALERT_FILE = "handoff-alert.json"
HANDOFF_EVENTS_LOG = "handoff-events.jsonl"

# Attempts for spawning a session (local screen or remote ssh+screen) before we
# give up on that transport.
SPAWN_ATTEMPTS = 3

# Consecutive "screen session not found" observations required before we believe
# the session is really gone. `screen -ls` can miss transiently (fork pressure,
# a half-written socket dir), and a single false negative used to abort a
# perfectly healthy run with "exited without writing result.json".
SESSION_DEATH_CONFIRMATIONS = 3

# Consecutive rsync-pull failures tolerated while polling a remote run before we
# raise an alert. Polling continues either way — the remote screen is still
# running; we just stop pretending the sync is fine.
REMOTE_SYNC_ALERT_AFTER = 3


def log_handoff_event(root: Path, task_id: str, event: str, **fields: Any) -> None:
    """Append one line to logs/handoff-events.jsonl. Best-effort, never raises."""
    record = {"ts": utc_now(), "task_id": task_id, "event": event}
    record.update(fields)
    resilience.append_jsonl(root / "logs" / HANDOFF_EVENTS_LOG, record)


def annotate_queue_entry(root: Path, task_id: str, alert: dict[str, Any] | None) -> str | None:
    """Attach (or clear) the alert on the task's queue token, wherever it sits.

    Best-effort: a queue token that has already moved on is not an error.
    """
    entry = find_queue_entry(root, task_id)
    if entry is None:
        return None
    try:
        payload = read_json(entry, {}) or {}
        if not isinstance(payload, dict):
            return None
        if alert is None:
            if "handoff_alert" not in payload:
                return None
            payload.pop("handoff_alert", None)
        else:
            payload["handoff_alert"] = alert
        write_json(entry, payload)
    except (OSError, json.JSONDecodeError):
        return None
    return str(entry)


def record_handoff_alert(
    root: Path,
    task_dir: Path,
    task_id: str,
    *,
    outcome: str,
    reason: str,
    session_name: str | None = None,
    session_log: Path | None = None,
    suggested_state: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Make a stalled/failed/degraded handoff visible. Returns the alert dict."""
    alert: dict[str, Any] = {
        "outcome": outcome,
        "reason": reason,
        "at": utc_now(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    }
    if session_name:
        alert["session_name"] = session_name
    if session_log is not None:
        alert["session_log"] = str(session_log)
    if suggested_state:
        alert["suggested_state"] = suggested_state
    alert.update(extra)

    try:
        write_json(task_dir / HANDOFF_ALERT_FILE, alert)
    except OSError as exc:
        print(f"WARNING: could not write handoff alert file: {exc}", file=sys.stderr)

    try:
        status = read_json(task_dir / "status.json", {}) or {}
        if isinstance(status, dict):
            status["handoff_alert"] = alert
            status["updated_at"] = utc_now()
            heartbeat = status.setdefault("heartbeat", {})
            heartbeat["message"] = f"handoff {outcome}: {reason}"
            write_json(task_dir / "status.json", status)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: could not update status.json with handoff alert: {exc}", file=sys.stderr)

    queue_entry = annotate_queue_entry(root, task_id, alert)
    log_handoff_event(
        root, task_id, f"handoff_{outcome}",
        reason=reason,
        session_name=session_name,
        queue_entry=queue_entry,
        suggested_state=suggested_state,
    )
    print(f"HANDOFF ALERT [{outcome}] {task_id}: {reason}", file=sys.stderr)
    print(f"  recorded in {task_dir / HANDOFF_ALERT_FILE}", file=sys.stderr)
    return alert


def clear_handoff_alert(root: Path, task_dir: Path, task_id: str) -> None:
    """Drop a resolved/superseded alert. Best-effort, never raises.

    An alert that outlives the problem is worse than no alert: the next operator
    learns to ignore the channel.
    """
    try:
        (task_dir / HANDOFF_ALERT_FILE).unlink()
    except OSError:
        pass
    try:
        status = read_json(task_dir / "status.json", {}) or {}
        if isinstance(status, dict) and status.pop("handoff_alert", None) is not None:
            write_json(task_dir / "status.json", status)
    except (OSError, json.JSONDecodeError):
        pass
    # Sweep every queue state, not just the live ones: an annotated token may
    # already have been moved on to done/ or failed/ by a finalize.
    for state in QUEUE_STATES:
        token = root / "queue" / state / f"{task_id}.json"
        if not token.exists():
            continue
        try:
            payload = read_json(token, {}) or {}
            if isinstance(payload, dict) and payload.pop("handoff_alert", None) is not None:
                write_json(token, payload)
        except (OSError, json.JSONDecodeError):
            continue


def collect_handoff_alerts(root: Path) -> list[dict[str, Any]]:
    """Every outstanding handoff alert in this project, for `status`."""
    alerts: list[dict[str, Any]] = []
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        return alerts
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        alert = read_json(task_dir / HANDOFF_ALERT_FILE, None)
        if isinstance(alert, dict):
            alerts.append({"task_id": task_dir.name, **alert})
    return alerts


def _spawn_with_retry(
    describe_what: str,
    spawn: Callable[[], Any],
    *,
    root: Path,
    task_id: str,
    already_live: Callable[[], bool] | None = None,
    attempts: int = SPAWN_ATTEMPTS,
) -> Any:
    """Run a session-spawn step with bounded backoff, logging each retry.

    Re-raises the final failure — the caller decides whether that means "fall
    back to another transport" or "alert and abort".
    """
    state = {"tries": 0}

    def _attempt() -> Any:
        # Double-spawn guard: a spawn can fail AFTER screen actually started
        # (an ssh connection dropped, a non-zero exit from the wrapper). Firing
        # a second Claude at the same task would put two agents in a race for
        # one result.json — strictly worse than the failure we are retrying.
        if state["tries"] and already_live is not None and already_live():
            log_handoff_event(
                root, task_id, "spawn_adopted_existing", transport=describe_what,
            )
            return None
        state["tries"] += 1
        return spawn()

    def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        detail = resilience.describe(exc)
        print(
            f"{describe_what} attempt {attempt} failed ({detail}); "
            f"retrying in {delay:.1f}s",
            file=sys.stderr,
        )
        log_handoff_event(
            root, task_id, "spawn_retry",
            transport=describe_what, attempt=attempt, error=detail, delay_s=delay,
        )

    return resilience.retry_call(_attempt, attempts=attempts, on_retry=_on_retry)


def _session_alive(
    *,
    remote: bool,
    screen_bin: str,
    session_name: str,
    ssh_target: str,
    sandboxed: bool = False,
    sandbox_cfg: "dict[str, Any] | None" = None,
    task_id: str = "",
) -> bool | None:
    """Liveness probe that distinguishes 'dead' from 'could not tell'.

    Returns True/False, or None when the probe itself failed. None must never be
    treated as death: `screen -ls`/ssh/`talos-sandbox status` failing is a
    statement about the probe, not about the session.
    """
    try:
        if sandboxed and sandbox_cfg is not None:
            # Liveness = the task's container is still running on the Shuttle.
            return sandbox_exec.is_alive(sandbox_cfg, task_id)
        if remote:
            return remote_exec.remote_screen_alive(ssh_target, session_name)
        return screen_session_alive(screen_bin, session_name)
    except (OSError, subprocess.SubprocessError):
        return None


DEFAULT_PERMISSION_MODE_BY_ROLE = {
    # All automated Talos roles use bypassPermissions.
    # acceptEdits requires a human to approve every Bash/git call — impossible
    # in a detached non-interactive screen session, so it silently blocks all
    # tool use. bypassPermissions is the correct mode for any unattended build.
    # Reserve acceptEdits (or stricter) ONLY for future read-only/research roles
    # that must never write files or execute code.
    "cto": "bypassPermissions",
    "advisor": "bypassPermissions",
    "builder": "bypassPermissions",
    "qa": "bypassPermissions",
    "backend": "bypassPermissions",
    "frontend": "bypassPermissions",
    "content-distribution": "bypassPermissions",
}


def run_handoff(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    task_id = args.task_id
    task_dir = root / "tasks" / task_id
    if not task_dir.is_dir():
        raise SystemExit(f"Unknown task: {task_id}")

    # Readiness gate: derive from DURABLE artifacts, not only mutable status.json.
    # (2026-07-28 Ergon T5/T6 post-mortem: a task whose status drifted out of
    # needs_approval — e.g. finished-but-never-finalized, or a prepare/handoff
    # version skew — hit an absolute refusal here with no recovery path.)
    status = read_json(task_dir / "status.json", {})
    state = status.get("state")
    if (task_dir / "result.json").exists():
        print(f"result.json already present for {task_id}; "
              f"finalizing (complete-handoff) instead of spawning a new session.")
        return complete_handoff(args)
    if state != "needs_approval":
        approval = read_json(task_dir / "approval.json", {})
        token_in_blocked = (root / "queue" / "blocked" / f"{task_id}.json").exists()
        # Self-heal ONLY pre-run skew states. Never running (session may be
        # live — incl. on a remote host the local screen check can't see),
        # never cancelled/terminal states.
        healable = state in ("pending", "claimed")
        if approval.get("state") == "ready_for_handoff" and token_in_blocked and healable:
            update_status(
                task_dir, "needs_approval", "operator_handoff",
                f"self-heal: prepared artifacts present but status was {state!r}",
            )
            print(f"Self-healed status {state!r} -> needs_approval "
                  f"(approval.json=ready_for_handoff, token in blocked/).")
        else:
            raise SystemExit(
                f"Task {task_id} state is {state!r} (expected needs_approval). "
                "If the session finished, result.json would trigger auto-finalize; "
                "if it died mid-run with no result, use max-talos-reaper. "
                "Otherwise run subscription-interactive first."
            )

    prompt_path = task_dir / "prompt.md"
    if not prompt_path.exists():
        raise SystemExit(
            f"No prompt.md at {prompt_path}. Run subscription-interactive first to render the prompt."
        )

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SystemExit("`claude` CLI not found on PATH. Install Claude Code first.")
    screen_bin = shutil.which("screen")
    if not screen_bin:
        raise SystemExit("`screen` not found on PATH.")

    with project_lock(root, "handoff"):
        task = read_json(task_dir / "task.json", {})
        role = task.get("role", "builder")
        role_prompt = build_role_system_prompt(root, project_root, role)

        permission_mode = args.permission_mode or DEFAULT_PERMISSION_MODE_BY_ROLE.get(role, "acceptEdits")

        handoff_prompt = build_handoff_prompt(project_root, task_dir)
        handoff_prompt_path = task_dir / "handoff-prompt.txt"
        write_text(handoff_prompt_path, handoff_prompt)

        session_log = task_dir / "claude-session.log"
        session_name = screen_session_name(task_id)

        if screen_session_alive(screen_bin, session_name):
            raise SystemExit(
                f"Screen session {session_name} already running. Attach with: screen -r {session_name}"
            )

        # A fresh attempt supersedes whatever the previous one alerted about.
        clear_handoff_alert(root, task_dir, task_id)

        # execution_host targeting. Precedence: task.execution_host >
        # config.json default_execution_host > engine default ("shuttle", env
        # TALOS_DEFAULT_EXECUTION_HOST). AWS-local is an explicit opt-in since
        # 2026-09-05 (fleet plan 1.2): the 3.7GB head must not build. The
        # resolved value is persisted into task.json so the autorunner, the
        # sync/reaper watchers and complete-handoff all see the same host.
        project_cfg = read_json(root / "config.json", {}) or {}
        execution_host = remote_exec.resolve_execution_host(task, project_cfg)
        if task.get("execution_host") != execution_host:
            task["execution_host"] = execution_host
            write_json(task_dir / "task.json", task)
        remote = False
        remote_project_root = ""
        sandboxed = False
        sandbox_cfg: "dict[str, Any] | None" = None
        # `project` in task.json is a dict ({"name","root"}); the sandbox CLI
        # wants the bare name string (it goes through shlex.quote). Normalise so
        # a dict here can't blow up spawn_sandbox with a TypeError (which used to
        # silently fall the task back to a LOCAL claude-p on the fragile head).
        _proj = task.get("project")
        sandbox_project = (
            _proj.get("name") if isinstance(_proj, dict) else _proj
        ) or project_root.name
        ssh_target = ""
        mirror_root = ""
        if execution_host == "shuttle-sandbox":
            # Containerised Talos Sandbox (Milestone 2). Opt-in; if the Shuttle
            # is reachable, runs the agent in an isolated container there.
            # SAFETY (gap #5 / preflight arm): a preflight failure (Shuttle
            # offline or kill switch) must NOT self-heal to a local claude-p
            # screen on the 3.7GB AWS head — same OOM policy as the spawn-
            # exception handler below. Fail loud: dead-man alert + SystemExit,
            # leaving the task re-runnable at needs_approval.
            sandbox_cfg = sandbox_exec.sandbox_config(task)
            ok, latency_ms, reason = sandbox_exec.preflight(sandbox_cfg)
            if ok:
                sandboxed = True
            else:
                # Mirror the spawn-exception fail-loud policy exactly.
                # A Shuttle that is offline or kill-switched is the same
                # situation as a dispatch that blew up one step later — the task
                # must NEVER fall back to a local claude-p on this host.
                remote_exec.log_fallback(
                    root / "logs", task_id,
                    f"sandbox preflight failed: {reason} "
                    f"(NO local fallback — AWS-head OOM guard)",
                    latency_ms, utc_now(),
                )
                record_handoff_alert(
                    root, task_dir, task_id,
                    outcome="sandbox_preflight_failed",
                    reason=(
                        f"shuttle-sandbox preflight failed ({reason} after "
                        f"{latency_ms}ms). Refused to fall back to a local "
                        f"claude-p on the AWS head (OOM risk); task left "
                        f"re-runnable at needs_approval."
                    ),
                    suggested_state="needs_approval",
                    ssh_target=sandbox_cfg.get("ssh_target"),
                    transport="shuttle-sandbox",
                    recovery=(
                        "task left in needs_approval; check the Shuttle "
                        "(ssh/ping dimitris@100.98.174.24), remove the kill "
                        "switch if present (~/.openclaw/talos-sandbox/DISABLED),"
                        " and re-run run-handoff once the Shuttle is reachable"
                    ),
                )
                print(
                    f"Sandbox preflight failed ({reason} after {latency_ms}ms). "
                    f"NOT falling back to local (AWS-head OOM guard); "
                    f"task {task_id} left parked at needs_approval — re-run "
                    f"run-handoff once the Shuttle is reachable.",
                    file=sys.stderr,
                )
                raise SystemExit(
                    f"shuttle-sandbox preflight failed for {task_id} and local "
                    f"fallback is disabled by design (AWS-head OOM guard). "
                    f"Task is re-runnable; check the Shuttle and re-run "
                    f"run-handoff."
                )
        elif execution_host == "shuttle":
            cfg = remote_exec.shuttle_config(task)
            ssh_target = cfg["ssh_target"]
            mirror_root = cfg["mirror_root"]
            # A Shuttle run needs the repo mirror path. Without it the builder
            # would only see its own brief and block immediately (2026-07-18).
            # FAIL LOUD — never run on the AWS head instead.
            remote_project_root = remote_exec.shuttle_project_root(task, project_cfg) or ""
            if not remote_project_root:
                reason = (
                    "execution_host=shuttle but no Shuttle repo mapping: set "
                    "execution_hosts.shuttle.project_root in .openclaw/claude-loop/"
                    "config.json (or task.shuttle.project_root), or set "
                    "execution_host=local explicitly in task.json"
                )
                remote_exec.log_fallback(
                    root / "logs", task_id,
                    f"{reason} (NO local fallback — AWS-head OOM guard)", 0, utc_now(),
                )
                record_handoff_alert(
                    root, task_dir, task_id,
                    outcome="shuttle_mapping_missing",
                    reason=reason,
                    suggested_state="needs_approval",
                    ssh_target=ssh_target,
                    transport="shuttle",
                    recovery=(
                        "clone the repo on the Shuttle, add execution_hosts.shuttle."
                        "project_root to the project's config.json, re-run run-handoff; "
                        "or set execution_host=local in task.json for an explicit "
                        "AWS-local run"
                    ),
                )
                print(f"Shuttle mapping missing for {task_id}: {reason}", file=sys.stderr)
                raise SystemExit(
                    f"execution_host=shuttle for {task_id} but no Shuttle repo "
                    f"mapping; local fallback is disabled by design (AWS-head OOM "
                    f"guard). Add execution_hosts.shuttle.project_root to config.json "
                    f"and re-run run-handoff."
                )
            _sh = task.get("shuttle") if isinstance(task.get("shuttle"), dict) else {}
            if _sh.get("project_root") != remote_project_root:
                task["shuttle"] = {**_sh, "project_root": remote_project_root}
                write_json(task_dir / "task.json", task)
            ok, latency_ms, reason = remote_exec.preflight(
                ssh_target, cfg["preflight_timeout_s"]
            )
            if ok:
                remote = True
            else:
                # Shuttle offline / unreachable -> FAIL LOUD. Same policy as the
                # sandbox arm: a task must NEVER self-heal into a local claude-p
                # on the 3.7GB AWS head. Dead-man alert + SystemExit, task left
                # re-runnable at needs_approval.
                remote_exec.log_fallback(
                    root / "logs", task_id,
                    f"shuttle preflight failed: {reason} "
                    f"(NO local fallback — AWS-head OOM guard)",
                    latency_ms, utc_now(),
                )
                record_handoff_alert(
                    root, task_dir, task_id,
                    outcome="shuttle_preflight_failed",
                    reason=(
                        f"Shuttle preflight failed ({reason} after {latency_ms}ms). "
                        f"Refused to fall back to a local claude-p on the AWS head "
                        f"(OOM risk); task left re-runnable at needs_approval."
                    ),
                    suggested_state="needs_approval",
                    ssh_target=ssh_target,
                    transport="shuttle",
                    recovery=(
                        "check the Shuttle (ssh/ping dimitris@100.98.174.24) and "
                        "re-run run-handoff once it is reachable"
                    ),
                )
                print(
                    f"Shuttle preflight failed ({reason} after {latency_ms}ms). "
                    f"NOT falling back to local (AWS-head OOM guard); task {task_id} "
                    f"left parked at needs_approval — re-run run-handoff once the "
                    f"Shuttle is reachable.",
                    file=sys.stderr,
                )
                raise SystemExit(
                    f"shuttle preflight failed for {task_id} and local fallback is "
                    f"disabled by design (AWS-head OOM guard). Task is re-runnable; "
                    f"check the Shuttle and re-run run-handoff."
                )

        if remote:
            remote_dir = remote_exec.remote_task_dir(mirror_root, task_id)
            # Rebuild the claude command with remote paths: the AWS task_dir/
            # project_root do not exist on the Shuttle. The builder runs INSIDE
            # the mirrored repo (cwd, so --setting-sources user,project loads the
            # repo's own CLAUDE.md/.claude) and writes task files into the
            # rsynced task mirror dir.
            remote_prompt = build_handoff_prompt(Path(remote_project_root), Path(remote_dir)) + (
                f"\nRepository: {remote_project_root} (your working directory). "
                f"Task files (prompt.md, result.json, logs) live in {remote_dir}; "
                f"writing there is the one allowed exception to the boundary above."
            )
            remote_argv = [
                "claude",
                "-p",
                shlex.quote(remote_prompt),
                "--add-dir",
                shlex.quote(remote_dir),
                "--permission-mode",
                shlex.quote(permission_mode),
                # Load the TARGET repo's own .claude context (skills + CLAUDE.md)
                # in addition to user settings. Default is effectively "user",
                # which switches the project's own agent context OFF.
                "--setting-sources",
                "user,project",
            ]
            if role_prompt:
                remote_argv += ["--append-system-prompt", shlex.quote(role_prompt)]
            if not args.quiet:
                remote_argv += ["--verbose", "--output-format", "stream-json"]
            remote_claude_cmd = " ".join(remote_argv)
            # Per-task model on the non-sandbox Shuttle path: SSH does not carry
            # the caller env, so the model must travel inside the remote command.
            # Same precedence as the local path — explicit ANTHROPIC_MODEL, else
            # task.json["model"], else the DEFAULT_ANTHROPIC_MODEL (opus-4-8).
            _remote_model = os.environ.get("ANTHROPIC_MODEL", "").strip() \
                or str(task.get("model") or "").strip()
            _remote_env = {"ANTHROPIC_MODEL": _remote_model} if _remote_model else None
            try:
                _spawn_with_retry(
                    "shuttle ssh dispatch",
                    lambda: remote_exec.spawn_remote(
                        task_dir=task_dir,
                        task_id=task_id,
                        ssh_target=ssh_target,
                        mirror_root=mirror_root,
                        session_name=session_name,
                        claude_cmd=remote_claude_cmd,
                        env=_remote_env,
                        cwd=remote_project_root,
                    ),
                    root=root,
                    task_id=task_id,
                    already_live=lambda: remote_exec.remote_screen_alive(
                        ssh_target, session_name
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — degraded, FAIL LOUD
                # A failed remote dispatch (rsync/ssh) after retries. Until
                # 2026-09-05 this silently fell back to a local claude-p on the
                # AWS head; that is exactly the OOM path the fleet plan forbids.
                # Same treatment as the sandbox arm: record the event, raise a
                # dead-man alert, leave the task re-runnable at needs_approval.
                detail = resilience.describe(exc)
                remote_exec.log_fallback(
                    root / "logs", task_id,
                    f"dispatch failed after {SPAWN_ATTEMPTS} attempts: {detail} "
                    f"(NO local fallback — AWS-head OOM guard)",
                    -1, utc_now(),
                )
                log_handoff_event(
                    root, task_id, "remote_dispatch_failed",
                    error=detail, ssh_target=ssh_target,
                    recovery="task left at needs_approval; check ssh/rsync to the Shuttle and re-run run-handoff",
                )
                record_handoff_alert(
                    root, task_dir, task_id,
                    outcome="shuttle_dispatch_failed",
                    reason=(
                        f"Shuttle dispatch failed after {SPAWN_ATTEMPTS} attempts "
                        f"({detail}). NOT falling back to local (AWS-head OOM guard); "
                        f"task left re-runnable at needs_approval."
                    ),
                    suggested_state="needs_approval",
                    ssh_target=ssh_target,
                    transport="shuttle",
                    recovery="check ssh/rsync to the Shuttle and re-run run-handoff",
                )
                print(
                    f"Shuttle dispatch failed after {SPAWN_ATTEMPTS} attempts "
                    f"({detail}). NOT falling back to local (AWS-head OOM guard); "
                    f"task {task_id} left parked at needs_approval.",
                    file=sys.stderr,
                )
                raise SystemExit(
                    f"shuttle dispatch failed for {task_id} and local fallback is "
                    f"disabled by design (AWS-head OOM guard). Task is re-runnable; "
                    f"check ssh/rsync to the Shuttle and re-run run-handoff."
                )
            else:
                update_status(
                    task_dir,
                    "running",
                    "claude_session",
                    f"Spawned {role} session {session_name} on shuttle "
                    f"({ssh_target}, permission_mode={permission_mode})",
                    0.5,
                )
                print(f"Spawned REMOTE screen session on shuttle: {session_name}")
                print(f"Host:        {ssh_target}  (mirror {remote_dir})")
                print(f"Role:        {role}  (permission_mode={permission_mode})")
                print(f"Watch live:  ssh {ssh_target} screen -r {session_name}")
                print(f"Session log: {session_log}  (rsynced back each poll)")

        if sandboxed:
            # Containerised sandbox dispatch. The branch talos/<task_id> is
            # created ON THE SHUTTLE (git worktree in the replica repo); no local
            # worktree here. The agent runs inside a container mounting only that
            # worktree; it commits to talos/<task_id> and writes its result file,
            # which we sync back while polling. merge_back_worktree already
            # handles a branch-without-local-worktree at finalize time.
            worktree_base = current_branch(project_root) or "HEAD"
            model = os.environ.get("ANTHROPIC_MODEL", "").strip() \
                or str(task.get("model") or "").strip() or "claude-opus-4-8"
            agent_cmd = task.get("sandbox_agent_cmd") or sandbox_exec.build_agent_container_cmd(
                handoff_prompt=build_handoff_prompt(Path("/work"), task_dir),
                role_prompt=role_prompt,
                permission_mode=permission_mode,
                model=model,
                quiet=args.quiet,
            )
            try:
                sandbox_info = _spawn_with_retry(
                    "shuttle-sandbox dispatch",
                    lambda: sandbox_exec.spawn_sandbox(
                        project_root=project_root,
                        project=sandbox_project,
                        task_id=task_id,
                        base_ref=worktree_base,
                        agent_cmd=agent_cmd,
                        cfg=sandbox_cfg,
                    ),
                    root=root,
                    task_id=task_id,
                    already_live=lambda: sandbox_exec.is_alive(sandbox_cfg, task_id),
                )
            except Exception as exc:  # noqa: BLE001 — SAFETY: alert + abort, NEVER local
                # SAFETY (Talos Sandbox M2 gap #1 — the dangerous one): a failed
                # shuttle-sandbox dispatch must NOT self-heal to a local claude-p
                # screen on THIS host. Unlike the plain "shuttle" host (whose
                # fallback is a bare screen on the Shuttle), the whole point of
                # shuttle-sandbox is to keep a heavy Opus build OFF the AWS head —
                # which is 3.7GB and OOMs, freezing the gateway (2026-09-04
                # incident). So the only safe degradation is fail-loud: record a
                # dead-man alert and abort, leaving the task re-runnable at
                # needs_approval for a human/cron to re-dispatch once the Shuttle
                # sandbox is healthy. DESIGN CALL: fail-and-alert was chosen over a
                # silent shuttle-SSH fallback so a broken sandbox is never masked.
                detail = resilience.describe(exc)
                remote_exec.log_fallback(
                    root / "logs", task_id,
                    f"sandbox dispatch failed after {SPAWN_ATTEMPTS} attempts: "
                    f"{detail} (NO local fallback — AWS-head OOM guard)",
                    -1, utc_now(),
                )
                record_handoff_alert(
                    root, task_dir, task_id,
                    outcome="sandbox_dispatch_failed",
                    reason=(
                        f"shuttle-sandbox dispatch failed after {SPAWN_ATTEMPTS} "
                        f"attempts: {detail}. Refused to fall back to a local "
                        f"claude-p on the AWS head (OOM risk); task left "
                        f"re-runnable at needs_approval."
                    ),
                    suggested_state="needs_approval",
                    ssh_target=sandbox_cfg.get("ssh_target"),
                    transport="shuttle-sandbox",
                    recovery=(
                        "task left in needs_approval; check the Shuttle sandbox "
                        "(talos-sandbox status / kill switch at "
                        "~/.openclaw/talos-sandbox/DISABLED) and re-run run-handoff"
                    ),
                )
                print(
                    f"Sandbox dispatch failed after {SPAWN_ATTEMPTS} attempts "
                    f"({detail}). NOT falling back to local (AWS-head OOM guard); "
                    f"task {task_id} left parked at needs_approval — re-run "
                    f"run-handoff once the Shuttle sandbox is healthy.",
                    file=sys.stderr,
                )
                raise SystemExit(
                    f"shuttle-sandbox dispatch failed for {task_id} and local "
                    f"fallback is disabled by design (AWS-head OOM guard). Task is "
                    f"re-runnable; fix the Shuttle sandbox and re-run run-handoff."
                ) from exc
            else:
                _st = read_json(task_dir / "status.json", {})
                # sandbox_info carries worktree_branch/base + preview/db/ports.
                _st.update(sandbox_info)
                write_json(task_dir / "status.json", _st)
                update_status(
                    task_dir,
                    "running",
                    "claude_session",
                    f"Spawned {role} sandbox on shuttle "
                    f"(branch {sandbox_info.get('worktree_branch')}, "
                    f"permission_mode={permission_mode})",
                    0.5,
                )
                print(f"Spawned SANDBOX on shuttle: {sandbox_project}/{task_id}")
                print(f"Branch:      {sandbox_info.get('worktree_branch')} "
                      f"(base {worktree_base})")
                if sandbox_info.get("preview_url"):
                    print(f"Preview:     {sandbox_info['preview_url']}")
                if sandbox_info.get("db"):
                    print(f"Database:    {sandbox_info['db']} "
                          f"(app={sandbox_info.get('app_port')} serve={sandbox_info.get('serve_port')})")
                print(f"Watch live:  ssh {sandbox_cfg['ssh_target']} "
                      f"docker logs -f talos-sbx-{task_id[:32]}")

        if not remote and not sandboxed:
            # Worktree-per-task: create an isolated git worktree so concurrent Talos
            # tasks can't clobber each other's uncommitted source changes. Only
            # attempted when project_root is its own git repo (not workspace-nested).
            # Falls back to project_root transparently if git is unavailable —
            # UNLESS --require-worktree was passed (parallel dispatch: two
            # sessions sharing project_root would corrupt each other), in which
            # case a failed worktree creation aborts the fire loudly.
            # Base branch is captured BEFORE creating the worktree; merge-back
            # targets it at finalize time.
            worktree_base = current_branch(project_root)
            # Worktree creation does repo-level git writes (branch create,
            # .gitignore edit) — serialize against any in-flight merge-back.
            with project_lock(root, "merge", blocking=True):
                worktree_path = create_task_worktree(project_root, task_id)
            if worktree_path is None and getattr(args, "require_worktree", False):
                raise SystemExit(
                    f"--require-worktree: could not create a git worktree for "
                    f"{task_id} in {project_root}. Refusing to run in the shared "
                    f"project checkout (parallel dispatch would corrupt it)."
                )
            execution_root = worktree_path or project_root

            # Rebuild the handoff prompt with the correct execution root.
            exec_prompt = build_handoff_prompt(project_root, task_dir, execution_root=execution_root)
            write_text(handoff_prompt_path, exec_prompt)

            # Build the claude argv using the (possibly new) execution root.
            local_argv = [
                shlex.quote(claude_bin),
                "-p",
                shlex.quote(exec_prompt),
                "--add-dir",
                shlex.quote(str(execution_root)),
                "--permission-mode",
                shlex.quote(permission_mode),
                # Load the TARGET repo's own .claude context (skills + CLAUDE.md)
                # in addition to user settings. Default is effectively "user",
                # which switches the project's own agent context OFF.
                "--setting-sources",
                "user,project",
            ]
            if role_prompt:
                local_argv += ["--append-system-prompt", shlex.quote(role_prompt)]
            # Default to stream-json so the session log shows real-time progress.
            if not args.quiet:
                local_argv += ["--verbose", "--output-format", "stream-json"]

            # Append worktree cleanup so it runs whether claude succeeds or fails.
            # DIRTY worktrees are KEPT: uncommitted work must never be destroyed
            # by cleanup (the model is instructed to commit; if it didn't, a
            # human can still salvage the files).
            worktree_cleanup = ""
            if worktree_path:
                wt_q = shlex.quote(str(worktree_path))
                pr_q = shlex.quote(str(project_root))
                worktree_cleanup = (
                    f" ; if [ -z \"$(git -C {wt_q} status --porcelain 2>/dev/null)\" ]; then"
                    f" git -C {pr_q} worktree remove --force {wt_q} 2>/dev/null || true;"
                    f" else echo 'worktree kept: uncommitted changes present'; fi"
                )

            inner = (
                f"cd {shlex.quote(str(execution_root))} && "
                + " ".join(local_argv)
                + f" 2>&1 | tee {shlex.quote(str(session_log))}"
                + worktree_cleanup
            )
            # Talos default model = Opus 4-8 (deliberate: Opus 5 = #1 token
            # burner, rolled back Aug-2026). Model-selection precedence (same on
            # local, shuttle and shuttle-sandbox paths):
            #   1. ANTHROPIC_MODEL env var — caller/shell override (highest)
            #   2. task.json["model"] — per-task CTO choice
            #   3. Default: claude-opus-4-8 (hard build/architecture)
            # Convention:
            #   claude-sonnet-4-6  → easy/read-only/research/diagnose
            #   claude-opus-4-8    → hard build/architecture/multi-file refactor (default)
            #   claude-fable-5     → deep reasoning/planning
            #   claude-fable-5-1   → deep reasoning/planning (Fable 5.1, 2026-09-01)
            spawn_env = os.environ.copy()
            _local_model = os.environ.get("ANTHROPIC_MODEL", "").strip() \
                or str(task.get("model") or "").strip()
            if _local_model:
                spawn_env["ANTHROPIC_MODEL"] = _local_model
            else:
                spawn_env.setdefault("ANTHROPIC_MODEL", "claude-opus-4-8")
            try:
                _spawn_with_retry(
                    "local screen spawn",
                    lambda: subprocess.run(
                        [screen_bin, "-dmS", session_name, "bash", "-c", inner],
                        check=True,
                        env=spawn_env,
                    ),
                    root=root,
                    task_id=task_id,
                    already_live=lambda: screen_session_alive(screen_bin, session_name),
                )
            except Exception as exc:  # noqa: BLE001 — alerted, then re-raised as SystemExit
                # This is the end of the line: local screen is the fallback, so
                # there is nothing left to fall back TO. Nothing ran, so the task
                # is still safely parked at needs_approval and re-runnable — but
                # the caller may be a cron/autorunner with nobody reading stderr,
                # so leave a dead-man record before failing.
                detail = resilience.describe(exc)
                record_handoff_alert(
                    root, task_dir, task_id,
                    outcome="spawn_failed",
                    reason=f"screen spawn failed after {SPAWN_ATTEMPTS} attempts: {detail}",
                    session_name=session_name,
                    session_log=session_log,
                    transport="local",
                    recovery="task left in needs_approval; re-run run-handoff",
                )
                raise SystemExit(
                    f"Could not spawn the Claude session for {task_id} after "
                    f"{SPAWN_ATTEMPTS} attempts: {detail}. Task is still parked at "
                    f"needs_approval — fix the host and re-run run-handoff."
                ) from exc
            update_status(
                task_dir,
                "running",
                "claude_session",
                f"Spawned {role} session {session_name} (permission_mode={permission_mode}"
                + (f", worktree={worktree_path.name}" if worktree_path else "") + ")",
                0.5,
            )
            # Record worktree metadata for merge-back + complete-handoff cleanup.
            if worktree_path:
                _st = read_json(task_dir / "status.json", {})
                _st["worktree_path"] = str(worktree_path)
                _st["worktree_branch"] = f"talos/{task_id}"
                _st["worktree_base"] = worktree_base
                write_json(task_dir / "status.json", _st)

            print(f"Spawned screen session: {session_name}")
            print(f"Role:        {role}  (permission_mode={permission_mode})")
            if worktree_path:
                print(f"Worktree:    {worktree_path}  (branch: talos/{task_id})")
            print(f"Watch live:  screen -r {session_name}   (detach with Ctrl+A D)")
            print(f"Session log: {session_log}")

        if args.detach:
            print("(--detach): not polling; run complete-handoff manually when result.json appears.")
            if remote:
                print(
                    "(remote --detach): rsync results back with: "
                    f"rsync -a {ssh_target}:{remote_exec.remote_task_dir(mirror_root, task_id)}/ "
                    f"{task_dir}/"
                )
            elif sandboxed:
                print(
                    "(sandbox --detach): poll `talos-sandbox status "
                    f"{task_id}` on the Shuttle; complete-handoff fetches the "
                    "branch and merges once result.json is synced back."
                )
            return 0

        result_path = task_dir / "result.json"
        timeout = args.timeout
        interval = args.poll_interval
        elapsed = 0.0
        # Consecutive-observation counters. Both exist because a SINGLE bad
        # observation used to be enough to abort a healthy run (`screen -ls`
        # missing a live session) or to be ignored forever (a wedged rsync).
        death_misses = 0
        sync_failures = 0
        sync_alerted = False
        print(f"Polling for result.json (timeout {timeout}s, interval {interval}s)...")
        while elapsed < timeout:
            if sandboxed:
                # Sync the agent's result file out of the sandbox worktree host
                # dir. Best-effort: absent until the agent writes it. We do NOT
                # alert on sync failure alone here (the file simply may not exist
                # yet); death detection below is what surfaces a crashed run.
                sandbox_exec.sync_result_back(
                    sandbox_cfg, sandbox_project, task_id, task_dir
                )
            if remote:
                if remote_exec.rsync_pull(ssh_target, mirror_root, task_id, task_dir):
                    sync_failures = 0
                else:
                    sync_failures += 1
                    if sync_failures >= REMOTE_SYNC_ALERT_AFTER and not sync_alerted:
                        # The remote session may well be finishing fine; what is
                        # broken is our ability to SEE it. Keep polling, but stop
                        # doing it silently — this used to be swallowed whole
                        # (rsync_pull ran with check=False and no return value),
                        # so a task could time out purely because results never
                        # made it back.
                        sync_alerted = True
                        record_handoff_alert(
                            root, task_dir, task_id,
                            outcome="remote_sync_degraded",
                            reason=(
                                f"{sync_failures} consecutive rsync pulls from "
                                f"{ssh_target} failed; results may not be arriving"
                            ),
                            session_name=session_name,
                            session_log=session_log,
                            ssh_target=ssh_target,
                            recovery="still polling; check ssh/rsync to the shuttle",
                        )
            if result_path.exists():
                break

            alive = _session_alive(
                remote=remote,
                screen_bin=screen_bin,
                session_name=session_name,
                ssh_target=ssh_target,
                sandboxed=sandboxed,
                sandbox_cfg=sandbox_cfg,
                task_id=task_id,
            )
            if alive is None:
                # Probe failed: "could not tell" is NOT "dead". Log it and keep
                # the death counter where it is.
                log_handoff_event(
                    root, task_id, "liveness_probe_failed",
                    session_name=session_name, remote=remote,
                )
            elif alive:
                death_misses = 0
            else:
                death_misses += 1
                if death_misses >= SESSION_DEATH_CONFIRMATIONS:
                    time.sleep(1)
                    if remote:
                        remote_exec.rsync_pull(ssh_target, mirror_root, task_id, task_dir)
                    if sandboxed:
                        sandbox_exec.sync_result_back(
                            sandbox_cfg, sandbox_project, task_id, task_dir
                        )
                    if result_path.exists():
                        break
                    record_handoff_alert(
                        root, task_dir, task_id,
                        outcome="session_died",
                        reason=(
                            f"screen session {session_name} gone on "
                            f"{SESSION_DEATH_CONFIRMATIONS} consecutive probes "
                            f"with no result.json"
                        ),
                        session_name=session_name,
                        session_log=session_log,
                        elapsed_s=int(elapsed),
                        suggested_state="failed",
                        recovery=f"inspect {session_log}, then re-run or reap the task",
                    )
                    raise SystemExit(
                        f"Screen session {session_name} exited without writing result.json. "
                        f"Check {session_log}."
                    )
            time.sleep(interval)
            elapsed += interval

        if not result_path.exists():
            # THE silent stall this whole task is about: before, run_handoff just
            # raised here. status.json still said "running" (last touched at spawn
            # time), the queue token still sat in blocked/, and a detached caller
            # saw nothing at all. Now the timeout is a recorded event.
            still_alive = _session_alive(
                remote=remote,
                screen_bin=screen_bin,
                session_name=session_name,
                ssh_target=ssh_target,
                sandboxed=sandboxed,
                sandbox_cfg=sandbox_cfg,
                task_id=task_id,
            )
            record_handoff_alert(
                root, task_dir, task_id,
                outcome="timeout",
                reason=f"no result.json after {timeout}s",
                session_name=session_name,
                session_log=session_log,
                elapsed_s=int(elapsed),
                session_alive=still_alive,
                # Only advise "failed" when we positively confirmed the session
                # is gone. A live session that is merely slow must not be
                # mislabelled — the poll timeout is our patience running out,
                # not the task's.
                suggested_state="failed" if still_alive is False else None,
                recovery=(
                    f"tail -f {session_log}; re-poll with a larger --timeout, "
                    f"or complete-handoff once result.json appears"
                ),
            )
            raise SystemExit(
                f"Timeout after {timeout}s waiting for {result_path}. "
                f"Screen session {session_name} may still be running. "
                f"Recorded in {task_dir / HANDOFF_ALERT_FILE}."
            )

        print(f"result.json detected after ~{int(elapsed)}s. Running complete-handoff...")
        # The run produced its artifact; any degradation alert raised while
        # polling is now history, not an outstanding problem.
        clear_handoff_alert(root, task_dir, task_id)
        return complete_handoff(args)


def complete_handoff(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    task_id = args.task_id
    task_dir = root / "tasks" / task_id
    if not task_dir.is_dir():
        raise SystemExit(f"Unknown task: {task_id}")

    result_path = task_dir / "result.json"
    if not result_path.exists():
        raise SystemExit(
            f"No result.json at {result_path}. Run the supervised Claude session first, then re-run complete-handoff."
        )

    result = read_json(result_path, None)
    if not isinstance(result, dict):
        raise SystemExit(f"Invalid result.json (expected JSON object): {result_path}")
    task = read_json(task_dir / "task.json", {})

    result_task_id = result.get("task_id")
    if result_task_id and result_task_id != task_id:
        raise SystemExit(
            f"result.json task_id mismatch: expected {task_id}, got {result_task_id}"
        )

    raw_state = result.get("state", "completed")
    # Normalize CTO-written state to one of: completed, failed, blocked.
    # CTOs sometimes write "verified", "done", "success", "ok", etc. — treat
    # those as completed. staged_pending_rollout is a dep-success state
    # (autorunner DEP_SUCCESS_RESULT_STATES) and MUST be finalizable, or chains
    # behind it deadlock forever. Only explicit fail/block states route to failed.
    SUCCESS_STATES = {"completed", "verified", "done", "success", "succeeded",
                      "ok", "passed", "staged_pending_rollout",
                      "completed_with_caveat", "completed_with_caveats"}
    FAIL_STATES = {"failed", "error", "errored", "rejected"}
    BLOCK_STATES = {"blocked", "needs_clarification", "needs_approval"}
    if raw_state in SUCCESS_STATES:
        result_state = "completed"
    elif raw_state in FAIL_STATES:
        result_state = "failed"
    elif raw_state in BLOCK_STATES:
        result_state = "blocked"
    else:
        raise SystemExit(
            f"result.json has unrecognized state: {raw_state!r}. "
            f"Expected one of: {sorted(SUCCESS_STATES | FAIL_STATES | BLOCK_STATES)}"
        )

    if result_state == "completed":
        contract_errors = validate_completed_result_contract(result, task)
        if contract_errors:
            raise SystemExit(
                "result.json is missing required completion evidence:\n- "
                + "\n- ".join(contract_errors)
            )

    merge_outcome: dict[str, Any] | None = None
    # The whole finalize (merge-back -> token move -> status) runs under the
    # BLOCKING per-project merge lock: the stall-watchdog tick, a human
    # complete-handoff and a run-handoff poller can all race here, and the
    # loser must WAIT, not die mid-finalize. Merge-back runs BEFORE the token
    # reaches done/ so a dependent task can never observe "done" while its
    # parent's commits are still stranded on an unmerged talos/ branch.
    with project_lock(root, "merge", blocking=True):
        source = find_queue_entry(root, task_id)
        if source is None:
            # Idempotent no-op: someone else finalized first (watchdog vs human
            # race) — that is success, not an error.
            for terminal in ("done", "failed"):
                if (root / "queue" / terminal / f"{task_id}.json").exists():
                    # Gap #3 guard: a sandboxed task sitting in queue/done whose
                    # recorded merge state is not clean was finalized WITHOUT its
                    # commits landing (the 2026-09-04 incident). Don't silently
                    # reaffirm success — flag it so nobody trusts a bogus "done".
                    _fst = read_json(task_dir / "status.json", {})
                    _fmerge = _fst.get("merge") if isinstance(_fst, dict) else None
                    _fmerge = _fmerge if isinstance(_fmerge, dict) else {}
                    if (terminal == "done" and isinstance(_fst, dict)
                            and _fst.get("sandboxed")
                            and _fmerge.get("state") not in MERGE_OK_STATES):
                        record_handoff_alert(
                            root, task_dir, task_id,
                            outcome="finalized_without_merge",
                            reason=(
                                f"task is in queue/done but sandbox merge state is "
                                f"{_fmerge.get('state')!r} — commits may still be "
                                f"stranded on talos/{task_id}."
                            ),
                            suggested_state="blocked",
                            transport="shuttle-sandbox",
                            recovery=(
                                f"verify talos/{task_id} merged into its base "
                                f"branch; re-merge by hand if not"
                            ),
                        )
                        print(json.dumps(
                            {"task_id": task_id, "state": "already_finalized",
                             "queue": terminal,
                             "warning": "sandbox merge not verified — see handoff alert"},
                            indent=2, sort_keys=True))
                        return 0
                    print(json.dumps({"task_id": task_id, "state": "already_finalized",
                                      "queue": terminal}, indent=2, sort_keys=True))
                    return 0
            raise SystemExit(
                f"No queue entry for {task_id} in any queue state. Cancelled?"
            )

        _cst = read_json(task_dir / "status.json", {})
        # Sandbox tasks (Milestone 2): the talos/<task_id> branch lives on the
        # Shuttle replica; fetch it into the canonical repo BEFORE merge-back,
        # inside this same blocking merge lock (the fetch must be serialized with
        # merge exactly as worktree-create already is). merge_back_worktree then
        # runs unchanged — it already handles a branch with no local worktree.
        if _cst.get("sandboxed") and result_state == "completed":
            _sbx_cfg = sandbox_exec.sandbox_config(task)
            _sbx_proj = _cst.get("project") or task.get("project")
            _sbx_project = (
                _sbx_proj.get("name") if isinstance(_sbx_proj, dict) else _sbx_proj
            ) or project_root.name
            try:
                remote_name = sandbox_exec.ensure_local_remote(
                    project_root, _sbx_cfg, _sbx_project
                )
                # Gap #3: a stale local worktree/branch (leftover from a prior
                # local fallback) makes git refuse the fetch ("refusing to fetch
                # into branch ... checked out at .worktrees/..."). Auto-prune it
                # first; the replica holds the authoritative branch.
                pruned = sandbox_exec.prune_stale_local_refs(project_root, task_id)
                if pruned:
                    print("pruned stale local refs before sandbox fetch: "
                          + "; ".join(pruned))
                sandbox_exec.fetch_branch(project_root, remote_name, task_id)
            except Exception as exc:  # noqa: BLE001
                # A failed fetch means there is nothing to merge. Surface it (do
                # NOT synthesise success); leave the sandbox up for inspection.
                raise SystemExit(
                    f"sandbox fetch of talos/{task_id} failed: {exc}. "
                    "The Shuttle replica may be unreachable; the sandbox is left "
                    "up for inspection (talos-sandbox status)."
                ) from exc
        if result_state == "completed":
            merge_outcome = merge_back_worktree(project_root, task_id, _cst, result)
            # Phase 1 (A.1): record the merge-proof tip_sha at finalize so the
            # reconciler can later prove DELIVERED without the branch present.
            record_merge_proof(project_root, merge_outcome,
                               base=_cst.get("worktree_base"))
            _cst = read_json(task_dir / "status.json", {})
            _cst["merge"] = merge_outcome
            write_json(task_dir / "status.json", _cst)
            if merge_outcome.get("state") not in MERGE_OK_STATES:
                # Gap #3: a sandbox task's commits live ONLY on the Shuttle replica
                # branch (fetched into talos/<id> just above) until this merge
                # lands — there is no local worktree copy. Moving the token to
                # done/ on an unclean merge reports success while the work is
                # stranded on talos/<id> (the 2026-09-04 "interests marked done
                # WITHOUT merging" incident). For sandbox tasks: alert + abort,
                # leaving the task finalizable once the merge is resolved by hand.
                # Local (non-sandbox) worktree behaviour is DELIBERATELY unchanged:
                # its branch is still present locally, so a dirty merge stays a
                # warning that marks done, exactly as before.
                if _cst.get("sandboxed"):
                    record_handoff_alert(
                        root, task_dir, task_id,
                        outcome="merge_back_unclean",
                        reason=(
                            f"sandbox merge-back not clean "
                            f"({merge_outcome.get('state')}): "
                            f"{merge_outcome.get('detail', '')}. Refusing to mark "
                            f"done — commits remain on talos/{task_id}."
                        ),
                        suggested_state="blocked",
                        transport="shuttle-sandbox",
                        recovery=(
                            f"talos/{task_id} is fetched locally; resolve the "
                            f"merge by hand onto {merge_outcome.get('base', 'the base branch')}, "
                            f"then re-run complete-handoff"
                        ),
                    )
                    raise SystemExit(
                        f"sandbox merge-back for {task_id} was not clean "
                        f"({merge_outcome.get('state')}): "
                        f"{merge_outcome.get('detail', '')}. Task NOT marked done; "
                        f"commits are safe on talos/{task_id}. Resolve and re-run "
                        f"complete-handoff."
                    )
                print(f"⚠ merge-back NOT clean: {merge_outcome.get('state')} — "
                      f"{merge_outcome.get('detail', '')}")
            elif _cst.get("sandboxed"):
                # Clean merge: free the Shuttle sandbox (container already exited)
                # and delete the replica branches. Best-effort — a leftover
                # lease/branch is reaped by talos-sandbox gc, never a blocker.
                _sbx_cfg = sandbox_exec.sandbox_config(task)
                sandbox_exec.teardown(_sbx_cfg, task_id)
                sandbox_exec.cleanup_remote_branches(project_root, _sbx_cfg, task_id)

        # Belt-and-suspenders worktree cleanup — AFTER merge-back, and only when
        # the worktree has no uncommitted changes (never destroy salvageable work).
        _wt_str = _cst.get("worktree_path")
        if _wt_str and Path(_wt_str).exists():
            wt_porcelain = subprocess.run(
                ["git", "-C", _wt_str, "status", "--porcelain"],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if wt_porcelain.returncode == 0 and not wt_porcelain.stdout.strip():
                remove_task_worktree(project_root, Path(_wt_str))
            else:
                print(f"worktree kept (uncommitted changes): {_wt_str}")

        target_state = "done" if result_state == "completed" else "failed"
        target = root / "queue" / target_state / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

        phase = "done" if result_state == "completed" else "failed"
        summary = (result.get("summary") or "Operator completed handoff").strip()
        update_status(
            task_dir,
            result_state,
            phase,
            f"Handoff completed: {summary[:120]}",
            1.0,
        )

    # The task reached a terminal state, so whatever a previous run-handoff
    # attempt alerted about (timeout, dead session, degraded sync) is resolved.
    clear_handoff_alert(root, task_dir, task_id)
    log_handoff_event(root, task_id, "finalized", state=result_state, queue=target_state)

    # Post-run DocSync hook (guarded). Returns None when disabled — then the
    # payload below is byte-identical to pre-Phase-2 behavior. When enabled,
    # a compact status dict is attached under payload["docsync"]. Failure is
    # NEVER allowed to rollback the completed task — the code commit is durable.
    docsync_outcome = None
    try:
        docsync_outcome = maybe_run_docsync(
            project_root=project_root,
            root=root,
            task_dir=task_dir,
            task=task,
            result=result,
            result_state=result_state,
        )
    except Exception as e:  # noqa: BLE001 — deliberate broad catch
        docsync_outcome = {"state": "errored", "error": repr(e)[:400]}

    payload: dict[str, Any] = {
        "task_id": task_id,
        "state": result_state,
        "queue": target_state,
    }
    if merge_outcome is not None:
        payload["merge"] = merge_outcome
    if docsync_outcome is not None:
        payload["docsync"] = docsync_outcome
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def merge_back_cmd(args: argparse.Namespace) -> int:
    """Standalone merge-back retry for an already-finalized task.

    complete-handoff is idempotent and will not re-merge once the token is in
    done/ — this command re-attempts JUST the merge (e.g. after committing the
    dirty file that caused skipped_dirty, or after a human resolved a
    conflict cause) and refreshes status.json `merge` so the autorunner dep
    gate unblocks dependents.
    """
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    task_id = args.task_id
    task_dir = root / "tasks" / task_id
    if not task_dir.is_dir():
        raise SystemExit(f"Unknown task: {task_id}")
    result = read_json(task_dir / "result.json", {}) or {}
    with project_lock(root, "merge", blocking=True):
        status = read_json(task_dir / "status.json", {})
        outcome = merge_back_worktree(project_root, task_id, status, result)
        # Phase 1 (A.1): stamp the merge-proof tip_sha on the re-merge outcome too.
        record_merge_proof(project_root, outcome, base=status.get("worktree_base"))
        status = read_json(task_dir / "status.json", {})
        status["merge"] = outcome
        write_json(task_dir / "status.json", status)
    print(json.dumps({"task_id": task_id, "merge": outcome}, indent=2, sort_keys=True))
    return 0 if outcome.get("state") in MERGE_OK_STATES else 1


def current_branch(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "branch", "--show-current"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip()
        return branch or None
    except Exception:
        return None


def run_worker(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    backend = args.backend or read_json(root / "config.json", {}).get("default_backend", "dry-run")
    target_id = getattr(args, "task_id", None)
    processed = 0
    while True:
        claimed = claim_next(root, target_id)
        if claimed is None:
            if target_id:
                # Explicit target requested but not claimable -> LOUD failure.
                # Never a silent processed=0 no-op (the 2026-07-18 root cause).
                print("processed=0")
                print(f"ERROR: task {target_id!r} is not in queue/pending/ — "
                      f"it was already prepared, cancelled, or never enqueued. "
                      f"Nothing was promoted.")
                return 2
            if args.once:
                break
            time.sleep(args.poll_interval or 5)
            continue
        update_status(claimed.task_dir, "claimed", "claimed", f"Claimed by {backend}", 0.1)
        if backend == "dry-run":
            complete_dry_run(root, claimed)
        elif backend == "subscription-interactive":
            prepare_subscription_interactive(root, claimed)
        elif backend == "api-automation":
            update_status(claimed.task_dir, "blocked", "backend_unimplemented", "api-automation backend is a v1 stub", 0.0)
            write_json(
                claimed.task_dir / "result.json",
                {
                    "task_id": claimed.task_id,
                    "state": "blocked",
                    "finished_at": utc_now(),
                    "summary": "api-automation backend is not wired yet.",
                    "next_actions": ["Provide API/Agent SDK credentials and implementation policy."],
                },
            )
            move_queue(root, claimed, "blocked")
        else:
            raise SystemExit(f"Unknown backend: {backend}")
        processed += 1
        if target_id or args.once:
            break
    print(f"processed={processed}")
    return 0


def dry_run(args: argparse.Namespace) -> int:
    args.backend = "dry-run"
    args.once = True
    return run_worker(args)


def status(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    counts = {state: len(list((root / "queue" / state).glob("*.json"))) for state in QUEUE_STATES}
    payload: dict[str, Any] = {"project_root": str(project_root), "queue": counts}
    # Only present when something is wrong, so a healthy project's payload stays
    # byte-identical to before. An outstanding alert means a handoff stalled,
    # died, or degraded and nobody has closed it out yet.
    alerts = collect_handoff_alerts(root)
    if alerts:
        payload["handoff_alerts"] = alerts
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.tasks:
        for task_dir in sorted((root / "tasks").iterdir()):
            if task_dir.is_dir():
                st = read_json(task_dir / "status.json", {})
                print(f"{task_dir.name}\t{st.get('state')}\t{st.get('phase')}\t{st.get('updated_at')}")
    return 0


def doctor(args: argparse.Namespace) -> int:
    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    checks = {
        "loop_dir": root.exists(),
        "config": (root / "config.json").exists(),
        "queue_dirs": all((root / "queue" / state).exists() for state in QUEUE_STATES),
        "claude_binary": shutil.which("claude") is not None,
        "tmux_binary": shutil.which("tmux") is not None,
    }
    print(json.dumps(checks, indent=2, sort_keys=True))
    return 0 if all(value for key, value in checks.items() if key not in {"tmux_binary"}) else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Phase 1: run the auto-delivery reconciler pass over this project.

    Read-only by default. With --heal (gated) it re-merges stranded done-tasks
    under the blocking merge lock. --report appends the EOD delivery ledger
    line; LOOPS.md is ingested (unless --no-loops) so open loops whose tasks are
    stuck/delivered are surfaced alongside the delivery report. This is the
    command the EOD cron runs.
    """
    from . import reconciler as _rec  # lazy: reconciler imports cli (circular)
    from . import loops as _loops

    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    config = read_json(root / "config.json", {})
    base = args.base or reconcile_config(config).get("base") or reconciler_default_base()
    heal = bool(args.heal)
    if heal and not reconcile_heal_enabled(config):
        raise SystemExit(
            "reconcile --heal is gated OFF for this project. Auto-merging stranded "
            "branches is the highest-blast-radius action; enable it deliberately via "
            "config.json reconciler.heal_enabled=true (or env TALOS_RECONCILE_HEAL=1) "
            "once merge-heal has been approved."
        )

    if args.scan_crashes:
        crashes = _rec.scan_crashed_sessions(root)
        if crashes:
            print(f"CRASH: {len(crashes)} crashed session(s) surfaced", file=sys.stderr)

    report = _rec.reconcile_project(
        root, project_root, heal=heal, base=base, ledger_sha=args.ledger_sha,
    )
    if args.report:
        _rec.write_delivery_report(root, report)

    loops_findings: list[dict[str, Any]] = []
    if not args.no_loops:
        loops_path = _loops.resolve_loops_path(project_root, args.loops_file)
        if loops_path is not None:
            loops_findings = _loops.sweep_loops(loops_path, report)
            report["loops"] = {"path": str(loops_path), "open": loops_findings}

    print(_rec.render_delivery_report(report))
    if loops_findings:
        print(_loops.render_loops_sweep(loops_findings))

    open_items = (report["counts"]["stuck"] + report["counts"]["stranded"]
                  + report["counts"]["stalled"])
    loops_stuck = any(f.get("stuck") for f in loops_findings)
    return 1 if (open_items or loops_stuck) else 0


def reconciler_default_base() -> str:
    from . import reconciler as _rec
    return _rec.DEFAULT_BASE


def cmd_deploy_watch(args: argparse.Namespace) -> int:
    """Phase 1: commit -> auto-redeploy -> verify-live watcher (dev).

    When the base branch tip advances past the last-deployed sha, run the deploy
    command, then the verify command, then record the new live sha. Fail-loud:
    a failed deploy or verify is journalled and exits non-zero; the live sha is
    only advanced on a clean deploy + verify. Serialized under the 'deploy' lock.
    """
    from . import deploy_watch as _dw

    project_root = project_root_from(args)
    root = require_bootstrap(project_root)
    config = read_json(root / "config.json", {})
    base = args.base or reconcile_config(config).get("base") or reconciler_default_base()
    deploy_cmd = shlex.split(args.deploy_cmd)
    verify_cmd = shlex.split(args.verify_cmd) if args.verify_cmd else None
    result = _dw.watch_once(
        root, project_root, base=base,
        deploy_cmd=deploy_cmd, verify_cmd=verify_cmd,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("action") in ("deployed", "noop") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openclaw-claude-loop")
    parser.add_argument("--project-root", help="Target project root. Defaults to current directory.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=bootstrap)

    p = sub.add_parser("enqueue")
    p.add_argument("title")
    p.add_argument("--id")
    p.add_argument("--goal")
    p.add_argument("--type", default="implementation")
    p.add_argument("--role", default="builder")
    p.add_argument("--instruction", action="append")
    p.add_argument("--instructions-file")
    p.add_argument("--file", action="append")
    p.add_argument("--write-scope", action="append")
    p.add_argument("--network", default="deny")
    p.add_argument("--max-runtime-seconds", type=int, default=1800)
    p.add_argument("--expected-output", action="append")
    p.add_argument(
        "--created-by", default="openclaw",
        help="Identity to stamp on task.json created_by. Default 'openclaw' is the "
             "raw-CLI/library default and is NOT on the autorunner's TRUSTED_CREATORS "
             "allowlist (max/argus/talos-*) -- tasks created that way are always parked "
             "for human review, never auto-fired. A caller that IS one of those trusted "
             "identities (e.g. a per-project Talos/CTO dispatch) should pass its real "
             "identity here explicitly, e.g. --created-by talos-<project>, instead of "
             "relying on the default and getting dead-lettered.")
    p.set_defaults(func=enqueue)

    p = sub.add_parser("run-worker")
    p.add_argument("--backend", choices=("dry-run", "subscription-interactive", "api-automation"))
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-interval", type=int)
    p.add_argument("--task-id", dest="task_id", default=None,
                   help="Claim THIS task specifically instead of the oldest "
                        "pending one. Makes prepare/dispatch deterministic; "
                        "exits 2 if the task is not in queue/pending/. "
                        "Omit for legacy FIFO behaviour.")
    p.set_defaults(func=run_worker)

    p = sub.add_parser("dry-run")
    p.set_defaults(func=dry_run)

    p = sub.add_parser("status")
    p.add_argument("--tasks", action="store_true")
    p.set_defaults(func=status)

    p = sub.add_parser("complete-handoff")
    p.add_argument("task_id")
    p.set_defaults(func=complete_handoff)

    p = sub.add_parser(
        "merge-back",
        help="Re-attempt merging a finalized task's talos/<id> branch into its base "
             "(after fixing whatever made the original merge skip/conflict).",
    )
    p.add_argument("task_id")
    p.set_defaults(func=merge_back_cmd)

    p = sub.add_parser(
        "run-handoff",
        help="Spawn a detached Claude Code session in screen for a blocked task, then auto-complete.",
    )
    p.add_argument("task_id")
    p.add_argument("--detach", action="store_true", help="Spawn screen and exit; skip polling.")
    p.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for result.json.")
    p.add_argument("--poll-interval", type=int, default=5, help="Polling interval in seconds.")
    p.add_argument(
        "--permission-mode",
        default=None,
        choices=("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"),
        help="Override Claude --permission-mode (default: bypassPermissions for cto, acceptEdits otherwise).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Use default Claude text output instead of streaming JSON (session log will be empty until the task completes).",
    )
    p.add_argument(
        "--require-worktree",
        dest="require_worktree",
        action="store_true",
        help="Abort instead of falling back to the shared project checkout when a "
             "git worktree cannot be created (mandatory for parallel dispatch).",
    )
    p.set_defaults(func=run_handoff)

    p = sub.add_parser(
        "reconcile",
        help="Run the auto-delivery reconciler pass (delivery proofs + stranded/"
             "stalled sweep); optional gated --heal, EOD --report, LOOPS.md sweep.",
    )
    p.add_argument("--base", default=None, help="Base branch (default: config reconciler.base or 'main').")
    p.add_argument("--ledger-sha", default=None, help="Live-deployed sha for the merged->live check.")
    p.add_argument("--heal", action="store_true",
                   help="Auto-merge stranded done-tasks (GATED: needs reconciler.heal_enabled).")
    p.add_argument("--report", action="store_true", help="Append an EOD delivery report to the ledger.")
    p.add_argument("--scan-crashes", action="store_true",
                   help="Also scream about claimed tasks whose session died with no result.")
    p.add_argument("--loops-file", default=None,
                   help="Path to LOOPS.md (default: auto-discover upward from project root).")
    p.add_argument("--no-loops", action="store_true", help="Skip LOOPS.md ingestion.")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser(
        "deploy-watch",
        help="Detect new base-branch commits, redeploy, verify live, record the new "
             "live sha (dev auto-redeploy watcher).",
    )
    p.add_argument("--base", default=None, help="Base branch to watch (default: config reconciler.base or 'main').")
    p.add_argument("--deploy-cmd", required=True, help="Command to (re)deploy when the base tip advances.")
    p.add_argument("--verify-cmd", default=None, help="Command to verify live after deploy (non-zero = fail-loud).")
    p.set_defaults(func=cmd_deploy_watch)

    p = sub.add_parser("doctor")
    p.set_defaults(func=doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
