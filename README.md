# openclaw-claude-loop

A **file-driven task queue** that lets an orchestrator delegate work to
per-project [Claude Code](https://claude.ai/code) sessions acting as autonomous
engineering agents ("CTO", "builder", "QA", etc.).

Each delegated task gets a structured contract: the orchestrator enqueues a task
with title, instructions, role, and constraints; the engine renders a prompt,
spawns a non-interactive `claude -p` session in a detached `screen`, waits for
`result.json`, validates it, and returns the parsed result. Task history lives
under `.openclaw/claude-loop/` inside the project it targets — completely
separate from your orchestrator's state.

---

## Install

```bash
pip install openclaw-claude-loop
# or for development:
pip install -e /path/to/this-repo
```

**Requires:** Python ≥ 3.10, `claude` CLI on `PATH` (Claude Code), `screen`.

---

## Quick start (Python API)

```python
from openclaw_claude_loop import ProjectLoop

loop = ProjectLoop("/path/to/your/project")

# Bootstrap the queue structure once per project
if not loop.is_bootstrapped:
    loop.bootstrap()

# Delegate a task — blocks until result.json arrives
result = loop.run(
    title="Add /healthcheck endpoint",
    instructions=[
        "Add GET /healthcheck to app/main.py returning {build_sha, timestamp}",
        "Add pytest in tests/test_healthcheck.py",
        "Run: pytest tests/test_healthcheck.py -v",
    ],
    role="cto",      # 'cto' | 'builder' | 'qa' | 'backend' | 'frontend' | ...
    timeout=900,
)
print(result["state"], result["summary"])
```

---

## CLI

```bash
# Bootstrap a project
openclaw-claude-loop --project-root /path/to/project bootstrap

# Enqueue a task
openclaw-claude-loop --project-root /path/to/project enqueue "Add /healthcheck" \
    --role cto \
    --instruction "Add GET /healthcheck to app/main.py returning {build_sha, timestamp}" \
    --instruction "Write pytest in tests/test_healthcheck.py and run it"

# Render the prompt (parks the task ready for hand-off)
openclaw-claude-loop --project-root /path/to/project run-worker \
    --backend subscription-interactive --once

# Spawn the Claude session and wait for the result
openclaw-claude-loop --project-root /path/to/project run-handoff <task_id>

# Check queue status
openclaw-claude-loop --project-root /path/to/project status --tasks
```

---

## Result contract

Every completed task must produce a `result.json` with:

```json
{
  "task_id": "20260101T120000Z-add-healthcheck",
  "state": "completed",
  "summary": "Added GET /healthcheck to app/main.py; pytest passes (1/1).",
  "changes": {
    "files_created": ["tests/test_healthcheck.py"],
    "files_modified": ["app/main.py"],
    "files_deleted": []
  },
  "verification": {
    "commands": ["pytest tests/test_healthcheck.py -v"],
    "results": ["1 passed in 0.12s"]
  },
  "artifacts": ["report.md"],
  "deployment_status": {
    "state": "not_deployed",
    "details": "Local change only; no deployment target in scope."
  },
  "next_actions": []
}
```

`complete-handoff` validates the contract and rejects incomplete results.
Valid `state` values: `completed`, `failed`, `needs_clarification`, `needs_approval`.

---

## Remote execution (optional)

Tasks can run on a remote host via SSH + rsync instead of locally.
Set in environment (or project `.env`):

```bash
TALOS_SSH_TARGET=user@hostname   # SSH target for the remote host
TALOS_MIRROR_ROOT=/home/user/.claude-loop-tasks  # mirror root on the remote
```

Then tag a task with `execution_host` in its `task.json` (or dispatch via the
`--execution-host` flag if using a wrapper dispatcher). The engine runs a 3-second
SSH preflight; on failure it falls back to local transparently.

---

## Expert sub-agent roster (optional)

The `experts/` directory contains a portable pack of nine specialist sub-agents
(QA verifier, security reviewer, spec keeper, test engineer, etc.) that the CTO
can autonomously delegate to. Install per project:

```bash
python3 experts/install_experts.py --project-root /path/to/project
# then fill in docs/CONVENTIONS.md for the project
```

Enable:

```python
ProjectLoop("/path/to/project").set_experts_enabled(True)
```

Or via env: `TALOS_EXPERTS=1`. See `experts/README.md` for full details.

---

## DocSync (optional, experimental)

`openclaw_claude_loop.docsync` provides a post-task doc-updater that proposes
documentation patches after each completed task. Off by default; opt in per
project via `docsync.enabled: true` in `.openclaw/claude-loop/config.json`.

---

## File layout (per project)

```
<project_root>/
├── CLAUDE.md (or context.md / AGENTS.md)   ← auto-discovered, included in CTO prompt
└── .openclaw/claude-loop/
    ├── config.json
    ├── CLAUDE.md                            ← worker preamble
    ├── roles/cto.md, builder.md, ...
    ├── queue/{pending,claimed,blocked,done,failed}/
    └── tasks/<task_id>/
        ├── task.json
        ├── status.json
        ├── prompt.md
        ├── result.json
        └── artifacts/
```

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-opus-5` | Model the spawned Claude session uses |
| `TALOS_SSH_TARGET` | `user@hostname` | SSH target for remote execution |
| `TALOS_MIRROR_ROOT` | `/home/user/.claude-loop-tasks` | Task mirror root on remote |
| `TALOS_EXPERTS` | _(off)_ | Set `1` to enable the expert roster |

See `.env.example` for all options.

---

## License

[MIT](LICENSE) — Copyright (c) 2026 Dimitrios Katsiros.
