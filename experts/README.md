# Talos Expert Roster

A portable, toggleable pack of expert Claude Code **sub-agents** that the
per-project CTO (Talos) can autonomously delegate to. Additive + non-breaking:
with experts disabled, Talos behaves exactly as before.

## What it is
- `agents/*.md` — 9 expert sub-agent definitions (system prompt + scoped tools +
  model). Installed into a project's `.claude/agents/` so the CTO's `claude -p`
  session can call them via the `Agent` tool.
- `CONVENTIONS.template.md` — the shared conventions doc every expert reads, so
  output stays consistent. Installed to `docs/CONVENTIONS.md` (fill it in).
- `ROUTER.md` — appended to the CTO system prompt; tells Talos how to decide which
  expert to delegate to, and enforces the two gates (verify-by-running, review).
- `install_experts.py` — drop the pack into / remove it from any project.

## The experts
| Sub-agent | Does | Tools | Model | Paired MCP |
|---|---|---|---|---|
| `creative-3d-frontend` | Three.js/R3F/drei, GSAP ScrollTrigger, Spline, shaders, scroll-driven heroes | Read/Edit/Write/Grep/Glob/Bash + Context7 | sonnet | **Context7** (live 3D API docs) |
| `design-ux` | Taste, layout, components, responsive, a11y; applies design-taste-frontend | Read/Edit/Write/Grep/Glob/Bash | sonnet | — (design-taste-frontend skill) |
| `backend-payments` | FastAPI/Node APIs, auth, data/migrations, Stripe billing | Read/Edit/Write/Grep/Glob/Bash + Context7 | sonnet | **Context7**, **Stripe** |
| `spec-keeper` | Contract/spec author + anti-drift GATE: API/interface/data contracts, `CONTRACTS.md` index, keeps specs in sync with code; SPECS-IN-SYNC/SPEC-DRIFT | Read/Edit/Write/Grep/Glob/Bash + Context7 | sonnet | **Context7** (contract conventions) |
| `test-engineer` | Test author + coverage GATE: WRITES unit/integration/e2e tests in the project's framework, TDD for new logic, characterization tests for changes; runs them; TESTS-GREEN/TESTS-RED. Authors — does not run the live app | Read/Write/Edit/Grep/Glob/Bash + Context7 | sonnet | **Context7** (test-framework docs) |
| `qa-verify` | Verify-by-running GATE: RUNS the suite + the real app, Playwright phone+desktop / Chromium+WebKit, screenshots. Runs — does not author tests | Read/Grep/Glob/Bash + browser | sonnet | **Browser/Playwright** |
| `security-reviewer` | Adversarial review GATE: auth, secrets, injection, correctness; SIGN-OFF/BLOCKED | Read/Grep/Glob/Bash | **opus** | — |
| `devops-deploy` | Docker, CI/CD, Caddy/nginx, edge/compute preview deploy, liveness | Read/Edit/Write/Grep/Glob/Bash | sonnet | (GitHub optional) |
| `growth-copy` | Landing/marketing copy, microcopy, creative/asset briefs | Read/Edit/Write/Grep/Glob + web_search/web_fetch | sonnet | **Higgsfield** (assets) |

## How autonomous delegation works
The CTO reads `ROUTER.md` (appended to its system prompt when enabled). For each
task it matches the work to the routing table and calls the relevant experts via
the `Agent` tool — Talos stays the orchestrator and writes the single
`result.json`. The mandatory gates for non-trivial / user-visible tasks, in order:
1. **spec reconcile / anti-drift** (`spec-keeper`) — fires when the task changed an
   API, endpoint, schema, public interface, or data contract: it authors/updates
   the matching contract + spec in the SAME task and reconciles it against the
   code, returning `SPECS-IN-SYNC` (or `SPEC-DRIFT` to fix). Code and spec ship
   together. Composes with — does not replace — the review gate below.
2. **tests authored + green** (`test-engineer`) — if the task added/changed
   behavior, it authors meaningful tests (happy + key edge/error) in the project's
   existing framework (TDD-first for new logic; characterization tests for changes)
   and returns `TESTS-GREEN`. It AUTHORS the suite; qa-verify RUNS it.
3. **verify-by-running** (`qa-verify`) — RUN the suite + the live app, observe
   behavior, evidence.
4. **adversarial review** (`security-reviewer`) — must return `SIGN-OFF` before
   `state: completed`.

## Operator steps (enable per project)
```bash
P=/path/to/your/project
EX=/path/to/claude-loop-module/experts

# 1. install the pack (additive; won't clobber an existing CONVENTIONS.md)
python3 $EX/install_experts.py --project-root $P

# 2. fill in the conventions for this project
$EDITOR $P/docs/CONVENTIONS.md

# 3. enable the router wiring (any one):
python3 - <<'PY'
from openclaw_claude_loop import ProjectLoop
ProjectLoop("/path/to/your/project").set_experts_enabled(True)
PY
#   or: export TALOS_EXPERTS=1
#   or: nothing — installing agents+router soft-auto-enables it

# check state
python3 $EX/install_experts.py --project-root $P --status
```
Disable: `set_experts_enabled(False)` (hard-off) or
`install_experts.py --project-root $P --remove`.

## MCP wiring
Sub-agent tool lists reference MCP tools by name (e.g. `mcp__context7__query-docs`).
A sub-agent can only USE an MCP that is connected in the CTO's Claude Code session.
The following MCPs need explicit wiring per project:

### Context7 (TOP PRIORITY — prevents 3D/SDK API hallucination)
```bash
claude mcp add --scope project context7 -- npx -y @upstash/context7-mcp
# run from the project root so it lands in the project's .mcp.json
```
Verify: `claude mcp list` shows context7 Connected. Free tier needs no key; a
Context7 API key (env `CONTEXT7_API_KEY`) raises limits.

### Browser / Playwright (for qa-verify)
- qa-verify uses Bash + the Playwright CLI by default: `npm i -D @playwright/test && npx playwright install`
  (install WebKit too: `npx playwright install webkit`). Reuse existing e2e setup.
  If you have a browser MCP wired in your Claude Code session, add its tool name to
  the `tools:` frontmatter in `experts/agents/qa-verify.md`.
  Note: heavy browser installs want disk + memory — avoid memory-constrained hosts.

### Stripe (for backend-payments)
```bash
claude mcp add --scope project stripe \
  -e STRIPE_API_KEY=sk_test_xxx -- npx -y @stripe/mcp --tools=all
```
Use a **test** key only. Keep it in env, never commit. Add `mcp__stripe__*` to
`backend-payments`'s `tools:` line once wired.

### Higgsfield (optional, for growth-copy creative assets)
If you use an image/asset generation MCP (e.g. Higgsfield), confirm the server
package/endpoint before wiring, then `claude mcp add --scope project higgsfield …`
with the API key in env. Until wired, growth-copy hands off generation prompts to
the operator (its default behavior).

### GitHub (optional, for devops-deploy / PRs)
`claude mcp add --scope project github -e GITHUB_TOKEN=ghp_xxx -- npx -y @modelcontextprotocol/server-github`
(the `gh` CLI is usually enough; wire only if PR automation is wanted).

## Notes
- A sub-agent listing an MCP tool that isn't connected simply can't call it — it
  is not an error; the expert degrades gracefully (and is told to say so).
- Models: experts default to `sonnet`; `security-reviewer` uses `opus` for the
  adversarial gate. Adjust per project budget in the agent frontmatter.
