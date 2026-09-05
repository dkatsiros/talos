"""SSH-remote execution helpers for the shuttle-offload path.

Implements Path C from projects/shuttle-offload/ARCHITECTURE.md: keep the whole
claude-loop file queue on AWS, but run the `screen -dmS ... claude -p ...`
execution step on the Shuttle home node via SSH, with rsync mirroring the task
folder before the run and pulling results back while polling.

Design invariants (see DECISIONS.md; updated 2026-09-05, fleet plan step 1.2):
- Default execution host is the SHUTTLE (resolve_execution_host). Precedence:
  task.execution_host > project config.json "default_execution_host" >
  DEFAULT_EXECUTION_HOST ("shuttle", env TALOS_DEFAULT_EXECUTION_HOST).
  AWS-local is an explicit opt-in — the 3.7GB AWS head must not build.
- A Shuttle run needs a repo mapping (shuttle_project_root): task.shuttle.
  project_root > config.json execution_hosts.shuttle.project_root. Without it the
  caller FAILS LOUD (handoff alert + SystemExit); it never runs locally instead.
- Preflight or dispatch failure also FAILS LOUD. There is no silent local
  fallback any more; log_fallback() still records the event for the watchers.
- The builder runs INSIDE the mirrored repo (cwd = project_root on the Shuttle,
  so --setting-sources user,project loads the repo's own CLAUDE.md/.claude) and
  writes task files into the rsynced task mirror dir.
- Only Dimitris's user runs agents (ssh target dimitris@...), agents write under
  /home/dimitris/. No new inbound ports, no LAN probing (enforced in the role
  prompt, ADR-005).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Defaults mirror the config shape in ARCHITECTURE.md § "Config shape". A task
# may override any of these via a top-level "shuttle" object in task.json.
# The SSH target/mirror root default to the live Shuttle node so the autorunner
# cron (which sets no TALOS_* env) routes shuttle tasks to the Shuttle rather
# than silently falling back to local — but both stay env-overridable so a
# public checkout can be re-pointed without a code edit.
DEFAULT_SSH_TARGET = os.environ.get("TALOS_SSH_TARGET", "dimitris@100.98.174.24")
DEFAULT_MIRROR_ROOT = os.environ.get("TALOS_MIRROR_ROOT", "/home/dimitris/.claude-loop-tasks")
DEFAULT_PREFLIGHT_TIMEOUT_S = 3
# Talos default model, mirrored from the local screen path in cli.run_handoff
# (spawn_env.setdefault). SSH does not forward arbitrary env vars, so the remote
# command must carry the model explicitly or the Shuttle falls back to whatever
# its own shell profile sets. Opus 4-8 is the deliberate default (Opus 5 = #1
# token burner; see the Aug-2026 rollback). An explicit ANTHROPIC_MODEL /
# task.json["model"] override still wins.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

# Where a task runs when nobody says otherwise. "shuttle" since 2026-09-05
# (fleet plan 1.2): the AWS head is a 3.7GB VM that has OOM'd under builds.
# Env-overridable so a public checkout / CI can pin "local".
VALID_EXECUTION_HOSTS = ("local", "shuttle", "shuttle-sandbox")
DEFAULT_EXECUTION_HOST = os.environ.get("TALOS_DEFAULT_EXECUTION_HOST", "shuttle").strip().lower() or "shuttle"


def resolve_execution_host(task: Mapping[str, Any] | None, project_config: Mapping[str, Any] | None = None) -> str:
    """Where this task's claude session runs.

    Precedence: task["execution_host"] > project config.json["default_execution_host"]
    > DEFAULT_EXECUTION_HOST. Unknown values raise so a typo cannot silently
    become a local run on the AWS head.
    """
    candidates = (
        (task or {}).get("execution_host"),
        (project_config or {}).get("default_execution_host"),
        DEFAULT_EXECUTION_HOST,
    )
    for value in candidates:
        if value is None or str(value).strip() == "":
            continue
        host = str(value).strip().lower()
        if host not in VALID_EXECUTION_HOSTS:
            raise ValueError(
                f"Invalid execution_host {value!r}: expected one of {VALID_EXECUTION_HOSTS}"
            )
        return host
    return "shuttle"


def shuttle_project_root(task: Mapping[str, Any] | None, project_config: Mapping[str, Any] | None = None) -> str | None:
    """Absolute path of the project's repo mirror on the Shuttle, or None.

    Precedence: task["shuttle"]["project_root"] > config.json
    ["execution_hosts"]["shuttle"]["project_root"]. Validated as an absolute
    path (same option-injection guard as mirror_root).
    """
    task_shuttle = (task or {}).get("shuttle")
    cfg_hosts = (project_config or {}).get("execution_hosts")
    cfg_shuttle = cfg_hosts.get("shuttle") if isinstance(cfg_hosts, Mapping) else None
    for source in (task_shuttle, cfg_shuttle):
        if isinstance(source, Mapping):
            value = source.get("project_root")
            if isinstance(value, str) and value.strip():
                value = value.strip()
                if not value.startswith("/"):
                    raise ValueError(
                        f"Invalid shuttle project_root {value!r}: must be an absolute path"
                    )
                return value.rstrip("/") or "/"
    return None

# Files that make up the task mirror. Everything else in the task dir is either
# regenerable or a result we pull back, so we keep the push minimal.
RSYNC_INCLUDES = ("prompt.md", "handoff-prompt.txt", "task.json", "status.json")
RSYNC_EXCLUDES = ("__pycache__", "*.pyc")
# Files produced on the remote that we pull back while polling.
RSYNC_PULL_BACK = ("status.json", "result.json", "claude-session.log")


_SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$")


def _validate_remote_config(ssh_target: str, mirror_root: str) -> None:
    """Reject values that could inject SSH/rsync options.

    A ssh_target that starts with '-' (or doesn't match user@host) becomes an
    SSH flag on the command line. A mirror_root that is relative or starts with
    '-' becomes an rsync option. Both are rejected with a clear error. (Ported
    from the public talos-export hardening — Fix F1 — so the option-injection
    guard survives the engine reconciliation.)
    """
    if not _SSH_TARGET_RE.match(ssh_target):
        raise ValueError(
            f"Invalid ssh_target {ssh_target!r}: must match user@hostname "
            "(alphanumeric, dots, hyphens, underscores only). "
            "Values starting with '-' are rejected to prevent option injection."
        )
    if not mirror_root.startswith("/"):
        raise ValueError(
            f"Invalid mirror_root {mirror_root!r}: must be an absolute path "
            "(a relative path or one starting with '-' would be interpreted as "
            "an rsync option)."
        )


def shuttle_config(task: dict[str, Any]) -> dict[str, Any]:
    """Resolve the shuttle connection config from the task envelope.

    A task may override any field under task["shuttle"]. ssh_target and
    mirror_root are validated to prevent option-injection via SSH/rsync.
    """
    cfg = task.get("shuttle") if isinstance(task.get("shuttle"), dict) else {}
    result = {
        "ssh_target": cfg.get("ssh_target", DEFAULT_SSH_TARGET),
        "mirror_root": cfg.get("mirror_root", DEFAULT_MIRROR_ROOT),
        "preflight_timeout_s": int(cfg.get("preflight_timeout_s", DEFAULT_PREFLIGHT_TIMEOUT_S)),
    }
    _validate_remote_config(result["ssh_target"], result["mirror_root"])
    return result


def _ssh_base(ssh_target: str, *, connect_timeout: int | None = None) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes"]
    if connect_timeout is not None:
        argv += ["-o", f"ConnectTimeout={connect_timeout}"]
    argv.append(ssh_target)
    return argv


def preflight(ssh_target: str, timeout_s: int = DEFAULT_PREFLIGHT_TIMEOUT_S) -> tuple[bool, int, str]:
    """Health-check the Shuttle before dispatch.

    Returns (ok, latency_ms, reason). ok is False on any non-zero exit,
    timeout, or ssh error — the caller then falls back to local.
    """
    argv = _ssh_base(ssh_target, connect_timeout=timeout_s) + ["true"]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s + 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        latency_ms = int((time.monotonic() - start) * 1000)
        return False, latency_ms, f"preflight timed out after {timeout_s}s"
    except OSError as exc:  # ssh binary missing, etc.
        latency_ms = int((time.monotonic() - start) * 1000)
        return False, latency_ms, f"preflight ssh error: {exc}"
    latency_ms = int((time.monotonic() - start) * 1000)
    if proc.returncode != 0:
        reason = (proc.stderr or "").strip() or f"preflight exit {proc.returncode}"
        return False, latency_ms, reason
    return True, latency_ms, "ok"


def log_fallback(logs_dir: Path, task_id: str, reason: str, latency_ms: int, ts: str) -> None:
    """Append one JSON line recording a Shuttle -> local fallback event."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"ts": ts, "task_id": task_id, "reason": reason, "latency_ms": latency_ms},
        ensure_ascii=False,
    )
    with (logs_dir / "shuttle-offload-fallback.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def remote_task_dir(mirror_root: str, task_id: str) -> str:
    return f"{mirror_root.rstrip('/')}/{task_id}"


def rsync_push(task_dir: Path, ssh_target: str, mirror_root: str, task_id: str) -> None:
    """Mirror the task folder up to the Shuttle before dispatch."""
    dest = f"{ssh_target}:{remote_task_dir(mirror_root, task_id)}/"
    # Ensure the remote parent exists (rsync -a will create the leaf).
    subprocess.run(
        _ssh_base(ssh_target) + [f"mkdir -p {shlex.quote(remote_task_dir(mirror_root, task_id))}"],
        check=True,
    )
    argv = ["rsync", "-a"]
    for pat in RSYNC_EXCLUDES:
        argv += ["--exclude", pat]
    # Whitelist only the task-envelope files that make up the mirror.
    argv += ["--include", "*/"]
    for name in RSYNC_INCLUDES:
        argv += ["--include", name]
    argv += ["--exclude", "*"]
    argv += [f"{str(task_dir).rstrip('/')}/", dest]
    subprocess.run(argv, check=True)


def rsync_pull(ssh_target: str, mirror_root: str, task_id: str, task_dir: Path) -> bool:
    """Pull result/status/log files back from the Shuttle while polling.

    Best-effort: a transient rsync failure during polling must not abort the
    poll loop (the remote screen keeps running), so we still never raise. But we
    now REPORT it — returns True on success, False on any non-zero exit or OSError.
    A repeatedly failing pull means results are not coming back, which used to be
    completely invisible: the run just timed out with no explanation.
    """
    src_dir = remote_task_dir(mirror_root, task_id)
    argv = ["rsync", "-a"]
    argv += ["--include", "*/"]
    for name in RSYNC_PULL_BACK:
        argv += ["--include", name]
    argv += ["--exclude", "*"]
    argv += [f"{ssh_target}:{src_dir}/", f"{str(task_dir).rstrip('/')}/"]
    try:
        proc = subprocess.run(argv, check=False)
    except OSError:
        return False
    return proc.returncode == 0


def resolve_anthropic_model(env: Mapping[str, str] | None = None) -> str:
    """Model the remote spawn should use — mirror of the local setdefault.

    The local path does `spawn_env.setdefault("ANTHROPIC_MODEL", ...)` on the
    caller's environment, so an explicit override (e.g. claude-fable-5 for hard
    builds) wins. We resolve against the same source here so local and remote
    runs of the same command pick the same model.
    """
    source = os.environ if env is None else env
    return (source.get("ANTHROPIC_MODEL") or "").strip() or DEFAULT_ANTHROPIC_MODEL


def build_remote_inner(
    remote_dir: str,
    claude_cmd: str,
    session_log_name: str = "claude-session.log",
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> str:
    """The command that runs inside the remote screen session.

    ~/.local/bin (where the official installer puts claude) is not on the PATH
    for a non-interactive ssh shell, so we prepend it explicitly. ANTHROPIC_MODEL
    is exported for the same reason: ssh does not carry the caller's env, so the
    Talos model default has to travel inside the remote command itself.

    cwd: directory claude runs in — the mirrored repo (so the repo's own
    CLAUDE.md/.claude load). Defaults to remote_dir. The session log always
    lands in remote_dir (absolute path) so rsync_pull finds it either way.
    """
    workdir = cwd or remote_dir
    session_log = f"{remote_dir.rstrip('/')}/{session_log_name}"
    return (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f"export ANTHROPIC_MODEL={shlex.quote(resolve_anthropic_model(env))} && "
        f"cd {shlex.quote(workdir)} && "
        f"{claude_cmd} 2>&1 | tee {shlex.quote(session_log)}"
    )


def build_remote_screen_argv(
    ssh_target: str,
    session_name: str,
    inner: str,
) -> list[str]:
    """Full argv to spawn a detached screen running `inner` on the Shuttle.

    The remote command (last argv element) contains `screen -dmS`, and `inner`
    contains the `claude -p ...` invocation and the remote mirror path.
    """
    remote_cmd = f"screen -dmS {shlex.quote(session_name)} bash -lc {shlex.quote(inner)}"
    return _ssh_base(ssh_target) + [remote_cmd]


def remote_screen_alive(ssh_target: str, session_name: str) -> bool:
    proc = subprocess.run(
        _ssh_base(ssh_target) + ["screen -ls || true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return session_name in (proc.stdout or "")


# --------------------------------------------------------------------------- #
# Importable ssh/screen/scp helpers (Milestone 2 — sandbox_exec reuses these).
#
# These are thin PUBLIC wrappers over the primitives above. They add NO new
# behaviour to the existing `shuttle` (non-container) host path — that path keeps
# calling spawn_remote/rsync_push/remote_screen_alive exactly as before. They
# exist so sandbox_exec.py (the shuttle-sandbox driver) can drive ssh/screen/scp
# without duplicating the connection-flag conventions established here.
# --------------------------------------------------------------------------- #
def ssh_argv(ssh_target: str, *, connect_timeout: int | None = None) -> list[str]:
    """Public alias for the internal ssh base-argv builder."""
    return _ssh_base(ssh_target, connect_timeout=connect_timeout)


def ssh_run(
    ssh_target: str,
    remote_cmd: str,
    *,
    connect_timeout: int | None = None,
    timeout: int | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run a single command on the remote host over ssh, capturing output.

    Same BatchMode/ConnectTimeout conventions as preflight(). Never raises on a
    non-zero remote exit unless check=True; callers inspect returncode/stdout.
    """
    argv = _ssh_base(ssh_target, connect_timeout=connect_timeout) + [remote_cmd]
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=check
    )


def spawn_remote_screen(ssh_target: str, session_name: str, inner: str) -> None:
    """Spawn a detached remote screen running `inner`. Raises on ssh failure.

    Shares build_remote_screen_argv with spawn_remote so the screen-launch
    convention (screen -dmS ... bash -lc ...) is identical for both the
    non-container shuttle path and the sandbox path.
    """
    argv = build_remote_screen_argv(ssh_target, session_name, inner)
    subprocess.run(argv, check=True)


def scp_pull(
    ssh_target: str,
    remote_path: str,
    local_path: Path,
    *,
    timeout: int | None = None,
) -> bool:
    """Copy a single file back from the remote host. Best-effort (returns bool)."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "scp", "-o", "BatchMode=yes",
        f"{ssh_target}:{remote_path}", str(local_path),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def spawn_remote(
    *,
    task_dir: Path,
    task_id: str,
    ssh_target: str,
    mirror_root: str,
    session_name: str,
    claude_cmd: str,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> str:
    """Push the task mirror and spawn the remote screen. Returns the remote dir.

    Raises on rsync/ssh failure so the caller can decide (the caller only
    reaches here after a successful preflight; a failure here is a real error,
    not "Shuttle offline").

    env: optional env dict forwarded to build_remote_inner for per-task model
         selection (e.g. {"ANTHROPIC_MODEL": "claude-sonnet-4-6"}). Without it
         the remote command carries DEFAULT_ANTHROPIC_MODEL — so the non-sandbox
         Shuttle path honours task.json["model"] exactly like the local path.
    """
    rsync_push(task_dir, ssh_target, mirror_root, task_id)
    remote_dir = remote_task_dir(mirror_root, task_id)
    inner = build_remote_inner(remote_dir, claude_cmd, env=env, cwd=cwd)
    argv = build_remote_screen_argv(ssh_target, session_name, inner)
    subprocess.run(argv, check=True)
    return remote_dir
