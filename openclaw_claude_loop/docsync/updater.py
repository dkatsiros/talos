"""Doc-updater subagent runner.

Reads a git diff + a project's known-doc excerpts, spawns `claude -p` with the
system prompt at `prompts/doc-updater.md`, parses back the JSON proposal block.

Phase 1: PROPOSE-only. No writes, no engine hook. See DESIGN.md.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# G9: allow-list of doc paths a proposal may reference. This is the *default*
# — a project's config may extend/replace it (`docsync.known_docs`).
PROJECT_DEFAULT_KNOWN_DOCS: tuple[str, ...] = (
    "context.md",
    "CLAUDE.md",
    "AGENTS.md",
    "AGENT.md",
    "README.md",
    "ARCHITECTURE.md",
    "ARCHITECTURE_RULES.md",
    "DECISIONS.md",
    "STATE.md",
    "CONTRACTS.md",
    "CONSTRAINTS.md",
    "CHANGELOG.md",
    "docs/**/*.md",
)

# G3: paths we treat as "doc-only / test-only / no-op" — a diff with ONLY these
# is skipped by the hook. NOT enforced in Phase-1 CLI (which always runs), but
# exposed for the future engine hook to consume.
DOC_ONLY_PATTERNS: tuple[str, ...] = (
    "*.md",
    "*.rst",
    "docs/**",
    "README*",
    "LICENSE*",
    "CHANGELOG*",
    "tests/**",
    "test_*.py",
    "*.test.*",
    "*_test.py",
)

DEFAULT_EXCERPT_LINES = 120
DEFAULT_MAX_DIFF_LOC = 500
DEFAULT_SUBAGENT_TIMEOUT = 300  # seconds


@dataclass
class Proposal:
    doc_path: str
    mode: str  # ADD | UPDATE | SUPERSEDE | NEW
    fence_id: str
    patch: str
    rationale: str
    confidence: float


@dataclass
class UpdaterResult:
    state: str  # completed | skipped | errored | truncated
    reason: Optional[str] = None
    proposals: list[Proposal] = field(default_factory=list)
    raw_output: str = ""
    diff_loc: int = 0
    diff_truncated: bool = False
    docs_seen: list[str] = field(default_factory=list)


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatchcase(path, pat):
            return True
        # Also match paths against basename patterns like "README*"
        if fnmatch.fnmatchcase(os.path.basename(path), pat):
            return True
    return False


def discover_known_docs(project_root: Path, allow_list: Iterable[str]) -> list[Path]:
    """Return existing doc files under project_root that match the allow-list.
    Recurses only under `docs/**` (guarded by the pattern) — root-level allowed
    names are matched directly."""
    hits: list[Path] = []
    seen: set[Path] = set()
    for pat in allow_list:
        if "**" in pat or "/" in pat:
            # Glob relative to project_root
            for p in project_root.glob(pat):
                if p.is_file():
                    rp = p.resolve()
                    if rp not in seen:
                        hits.append(p)
                        seen.add(rp)
        else:
            # Single filename at project root only.
            p = project_root / pat
            if p.is_file():
                rp = p.resolve()
                if rp not in seen:
                    hits.append(p)
                    seen.add(rp)
    return hits


def _read_excerpt(path: Path, max_lines: int = DEFAULT_EXCERPT_LINES) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as e:
        return f"[READ ERROR: {e}]"
    if len(lines) > max_lines:
        return "".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more lines truncated)\n"
    return "".join(lines)


def _load_git_diff(project_root: Path, git_ref: str) -> tuple[str, int, bool]:
    """Get the unified diff for `git_ref`. Returns (diff_text, loc, truncated=False).
    Truncation happens later, in `build_updater_input`."""
    git = shutil.which("git") or "/usr/bin/git"
    # Accept both "abc..def" and "abc" (means abc^..abc).
    ref_args: list[str]
    if ".." in git_ref:
        ref_args = [git_ref]
    else:
        ref_args = [f"{git_ref}^..{git_ref}"]
    cmd = [git, "-C", str(project_root), "diff", "--no-color", "--unified=3", *ref_args]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git diff failed for {git_ref}: {e.stderr.strip()}") from e
    loc = sum(1 for _ in out.splitlines())
    return out, loc, False


def _truncate_diff_by_loc(diff_text: str, max_loc: int) -> tuple[str, bool]:
    lines = diff_text.splitlines(keepends=True)
    if len(lines) <= max_loc:
        return diff_text, False
    kept = lines[:max_loc]
    kept.append(
        f"\n… ({len(lines) - max_loc} more diff lines truncated by G5 max_diff_loc cap)\n"
    )
    return "".join(kept), True


def build_updater_input(
    diff_text: str,
    project_root: Path,
    summary: str,
    known_docs: Iterable[Path],
    max_diff_loc: int = DEFAULT_MAX_DIFF_LOC,
    excerpt_lines: int = DEFAULT_EXCERPT_LINES,
) -> tuple[str, bool, list[str]]:
    """Return (user_message, diff_truncated, docs_paths_seen)."""
    trunc_diff, truncated = _truncate_diff_by_loc(diff_text, max_diff_loc)
    doc_blocks = []
    seen_paths: list[str] = []
    for doc in known_docs:
        rel = doc.relative_to(project_root)
        rel_str = str(rel)
        seen_paths.append(rel_str)
        excerpt = _read_excerpt(doc, excerpt_lines)
        doc_blocks.append(f"### `{rel_str}`\n\n```\n{excerpt}\n```\n")
    docs_section = "\n".join(doc_blocks) if doc_blocks else "_(no known docs found)_"
    trunc_note = " (TRUNCATED — see G5 cap)" if truncated else ""

    user_msg = textwrap.dedent(
        f"""\
        You are running post-task DocSync on a completed Talos code change.
        Follow your system prompt exactly.

        ## project_root
        `{project_root}`

        ## summary (CTO)
        {summary or "_(none provided)_"}

        ## docs (excerpts)
        {docs_section}

        ## diff{trunc_note}
        ```diff
        {trunc_diff}
        ```
        """
    )
    return user_msg, truncated, seen_paths


PROMPTS_DIR = Path(__file__).parent / "prompts"
DOC_UPDATER_SYSTEM_PROMPT = PROMPTS_DIR / "doc-updater.md"


def _find_claude_binary() -> str:
    for name in ("claude",):
        p = shutil.which(name)
        if p:
            return p
    return "claude"


def _spawn_updater_claude(
    system_prompt: str,
    user_msg: str,
    timeout: int = DEFAULT_SUBAGENT_TIMEOUT,
    model: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> tuple[str, int]:
    """Run `claude -p` non-interactively. Returns (stdout, returncode).
    On timeout, RuntimeError is raised so the hook can log ERRORED (§4 G11-adjacent).

    Tool use is disabled: the doc-updater is a pure reasoning task — it must
    produce its JSON proposal from the prompt input alone. No filesystem, no
    Bash, no MCP. This prevents the subagent from wandering off and modifying
    files itself (defence in depth against §4 G10/G7 escapes).
    """
    claude = _find_claude_binary()
    args = [
        claude, "-p",
        "--append-system-prompt", system_prompt,
        "--dangerously-skip-permissions",
        "--disallowed-tools",
        "Bash,Read,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task,BashOutput,KillBash,SlashCommand,Glob,Grep",
    ]
    if model:
        args += ["--model", model]
    if extra_args:
        args += list(extra_args)
    try:
        cp = subprocess.run(
            args,
            input=user_msg,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"doc-updater subagent timed out after {timeout}s") from e
    return cp.stdout or "", cp.returncode


_JSON_FENCE_RE = re.compile(
    r"```json\s*\n(?P<body>\{.*?\})\s*\n```", re.DOTALL | re.IGNORECASE
)


def _extract_proposals_json(raw: str) -> dict:
    """Find the last ```json {...} ``` fenced block. Fall back to whole-output
    parse if the model forgot the fence."""
    matches = list(_JSON_FENCE_RE.finditer(raw))
    if matches:
        body = matches[-1].group("body")
        return json.loads(body)
    # Fallback: try the last JSON object we can find.
    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    raise ValueError("no ```json … ``` fence in doc-updater output")


def _coerce_proposals(payload: dict) -> list[Proposal]:
    props = payload.get("proposals") or []
    if not isinstance(props, list):
        raise ValueError(f"proposals must be a list, got {type(props).__name__}")
    out: list[Proposal] = []
    for i, p in enumerate(props):
        if not isinstance(p, dict):
            raise ValueError(f"proposal[{i}] not an object")
        try:
            confidence = float(p.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        out.append(
            Proposal(
                doc_path=str(p.get("doc_path") or "").strip(),
                mode=str(p.get("mode") or "").strip().upper(),
                fence_id=str(p.get("fence_id") or "").strip(),
                patch=str(p.get("patch") or ""),
                rationale=str(p.get("rationale") or "").strip(),
                confidence=confidence,
            )
        )
    return out


def run_doc_updater(
    *,
    project_root: Path,
    diff_text: Optional[str] = None,
    git_ref: Optional[str] = None,
    summary: str = "",
    known_docs: Optional[Iterable[Path]] = None,
    known_doc_globs: Iterable[str] = PROJECT_DEFAULT_KNOWN_DOCS,
    max_diff_loc: int = DEFAULT_MAX_DIFF_LOC,
    timeout: int = DEFAULT_SUBAGENT_TIMEOUT,
    model: Optional[str] = None,
    dry_run_stub: Optional[str] = None,
) -> UpdaterResult:
    """Run the doc-updater subagent for a code diff.

    Exactly one of `diff_text` or `git_ref` must be provided.

    `dry_run_stub`, if set, short-circuits the claude spawn and returns the
    given raw output as if it came from the subagent — used by tests to avoid
    network cost.
    """
    project_root = project_root.resolve()
    if not project_root.is_dir():
        return UpdaterResult(state="errored", reason=f"project_root does not exist: {project_root}")
    if (diff_text is None) == (git_ref is None):
        return UpdaterResult(
            state="errored",
            reason="exactly one of diff_text or git_ref must be provided",
        )
    if git_ref is not None:
        try:
            diff_text, diff_loc, _ = _load_git_diff(project_root, git_ref)
        except RuntimeError as e:
            return UpdaterResult(state="errored", reason=str(e))
    else:
        diff_loc = sum(1 for _ in diff_text.splitlines())

    if not diff_text.strip():
        return UpdaterResult(state="skipped", reason="empty diff", diff_loc=0)

    docs = list(known_docs) if known_docs is not None else discover_known_docs(project_root, known_doc_globs)

    user_msg, truncated, seen = build_updater_input(
        diff_text=diff_text,
        project_root=project_root,
        summary=summary,
        known_docs=docs,
        max_diff_loc=max_diff_loc,
    )
    try:
        system_prompt = DOC_UPDATER_SYSTEM_PROMPT.read_text(encoding="utf-8")
    except OSError as e:
        return UpdaterResult(state="errored", reason=f"prompt read failed: {e}")

    if dry_run_stub is not None:
        raw = dry_run_stub
        rc = 0
    else:
        try:
            raw, rc = _spawn_updater_claude(
                system_prompt=system_prompt,
                user_msg=user_msg,
                timeout=timeout,
                model=model,
            )
        except RuntimeError as e:
            return UpdaterResult(
                state="errored",
                reason=str(e),
                diff_loc=diff_loc,
                diff_truncated=truncated,
                docs_seen=seen,
            )
    if rc != 0:
        return UpdaterResult(
            state="errored",
            reason=f"claude subagent exited {rc}",
            raw_output=raw,
            diff_loc=diff_loc,
            diff_truncated=truncated,
            docs_seen=seen,
        )
    try:
        payload = _extract_proposals_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return UpdaterResult(
            state="errored",
            reason=f"could not parse subagent JSON: {e}",
            raw_output=raw,
            diff_loc=diff_loc,
            diff_truncated=truncated,
            docs_seen=seen,
        )
    try:
        props = _coerce_proposals(payload)
    except ValueError as e:
        return UpdaterResult(
            state="errored",
            reason=f"bad proposal schema: {e}",
            raw_output=raw,
            diff_loc=diff_loc,
            diff_truncated=truncated,
            docs_seen=seen,
        )
    state = "completed" if not truncated else "truncated"
    return UpdaterResult(
        state=state,
        proposals=props,
        raw_output=raw,
        diff_loc=diff_loc,
        diff_truncated=truncated,
        docs_seen=seen,
    )
