"""LOOPS.md ingestion for the EOD reconcile sweep (Phase 1).

``LOOPS.md`` is Max's open-loops ledger (the "soft threads" he decomposes from
Dimitris's asks). The EOD reconciler reads it — read-only, it never edits the
file — and cross-references every OPEN loop against the delivery report so:

  * an open loop whose referenced task is STUCK/stranded is escalated (🔴), and
  * an open loop whose referenced task is already DELIVERED is flagged closeable.

This is how "nothing dies in the chat scrollback": a loop the human forgot to
close, but whose work actually landed, surfaces at EOD; a loop whose task is
wedged surfaces loudly instead of looking merely slow.

Parsing is intentionally forgiving: LOOPS.md is human-authored markdown. A loop
line is a list item (``1.`` / ``6a.`` / ``-`` / ``*``) carrying a status emoji.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Status emoji -> normalized state. Order matters only for first-match.
STATUS_EMOJI = {
    "✅": "done",
    "🔄": "in_flight",
    "⏳": "pending",
    "❌": "failed",
}

# Task ids in this system look like 20260905T135939Z<-slug>.
TASK_ID_RE = re.compile(r"\b(\d{8}T\d{6}Z[A-Za-z0-9_-]*)\b")
LIST_MARKER_RE = re.compile(r"^(\d+[a-z]?\.|[-*])\s+")


def parse_loops(text: str) -> list[dict[str, Any]]:
    """Parse LOOPS.md into a list of loop dicts.

    Each returned dict has: ``status`` (done/in_flight/pending/failed),
    ``title`` (the bolded label if present, else the trimmed line),
    ``task_ids`` (list of referenced task ids), and ``raw`` (trimmed source).
    Non-loop lines (headers, prose, blanks) are skipped.
    """
    loops: list[dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or not LIST_MARKER_RE.match(s):
            continue
        status = None
        for emoji, name in STATUS_EMOJI.items():
            if emoji in s:
                status = name
                break
        if status is None:
            continue
        body = LIST_MARKER_RE.sub("", s, count=1)
        m = re.search(r"\*\*(.+?)\*\*", body)
        title = (m.group(1) if m else body).strip()
        loops.append({
            "status": status,
            "title": title[:120],
            "task_ids": TASK_ID_RE.findall(s),
            "raw": s[:200],
        })
    return loops


def sweep_loops(loops_path: str | Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Cross-reference OPEN loops in ``loops_path`` against a reconciler report.

    Returns one finding per open (non-done) loop. Findings carry the loop fields
    plus ``note`` (why it surfaced) and ``stuck`` (True iff a referenced task is
    stuck/stranded/stalled in the reconciler — the loud case).
    """
    text = Path(loops_path).read_text(encoding="utf-8")
    loops = parse_loops(text)

    delivered_ids = {d.get("task_id") for d in report.get("delivered", [])}
    stuck_ids = {
        i.get("task_id")
        for i in (report.get("stuck", []) + report.get("stranded", [])
                  + report.get("stalled", []))
    }

    findings: list[dict[str, Any]] = []
    for loop in loops:
        if loop["status"] == "done":
            continue  # a closed loop needs no sweep
        referenced_stuck = [t for t in loop["task_ids"] if t in stuck_ids]
        referenced_delivered = [t for t in loop["task_ids"] if t in delivered_ids]
        if referenced_stuck:
            note = ("referenced task(s) "
                    + ", ".join(referenced_stuck)
                    + " are STUCK in the reconciler")
            stuck = True
        elif referenced_delivered:
            note = (f"marked {loop['status']} but task(s) "
                    + ", ".join(referenced_delivered)
                    + " are DELIVERED — loop is closeable")
            stuck = False
        elif loop["task_ids"]:
            note = ("referenced task(s) " + ", ".join(loop["task_ids"])
                    + " not seen by the reconciler (not yet done / different project)")
            stuck = False
        else:
            note = "open loop with no task id; verify by hand"
            stuck = False
        findings.append({**loop, "note": note, "stuck": stuck})
    return findings


def render_loops_sweep(findings: list[dict[str, Any]]) -> str:
    """One-glance operator summary of the open-loops sweep."""
    if not findings:
        return "loops sweep · (no open loops)"
    lines = [f"loops sweep · {len(findings)} open loop(s)"]
    for f in findings:
        icon = "🔴" if f.get("stuck") else "⏳"
        lines.append(f"  {icon} [{f['status']}] {f['title']}: {f['note']}")
    return "\n".join(lines)


def resolve_loops_path(project_root: str | Path, explicit: str | None = None) -> Path | None:
    """Locate LOOPS.md. Explicit path wins; else auto-discover upward.

    LOOPS.md typically lives at the workspace root (an ancestor of the project),
    so we walk up from ``project_root`` to the filesystem root and return the
    first ``LOOPS.md`` found. Returns None if none exists.
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    here = Path(project_root).expanduser().resolve()
    for base in (here, *here.parents):
        candidate = base / "LOOPS.md"
        if candidate.is_file():
            return candidate
    return None
