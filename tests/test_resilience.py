"""Tests for the retry/backoff primitives and the hardened applier paths.

Every failure here is SIMULATED via injected faults — no real sleeping, no real
IO errors. The point of each test is the same: a transient fault must be
retried, and a permanent one must end up somewhere a human can see, never in a
silent drop.
"""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from openclaw_claude_loop import resilience
from openclaw_claude_loop.docsync import applier as applier_mod
from openclaw_claude_loop.docsync.applier import (
    AUTO_COMMIT_MODE,
    PROPOSE_MODE,
    ApplyArtifactError,
    apply_proposals,
)
from openclaw_claude_loop.docsync.updater import Proposal


class RecordingSleep:
    """Stand-in for time.sleep that records the delays instead of taking them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _oserror(code: int, message: str = "boom") -> OSError:
    return OSError(code, message)


# ----------------------------------------------------------------------------- #
# retry_call                                                                     #
# ----------------------------------------------------------------------------- #

def test_success_on_first_try_does_not_sleep():
    sleep = RecordingSleep()
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert resilience.retry_call(fn, sleep=sleep) == "ok"
    assert len(calls) == 1
    assert sleep.delays == []


def test_transient_failure_is_retried_then_succeeds():
    sleep = RecordingSleep()
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _oserror(errno.EBUSY, "device busy")
        return "recovered"

    retries = []
    result = resilience.retry_call(
        flaky,
        attempts=3,
        sleep=sleep,
        on_retry=lambda attempt, exc, delay: retries.append((attempt, delay)),
    )

    assert result == "recovered"
    assert attempts["n"] == 3
    # Exponential, deterministic: 0.5s then 1.0s.
    assert sleep.delays == [0.5, 1.0]
    assert retries == [(1, 0.5), (2, 1.0)]


def test_exhausted_retries_reraise_the_original_exception():
    sleep = RecordingSleep()
    original = _oserror(errno.EIO, "io error")

    def always_fails():
        raise original

    with pytest.raises(OSError) as excinfo:
        resilience.retry_call(always_fails, attempts=3, sleep=sleep)

    # The caller sees the REAL error, not a wrapper that hides the errno.
    assert excinfo.value is original
    assert len(sleep.delays) == 2  # 3 attempts -> 2 sleeps


def test_permanent_failure_fails_fast_without_retrying():
    sleep = RecordingSleep()
    calls = []

    def denied():
        calls.append(1)
        raise _oserror(errno.EACCES, "permission denied")

    with pytest.raises(OSError):
        resilience.retry_call(denied, attempts=5, sleep=sleep)

    # EACCES will not fix itself; burning the budget just delays the report.
    assert len(calls) == 1
    assert sleep.delays == []


def test_subprocess_failures_are_treated_as_transient():
    assert resilience.is_transient(subprocess.CalledProcessError(1, ["screen"]))
    assert resilience.is_transient(subprocess.TimeoutExpired(["ssh"], 5))
    assert resilience.is_transient(_oserror(errno.ECONNRESET))
    assert not resilience.is_transient(_oserror(errno.ENOENT))
    assert not resilience.is_transient(ValueError("not an io problem"))


def test_backoff_is_capped():
    assert resilience.backoff_delay(1, 0.5, 8.0) == 0.5
    assert resilience.backoff_delay(2, 0.5, 8.0) == 1.0
    assert resilience.backoff_delay(20, 0.5, 8.0) == 8.0


def test_describe_renders_subprocess_and_oserror():
    assert "exit 2" in resilience.describe(subprocess.CalledProcessError(2, ["screen", "-dmS"]))
    assert f"errno {errno.EBUSY}" in resilience.describe(_oserror(errno.EBUSY))


def test_append_jsonl_appends_and_never_raises(tmp_path):
    log = tmp_path / "nested" / "events.jsonl"
    assert resilience.append_jsonl(log, {"a": 1}) is True
    assert resilience.append_jsonl(log, {"a": 2}) is True
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["a"] for line in lines] == [1, 2]

    # An unwritable target reports False instead of taking the run down.
    blocked = tmp_path / "events.jsonl" / "impossible.jsonl"
    (tmp_path / "events.jsonl").write_text("i am a file", encoding="utf-8")
    assert resilience.append_jsonl(blocked, {"a": 3}) is False


# ----------------------------------------------------------------------------- #
# apply_proposals — the SPOF that used to lose a whole batch                     #
# ----------------------------------------------------------------------------- #

def _mkproject(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(
        "# CHANGELOG\n\n"
        "## Unreleased\n\n"
        "- prior line\n\n"
        "<!-- AUTO-BEGIN: docsync-unreleased -->\n"
        "OLD\n"
        "<!-- AUTO-END: docsync-unreleased -->\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# README\n\nHi.\n\n"
        "<!-- AUTO-BEGIN: docsync-readme -->\n"
        "OLD README\n"
        "<!-- AUTO-END: docsync-readme -->\n",
        encoding="utf-8",
    )
    return tmp_path


class FlakyWrite:
    """atomic_write wrapper that fails the first N calls for a target path."""

    def __init__(self, real, fail_paths: dict[str, int], exc: OSError) -> None:
        self.real = real
        self.fail_paths = dict(fail_paths)
        self.exc = exc
        self.calls: list[str] = []

    def __call__(self, path: str, content: str) -> None:
        self.calls.append(path)
        for needle, remaining in list(self.fail_paths.items()):
            if path.endswith(needle) and remaining != 0:
                if remaining > 0:
                    self.fail_paths[needle] = remaining - 1
                raise self.exc
        self.real(path, content)


def test_transient_doc_write_is_retried_instead_of_aborting(tmp_path, monkeypatch):
    """A doc write that blips once must land on the retry, not kill the batch."""
    project = _mkproject(tmp_path)
    flaky = FlakyWrite(
        applier_mod.atomic_write,
        {"CHANGELOG.md": 1},  # fail exactly once
        _oserror(errno.EBUSY, "device busy"),
    )
    monkeypatch.setattr(applier_mod, "atomic_write", flaky)
    monkeypatch.setattr(resilience.time, "sleep", lambda _d: None)

    result = apply_proposals(
        project_root=project,
        task_id="t-transient",
        proposals=[Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased",
                            "REPLACED", "r", 0.95)],
        mode=AUTO_COMMIT_MODE,
        confidence_threshold=0.8,
    )

    assert [d.action for d in result.decisions] == ["written"]
    assert "REPLACED" in (project / "CHANGELOG.md").read_text(encoding="utf-8")
    assert result.errors == []
    # Two attempts on the doc: the failure and the retry that stuck.
    assert flaky.calls.count(str(project / "CHANGELOG.md")) == 2


def test_persistent_doc_write_failure_is_surfaced_and_batch_continues(tmp_path, monkeypatch):
    """One unwritable doc must not cost us the other decisions or the artifact.

    Before: the raise escaped apply_proposals mid-loop and the artifact write
    (the last statement in the function) never ran — the entire batch vanished.
    """
    project = _mkproject(tmp_path)
    flaky = FlakyWrite(
        applier_mod.atomic_write,
        {"CHANGELOG.md": -1},  # always fail
        _oserror(errno.EIO, "io error"),
    )
    monkeypatch.setattr(applier_mod, "atomic_write", flaky)
    monkeypatch.setattr(resilience.time, "sleep", lambda _d: None)

    result = apply_proposals(
        project_root=project,
        task_id="t-persistent",
        proposals=[
            Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased", "DOOMED", "r", 0.95),
            Proposal("README.md", "UPDATE", "docsync-readme", "SURVIVES", "r", 0.95),
        ],
        mode=AUTO_COMMIT_MODE,
        confidence_threshold=0.8,
    )

    actions = [d.action for d in result.decisions]
    assert actions == ["failed-write", "written"]
    # The second proposal still landed.
    assert "SURVIVES" in (project / "README.md").read_text(encoding="utf-8")
    # The failure is reported, not buried in a "dropped" bucket.
    assert len(result.errors) == 1
    assert "CHANGELOG.md" in result.errors[0]
    assert "write failed" in result.errors[0]
    assert result.summary()["errors"] == result.errors
    assert result.summary()["counts"]["failed-write"] == 1
    # And the artifact — the durable trail — still exists.
    assert result.proposal_artifact_path is not None
    payload = json.loads(result.proposal_artifact_path.read_text(encoding="utf-8"))
    assert {p["action"] for p in payload["proposals"]} == {"failed-write", "written"}
    # Retries were actually attempted on the doomed doc (3 attempts).
    assert flaky.calls.count(str(project / "CHANGELOG.md")) == applier_mod.FS_ATTEMPTS


def test_artifact_write_failure_raises_typed_error_carrying_the_result(tmp_path, monkeypatch):
    """Losing the artifact is the one fault we refuse to swallow."""
    project = _mkproject(tmp_path)
    flaky = FlakyWrite(
        applier_mod.atomic_write,
        {"proposals.json": -1},
        _oserror(errno.EIO, "io error"),
    )
    monkeypatch.setattr(applier_mod, "atomic_write", flaky)
    monkeypatch.setattr(resilience.time, "sleep", lambda _d: None)

    with pytest.raises(ApplyArtifactError) as excinfo:
        apply_proposals(
            project_root=project,
            task_id="t-artifact",
            proposals=[Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased",
                                "BODY", "r", 0.95)],
            mode=PROPOSE_MODE,
        )

    # The decisions survive on the exception, so a caller can still report them.
    carried = excinfo.value.result
    assert [d.action for d in carried.decisions] == ["proposed"]
    assert any("artifact write failed" in e for e in carried.errors)

    # And a durable error line was written for whoever reads logs later.
    log = project / ".openclaw" / "claude-loop" / "logs" / "docsync-errors.jsonl"
    entries = resilience.tail_jsonl(log)
    assert len(entries) == 1
    assert entries[0]["task_id"] == "t-artifact"
    assert "io error" in entries[0]["error"]


def test_corrupt_prior_artifact_degrades_loudly_not_silently(tmp_path):
    """A corrupt artifact disables G12 idempotency — say so instead of hiding it."""
    project = _mkproject(tmp_path)
    artifact = (project / ".openclaw" / "claude-loop" / "docsync" / "proposals"
                / "t-corrupt" / "proposals.json")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{not json at all", encoding="utf-8")

    result = apply_proposals(
        project_root=project,
        task_id="t-corrupt",
        proposals=[Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased",
                            "BODY", "r", 0.95)],
        mode=PROPOSE_MODE,
    )

    # The run still completes (degrading is correct)...
    assert [d.action for d in result.decisions] == ["proposed"]
    # ...but the degradation is visible.
    assert len(result.warnings) == 1
    assert "idempotency degraded" in result.warnings[0]
    assert result.summary()["warnings"] == result.warnings


def test_clean_run_summary_has_no_warning_or_error_keys(tmp_path):
    """Backward compatibility: an untroubled run's summary is unchanged."""
    project = _mkproject(tmp_path)
    result = apply_proposals(
        project_root=project,
        task_id="t-clean",
        proposals=[Proposal("CHANGELOG.md", "UPDATE", "docsync-unreleased",
                            "BODY", "r", 0.95)],
        mode=PROPOSE_MODE,
    )
    summary = result.summary()
    assert "warnings" not in summary
    assert "errors" not in summary
    assert set(summary) == {"mode", "task_id", "proposals_dir", "counts",
                            "proposal_artifact_path"}
