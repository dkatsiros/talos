# Project Conventions (shared by Talos + all experts)

> This is the single source of truth every Talos expert references so output stays
> consistent. The CTO and every sub-agent (creative-3d-frontend, design-ux,
> backend-payments, spec-keeper, test-engineer, qa-verify, security-reviewer,
> devops-deploy, growth-copy) read this file before acting. Fill it in for THIS
> project; delete what doesn't apply.
> Keep it short and current — stale conventions are worse than none.

## Stack
- Frontend: <framework + version, e.g. Next.js 15 / Vite + React 19>
- Styling: <Tailwind v? / CSS modules / styled-components> + design tokens at <path>
- 3D/motion (if any): <three, @react-three/fiber, drei, gsap versions>
- Backend: <FastAPI / Node + framework + version>
- DB / ORM: <Postgres + Prisma / SQLAlchemy>
- Payments: <Stripe? test mode? price IDs source>
- Package manager: <npm / pnpm / yarn / uv / poetry>

## How to run the app (verify-by-running uses this)
- Dev server: `<command>` → serves at `<url>`
- Production build: `<command>`
- Tests: `<command>`  | E2E: `<command / path to playwright setup>`
- Preview/deploy: <docker compose service / static dir / edge-proxy route>

## Design language (design-ux + creative-3d defer to this)
- Palette: <neutral base + single accent, hex tokens>
- Type: <display font / body font / scale>
- Component library: <reuse these; don't fork>
- Motion budget: <subtle / cinematic; respect prefers-reduced-motion>

## Conventions / non-negotiables
- <e.g. no emojis in code; commit style; file/folder structure; naming>
- <e.g. all API input validated with zod; errors structured as {code,message}>
- Approval-required actions (do NOT do without explicit task approval):
  dependency changes, schema migrations, git push, production deploy, external posts.

## Secrets / env
- Env file: `<.env / where>` (never commit; never log). Required vars: <list>
- Stripe/test keys, DB URL, etc. come from env only.

## Definition of done (the review + verify gates check this)
A task is DONE only when:
1. The change is implemented and the build/typecheck/tests pass.
2. If the task changed any API/endpoint/schema/public interface/data contract, the
   spec-keeper expert authored/updated the matching contract + spec in the SAME
   change and returned SPECS-IN-SYNC (no SPEC-DRIFT) — code and spec never diverge.
3. The test-engineer expert authored/extended meaningful tests for the new/changed
   behavior (happy path + key edge/error cases) in this project's framework and
   returned TESTS-GREEN — no behavior change ships untested (TDD-first for new
   logic; characterization tests for changes to existing code).
4. The qa-verify expert RAN the suite + the app and observed the claimed behavior
   (screenshots in artifacts for UI; endpoint calls for backend) — no regressions,
   no mobile overflow. (test-engineer AUTHORS the tests; qa-verify RUNS them.)
5. The security-reviewer expert returned SIGN-OFF (no BLOCKERs).
6. `result.json` + `report.md` are written with evidence, and (if user-visible)
   `deployment_status` points at a live preview URL.
