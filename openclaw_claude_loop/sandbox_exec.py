"""AWS-side driver for the ``shuttle-sandbox`` execution host (Milestone 2).

Sibling of ``remote_exec.py``. Where ``remote_exec`` runs the agent in a bare
``screen`` on the Shuttle (the non-container fallback), ``sandbox_exec`` drives
the *containerised* Talos Sandbox: one task = one git branch + one throwaway
container on the Shuttle, isolated port/DB/preview URL, merged back through the
existing Talos merge path (design: docs/talos-sandbox-design.md §2.2, §3).

Division of labour with the Shuttle-side ``talos-sandbox`` CLI (Milestone 1):

    AWS (this module)                         Shuttle (talos-sandbox CLI)
    -----------------------------------       ---------------------------------
    ensure replica bare repo (git init)       lease ports/db/preview  (up)
    push base -> base/<task_id>               worktree add talos/<task_id>
    ssh: talos-sandbox up ... --cmd <agent>   run container mounting only /work
    poll: talos-sandbox status <task_id>      (container runs the agent detached)
    sync result.json back (scp)               teardown: container/db/serve (down)
    git fetch talos/<task_id>
    complete-handoff -> merge_back_worktree

Design invariants (mirror remote_exec):
- Nothing here runs unless task.json carries ``execution_host == "shuttle-sandbox"``.
- Opt-in only; a preflight/kill-switch failure falls back to the local path via
  the caller (cli.run_handoff), never a hard raise at dispatch.
- A dead / timed-out sandbox surfaces through the EXISTING handoff-alert path in
  cli.py — this module NEVER synthesises a "completed" result (that was the
  cq-event-cto max_turns no-op bug; see design §6, §7.6).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import remote_exec

# Defaults mirror the M1 talos-sandbox CLI config (bin/talos-sandbox) and the
# Shuttle SSH target from remote_exec. A task may override any of these via a
# top-level "sandbox" object in task.json.
DEFAULT_SSH_TARGET = remote_exec.DEFAULT_SSH_TARGET  # dimitris@100.98.174.24
DEFAULT_REMOTE_ROOT = "/home/dimitris/talos-sandbox"
DEFAULT_BIN = "/home/dimitris/talos-sandbox/bin/talos-sandbox"
DEFAULT_TAILNET = "shuttle.tail1abf3d.ts.net"
DEFAULT_PREFLIGHT_TIMEOUT_S = 4
DEFAULT_SSH_TIMEOUT_S = 30

# Kill switch — mirrors the autorunner / M1 CLI convention. Presence blocks the
# sandbox path; the caller falls back to the local screen path.
DISABLED_FILE = os.path.expanduser("~/.openclaw/talos-sandbox/DISABLED")

# The result file the in-container agent writes into the worktree (/work). It is
# written AFTER the agent commits its real changes, so it never enters the
# committed branch; we scp it back out of the worktree host dir on the Shuttle.
RESULT_IN_WORKTREE = ".talos-result.json"

# git remote name pointing at the Shuttle replica bare repo (one per project).
REMOTE_NAME = "shuttle-sandbox"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def is_disabled() -> bool:
    """True if the kill switch file is present."""
    return os.path.exists(DISABLED_FILE)


def sandbox_config(task: dict[str, Any]) -> dict[str, Any]:
    """Resolve the sandbox connection config from the task envelope."""
    cfg = task.get("sandbox") if isinstance(task.get("sandbox"), dict) else {}
    return {
        "ssh_target": cfg.get("ssh_target", DEFAULT_SSH_TARGET),
        "remote_root": cfg.get("remote_root", DEFAULT_REMOTE_ROOT).rstrip("/"),
        "bin": cfg.get("bin", DEFAULT_BIN),
        "tailnet": cfg.get("tailnet", DEFAULT_TAILNET),
        "preflight_timeout_s": int(cfg.get("preflight_timeout_s", DEFAULT_PREFLIGHT_TIMEOUT_S)),
        "ssh_timeout_s": int(cfg.get("ssh_timeout_s", DEFAULT_SSH_TIMEOUT_S)),
    }


def replica_repo_path(cfg: dict[str, Any], project: str) -> str:
    """Absolute path of the replica bare repo on the Shuttle."""
    return f"{cfg['remote_root']}/repos/{project}.git"


def replica_remote_url(cfg: dict[str, Any], project: str) -> str:
    """git remote URL (scp-like ssh syntax) for the replica bare repo."""
    return f"{cfg['ssh_target']}:{replica_repo_path(cfg, project)}"


def worktree_host_path(cfg: dict[str, Any], project: str, task_id: str) -> str:
    """Host path of the task worktree on the Shuttle (mounted as /work)."""
    return f"{cfg['remote_root']}/wt/{project}/{task_id}"


def base_branch(task_id: str) -> str:
    return f"base/{task_id}"


def task_branch(task_id: str) -> str:
    return f"talos/{task_id}"


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def preflight(cfg: dict[str, Any]) -> tuple[bool, int, str]:
    """Health-check the Shuttle (reuses remote_exec.preflight). Also fails when
    the kill switch is present, so the caller falls back to local."""
    if is_disabled():
        return False, 0, f"kill switch active ({DISABLED_FILE})"
    return remote_exec.preflight(cfg["ssh_target"], cfg["preflight_timeout_s"])


# --------------------------------------------------------------------------- #
# Replica repo + base push
# --------------------------------------------------------------------------- #
def ensure_replica(cfg: dict[str, Any], project: str) -> None:
    """Create the replica bare repo on the Shuttle if it does not exist.

    Idempotent: ``git init --bare`` on an existing repo is a no-op that only
    reinitialises metadata. Raises on ssh failure.
    """
    repo = replica_repo_path(cfg, project)
    remote_cmd = (
        f"test -d {shlex.quote(repo)} || "
        f"(mkdir -p {shlex.quote(os.path.dirname(repo))} && "
        f"git init --bare {shlex.quote(repo)})"
    )
    proc = remote_exec.ssh_run(
        cfg["ssh_target"], remote_cmd, timeout=cfg["ssh_timeout_s"]
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ensure_replica failed for {project}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )


def ensure_local_remote(project_root: Path, cfg: dict[str, Any], project: str) -> str:
    """Ensure the canonical repo has a git remote pointing at the replica.

    Returns the remote name. Idempotent (set-url if it already exists).
    """
    url = replica_remote_url(cfg, project)
    have = subprocess.run(
        ["git", "-C", str(project_root), "remote", "get-url", REMOTE_NAME],
        capture_output=True, text=True, check=False,
    )
    if have.returncode == 0:
        if have.stdout.strip() != url:
            subprocess.run(
                ["git", "-C", str(project_root), "remote", "set-url", REMOTE_NAME, url],
                check=True,
            )
    else:
        subprocess.run(
            ["git", "-C", str(project_root), "remote", "add", REMOTE_NAME, url],
            check=True,
        )
    return REMOTE_NAME


def push_base(project_root: Path, cfg: dict[str, Any], project: str,
              base_ref: str, task_id: str) -> str:
    """Push the base ref to base/<task_id> on the replica. Returns the branch name.

    ``base_ref`` is the branch/commit the sandbox worktree is created from
    (recorded as worktree_base by the caller, exactly as the local path does).
    """
    branch = base_branch(task_id)
    # +<src>:<dst> forces the ref so a re-dispatch of the same id overwrites a
    # stale base cleanly (mirrors M1 "stale branches renamed, never reused").
    refspec = f"+{base_ref}:refs/heads/{branch}"
    proc = subprocess.run(
        ["git", "-C", str(project_root), "push", REMOTE_NAME, refspec],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"push_base {base_ref}->{branch} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return branch


# --------------------------------------------------------------------------- #
# Spawn / liveness / result sync / teardown
# --------------------------------------------------------------------------- #
def spawn_sandbox(
    *,
    project_root: Path,
    project: str,
    task_id: str,
    base_ref: str,
    agent_cmd: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Bring up the sandbox for a task and start the agent inside its container.

    Steps (design §2.2): ensure replica -> push base -> ``talos-sandbox up`` with
    the agent command as the container command. ``up`` starts the container
    detached and returns; the agent runs on independently of this ssh session.

    Returns a dict recorded into status.json (branch, base, remote, replica url).
    Raises on any hard failure so the caller can fall back to local.
    """
    ensure_replica(cfg, project)
    remote_name = ensure_local_remote(project_root, cfg, project)
    base = push_base(project_root, cfg, project, base_ref, task_id)

    # `up` leases port/db/preview, adds the worktree on talos/<task_id> from
    # base/<task_id>, and runs the container with --cmd (the agent). Container is
    # `docker run -d` so `up` returns after starting it.
    up_cmd = (
        f"{shlex.quote(cfg['bin'])} up {shlex.quote(project)} {shlex.quote(task_id)} "
        f"--base {shlex.quote(base)} --cmd {shlex.quote(agent_cmd)}"
    )
    proc = remote_exec.ssh_run(cfg["ssh_target"], up_cmd, timeout=cfg["ssh_timeout_s"])
    if proc.returncode != 0:
        # Best-effort teardown of a partial lease before surfacing the failure.
        teardown(cfg, task_id)
        raise RuntimeError(
            f"talos-sandbox up failed for {task_id}: "
            f"{(proc.stderr or proc.stdout).strip()[:400]}"
        )

    info = _parse_up_output(proc.stdout or "")
    info.update({
        "sandboxed": True,
        "worktree_branch": task_branch(task_id),
        "worktree_base": base_ref,
        "sandbox_base_branch": base,
        "sandbox_remote": remote_name,
        "replica_url": replica_remote_url(cfg, project),
        "project": project,
    })
    return info


