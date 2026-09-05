"""AUTO/MANUAL fence machinery — same convention as Atlas project-skills.

Prior art: `scripts/project-skills/regen.py` (fence regex, replace_auto_regions).
We DO NOT import that module because Atlas lives in the workspace repo, not the
claude-loop-module distribution. We keep the same convention so a doc fenced by
Atlas is round-tripped by DocSync (and vice versa).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

MANUAL_FENCE_RE = re.compile(
    r"<!--\s*MANUAL-BEGIN:\s*([a-z0-9\-]+)\s*-->\n(.*?)\n<!--\s*MANUAL-END:\s*\1\s*-->",
    re.DOTALL,
)
AUTO_FENCE_RE = re.compile(
    r"<!--\s*AUTO-BEGIN:\s*([a-z0-9\-]+)\s*-->\n(.*?)\n<!--\s*AUTO-END:\s*\1\s*-->",
    re.DOTALL,
)


def extract_auto_regions(text: str) -> dict[str, str]:
    return {rid: body.strip() for rid, body in AUTO_FENCE_RE.findall(text)}


def extract_manual_regions(text: str) -> dict[str, str]:
    return {rid: body.strip() for rid, body in MANUAL_FENCE_RE.findall(text)}


def replace_auto_regions(text: str, new_regions: dict[str, str]) -> str:
    """Edit ONLY existing AUTO fences in-place. Regions with no matching fence in
    `text` are silently skipped by this helper — caller is responsible for
    calling `append_fresh_auto_region` when a fresh fence is needed."""
    out = text
    for rid, body in new_regions.items():
        pat = re.compile(
            r"(<!--\s*AUTO-BEGIN:\s*" + re.escape(rid) + r"\s*-->\n).*?"
            r"(\n<!--\s*AUTO-END:\s*" + re.escape(rid) + r"\s*-->)",
            re.DOTALL,
        )
        if pat.search(out):
            out = pat.sub(lambda m: m.group(1) + body.rstrip() + m.group(2), out)
    return out


def append_fresh_auto_region(text: str, region_id: str, body: str) -> str:
    """G7: on a first-time doc with no fences yet, the applier writes a fresh
    AUTO fence AT THE END, never in the middle. This never modifies existing
    prose."""
    if not re.match(r"^[a-z0-9\-]+$", region_id):
        raise ValueError(f"invalid fence id: {region_id!r}")
    block = (
        f"\n\n<!-- AUTO-BEGIN: {region_id} -->\n"
        f"{body.rstrip()}\n"
        f"<!-- AUTO-END: {region_id} -->\n"
    )
    # Trim a trailing newline to avoid triple-blank-line gap.
    if text.endswith("\n"):
        return text + block.lstrip("\n")
    return text + block


def content_hash(text: str) -> str:
    """G12 idempotency helper: content-hash of a proposed edit body."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def atomic_write(path: str, content: str) -> None:
    """Write `content` to `path` atomically (temp file + os.replace).
    Mirrors Atlas's safety pattern: partial writes are never visible."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".docsync-",
        suffix=".tmp",
        dir=d,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
