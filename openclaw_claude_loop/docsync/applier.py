"""Doc-updater applier — enforces §4 guards on proposal payloads.

Phase 1 exposes PROPOSE mode only (writes to the proposals artifact dir).
AUTO_COMMIT_MODE is defined but not yet wired into the CLI — Phase 2.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .fence import (
    AUTO_FENCE_RE,
    append_fresh_auto_region,
    atomic_write,
    content_hash,
    replace_auto_regions,
)
from .updater import PROJECT_DEFAULT_KNOWN_DOCS, Proposal

PROPOSE_MODE = "propose"
AUTO_COMMIT_MODE = "auto-commit"

# G14 default: proposals below this confidence never live-commit even in
# auto-commit mode; they always land as review artifacts.
DEFAULT_CONFIDENCE_THRESHOLD = 0.8

# G8: NEW-mode always downgrades to proposal, never applies.
_ALWAYS_PROPOSE_MODES = {"NEW"}


@dataclass
class ApplyDecision:
    proposal: Proposal
    action: str  # "written" | "proposed" | "dropped-unknown-doc" | "dropped-out-of-scope"
                 # | "dropped-low-confidence" | "dropped-idempotent" | "dropped-invalid-fence"
                 # | "dropped-doc-missing" | "dropped-invalid-mode"
    detail: str = ""
    written_path: Optional[Path] = None


@dataclass
class ApplyResult:
    mode: str
    task_id: str
    project_root: Path
    proposals_dir: Path
    decisions: list[ApplyDecision] = field(default_factory=list)
    proposal_artifact_path: Optional[Path] = None

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for d in self.decisions:
            counts[d.action] = counts.get(d.action, 0) + 1
        return {
            "mode": self.mode,
            "task_id": self.task_id,
            "proposals_dir": str(self.proposals_dir),
            "counts": counts,
            "proposal_artifact_path": str(self.proposal_artifact_path) if self.proposal_artifact_path else None,
        }


_VALID_MODES = {"ADD", "UPDATE", "SUPERSEDE", "NEW"}
_FENCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


def _glob_to_regex(pat: str) -> re.Pattern:
    """Tiny glob→regex helper that (unlike fnmatch) honours `**` as
    "zero or more path segments". Not a full glob implementation; enough for the
    allow-list patterns we ship (`docs/**/*.md`, etc.)."""
    parts = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                # `**` — match any number of chars including `/`. Consume `**`
                # and optionally the following `/` so `**/x` matches both
                # `x` (zero dirs) and `a/b/x`.
                i += 2
                if i < len(pat) and pat[i] == "/":
                    parts.append(r"(?:.*/)?")
                    i += 1
                else:
                    parts.append(r".*")
            else:
                parts.append(r"[^/]*")
                i += 1
        elif c == "?":
            parts.append(r"[^/]")
            i += 1
        elif c == ".":
            parts.append(r"\.")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _is_known_doc(rel_path: str, allow_list: Iterable[str]) -> bool:
    """G9: strict allow-list. Matches on the pattern directly (for globs) and
    on the basename (for filename-only entries)."""
    rel_norm = rel_path.replace("\\", "/")
    if rel_norm.startswith("/"):
        return False
    if ".." in Path(rel_norm).parts:
        return False
    for pat in allow_list:
        pat_norm = pat.replace("\\", "/")
        if "/" in pat_norm or "**" in pat_norm:
            if _glob_to_regex(pat_norm).match(rel_norm):
                return True
        else:
            if fnmatch.fnmatchcase(os.path.basename(rel_norm), pat_norm):
                return True
    return False


def _path_within(project_root: Path, rel_path: str) -> Optional[Path]:
    """G10: resolve `rel_path` under `project_root` and refuse anything that
    escapes via `..` or an absolute path."""
    if os.path.isabs(rel_path):
        return None
    if ".." in Path(rel_path).parts:
        return None
    resolved = (project_root / rel_path).resolve()
    root_resolved = project_root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return resolved


def _seen_hashes(project_root: Path, task_id: str) -> set[str]:
    """G12: read prior proposals for this task_id and return the set of patch
    content-hashes we've already emitted, so re-runs are idempotent."""
    seen: set[str] = set()
    artifact = _artifact_path(project_root, task_id)
    if artifact.exists():
        try:
            data = json.loads(artifact.read_text(encoding="utf-8"))
            for p in data.get("proposals") or []:
                h = p.get("hash")
                if h:
                    seen.add(h)
        except (OSError, json.JSONDecodeError):
            pass
    return seen


def _artifact_path(project_root: Path, task_id: str) -> Path:
    return project_root / ".openclaw" / "claude-loop" / "docsync" / "proposals" / task_id / "proposals.json"


