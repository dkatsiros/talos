"""High-level Python client for OpenClaw Claude Loop.

Lets an orchestrator delegate tasks to a per-project CTO without juggling CLI argv.

Example:
    from openclaw_claude_loop import ProjectLoop

    loop = ProjectLoop("/path/to/project")
    result = loop.run(
        title="Add /healthcheck endpoint",
        instructions=[
            "Use FastAPI app at app/main.py",
            "Return JSON {build_sha, timestamp}",
            "Add pytest in tests/test_healthcheck.py and run it",
        ],
        timeout=900,
    )
    print(result["state"], result["summary"])
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


class ProjectLoopError(RuntimeError):
    """Raised when a loop operation fails."""


class ProjectLoop:
    """Per-project handle for the Claude Loop file queue."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).expanduser().resolve()
        self._module_argv = [sys.executable, "-m", "openclaw_claude_loop", "--project-root", str(self.project_root)]

    @property
    def loop_root(self) -> Path:
        return self.project_root / ".openclaw" / "claude-loop"

    @property
    def is_bootstrapped(self) -> bool:
        return (self.loop_root / "config.json").exists()

    def bootstrap(self, *, force: bool = False) -> None:
        args = ["bootstrap"]
        if force:
            args.append("--force")
        self._run(args)

    def set_experts_enabled(self, enabled: bool) -> None:
        """Toggle the optional expert roster for this project (config flag).

        Additive + non-breaking. Setting True makes the CTO autonomously delegate
        task slices to the installed expert sub-agents (.claude/agents/) and
        enforce the verify-by-running + adversarial-review gates. False hard-
        disables even if agents are installed. Requires the experts pack to be
        installed first (see experts/install_experts.py).
        """
        if not self.is_bootstrapped:
            raise ProjectLoopError(
                f"Project not bootstrapped at {self.loop_root}. Call .bootstrap() first."
            )
        config_path = self.loop_root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        config.setdefault("experts", {})["enabled"] = bool(enabled)
        tmp = config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(config_path)

    def delegate(
        self,
        title: str,
        *,
        instructions: str | Sequence[str],
        role: str = "cto",
        goal: str | None = None,
        write_scope: Sequence[str] | None = None,
        expected_outputs: Sequence[str] | None = None,
        files_of_interest: Sequence[str] | None = None,
        network: str = "deny",
        max_runtime_seconds: int = 1800,
        created_by: str | None = None,
    ) -> str:
        """Enqueue a task. Returns the task_id.

        created_by: identity to stamp on task.json. Defaults to the CLI's own
        'openclaw' default, which is NOT on the autorunner's TRUSTED_CREATORS
        allowlist (orchestrator/ci/<project>-*) and will always be parked for
        human review rather than auto-fired. If this delegate() call is being
        made on behalf of a verified trusted identity (e.g. a per-project
        Talos/CTO dispatch), pass that identity explicitly here — do not rely
        on the generic default and then expect it to be auto-run.
        """
        if not self.is_bootstrapped:
            raise ProjectLoopError(
                f"Project not bootstrapped at {self.loop_root}. Call .bootstrap() first."
            )
        instr_list = [instructions] if isinstance(instructions, str) else list(instructions)
        argv = ["enqueue", title, "--role", role, "--network", network,
                "--max-runtime-seconds", str(max_runtime_seconds)]
        if created_by:
            argv += ["--created-by", created_by]
        for line in instr_list:
            argv += ["--instruction", line]
        if goal:
            argv += ["--goal", goal]
        for path in write_scope or ():
            argv += ["--write-scope", path]
        for item in expected_outputs or ():
            argv += ["--expected-output", item]
        for path in files_of_interest or ():
            argv += ["--file", path]
        proc = self._run(argv, capture=True)
        task_id = proc.stdout.strip()
        if not task_id:
            raise ProjectLoopError(f"enqueue produced no task_id (stderr: {proc.stderr})")
        return task_id

    def prepare(self, task_id: str | None = None) -> None:
        """Render prompt.md for a pending task and park it in blocked/.

        task_id given -> prepares THAT task deterministically (exits non-zero if
        it is not pending). This is the correct call: previously the id was
        ignored and the CLI worker ran blind FIFO, so it could promote a
        different task — or nothing — while silently reporting success. The task
        you asked for then rotted in pending/, which the autorunner never reads
        (an early production incident).

        task_id omitted -> legacy FIFO (oldest pending task).
        """
        argv = ["run-worker", "--backend", "subscription-interactive", "--once"]
        if task_id:
            argv += ["--task-id", task_id]
        self._run(argv)

    def execute(
        self,
        task_id: str,
        *,
        detach: bool = False,
        timeout: int = 1800,
        poll_interval: int = 5,
        permission_mode: str | None = None,
        require_worktree: bool = False,
    ) -> dict[str, Any] | None:
        """Spawn the Claude session for a parked task. Blocks until result.json arrives.

        Returns the parsed result.json dict, or None if detach=True.
        require_worktree=True aborts instead of running in the shared project
        checkout when a git worktree cannot be created (parallel dispatch).
        """
        argv = ["run-handoff", task_id, "--timeout", str(timeout), "--poll-interval", str(poll_interval)]
        if detach:
            argv.append("--detach")
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if require_worktree:
            argv.append("--require-worktree")
        self._run(argv)
        if detach:
            return None
        return self.result(task_id)

    def run(
        self,
        title: str,
        *,
        instructions: str | Sequence[str],
        role: str = "cto",
        goal: str | None = None,
        write_scope: Sequence[str] | None = None,
        expected_outputs: Sequence[str] | None = None,
        files_of_interest: Sequence[str] | None = None,
        timeout: int = 1800,
        poll_interval: int = 5,
        permission_mode: str | None = None,
        max_runtime_seconds: int = 1800,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """delegate + prepare + execute in one blocking call. Returns result dict.

        created_by: see delegate() — pass your verified trusted identity if you
        want this task eligible for auto-run instead of always being parked.
        """
        task_id = self.delegate(
            title,
            instructions=instructions,
            role=role,
            goal=goal,
            write_scope=write_scope,
            expected_outputs=expected_outputs,
            files_of_interest=files_of_interest,
            max_runtime_seconds=max_runtime_seconds,
            created_by=created_by,
        )
        self.prepare(task_id)
        result = self.execute(
            task_id,
            timeout=timeout,
            poll_interval=poll_interval,
            permission_mode=permission_mode,
        )
        if result is None:
            raise ProjectLoopError(f"Task {task_id} did not produce a result.json")
        return result

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        """Return queue counts; if task_id given, also include task state."""
        proc = self._run(["status"] + (["--tasks"] if task_id else []), capture=True)
        # First line of stdout is the queue counts JSON block (multi-line)
        # Parse from the start until the first non-JSON line; cheaper to just split.
        head, _, tail = proc.stdout.partition("\n}\n")
        if not tail:
            return json.loads(proc.stdout)
        counts = json.loads(head + "\n}")
        info: dict[str, Any] = {"queue": counts.get("queue", {}), "project_root": counts.get("project_root")}
        if task_id:
            for line in tail.splitlines():
                parts = line.split("\t")
                if parts and parts[0] == task_id:
                    info["task"] = {
                        "state": parts[1] if len(parts) > 1 else None,
                        "phase": parts[2] if len(parts) > 2 else None,
                        "updated_at": parts[3] if len(parts) > 3 else None,
                    }
                    break
        return info

    def result(self, task_id: str) -> dict[str, Any] | None:
        """Read and parse result.json for a task, or None if not present yet."""
        path = self.loop_root / "tasks" / task_id / "result.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def task_status(self, task_id: str) -> dict[str, Any] | None:
        path = self.loop_root / "tasks" / task_id / "status.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _run(self, argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = self._module_argv + argv
        if capture:
            return subprocess.run(cmd, text=True, capture_output=True, check=True)
        return subprocess.run(cmd, check=True)
