"""Commit -> auto-redeploy -> verify-live watcher (Phase 1, dev).

Watches a base branch tip. When it advances past the last-deployed sha this
runs the project's deploy command, then a verify command, then records the new
live sha. It is the dev half of the delivery invariant: the reconciler proves
``merged``; this watcher makes ``merged -> live`` actually happen and records the
ledger sha the reconciler's R4 check reads.

Failure posture (matches the reconciler): fail-loud, never silent. A failed
deploy or a failed verify is written to the append-only journal and the live
sha is NOT advanced, so the next tick retries — a broken deploy can never be
mistaken for a delivered one. Deploys are serialized under the per-project
'deploy' lock so two ticks cannot deploy concurrently.

``run`` is injectable (argv -> (returncode, output)) so the whole flow is
testable without a real git repo or a real deploy.
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import cli, resilience

DEPLOY_STATE = "deploy-watch.state.json"          # under loop root
DEPLOY_LOG = "deploy-watch.jsonl"                  # under logs/, append-only

Runner = Callable[[list[str]], "tuple[int, str]"]

# Actions that mean "the tick did the right thing" (CLI exits 0 on these).
OK_ACTIONS = {"deployed", "noop"}


def _default_run(argv: list[str]) -> "tuple[int, str]":
    p = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=600)
    return p.returncode, (p.stdout + p.stderr)


def resolve_tip(project_root: Path, ref: str, run: Runner | None = None) -> str | None:
    """Resolve ``ref`` to a full commit sha via git, or None on failure."""
    run = run or _default_run
    rc, out = run([
        "git", "-C", str(project_root),
        "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
    ])
    sha = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return sha if rc == 0 and sha else None


def _lock(root: Path, use_lock: bool):
    if not use_lock:
        return contextlib.nullcontext()
    return cli.project_lock(root, "deploy", blocking=True)


def _record(root: Path, result: dict[str, Any]) -> None:
    resilience.append_jsonl(root / "logs" / DEPLOY_LOG, result)


def watch_once(
    root: Path,
    project_root: Path,
    *,
    base: str,
    deploy_cmd: list[str],
    verify_cmd: list[str] | None = None,
    run: Runner | None = None,
    ts: str | None = None,
    use_lock: bool = True,
) -> dict[str, Any]:
    """Run one watch tick. Returns a result dict (also appended to the journal).

    ``action`` is one of: ``noop`` (live already at base tip), ``deployed``
    (deploy + verify clean, live sha advanced), ``deploy_failed``,
    ``verify_failed``, or ``error`` (base tip unresolvable). Only ``deployed``
    advances the recorded live sha.
    """
    run = run or _default_run
    ts = ts or cli.utc_now()
    root = Path(root)
    project_root = Path(project_root)
    state_path = root / DEPLOY_STATE
    state = cli.read_json(state_path, {}) or {}
    last_deployed = state.get("deployed_sha")

    tip = resolve_tip(project_root, base, run=run)
    result: dict[str, Any] = {
        "ts": ts, "base": base, "tip": tip, "last_deployed": last_deployed,
    }

    if not tip:
        result["action"] = "error"
        result["detail"] = f"cannot resolve base tip for {base!r}"
        _record(root, result)
        return result

    if last_deployed == tip:
        result["action"] = "noop"
        result["detail"] = f"live already at {tip[:12]}"
        _record(root, result)
        return result

    # New commit(s) on base: deploy, serialized under the deploy lock.
    with _lock(root, use_lock):
        drc, dout = run(list(deploy_cmd))
        result["deploy_rc"] = drc
        result["deploy_output"] = dout.strip()[-800:]
        if drc != 0:
            result["action"] = "deploy_failed"
            result["detail"] = "deploy command exited non-zero; live sha NOT advanced"
            _record(root, result)
            return result

        if verify_cmd:
            vrc, vout = run(list(verify_cmd))
            result["verify_rc"] = vrc
            result["verify_output"] = vout.strip()[-800:]
            if vrc != 0:
                result["action"] = "verify_failed"
                result["detail"] = ("verify command exited non-zero after deploy; "
                                    "live sha NOT advanced (fail-loud)")
                _record(root, result)
                return result

        state["deployed_sha"] = tip
        state["deployed_at"] = ts
        state["base"] = base
        cli.write_json(state_path, state)

    result["action"] = "deployed"
    result["detail"] = f"deployed + verified {tip[:12]}"
    _record(root, result)
    return result
