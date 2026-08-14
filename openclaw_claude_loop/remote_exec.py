"""SSH-remote execution helpers for the remote-offload path.

Implements the remote-execution path: keep the whole claude-loop file queue on
the local host, but run the `screen -dmS ... claude -p ...` execution step on a
remote node via SSH, with rsync mirroring the task folder before the run and
pulling results back while polling.

Design invariants:
- Default execution host is local; nothing here runs unless the task envelope
  carries execution_host == "remote" (or a named host).
- Any preflight failure falls back to the local screen path. We never raise on
  the remote being offline; we log one JSON line and let the caller run locally.
- Only the configured SSH user runs agents. No new inbound ports.

Configuration:
  Set TALOS_SSH_TARGET=user@hostname and TALOS_MIRROR_ROOT=/path/to/tasks in
  your environment (or in the project's .env file) to point the offload path at
  your remote execution host. See .env.example for all available options.
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

# Defaults read from environment variables so the package ships without any
# hardcoded host details. Set TALOS_SSH_TARGET and TALOS_MIRROR_ROOT in your
# environment or project .env file. A task may also override any of these via a
# top-level "remote" object in task.json (keys: ssh_target, mirror_root,
# preflight_timeout_s).
DEFAULT_SSH_TARGET: str = os.environ.get("TALOS_SSH_TARGET", "user@hostname")
DEFAULT_MIRROR_ROOT: str = os.environ.get("TALOS_MIRROR_ROOT", "/home/user/.claude-loop-tasks")
DEFAULT_PREFLIGHT_TIMEOUT_S = 3
# Talos default model, mirrored from the local screen path in cli.run_handoff
# (spawn_env.setdefault). SSH does not forward arbitrary env vars, so the remote
# command must carry the model explicitly or the remote host falls back to whatever
# its own shell profile sets.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

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
    '-' becomes an rsync option. Both are rejected with a clear error.
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


def remote_config(task: dict[str, Any]) -> dict[str, Any]:
    """Resolve the remote-host connection config from the task envelope.

    Reads TALOS_SSH_TARGET / TALOS_MIRROR_ROOT from the environment for
    defaults; a task envelope may override any field under task["remote"].
    Returns the resolved config after validating ssh_target and mirror_root
    to prevent option-injection via SSH/rsync.
    """
    cfg = task.get("remote") if isinstance(task.get("remote"), dict) else {}
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
    """Health-check the remote host before dispatch.

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
    """Append one JSON line recording a remote-host -> local fallback event."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"ts": ts, "task_id": task_id, "reason": reason, "latency_ms": latency_ms},
        ensure_ascii=False,
    )
    with (logs_dir / "remote-offload-fallback.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def remote_task_dir(mirror_root: str, task_id: str) -> str:
    return f"{mirror_root.rstrip('/')}/{task_id}"


def rsync_push(task_dir: Path, ssh_target: str, mirror_root: str, task_id: str) -> None:
    """Mirror the task folder up to the remote host before dispatch."""
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


def rsync_pull(ssh_target: str, mirror_root: str, task_id: str, task_dir: Path) -> None:
    """Pull result/status/log files back from the remote host while polling.

    Best-effort: a transient rsync failure during polling should not abort the
    poll loop (the remote screen keeps running), so we swallow non-zero exits.
    """
    src_dir = remote_task_dir(mirror_root, task_id)
    argv = ["rsync", "-a"]
    argv += ["--include", "*/"]
    for name in RSYNC_PULL_BACK:
        argv += ["--include", name]
    argv += ["--exclude", "*"]
    argv += [f"{ssh_target}:{src_dir}/", f"{str(task_dir).rstrip('/')}/"]
    subprocess.run(argv, check=False)


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
) -> str:
    """The command that runs inside the remote screen session.

    ~/.local/bin (where the official Claude Code installer puts `claude`) is not
    on the PATH for a non-interactive ssh shell, so we prepend it explicitly.
    ANTHROPIC_MODEL is exported for the same reason: ssh does not carry the
    caller's env, so the model default has to travel inside the remote command.
    """
    return (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f"export ANTHROPIC_MODEL={shlex.quote(resolve_anthropic_model(env))} && "
        f"cd {shlex.quote(remote_dir)} && "
        f"{claude_cmd} 2>&1 | tee {shlex.quote(session_log_name)}"
    )


def build_remote_screen_argv(
    ssh_target: str,
    session_name: str,
    inner: str,
) -> list[str]:
    """Full argv to spawn a detached screen running `inner` on the remote host.

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


def spawn_remote(
    *,
    task_dir: Path,
    task_id: str,
    ssh_target: str,
    mirror_root: str,
    session_name: str,
    claude_cmd: str,
) -> str:
    """Push the task mirror and spawn the remote screen. Returns the remote dir.

    Raises on rsync/ssh failure so the caller can decide (the caller only
    reaches here after a successful preflight; a failure here is a real error,
    not "remote host offline").
    """
    rsync_push(task_dir, ssh_target, mirror_root, task_id)
    remote_dir = remote_task_dir(mirror_root, task_id)
    inner = build_remote_inner(remote_dir, claude_cmd)
    argv = build_remote_screen_argv(ssh_target, session_name, inner)
    subprocess.run(argv, check=True)
    return remote_dir
