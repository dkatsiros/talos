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
from typing import Any, Iterator

from . import __version__
from . import remote_exec
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

    Blind FIFO was the root cause of a silent-black-hole bug: a caller asked to
    prepare task X, FIFO promoted task Y (or nothing at all), and X rotted in
    pending/ — a directory the autorunner never reads — with zero log lines and
    attempt=0. Targeted claiming makes dispatch deterministic.
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
    # queue/claimed/ was missing in every project (verified in production). The
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
    # claude-loop-<id[:40]> — any external watcher that derives the same session
    # name from the task_id depends on this convention.
    pattern = re.compile(r"^\s*\d+\." + re.escape(session_name) + r"\s", re.MULTILINE)
    return bool(pattern.search(result.stdout))


# ---------------------------------------------------------------------------
# Worktree-per-task helpers
# ---------------------------------------------------------------------------

def _is_own_git_repo(project_root: Path) -> bool:
    """True only when project_root is the top-level of its own git repository.

    Guards against workspace-nested projects (e.g. projects/example inside
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
    # (A task whose status drifted out of needs_approval — e.g. finished-but-never-
    # finalized, or a prepare/handoff version skew — could hit an absolute refusal
    # here with no recovery path.)
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
                "if it died mid-run with no result, use your task-reaper tooling. "
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

        # execution_host targeting (remote-offload, Path C). Default is local;
        # behaviour is byte-identical to the pre-existing path when unset.
        execution_host = task.get("execution_host", "local")
        remote = False
        ssh_target = ""
        mirror_root = ""
        if execution_host == "remote":
            cfg = remote_exec.remote_config(task)
            ssh_target = cfg["ssh_target"]
            mirror_root = cfg["mirror_root"]
            ok, latency_ms, reason = remote_exec.preflight(
                ssh_target, cfg["preflight_timeout_s"]
            )
            if ok:
                remote = True
            else:
                # Remote host offline / unreachable -> log one line and run local.
                remote_exec.log_fallback(
                    root / "logs", task_id, reason, latency_ms, utc_now()
                )
                print(
                    f"Remote host preflight failed ({reason} after {latency_ms}ms); "
                    "falling back to local screen."
                )

        if remote:
            remote_dir = remote_exec.remote_task_dir(mirror_root, task_id)
            # Rebuild the claude command with remote mirror paths: the local
            # task_dir/project_root do not exist on the remote host, the rsynced
            # mirror does.
            remote_prompt = build_handoff_prompt(Path(remote_dir), Path(remote_dir))
            remote_argv = [
                "claude",
                "-p",
                shlex.quote(remote_prompt),
                "--add-dir",
                shlex.quote(remote_dir),
                "--permission-mode",
                shlex.quote(permission_mode),
            ]
            if role_prompt:
                remote_argv += ["--append-system-prompt", shlex.quote(role_prompt)]
            if not args.quiet:
                remote_argv += ["--verbose", "--output-format", "stream-json"]
            remote_claude_cmd = " ".join(remote_argv)
            remote_exec.spawn_remote(
                task_dir=task_dir,
                task_id=task_id,
                ssh_target=ssh_target,
                mirror_root=mirror_root,
                session_name=session_name,
                claude_cmd=remote_claude_cmd,
            )
            update_status(
                task_dir,
                "running",
                "claude_session",
                f"Spawned {role} session {session_name} on remote host "
                f"({ssh_target}, permission_mode={permission_mode})",
                0.5,
            )
            print(f"Spawned REMOTE screen session: {session_name}")
            print(f"Host:        {ssh_target}  (mirror {remote_dir})")
            print(f"Role:        {role}  (permission_mode={permission_mode})")
            print(f"Watch live:  ssh {ssh_target} screen -r {session_name}")
            print(f"Session log: {session_log}  (rsynced back each poll)")
        else:
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
            # Default model = claude-opus-5 (override via ANTHROPIC_MODEL).
            # setdefault preserves explicit overrides — e.g. ANTHROPIC_MODEL=claude-fable-5
            # still wins for hard builds.
            spawn_env = os.environ.copy()
            spawn_env.setdefault("ANTHROPIC_MODEL", "claude-opus-5")
            subprocess.run(
                [screen_bin, "-dmS", session_name, "bash", "-c", inner],
                check=True,
                env=spawn_env,
            )
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
            return 0

        result_path = task_dir / "result.json"
        timeout = args.timeout
        interval = args.poll_interval
        elapsed = 0.0
        print(f"Polling for result.json (timeout {timeout}s, interval {interval}s)...")
        while elapsed < timeout:
            if remote:
                remote_exec.rsync_pull(ssh_target, mirror_root, task_id, task_dir)
            if result_path.exists():
                break
            alive = (
                remote_exec.remote_screen_alive(ssh_target, session_name)
                if remote
                else screen_session_alive(screen_bin, session_name)
            )
            if not alive:
                time.sleep(1)
                if remote:
                    remote_exec.rsync_pull(ssh_target, mirror_root, task_id, task_dir)
                if result_path.exists():
                    break
                raise SystemExit(
                    f"Screen session {session_name} exited without writing result.json. "
                    f"Check {session_log}."
                )
            time.sleep(interval)
            elapsed += interval

        if not result_path.exists():
            raise SystemExit(
                f"Timeout after {timeout}s waiting for {result_path}. "
                f"Screen session {session_name} may still be running."
            )

        print(f"result.json detected after ~{int(elapsed)}s. Running complete-handoff...")
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
                    print(json.dumps({"task_id": task_id, "state": "already_finalized",
                                      "queue": terminal}, indent=2, sort_keys=True))
                    return 0
            raise SystemExit(
                f"No queue entry for {task_id} in any queue state. Cancelled?"
            )

        _cst = read_json(task_dir / "status.json", {})
        if result_state == "completed":
            merge_outcome = merge_back_worktree(project_root, task_id, _cst, result)
            _cst = read_json(task_dir / "status.json", {})
            _cst["merge"] = merge_outcome
            write_json(task_dir / "status.json", _cst)
            if merge_outcome.get("state") not in MERGE_OK_STATES:
                print(f"⚠ merge-back NOT clean: {merge_outcome.get('state')} — "
                      f"{merge_outcome.get('detail', '')}")

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
                # Never a silent processed=0 no-op (the root cause of the silent-black-hole bug).
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
    print(json.dumps({"project_root": str(project_root), "queue": counts}, indent=2, sort_keys=True))
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
             "allowlist (orchestrator/ci/<project>-*) -- tasks created that way are always "
             "parked for human review, never auto-fired. A caller that IS one of those "
             "trusted identities (e.g. a per-project Talos/CTO dispatch) should pass its "
             "real identity here explicitly, e.g. --created-by <project>-cto, instead of "
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

    p = sub.add_parser("doctor")
    p.set_defaults(func=doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
