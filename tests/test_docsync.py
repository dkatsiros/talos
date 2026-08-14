"""Unit tests for the DocSync Phase-1 module.

These tests never spawn `claude -p`; they exercise fence utils and the
applier's §4 guards using injected proposal payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openclaw_claude_loop.docsync.applier import (
    AUTO_COMMIT_MODE,
    PROPOSE_MODE,
    apply_proposals,
)
from openclaw_claude_loop.docsync.fence import (
    append_fresh_auto_region,
    atomic_write,
    content_hash,
    extract_auto_regions,
    extract_manual_regions,
    replace_auto_regions,
)
from openclaw_claude_loop.docsync.updater import (
    PROJECT_DEFAULT_KNOWN_DOCS,
    Proposal,
    UpdaterResult,
    build_updater_input,
    discover_known_docs,
    run_doc_updater,
)


# ----------------------------------------------------------------------------- #
# fence.py                                                                       #
# ----------------------------------------------------------------------------- #

def test_extract_auto_regions_only_auto_fences():
    src = (
        "# Hello\n\n"
        "<!-- MANUAL-BEGIN: prose -->\n"
        "Do not touch me.\n"
        "<!-- MANUAL-END: prose -->\n\n"
        "<!-- AUTO-BEGIN: nav -->\n"
        "- one\n- two\n"
        "<!-- AUTO-END: nav -->\n"
    )
    auto = extract_auto_regions(src)
    manual = extract_manual_regions(src)
    assert auto == {"nav": "- one\n- two"}
    assert manual == {"prose": "Do not touch me."}


def test_replace_auto_regions_leaves_prose_untouched():
    src = (
        "# H\n\n"
        "Prose here.\n\n"
        "<!-- AUTO-BEGIN: a -->\n"
        "OLD\n"
        "<!-- AUTO-END: a -->\n\n"
        "<!-- MANUAL-BEGIN: keep -->\n"
        "STAY\n"
        "<!-- MANUAL-END: keep -->\n"
    )
    out = replace_auto_regions(src, {"a": "NEW"})
    # AUTO body replaced
    assert "OLD" not in out
    assert "NEW" in out
    # Manual + prose intact
    assert "STAY" in out
    assert "Prose here." in out
    # Fence markers preserved exactly
    assert out.count("<!-- AUTO-BEGIN: a -->") == 1
    assert out.count("<!-- AUTO-END: a -->") == 1


def test_replace_auto_regions_skips_missing_fence():
    src = "# H\n\nNo fences.\n"
    out = replace_auto_regions(src, {"nonexistent": "X"})
    assert out == src  # Silently no-op — applier layer decides what to do


def test_append_fresh_auto_region_appends_at_end():
    src = "# H\n\nBody text.\n"
    out = append_fresh_auto_region(src, "nav", "- one")
    assert out.startswith(src.rstrip("\n"))
    assert "<!-- AUTO-BEGIN: nav -->" in out
    assert "<!-- AUTO-END: nav -->" in out
    # Prose untouched
    assert "Body text." in out
    # Fence body present
    assert "- one" in out


def test_append_fresh_auto_region_rejects_bad_id():
    with pytest.raises(ValueError):
        append_fresh_auto_region("x", "Bad Id With Spaces", "body")


def test_atomic_write_creates_and_replaces(tmp_path):
    p = tmp_path / "sub" / "file.md"
    atomic_write(str(p), "hello\n")
    assert p.read_text() == "hello\n"
    atomic_write(str(p), "again\n")
    assert p.read_text() == "again\n"


def test_content_hash_stable():
    a = content_hash("hello")
    b = content_hash("hello")
    c = content_hash("Hello")
    assert a == b
    assert a != c
    assert len(a) == 16


# ----------------------------------------------------------------------------- #
# updater.py                                                                     #
# ----------------------------------------------------------------------------- #

def test_discover_known_docs_root_and_globs(tmp_path):
    (tmp_path / "README.md").write_text("# r")
    (tmp_path / "context.md").write_text("# c")
    (tmp_path / "not-a-doc.txt").write_text("nope")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "API.md").write_text("# api")

    hits = discover_known_docs(tmp_path, PROJECT_DEFAULT_KNOWN_DOCS)
    names = sorted(str(p.relative_to(tmp_path)) for p in hits)
    assert "README.md" in names
    assert "context.md" in names
    assert any(n.startswith("docs/") and n.endswith("API.md") for n in names)
    assert "not-a-doc.txt" not in names


def test_build_updater_input_truncates_oversize_diff(tmp_path):
    (tmp_path / "README.md").write_text("# hello")
    docs = discover_known_docs(tmp_path, ("README.md",))
    huge_diff = "\n".join(f"+line {i}" for i in range(2000))
    msg, truncated, seen = build_updater_input(
        diff_text=huge_diff,
        project_root=tmp_path,
        summary="",
        known_docs=docs,
        max_diff_loc=100,
    )
    assert truncated is True
    assert "truncated by G5" in msg
    assert seen == ["README.md"]


def test_run_doc_updater_dry_stub_parses_proposals(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("# CHANGELOG\n\n## Unreleased\n\n- old\n")
    stub = (
        "Here are my proposals.\n\n"
        "```json\n"
        + json.dumps({
            "proposals": [
                {
                    "doc_path": "CHANGELOG.md",
                    "mode": "ADD",
                    "fence_id": "docsync-unreleased",
                    "patch": "- new entry from the diff",
                    "rationale": "test",
                    "confidence": 0.9,
                }
            ]
        })
        + "\n```\n"
    )
    ur = run_doc_updater(
        project_root=tmp_path,
        diff_text="--- a/x\n+++ b/x\n@@\n-a\n+b\n",
        summary="test summary",
        dry_run_stub=stub,
    )
    assert ur.state == "completed"
    assert len(ur.proposals) == 1
    assert ur.proposals[0].fence_id == "docsync-unreleased"
    assert ur.proposals[0].confidence == 0.9


def test_run_doc_updater_empty_diff_skips(tmp_path):
    ur = run_doc_updater(
        project_root=tmp_path,
        diff_text="   \n\n",
        dry_run_stub="```json\n{\"proposals\": []}\n```\n",
    )
    assert ur.state == "skipped"


def test_run_doc_updater_bad_json_errored(tmp_path):
    ur = run_doc_updater(
        project_root=tmp_path,
        diff_text="--- a\n+++ b\n",
        dry_run_stub="not json anywhere here",
    )
    assert ur.state == "errored"
    assert "could not parse" in (ur.reason or "").lower()


# ----------------------------------------------------------------------------- #
# applier.py — §4 guards                                                        #
# ----------------------------------------------------------------------------- #

def _mkproject(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(
        "# CHANGELOG\n\n"
        "## Unreleased\n\n"
        "- prior line\n\n"
        "<!-- AUTO-BEGIN: docsync-unreleased -->\n"
        "OLD\n"
        "<!-- AUTO-END: docsync-unreleased -->\n"
    )
    (tmp_path / "README.md").write_text("# README\n\nHi.\n")
    return tmp_path


def test_guard_g9_unknown_doc_dropped(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("secret/.env", "UPDATE", "docsync-x", "hax", "want to write env", 0.99)
    r = apply_proposals(project_root=project, task_id="t1", proposals=[p], mode=PROPOSE_MODE)
    assert r.decisions[0].action == "dropped-unknown-doc"


def test_guard_g10_out_of_scope_dropped(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("../outside.md", "UPDATE", "docsync-x", "x", "escapes root", 0.99)
    r = apply_proposals(project_root=project, task_id="t2", proposals=[p], mode=PROPOSE_MODE)
    # G9 catches "../*.md" first because ".." matches basename+glob; G10 is the
    # belt-and-braces. Either "dropped-unknown-doc" or "dropped-out-of-scope"
    # is a correct refusal — we just need it NOT written.
    assert r.decisions[0].action in {"dropped-unknown-doc", "dropped-out-of-scope"}


def test_guard_g14_confidence_downgrades_in_auto_commit(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased", "NEW BODY",
                 "low-conf", 0.5)
    r = apply_proposals(
        project_root=project, task_id="t3", proposals=[p],
        mode=AUTO_COMMIT_MODE, confidence_threshold=0.8,
    )
    assert r.decisions[0].action == "proposed"
    # Live doc UNCHANGED
    assert "OLD" in (project / "CHANGELOG.md").read_text()


def test_guard_g12_idempotency_second_run_is_dedup(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased", "SAME", "r", 0.9)
    r1 = apply_proposals(project_root=project, task_id="t4", proposals=[p], mode=PROPOSE_MODE)
    r2 = apply_proposals(project_root=project, task_id="t4", proposals=[p], mode=PROPOSE_MODE)
    assert r1.decisions[0].action == "proposed"
    assert r2.decisions[0].action == "dropped-idempotent"


def test_propose_mode_never_writes_docs(tmp_path):
    project = _mkproject(tmp_path)
    original = (project / "CHANGELOG.md").read_text()
    p = Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased", "PROPOSED_BODY",
                 "check", 0.95)
    r = apply_proposals(project_root=project, task_id="t5", proposals=[p], mode=PROPOSE_MODE)
    assert r.decisions[0].action == "proposed"
    assert (project / "CHANGELOG.md").read_text() == original
    # Artifact was written
    assert r.proposal_artifact_path is not None
    assert r.proposal_artifact_path.exists()
    payload = json.loads(r.proposal_artifact_path.read_text())
    assert payload["proposals"][0]["patch"] == "PROPOSED_BODY"


def test_auto_commit_writes_only_into_existing_fence(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased", "REPLACED",
                 "r", 0.95)
    r = apply_proposals(
        project_root=project, task_id="t6", proposals=[p], mode=AUTO_COMMIT_MODE,
        confidence_threshold=0.8,
    )
    assert r.decisions[0].action == "written"
    text = (project / "CHANGELOG.md").read_text()
    assert "REPLACED" in text
    assert "OLD" not in text
    # Manual/prose untouched
    assert "prior line" in text


def test_auto_commit_update_missing_fence_downgrades_to_propose(tmp_path):
    project = _mkproject(tmp_path)
    original = (project / "README.md").read_text()  # README has NO fences
    p = Proposal("README.md", "UPDATE", "docsync-doesnt-exist", "X", "r", 0.95)
    r = apply_proposals(
        project_root=project, task_id="t7", proposals=[p], mode=AUTO_COMMIT_MODE,
        confidence_threshold=0.8,
    )
    assert r.decisions[0].action == "proposed"
    # README untouched
    assert (project / "README.md").read_text() == original


def test_auto_commit_new_mode_never_creates_doc(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("docs/newthing.md", "NEW", "docsync-x", "", "propose new doc", 0.99)
    r = apply_proposals(
        project_root=project, task_id="t8", proposals=[p], mode=AUTO_COMMIT_MODE,
    )
    assert r.decisions[0].action == "proposed"
    assert not (project / "docs" / "newthing.md").exists()


def test_invalid_mode_dropped(tmp_path):
    project = _mkproject(tmp_path)
    p = Proposal("CHANGELOG.md", "REWRITE_EVERYTHING", "docsync-x", "x", "r", 0.99)
    r = apply_proposals(project_root=project, task_id="t9", proposals=[p], mode=PROPOSE_MODE)
    assert r.decisions[0].action == "dropped-invalid-mode"
