"""Auto-delivery reconciler — Phase 0 (Fable design 2026-09-05).

ADDITIVE + OPT-IN. This module does NOT modify the live finalize/merge path in
``cli.py``. It reuses cli.py primitives (``project_lock``, ``merge_back_worktree``,
``read_json``, ``record_handoff_alert``, ``_git``, ``MERGE_OK_STATES``) by import
so there is exactly one source of truth for locking and merging.

Wiring the reconciler into the autorunner tick / an EOD cron is a *separate*
Dimitris-reviewed step (highest blast radius = the merge path). Until then this
runs standalone:

    python3 -m openclaw_claude_loop.reconciler --project-root <path>            # read-only proofs
    python3 -m openclaw_claude_loop.reconciler --project-root <path> --scan-crashes
    python3 -m openclaw_claude_loop.reconciler --project-root <path> --heal      # merges (gated)
    python3 -m openclaw_claude_loop.reconciler --project-root <path> --report    # delivery report

Core invariant (the single closing statement of the whole design):

    DELIVERED(task)  <=>  git merge-base --is-ancestor <task.tip_sha> <live_sha>

One git command proves both halves: the work is on the base branch (merge half)
and — when a ledger sha is supplied — the base is what's actually serving
(deploy half). This module computes that fact; it never *claims* it.

Failure posture: fail-loud, never silent-drop. Anything unmergeable, stranded,
stalled, or crashed becomes a durable ATTENTION entry + a handoff alert + an
append-only journal line. There is no code path that records "delivered"
without the is-ancestor check passing in the same run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from . import cli, resilience

# --------------------------------------------------------------------------- #
# Durable artifacts the reconciler owns (append-only journals + a derived index)
# --------------------------------------------------------------------------- #
RECONCILE_EVENTS_LOG = "reconcile-events.jsonl"   # under logs/, append-only
DELIVERY_REPORT_LOG = "delivery-report.jsonl"     # under logs/, append-only
ATTENTION_FILE = "ATTENTION.json"                 # under loop root, regenerable index

# A blocked task carrying a result.json older than this without being finalized
# means Pattern A (autorunner auto-finalize) has stalled — surface it.
FINALIZER_STALL_SECONDS = 2 * 60 * 60             # 2 hours (design A.3 R3)

DEFAULT_BASE = "main"


# --------------------------------------------------------------------------- #
# Git proof primitives
# --------------------------------------------------------------------------- #
def rev_parse(project_root: Path, ref: str) -> str | None:
    """Resolve ``ref`` to a full SHA, or None if it does not resolve."""
    out = cli._git(project_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = out.stdout.strip()
    return sha or None


def is_ancestor(project_root: Path, ancestor_sha: str | None, descendant_ref: str) -> bool:
    """True iff ``ancestor_sha`` is an ancestor of ``descendant_ref``.

    ``git merge-base --is-ancestor`` exits 0 (yes) / 1 (no) / other (error).
    A missing sha, an unresolvable descendant, or a git error all read as
    False — the caller then treats the task as NOT proven and escalates, which
    is the fail-loud default (we never infer delivery from an error).
    """
    if not ancestor_sha:
        return False
    if rev_parse(project_root, descendant_ref) is None:
        return False
    out = cli._git(project_root, "merge-base", "--is-ancestor", ancestor_sha, descendant_ref)
    return out.returncode == 0


def commits_ahead(project_root: Path, base: str, branch: str) -> int:
    """Number of commits on ``branch`` not reachable from ``base``; -1 on error."""
    out = cli._git(project_root, "rev-list", "--count", f"{base}..{branch}")
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


def compute_merge_proof(project_root: Path, branch: str, base: str = DEFAULT_BASE) -> dict[str, Any]:
    """The A.1 merge-proof primitive: capture tip/base SHAs for a task branch.

    This is the helper that ``merge_back_worktree`` should call at finalize time
    (deferred, Dimitris-gated). The reconciler also uses it to backfill proofs
    for legacy done-tasks that predate SHA recording.

    ``proof_quality`` is ``exact`` when the branch tip resolves, else
    ``inferred`` (we fall back to the base HEAD — a weaker proof, honestly
    labelled so R1 can treat it accordingly).
    """
    tip = rev_parse(project_root, branch)
    base_sha = rev_parse(project_root, base)
    if tip is not None:
        return {"tip_sha": tip, "base_sha": base_sha, "base": base,
                "branch": branch, "proof_quality": "exact"}
    # Branch gone (merged+deleted, or never committed): best-available proof.
    return {"tip_sha": base_sha, "base_sha": base_sha, "base": base,
            "branch": branch, "proof_quality": "inferred"}


def delivery_proof(project_root: Path, task_id: str, status: dict[str, Any],
                   base_default: str = DEFAULT_BASE) -> dict[str, Any]:
    """Prove (or fail to prove) that a task's commits are on its base branch.

    Returns a dict with ``proven`` (bool) plus the evidence. Precedence for the
    tip SHA:
      1. ``status.merge.tip_sha`` — recorded at finalize (the durable proof).
      2. ``status.merge.branch`` / ``talos/<id>`` still resolvable — recompute.
      3. Nothing resolvable — unproven, proof_quality ``none``.
    """
    merge = status.get("merge") if isinstance(status.get("merge"), dict) else {}
    base = merge.get("base") or base_default
    merge_state = merge.get("state")

    tip = merge.get("tip_sha")
    proof_quality = "recorded" if tip else "none"
    branch = merge.get("branch") or f"talos/{task_id}"

    if not tip:
        # No recorded proof — try to recompute from a still-present branch.
        recomputed = compute_merge_proof(project_root, branch, base)
        tip = recomputed["tip_sha"]
        proof_quality = recomputed["proof_quality"] if tip else "none"

    proven = bool(tip) and is_ancestor(project_root, tip, base)
    return {
        "task_id": task_id,
        "tip_sha": tip,
        "base": base,
        "branch": branch,
        "merge_state": merge_state,
        "proof_quality": proof_quality,
        "proven": proven,
    }


# --------------------------------------------------------------------------- #
# Journals / attention index (fail-loud, append-never-overwrite)
# --------------------------------------------------------------------------- #
def log_reconcile_event(root: Path, event: str, **fields: Any) -> None:
    """Append one line to logs/reconcile-events.jsonl. Best-effort, never raises."""
    record = {"ts": cli.utc_now(), "event": event}
    record.update(fields)
    resilience.append_jsonl(root / "logs" / RECONCILE_EVENTS_LOG, record)


def write_attention(root: Path, items: list[dict[str, Any]]) -> Path:
    """Rewrite the derived ATTENTION index (the only file this module overwrites).

    It is always regenerable from the journals, so overwriting is safe.
    """
    path = root / ATTENTION_FILE
    cli.write_json(path, {
        "generated_at": cli.utc_now(),
        "open_items": items,
        "count": len(items),
    })
    return path


# --------------------------------------------------------------------------- #
# Fail-loud crash detection (design item 6): a crashed builder != silence
# --------------------------------------------------------------------------- #
def _screen_alive_default(task_id: str) -> bool:
    screen_bin = cli.shutil.which("screen") or "screen"
    return cli.screen_session_alive(screen_bin, cli.screen_session_name(task_id))


def scan_crashed_sessions(
    root: Path,
    *,
    session_alive: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """SCREAM about any claimed task whose worker session died with no result.

    A spawn crash / RAM-kill leaves the token in queue/claimed/ with no
    result.json and a dead session. Without this, a crashed builder looks
    exactly like a slow one — silence. Each detection fires a
    ``session_crashed`` handoff alert (four-channel) and returns the finding.

    ``session_alive`` is injectable for testing; defaults to the real screen
    liveness check used by run-handoff.
    """
    alive = session_alive or _screen_alive_default
    findings: list[dict[str, Any]] = []
    claimed_dir = root / "queue" / "claimed"
    if not claimed_dir.is_dir():
        return findings
    for token in sorted(claimed_dir.glob("*.json")):
        task_id = token.stem
        task_dir = root / "tasks" / task_id
        if not task_dir.is_dir():
            continue
        # A result.json means the worker at least finished writing output — that
        # is Pattern A / finalizer territory (R3), not a crash.
        if (task_dir / "result.json").exists():
            continue
        # Already alerted? record_handoff_alert dedups on the alert file, but we
        # still re-scream (idempotent) so a persistent crash keeps surfacing.
        if alive(task_id):
            continue
        finding = {
            "task_id": task_id,
            "class": "session_crashed",
            "detail": "worker session is not alive and no result.json was written "
                      "— crash / RAM-kill / OOM; NOT silent progress",
        }
        cli.record_handoff_alert(
            root, task_dir, task_id,
            outcome="session_crashed",
            reason=finding["detail"],
            suggested_state="failed",
            recovery="inspect the session log, then re-dispatch or mark failed",
        )
        log_reconcile_event(root, "session_crashed", task_id=task_id)
        findings.append(finding)
    return findings


# --------------------------------------------------------------------------- #
# The reconciler pass (R1–R4)
# --------------------------------------------------------------------------- #
def _live_queue_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for state in ("pending", "claimed", "blocked"):
        d = root / "queue" / state
        if d.is_dir():
            ids.update(p.stem for p in d.glob("*.json"))
    return ids


def reconcile_project(
    root: Path,
    project_root: Path,
    *,
    heal: bool = False,
    base: str = DEFAULT_BASE,
    ledger_sha: str | None = None,
) -> dict[str, Any]:
    """Run R1–R4 for one project. Returns a structured report.

    heal=False (default): read-only. Proves the delivery invariant, lists what
    is unmerged/stranded/stalled, writes ATTENTION + journal. Merges NOTHING.
    heal=True: additionally re-runs merge_back_worktree (the merge_back_cmd
    heal primitive) for unmerged done-tasks, under the blocking merge lock.
    Healing is the highest-blast-radius action and is off by default.
    """
    delivered: list[dict[str, Any]] = []
    healed: list[dict[str, Any]] = []
    stuck: list[dict[str, Any]] = []
    stranded: list[dict[str, Any]] = []
    stalled: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []

    done_dir = root / "queue" / "done"
    done_ids: list[str] = sorted(p.stem for p in done_dir.glob("*.json")) if done_dir.is_dir() else []

    # --- R1: merge-ancestor invariant over queue/done/ --------------------- #
    for task_id in done_ids:
        task_dir = root / "tasks" / task_id
        status = cli.read_json(task_dir / "status.json", {}) or {}
        try:
            proof = delivery_proof(project_root, task_id, status, base_default=base)
        except Exception as exc:  # noqa: BLE001 — never let one task abort the sweep
            item = {"task_id": task_id, "class": "proof_error", "detail": repr(exc)[:300]}
            stuck.append(item)
            attention.append(item)
            log_reconcile_event(root, "proof_error", task_id=task_id, error=repr(exc)[:300])
            continue

        clean_state = proof["merge_state"] in cli.MERGE_OK_STATES or proof["merge_state"] is None
        if proof["proven"] and clean_state:
            delivered.append(proof)
            continue

        # Completed-but-unmerged (the interests-chips bug), caught mechanically.
        if not heal:
            item = {"task_id": task_id, "class": "unmerged",
                    "detail": f"done token but is-ancestor({proof['tip_sha']}, {base}) "
                              f"failed or merge_state={proof['merge_state']!r}",
                    "tip_sha": proof["tip_sha"], "base": base}
            stuck.append(item)
            attention.append(item)
            log_reconcile_event(root, "unmerged", task_id=task_id,
                                tip_sha=proof["tip_sha"], merge_state=proof["merge_state"])
            continue

        # heal=True: re-run the merge under the blocking merge lock.
        result = cli.read_json(task_dir / "result.json", {}) or {}
        with cli.project_lock(root, "merge", blocking=True):
            fresh_status = cli.read_json(task_dir / "status.json", {}) or {}
            outcome = cli.merge_back_worktree(project_root, task_id, fresh_status, result)
            # Enrich the recorded merge with a durable proof (A.1 backfill).
            enrich = compute_merge_proof(
                project_root, outcome.get("branch") or proof["branch"],
                outcome.get("base") or base)
            outcome.setdefault("tip_sha", enrich["tip_sha"])
            outcome.setdefault("base_sha", enrich["base_sha"])
            fresh_status["merge"] = outcome
            cli.write_json(task_dir / "status.json", fresh_status)

        reproof = delivery_proof(project_root, task_id,
                                 cli.read_json(task_dir / "status.json", {}) or {},
                                 base_default=base)
        if reproof["proven"] and outcome.get("state") in cli.MERGE_OK_STATES:
            healed.append({**reproof, "heal_outcome": outcome.get("state")})
            delivered.append(reproof)
            log_reconcile_event(root, "healed", task_id=task_id,
                                merge_state=outcome.get("state"))
            cli.record_handoff_alert(
                root, task_dir, task_id,
                outcome="reconciler_healed",
                reason=f"reconciler merged stranded {reproof['branch']} "
                       f"({outcome.get('state')})",
                suggested_state="done",
            )
        else:
            # Unmergeable — escalate, NEVER drop. Reappears every sweep until fixed.
            item = {"task_id": task_id, "class": "unmergeable",
                    "detail": f"heal failed: merge_state={outcome.get('state')!r} "
                              f"{outcome.get('detail', '')}"[:300],
                    "branch": reproof["branch"]}
            stuck.append(item)
            attention.append(item)
            cli.record_handoff_alert(
                root, task_dir, task_id,
                outcome="reconciler_unmergeable",
                reason=item["detail"],
                suggested_state="blocked",
            )
            log_reconcile_event(root, "unmergeable", task_id=task_id,
                                merge_state=outcome.get("state"))

    # --- R2: stranded talos/* branch sweep -------------------------------- #
    live_ids = _live_queue_ids(root)
    proven_ids = {d["task_id"] for d in delivered}
    branch_out = cli._git(project_root, "branch", "--list", "talos/*")
    for raw in branch_out.stdout.splitlines():
        branch = raw.strip().lstrip("* ").strip()
        if not branch:
            continue
        branch_task_id = branch[len("talos/"):]
        ahead = commits_ahead(project_root, base, branch)
        if ahead <= 0:
            continue  # nothing to lose; empty/merged branch
        if (branch_task_id in live_ids or branch_task_id in proven_ids
                or branch_task_id in done_ids):
            # A live task, an already-proven task, or a done-task already
            # handled by R1 (unmerged/unmergeable) owns it — not "stranded".
            continue
        item = {"task_id": branch_task_id, "class": "stranded_branch",
                "branch": branch, "commits_ahead": ahead,
                "detail": f"{branch} has {ahead} commit(s) ahead of {base} but no "
                          f"live queue token owns it; NOT auto-merged (unknown intent)"}
        stranded.append(item)
        attention.append(item)
        log_reconcile_event(root, "stranded_branch", branch=branch, commits_ahead=ahead)

    # --- R3: unfinalized completions in queue/blocked/ -------------------- #
    blocked_dir = root / "queue" / "blocked"
    if blocked_dir.is_dir():
        now = cli.time.time()
        for token in sorted(blocked_dir.glob("*.json")):
            task_id = token.stem
            result_path = root / "tasks" / task_id / "result.json"
            if not result_path.exists():
                continue
            age = now - result_path.stat().st_mtime
            if age <= FINALIZER_STALL_SECONDS:
                continue
            item = {"task_id": task_id, "class": "finalizer_stalled",
                    "detail": f"result.json is {int(age // 60)} min old and still "
                              f"blocked; autorunner Pattern A may be stalled"}
            stalled.append(item)
            attention.append(item)
            log_reconcile_event(root, "finalizer_stalled", task_id=task_id,
                                age_seconds=int(age))

    # --- R4: delivery invariant (merged -> live) -------------------------- #
    delivered_live: list[dict[str, Any]] = []
    if ledger_sha:
        for proof in delivered:
            live = is_ancestor(project_root, proof["tip_sha"], ledger_sha)
            entry = {**proof, "live": live, "ledger_sha": ledger_sha}
            if live:
                delivered_live.append(entry)
            else:
                item = {"task_id": proof["task_id"], "class": "merged_not_live",
                        "detail": f"proven on {base} but not an ancestor of the "
                                  f"live sha {ledger_sha[:12]}"}
                attention.append(item)
                log_reconcile_event(root, "merged_not_live", task_id=proof["task_id"])

    report = {
        "generated_at": cli.utc_now(),
        "project_root": str(project_root),
        "base": base,
        "ledger_sha": ledger_sha,
        "healed_enabled": heal,
        "counts": {
            "done_total": len(done_ids),
            "delivered": len(delivered),
            "delivered_live": len(delivered_live),
            "healed": len(healed),
            "stuck": len(stuck),
            "stranded": len(stranded),
            "stalled": len(stalled),
        },
        "delivered": delivered,
        "healed": healed,
        "stuck": stuck,
        "stranded": stranded,
        "stalled": stalled,
        "attention": attention,
    }
    write_attention(root, attention)
    return report


# --------------------------------------------------------------------------- #
# EOD delivery report + dead-man
# --------------------------------------------------------------------------- #
def render_delivery_report(report: dict[str, Any]) -> str:
    """One-glance operator summary. STUCK items always listed with a reason."""
    c = report["counts"]
    lines = [
        f"delivery report · {report['generated_at']}",
        f"done {c['done_total']} · DELIVERED {c['delivered']}"
        + (f" (live {c['delivered_live']})" if report.get("ledger_sha") else "")
        + f" · healed {c['healed']} · STUCK {c['stuck'] + c['stranded'] + c['stalled']}",
    ]
    for proof in report["delivered"]:
        tip = (proof.get("tip_sha") or "")[:12]
        lines.append(f"  ✅ {proof['task_id']} @ {tip}")
    for item in report["stuck"] + report["stranded"] + report["stalled"]:
        lines.append(f"  🔴 [{item['class']}] {item['task_id']}: {item['detail']}")
    if not (report["stuck"] or report["stranded"] or report["stalled"]):
        lines.append("  (nothing stuck)")
    return "\n".join(lines)


def write_delivery_report(root: Path, report: dict[str, Any]) -> Path:
    """Append the report to the append-only ledger. Its ABSENCE is the alarm.

    A dead-man sentinel (delivery_report_stale) checks this file's mtime; no
    fresh entry by the deadline means the reconciler is down.
    """
    resilience.append_jsonl(root / "logs" / DELIVERY_REPORT_LOG, {
        "ts": report["generated_at"],
        "counts": report["counts"],
        "stuck_ids": [i["task_id"] for i in report["stuck"]],
        "stranded_ids": [i["task_id"] for i in report["stranded"]],
        "stalled_ids": [i["task_id"] for i in report["stalled"]],
    })
    return root / "logs" / DELIVERY_REPORT_LOG


def delivery_report_stale(root: Path, max_age_seconds: float) -> bool:
    """Dead-man: True if the last delivery report is older than max_age (or absent).

    A missing / stale report means the reconciler itself stopped running — the
    system's silence made loud.
    """
    path = root / "logs" / DELIVERY_REPORT_LOG
    if not path.exists():
        return True
    return (cli.time.time() - path.stat().st_mtime) > max_age_seconds


# --------------------------------------------------------------------------- #
# CLI (standalone — NOT wired into cli.py's parser; wiring is a gated step)
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m openclaw_claude_loop.reconciler",
        description="Auto-delivery reconciler (Phase 0). Read-only unless --heal.",
    )
    p.add_argument("--project-root", default=".", help="project root to reconcile")
    p.add_argument("--base", default=DEFAULT_BASE, help="base branch (default: main)")
    p.add_argument("--ledger-sha", default=None,
                   help="live-deployed sha for the delivery (merged->live) check")
    p.add_argument("--heal", action="store_true",
                   help="re-merge stranded done-tasks (highest blast radius; off by default)")
    p.add_argument("--scan-crashes", action="store_true",
                   help="scream about claimed tasks whose session died with no result")
    p.add_argument("--report", action="store_true",
                   help="append an EOD delivery report to the ledger")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    root = cli.loop_root(project_root)
    if not (root / "config.json").exists():
        print(f"Not bootstrapped: {project_root}", file=sys.stderr)
        return 2

    if args.scan_crashes:
        crashes = scan_crashed_sessions(root)
        if crashes:
            print(f"CRASH: {len(crashes)} crashed session(s) surfaced", file=sys.stderr)

    report = reconcile_project(
        root, project_root,
        heal=args.heal, base=args.base, ledger_sha=args.ledger_sha,
    )
    if args.report:
        write_delivery_report(root, report)
    print(render_delivery_report(report))

    # Exit non-zero when anything needs a human — CI / cron can gate on it.
    open_items = (report["counts"]["stuck"] + report["counts"]["stranded"]
                  + report["counts"]["stalled"])
    return 1 if open_items else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
