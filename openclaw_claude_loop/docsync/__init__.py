"""Talos DocSync — post-run doc updater.

Design: `projects/talos-docsync/DESIGN.md`.
Phase 1 shipped the doc-updater core + standalone CLI.
Phase 2 wires the `maybe_run_docsync` hook into `cli.py:complete_handoff`,
default-OFF everywhere (per-project opt-in via `docsync.enabled: true`).
"""

from .fence import AUTO_FENCE_RE, MANUAL_FENCE_RE, extract_auto_regions, replace_auto_regions
from .applier import (
    PROPOSE_MODE,
    AUTO_COMMIT_MODE,
    ApplyArtifactError,
    ApplyResult,
    apply_proposals,
)
from .hook import DOCSYNC_MARKER, maybe_run_docsync
from .updater import PROJECT_DEFAULT_KNOWN_DOCS, run_doc_updater

__all__ = [
    "AUTO_FENCE_RE",
    "MANUAL_FENCE_RE",
    "extract_auto_regions",
    "replace_auto_regions",
    "PROPOSE_MODE",
    "AUTO_COMMIT_MODE",
    "ApplyArtifactError",
    "ApplyResult",
    "apply_proposals",
    "DOCSYNC_MARKER",
    "maybe_run_docsync",
    "PROJECT_DEFAULT_KNOWN_DOCS",
    "run_doc_updater",
]
