"""Retry + failure-visibility primitives for the handoff and DocSync paths.

A Graphify sweep (2026-08-22) flagged `cli.run_handoff` and
`docsync.applier.apply_proposals` as single points of failure: every fragile
step in both (screen spawn, ssh/rsync dispatch, doc write, artifact write) got
exactly ONE attempt, and a failure produced no durable signal. A transient blip
was therefore indistinguishable from "still working" — the task sat at
state=running until a human happened to look.

Two primitives fix that, and only that:

  retry_call()  — bounded exponential backoff for *transient* faults. It NEVER
                  swallows: the final exception is re-raised unchanged, and a
                  fault classified as non-transient is re-raised immediately
                  instead of burning the retry budget.
  append_jsonl() — best-effort durable event line. Returns False rather than
                  raising, because visibility must never itself become the
                  thing that crashes a run.
"""

from __future__ import annotations

import errno
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0

# errno values that mean "the system was busy / interrupted / the link
# hiccuped" — worth a second look. Everything else (EACCES, ENOENT, EROFS,
# ENOSPC, EISDIR...) is a hard fault: retrying only delays the error report and
# makes the log harder to read.
TRANSIENT_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EINTR,
        errno.EBUSY,
        errno.ETIMEDOUT,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOLCK,
        errno.ENOMEM,
        errno.EIO,
        errno.ESTALE,
        errno.EPIPE,
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENETDOWN,
    }
)


def is_transient(exc: BaseException) -> bool:
    """Classify a fault as worth retrying.

    `subprocess` failures count as transient: screen/ssh/rsync exit codes are
    opaque enough that we cannot tell a fork-pressure blip from a real refusal,
    and the retry budget is small and bounded. OSError is classified by errno,
    with an unknown/absent errno treated as permanent (fail fast, report loud).
    """
    if isinstance(exc, (subprocess.TimeoutExpired, subprocess.CalledProcessError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in TRANSIENT_ERRNOS
    return False


def backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> float:
    """Deterministic exponential backoff for attempt N (1-based).

    No jitter on purpose: these retries are per-task and serialized behind a
    per-project lock, so there is no thundering herd to spread out, and a
    deterministic schedule is testable.
    """
    if attempt < 1:
        attempt = 1
    return min(max_delay, base_delay * (2 ** (attempt - 1)))


def describe(exc: BaseException) -> str:
    """One-line, log-safe rendering of a failure."""
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(part) for part in cmd)
        return f"CalledProcessError: exit {exc.returncode} from {str(cmd)[:200]}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"TimeoutExpired: {exc.timeout}s"
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"{type(exc).__name__}[errno {exc.errno}]: {exc}"
    return f"{type(exc).__name__}: {exc}"


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    should_retry: Callable[[BaseException], bool] = is_transient,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call `fn`, retrying transient failures with exponential backoff.

    The successful return value is passed through untouched. The final failure
    is re-raised unchanged — this helper exists to avoid giving up on the first
    blip, NOT to hide errors. `on_retry(attempt, exc, delay)` fires before each
    sleep so callers can log the intermediate failures they would otherwise
    never see.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    # Resolved at CALL time, not as a default argument, so tests (and any
    # caller that wants a different clock) can patch time.sleep.
    sleeper = sleep if sleep is not None else time.sleep
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raised below, never swallowed
            if attempt >= attempts or not should_retry(exc):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleeper(delay)
    raise AssertionError("unreachable: retry_call exhausted without raising")


def append_jsonl(path: Path, record: dict[str, Any]) -> bool:
    """Append one JSON line. Best-effort: returns False instead of raising.

    Durable event logs are a *secondary* visibility channel — the primary ones
    (status.json, stderr) are written by the caller. If this fails we must not
    take the run down with it.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def tail_jsonl(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Read back the last `limit` well-formed records. Best-effort, never raises."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out
