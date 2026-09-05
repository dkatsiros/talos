"""Standalone DocSync CLI — Phase 1 only.

    python3 -m openclaw_claude_loop.docsync propose \\
        --project-root /path/to/project \\
        --diff-from <git_ref>            \\
        [--task-id my-task]              \\
        [--summary "one-liner CTO summary"] \\
        [--max-diff-loc 500]              \\
        [--model claude-haiku-4-5]        \\
        [--dry-run-stub <path>]            # for tests: read subagent output from file

The engine hook in `cli.py:complete_handoff` is NOT wired in Phase 1. This
module CLI drives the same code path so we can prove the doc-updater on real
diffs before touching the shared engine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .applier import (
    AUTO_COMMIT_MODE,
    DEFAULT_CONFIDENCE_THRESHOLD,
    PROPOSE_MODE,
    ApplyArtifactError,
    apply_proposals,
)
from .updater import DEFAULT_MAX_DIFF_LOC, PROJECT_DEFAULT_KNOWN_DOCS, run_doc_updater


def _sanitize_task_id(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", s.strip())
    return s.strip("-") or "docsync-manual"


def cmd_propose(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.is_dir():
        print(json.dumps({"error": f"project_root not found: {project_root}"}), file=sys.stderr)
        return 2

    dry_stub = None
    if args.dry_run_stub:
        p = Path(args.dry_run_stub).expanduser()
        dry_stub = p.read_text(encoding="utf-8")

    diff_text = None
    if args.diff_file:
        diff_text = Path(args.diff_file).expanduser().read_text(encoding="utf-8")

    ur = run_doc_updater(
        project_root=project_root,
        diff_text=diff_text,
        git_ref=args.diff_from,
        summary=args.summary or "",
        max_diff_loc=args.max_diff_loc,
        model=args.model,
        dry_run_stub=dry_stub,
    )

    task_id = _sanitize_task_id(args.task_id or (args.diff_from or "manual"))
    try:
        result = apply_proposals(
            project_root=project_root,
            task_id=task_id,
            proposals=ur.proposals,
            mode=args.mode,
            confidence_threshold=args.confidence_threshold,
        )
    except ApplyArtifactError as exc:
        # The decisions were made but could not be persisted. Report them on
        # stderr with a non-zero exit instead of dying on a traceback that shows
        # the operator nothing about what the run actually decided.
        print(
            json.dumps(
                {"error": str(exc), "applier": exc.result.summary()},
                indent=2, sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    out = {
        "updater": {
            "state": ur.state,
            "reason": ur.reason,
            "proposals_count": len(ur.proposals),
            "diff_loc": ur.diff_loc,
            "diff_truncated": ur.diff_truncated,
            "docs_seen": ur.docs_seen,
        },
        "applier": result.summary(),
    }
    if args.print_raw and ur.raw_output:
        out["raw_output"] = ur.raw_output
    print(json.dumps(out, indent=2, sort_keys=True))
    # Non-zero exit if the subagent errored, so a caller (or CI) can notice.
    return 0 if ur.state in ("completed", "truncated", "skipped") else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m openclaw_claude_loop.docsync")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="Run doc-updater on a diff, PROPOSE mode.")
    p.add_argument("--project-root", required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff-from", help="git ref (e.g. HEAD~1..HEAD or a single sha)")
    src.add_argument("--diff-file", help="path to a unified diff on disk")
    p.add_argument("--summary", default="", help="CTO summary of what the diff did (optional)")
    p.add_argument("--task-id", default=None,
                   help="artifact folder id under .openclaw/claude-loop/docsync/proposals/. "
                        "Defaults to sanitized --diff-from.")
    p.add_argument("--mode", choices=(PROPOSE_MODE, AUTO_COMMIT_MODE), default=PROPOSE_MODE,
                   help="Phase 1 keeps this at 'propose'.")
    p.add_argument("--max-diff-loc", type=int, default=DEFAULT_MAX_DIFF_LOC)
    p.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    p.add_argument("--model", default=None, help="Override claude --model (e.g. claude-haiku-4-5)")
    p.add_argument("--print-raw", action="store_true", help="Include raw subagent output in JSON.")
    p.add_argument("--dry-run-stub", default=None,
                   help="Test helper: read subagent output from this file instead of spawning claude.")
    p.set_defaults(func=cmd_propose)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