def apply_proposals(
    *,
    project_root: Path,
    task_id: str,
    proposals: Iterable[Proposal],
    mode: str = PROPOSE_MODE,
    known_doc_globs: Iterable[str] = PROJECT_DEFAULT_KNOWN_DOCS,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> ApplyResult:
    """Apply a batch of proposals under §4 guards.

    Phase 1 semantics:
      - mode="propose"   → nothing is written into docs; all proposals go into
        the artifact under `.openclaw/claude-loop/docsync/proposals/<task_id>/`.
      - mode="auto-commit" → live-write into AUTO fences ONLY, subject to G7
        (fence-only), G12 (idempotency), G14 (confidence gate). NEW-mode always
        downgrades to proposal (G8).
    """
    project_root = project_root.resolve()
    proposals_dir = project_root / ".openclaw" / "claude-loop" / "docsync" / "proposals" / task_id
    result = ApplyResult(
        mode=mode,
        task_id=task_id,
        project_root=project_root,
        proposals_dir=proposals_dir,
    )
    seen = _seen_hashes(project_root, task_id)

    for p in proposals:
        # Basic validity
        if p.mode not in _VALID_MODES:
            result.decisions.append(ApplyDecision(p, "dropped-invalid-mode",
                                                   f"mode={p.mode!r}"))
            continue
        if not p.doc_path:
            result.decisions.append(ApplyDecision(p, "dropped-invalid-mode",
                                                   "empty doc_path"))
            continue

        # G9: known-doc allow-list
        if not _is_known_doc(p.doc_path, known_doc_globs):
            result.decisions.append(ApplyDecision(p, "dropped-unknown-doc",
                                                   f"{p.doc_path} not in allow-list"))
            continue

        # G10: per-project scope
        target = _path_within(project_root, p.doc_path)
        if target is None:
            result.decisions.append(ApplyDecision(p, "dropped-out-of-scope",
                                                   f"{p.doc_path} escapes project_root"))
            continue

        # G8: NEW is always a proposal
        force_propose = mode == PROPOSE_MODE or p.mode in _ALWAYS_PROPOSE_MODES

        # G12: content-hash idempotency
        h = content_hash(f"{p.doc_path}\0{p.fence_id}\0{p.patch}")
        if h in seen:
            result.decisions.append(ApplyDecision(p, "dropped-idempotent",
                                                   f"hash={h} already proposed"))
            continue
        seen.add(h)

        # G14: confidence gate (in auto-commit mode a low-confidence patch is
        # DEMOTED to proposal — still logged, never applied).
        if p.confidence < confidence_threshold and mode == AUTO_COMMIT_MODE:
            force_propose = True

        if force_propose:
            result.decisions.append(ApplyDecision(p, "proposed",
                                                   f"mode={mode}, force_propose=True, hash={h}"))
            continue

        # AUTO_COMMIT path (Phase 2 — kept safe here for tests + future wiring)
        if p.mode == "NEW":
            # Should have been caught above; belt & suspenders.
            result.decisions.append(ApplyDecision(p, "proposed", "NEW always downgraded"))
            continue

        if not _FENCE_ID_RE.match(p.fence_id):
            result.decisions.append(ApplyDecision(p, "dropped-invalid-fence",
                                                   f"fence_id={p.fence_id!r}"))
            continue

        if not target.exists():
            # G7: applier appends AT END of an existing file. Missing file →
            # can't safely create prose; drop and log so a human decides.
            result.decisions.append(ApplyDecision(p, "dropped-doc-missing",
                                                   f"{p.doc_path} does not exist"))
            continue

        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as e:
            result.decisions.append(ApplyDecision(p, "dropped-doc-missing", str(e)))
            continue

        # G7 core: only touch AUTO fences.
        if p.mode == "UPDATE" or p.mode == "SUPERSEDE":
            regions = {rid for rid, _ in AUTO_FENCE_RE.findall(existing)}
            if p.fence_id not in regions:
                # No matching fence to update → propose instead (never touch
                # human-authored prose).
                result.decisions.append(ApplyDecision(
                    p, "proposed",
                    f"UPDATE requested but fence '{p.fence_id}' not present; downgraded"))
                continue
            new_text = replace_auto_regions(existing, {p.fence_id: p.patch})
        else:  # ADD
            regions = {rid for rid, _ in AUTO_FENCE_RE.findall(existing)}
            if p.fence_id in regions:
                result.decisions.append(ApplyDecision(
                    p, "proposed",
                    f"ADD requested but fence '{p.fence_id}' already exists; downgraded"))
                continue
            new_text = append_fresh_auto_region(existing, p.fence_id, p.patch)

        atomic_write(str(target), new_text)
        result.decisions.append(ApplyDecision(
            p, "written", f"hash={h}, mode={p.mode}", written_path=target))

    # Persist the artifact (always, so an all-dropped run still leaves a trail).
    proposals_dir.mkdir(parents=True, exist_ok=True)
    artifact = _artifact_path(project_root, task_id)
    payload = {
        "task_id": task_id,
        "mode": mode,
        "confidence_threshold": confidence_threshold,
        "proposals": [
            {
                "doc_path": d.proposal.doc_path,
                "mode": d.proposal.mode,
                "fence_id": d.proposal.fence_id,
                "patch": d.proposal.patch,
                "rationale": d.proposal.rationale,
                "confidence": d.proposal.confidence,
                "action": d.action,
                "detail": d.detail,
                "hash": content_hash(
                    f"{d.proposal.doc_path}\0{d.proposal.fence_id}\0{d.proposal.patch}"
                ),
            }
            for d in result.decisions
        ],
    }
    atomic_write(str(artifact), json.dumps(payload, indent=2, sort_keys=True) + "\n")
    result.proposal_artifact_path = artifact
    return result
