"""Post-run DocSync hook — Phase 2 wiring for `cli.complete_handoff`.

Design: `projects/talos-docsync/DESIGN.md` §3 (attachment) and §4 (guards).
This hook implements the guards deferred by `__main__.py`'s CLI (which always
runs on demand): G1 opt-in, G2 success-only, G3 code-changed-only, G4
anti-recursion marker, G5 size cap, G11 kill-switch, G13 daily token cap.

Guards G6-G10, G12, G14 live inside `updater.py` and `applier.py`; this hook is
a thin, cheap gate that decides whether to invoke them at all.

CRITICAL invariant (Phase-2 safety requirement):
    When the hook is disabled (G11 kill-switch OR G1 opt-out — the default for
    every project), `maybe_run_docsync` returns None and performs NO filesystem
    side effects (no logs, no artifacts, no `docsync/` directory creation).
    `complete_handoff` uses that None to keep the printed payload byte-identical
    to pre-Phase-2 behavior. Any change here that adds side effects when
    disabled is a Phase-2 regression.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .applier import PROPOSE_MODE, apply_proposals
from .updater import DOC_ONLY_PATTERNS, run_doc_updater

DEFAULT_MAX_DIFF_LOC = 500
DEFAULT_DAILY_TOKEN_CAP = 200_000
DOCSYNC_MARKER = "_DOCSYNC_MARKER"


def _read_config(project_root: Path) -> dict:
    p = project_root / ".openclaw" / "claude-loop" / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _kill_switch_active(project_root: Path) -> bool:
    """G11: presence of either sentinel file makes the hook a no-op."""
    global_kill = Path.home() / ".openclaw" / "docsync.DISABLED"
    project_kill = project_root / ".openclaw" / "claude-loop" / "docsync.DISABLED"
    return global_kill.exists() or project_kill.exists()


def _has_docsync_marker_on_head(project_root: Path) -> bool:
    """G4: read HEAD's commit message; look for `_DOCSYNC_MARKER: true`."""
    git = shutil.which("git") or "/usr/bin/git"
    try:
        cp = subprocess.run(
            [git, "-C", str(project_root), "log", "-1", "--pretty=%B"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if cp.returncode != 0:
        return False
    return f"{DOCSYNC_MARKER}: true" in (cp.stdout or "")


def _git_changed_files_head(project_root: Path) -> list[str]:
    """A1 fallback: `git diff --name-only HEAD~1..HEAD`. Defensive on all errors."""
    git = shutil.which("git") or "/usr/bin/git"
    try:
        cp = subprocess.run(
            [git, "-C", str(project_root), "diff", "--name-only", "HEAD~1..HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    return [line.strip() for line in (cp.stdout or "").splitlines() if line.strip()]


def _git_diff_loc_head(project_root: Path) -> int:
    """Compute the LOC of `HEAD~1..HEAD` for the G5 size gate. Zero on error."""
    git = shutil.which("git") or "/usr/bin/git"
    try:
        cp = subprocess.run(
            [git, "-C", str(project_root), "diff", "--no-color", "--unified=3",
             "HEAD~1..HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if cp.returncode != 0:
        return 0
    return sum(1 for _ in (cp.stdout or "").splitlines())


def _filter_out_doc_only(paths: list[str],
                         patterns: tuple[str, ...] = DOC_ONLY_PATTERNS) -> list[str]:
    """G3: keep only paths that are NOT doc-only / test-only."""
    code = []
    for p in paths:
        norm = p.replace("\\", "/")
        base = os.path.basename(norm)
        if any(fnmatch.fnmatchcase(norm, pat) or fnmatch.fnmatchcase(base, pat)
               for pat in patterns):
            continue
        code.append(p)
    return code


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_path(project_root: Path) -> Path:
    return project_root / ".openclaw" / "claude-loop" / "docsync" / "budget.json"


def _read_budget(project_root: Path) -> dict:
    p = _budget_path(project_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_budget(project_root: Path, data: dict) -> None:
    p = _budget_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _spent_today(project_root: Path) -> int:
    return int(_read_budget(project_root).get(_today_utc(), 0) or 0)


def _add_spend(project_root: Path, tokens: int) -> None:
    b = _read_budget(project_root)
    day = _today_utc()
    b[day] = int(b.get(day, 0) or 0) + int(tokens)
    _write_budget(project_root, b)


def maybe_run_docsync(
    *,
    project_root: Path,
    root: Path,
    task_dir: Path,
    task: dict[str, Any],
    result: dict[str, Any],
    result_state: str,
) -> Optional[dict]:
    """Gate + (when enabled) invoke the doc-updater after a Talos completion.

    Returns None when the hook is disabled — so `complete_handoff`'s printed
    payload stays byte-identical to pre-Phase-2 behavior. Returns a compact
    dict in every enabled case (skipped/completed/errored) so the caller can
    attach it under `payload["docsync"]` for visibility.
    """
    # G11 first (cheapest, no config read): kill-switch = global no-op.
    if _kill_switch_active(project_root):
        return None

    cfg = _read_config(project_root).get("docsync") or {}

    # G1: hook only fires when a project explicitly opts in.
    if not cfg.get("enabled"):
        return None

    # From here on, the project is opted in — return dicts (never None) so the
    # operator sees what the hook did.

    # G2: never propose docs off a failed / blocked task.
    if result_state != "completed":
        return {"state": "skipped", "reason": "task_not_completed"}

    # G4: HEAD already carries the DocSync marker (we're being invoked from
    # a docsync commit itself) — bail out to break recursion.
    if _has_docsync_marker_on_head(project_root):
        return {"state": "skipped", "reason": "docsync_marker_on_head"}

    # G3: skip when the task's diff has no non-doc/non-test files.
    changes = result.get("changes") or {}
    reported: list[str] = []
    for k in ("files_created", "files_modified", "files_deleted"):
        v = changes.get(k) or []
        if isinstance(v, list):
            reported.extend(str(x) for x in v)
    changed = reported
    if not changed:
        # A1 fallback: contract only guarantees the lists exist, not that they
        # match reality. Cross-check via git.
        changed = _git_changed_files_head(project_root)
    code_files = _filter_out_doc_only(changed)
    if not code_files:
        return {"state": "skipped", "reason": "no_code_change"}

    # G13: daily token cap. If today is already over, skip cleanly.
    daily_cap = int(cfg.get("daily_token_cap") or DEFAULT_DAILY_TOKEN_CAP)
    spent = _spent_today(project_root)
    if spent >= daily_cap:
        return {"state": "skipped", "reason": "daily_cap_exceeded",
                "spent": spent, "cap": daily_cap}

    # G5: hard size ceiling. Above 10× max_diff_loc the doc-updater cannot do
    # anything useful; below, let its built-in per-run truncation handle it.
    max_diff_loc = int(cfg.get("max_diff_loc") or DEFAULT_MAX_DIFF_LOC)
    diff_loc = _git_diff_loc_head(project_root)
    if diff_loc > max_diff_loc * 10:
        return {"state": "skipped", "reason": "diff_too_large",
                "diff_loc": diff_loc, "cap": max_diff_loc}

    # All gates passed. Invoke updater + applier.
    task_id = str(task.get("id") or task.get("task_id") or task_dir.name)
    mode = cfg.get("mode") or PROPOSE_MODE
    summary = str(result.get("summary") or "")

    ur = run_doc_updater(
        project_root=project_root,
        git_ref="HEAD~1..HEAD",
        summary=summary,
        max_diff_loc=max_diff_loc,
    )

    # Best-effort token accounting for G13 (chars/4 approximation — the exact
    # count doesn't matter for a daily bookkeeping cap).
    if ur.raw_output:
        try:
            _add_spend(project_root, max(1, len(ur.raw_output) // 4))
        except OSError:
            pass

    if ur.state == "errored":
        return {"state": "errored", "reason": ur.reason or "updater errored",
                "diff_loc": ur.diff_loc}

    if not ur.proposals:
        return {"state": "skipped", "reason": "no_proposals",
                "diff_loc": ur.diff_loc}

    ar = apply_proposals(
        project_root=project_root,
        task_id=task_id,
        proposals=ur.proposals,
        mode=mode,
    )

    counts = ar.summary().get("counts", {})
    return {
        "state": "completed",
        "mode": mode,
        "proposals": len(ur.proposals),
        "written": int(counts.get("written", 0)),
        "proposed": int(counts.get("proposed", 0)),
        "dropped": sum(v for k, v in counts.items()
                       if k.startswith("dropped-")),
        "artifact": str(ar.proposal_artifact_path) if ar.proposal_artifact_path else None,
        "diff_loc": ur.diff_loc,
    }