def _parse_up_output(stdout: str) -> dict[str, Any]:
    """Extract app/serve ports, db and preview URL from `talos-sandbox up` output.

    Best-effort: absence of a field must never break the spawn (the container is
    already running by the time up prints). Fields feed status.json / the QA
    preview link, not control flow.
    """
    info: dict[str, Any] = {}
    m = re.search(r"Leased:\s*app=(\d+)\s+serve=(\d+)\s+db=(\S+)", stdout)
    if m:
        info["app_port"] = int(m.group(1))
        info["serve_port"] = int(m.group(2))
        info["db"] = m.group(3)
    m = re.search(r"Preview:\s+(\S+)", stdout)
    if m:
        info["preview_url"] = m.group(1)
    return info


def is_alive(cfg: dict[str, Any], task_id: str) -> bool | None:
    """Liveness probe for a sandboxed task via ``talos-sandbox status <task_id>``.

    Returns True while the task's container is running, False once it has exited
    (agent finished or died), or None when the probe itself failed (ssh error) —
    None must NEVER be treated as death, exactly like remote_exec's contract.
    """
    status_cmd = f"{shlex.quote(cfg['bin'])} status {shlex.quote(task_id)}"
    try:
        proc = remote_exec.ssh_run(
            cfg["ssh_target"], status_cmd,
            connect_timeout=cfg["preflight_timeout_s"], timeout=cfg["ssh_timeout_s"],
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout or ""
    if task_id not in out:
        # No lease for this task at all: nothing is running.
        return False
    # status prints "container=<12hex>" while running, "container=gone" when the
    # container has exited (see M1 cmd_status).
    m = re.search(r"container=(\S+)", out)
    if not m:
        return None
    return m.group(1) not in ("gone", "")


def sync_result_back(cfg: dict[str, Any], project: str, task_id: str,
                     task_dir: Path) -> bool:
    """scp the agent's result file from the worktree host dir into the AWS task dir.

    The agent writes ``/work/.talos-result.json`` (uncommitted, so it never
    enters the branch). We copy it to ``task_dir/result.json`` so the existing
    poll loop / complete-handoff finalise unchanged. Best-effort (returns bool).
    """
    remote_path = f"{worktree_host_path(cfg, project, task_id)}/{RESULT_IN_WORKTREE}"
    return remote_exec.scp_pull(
        cfg["ssh_target"], remote_path, task_dir / "result.json",
        timeout=cfg["ssh_timeout_s"],
    )


def fetch_branch(project_root: Path, remote_name: str, task_id: str) -> None:
    """git fetch the task branch from the replica into the canonical repo.

    Runs on the AWS side inside the same blocking merge lock as merge_back
    (caller's responsibility). Raises on failure — a failed fetch means there is
    nothing to merge and must surface, never be papered over.
    """
    branch = task_branch(task_id)
    refspec = f"+refs/heads/{branch}:refs/heads/{branch}"
    proc = subprocess.run(
        ["git", "-C", str(project_root), "fetch", remote_name, refspec],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"fetch_branch {branch} from {remote_name} failed: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )


def prune_stale_local_refs(project_root: Path, task_id: str) -> list[str]:
    """Remove leftover local worktrees/branches that would block a sandbox fetch.

    A sandbox task's authoritative branch lives on the Shuttle replica; there is
    no local worktree for it. But a PRIOR local fallback (or an aborted local
    run) can leave a ``.worktrees/<task_id>`` worktree checked out on
    ``talos/<task_id>`` in the canonical repo. Git then refuses the fetch with
    ``refusing to fetch into branch 'refs/heads/talos/<id>' checked out at
    '.worktrees/<id>'``. This prunes that stale state so ``fetch_branch`` (a
    forced fetch) can land the replica's branch cleanly.

    Best-effort and idempotent: every git call runs with ``check=False`` and a
    missing worktree/branch is simply skipped. Returns a list of the actions
    taken, for logging. Never raises.
    """
    actions: list[str] = []

    # 1. Remove any local worktree registered for this task id. `worktree remove`
    #    both deletes the directory and drops the admin entry that pins the branch.
    wt = project_root / ".worktrees" / task_id
    if wt.exists():
        rm = subprocess.run(
            ["git", "-C", str(project_root), "worktree", "remove", "--force", str(wt)],
            capture_output=True, text=True, check=False,
        )
        if rm.returncode == 0:
            actions.append(f"removed stale local worktree {wt}")
    # Drop admin entries for worktree dirs deleted out-of-band (also releases the
    # branch checkout lock if the directory is already gone).
    subprocess.run(
        ["git", "-C", str(project_root), "worktree", "prune"],
        capture_output=True, text=True, check=False,
    )

    # 2. Delete stale local talos/<task_id>* branches so the forced fetch can
    #    write refs/heads/talos/<task_id>. The replica holds the real branch; any
    #    local copy here is leftover from a prior local run and is not needed.
    listed = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--list", f"{task_branch(task_id)}*"],
        capture_output=True, text=True, check=False,
    )
    for line in listed.stdout.splitlines():
        # `git branch --list` marks the current branch with a leading '*'.
        name = line.lstrip("* ").strip()
        if not name:
            continue
        dele = subprocess.run(
            ["git", "-C", str(project_root), "branch", "-D", name],
            capture_output=True, text=True, check=False,
        )
        if dele.returncode == 0:
            actions.append(f"deleted stale local branch {name}")

    return actions


def teardown(cfg: dict[str, Any], task_id: str) -> bool:
    """Tear down the sandbox on the Shuttle (``talos-sandbox down``). Best-effort."""
    down_cmd = f"{shlex.quote(cfg['bin'])} down {shlex.quote(task_id)}"
    try:
        proc = remote_exec.ssh_run(cfg["ssh_target"], down_cmd, timeout=cfg["ssh_timeout_s"])
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def cleanup_remote_branches(project_root: Path, cfg: dict[str, Any],
                            task_id: str) -> None:
    """Delete base/<id> and talos/<id> on the replica after a successful merge.

    Best-effort: leftover branches are harmless (gc/re-push overwrite them), so
    a cleanup failure never propagates.
    """
    # Delete via the local remote so we reuse the configured URL/auth.
    for branch in (base_branch(task_id), task_branch(task_id)):
        subprocess.run(
            ["git", "-C", str(project_root), "push", REMOTE_NAME, "--delete", branch],
            capture_output=True, text=True, check=False,
        )


def build_agent_container_cmd(
    *,
    handoff_prompt: str,
    role_prompt: str,
    permission_mode: str,
    model: str,
    quiet: bool,
    result_path_in_container: str = f"/work/{RESULT_IN_WORKTREE}",
) -> str:
    """Build the bash command run INSIDE the sandbox container (cwd=/work).

    The container mounts the worktree at /work (already on branch talos/<id>).
    The agent runs there, commits its work, and (per its role prompt) writes
    result.json. We DO NOT synthesise a result on timeout/death — a dead
    container simply produces no result file and surfaces as an alert upstream
    (design §6: no cq-event-cto-style no-op fallback).

    ANTHROPIC_MODEL is exported inside the command because docker env is the only
    channel; the container image already carries CLAUDE_CODE_OAUTH_TOKEN via
    talos-sandbox up (from ~/.config/talos-auth/env).
    """
    # The default handoff prompt tells the agent to write result.json to the AWS
    # task-dir path, which does NOT exist inside the container. Redirect it to the
    # worktree-local result file we sync back (RESULT_IN_WORKTREE). Commit-before-
    # result is preserved (uncommitted changes are lost on teardown).
    sandbox_directive = (
        "\n\n--- SANDBOX EXECUTION (Talos Sandbox) ---\n"
        "You are running inside an isolated container. Your git worktree is /work "
        "(already checked out on your task branch).\n"
        "1. Make and COMMIT all changes inside /work "
        "(`git add -A && git commit -m ...`) — uncommitted work is LOST on teardown.\n"
        f"2. Write your final result JSON to {result_path_in_container} — this is "
        "the ONLY result path synced back; ignore any other result path in the "
        "brief above.\n"
        "3. `deployment_status` MUST be a JSON OBJECT, never a string. It must be "
        'exactly of the form {"state": "<one of: deployed|not_deployed|'
        'not_applicable|blocked>", "details": "<non-empty explanation>"}. '
        "You are inside a throwaway container with NO access to the host, staging, "
        "or production — so you cannot deploy anything. The honest, required value "
        'for sandbox work is {"state": "not_deployed", "details": "built and '
        'committed inside the Talos sandbox container; no host access — deployment '
        'happens on merge-back by the operator"}. Never write deployment_status as '
        "a bare string; complete-handoff will reject the result and the task cannot "
        "close.\n"
    )
    argv = [
        "claude", "-p", shlex.quote(handoff_prompt + sandbox_directive),
        "--add-dir", "/work",
        "--permission-mode", shlex.quote(permission_mode),
    ]
    if role_prompt:
        argv += ["--append-system-prompt", shlex.quote(role_prompt)]
    if not quiet:
        argv += ["--verbose", "--output-format", "stream-json"]
    claude_cmd = " ".join(argv)
    return (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f"export ANTHROPIC_MODEL={shlex.quote(model)} && "
        "git config --global --add safe.directory /work && "
        "cd /work && "
        f"{claude_cmd} 2>&1 | tee /work/claude-session.log"
    )
