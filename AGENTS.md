# Agent rules — pap-analytics-mcp

## Read first

1. `docs/ARCHITECTURE.md` (whole file, it is short).
2. Your work package in `docs/plan/WP-*.md` – named in your issue. Read `docs/plan/README.md` for dependencies and the shared Definition of Done.
3. Only the `docs/DECISIONS.md` entries your work package references.

Do not read or change other work packages' scope. If your package needs a change in another package's contract, stop and raise it in the issue (see "Contract changes").

## Working on a work package

- One issue = one work package (or one explicitly named part of it). Status lives in Multica, never in the repo – do not add checkboxes, "done" markers or progress notes to `docs/plan/`.
- Stay inside the package's **Scope** and **Files** list. Anything listed under **Out of scope** belongs to another package.
- Satisfy every **Acceptance** item and the shared Definition of Done (`docs/plan/README.md`). Write the tests first where practical.
- Interfaces marked **Contract** in a package are consumed by other packages: implement them exactly; signature changes need a contract change.
- If the plan is wrong or ambiguous, make the smallest reasonable assumption, state it in the PR and the issue, and continue. Significant/hard-to-reverse unknowns: ask in the issue and set it `blocked`.

### Contract changes

A change to the canonical model (`model.py`), the adapter protocol, the store API, analytics result types or MCP tool schemas is a **contract change**: update `docs/ARCHITECTURE.md` and the affected `docs/plan/WP-*.md` in the same PR, bump `SCHEMA_VERSION` when persisted data changes, and mention it in the PR title (`contract:`).

## Development

- Branch from `main`. Always work in your own `git worktree` on branch `dav-<n>-<short-name>` (`git worktree add -b dav-<n>-<name> ../pap-analytics-mcp-<name> main`), even for small changes. The shared checkout `~/Projects/pap-analytics-mcp` stays clean on `main`.
- Worktree cleanup is mandatory: after your PR is merged/closed remove its worktree and branch and run `git worktree prune`. At the start of work inspect `git worktree list`; remove only worktrees confirmed to belong to merged/closed PRs, after checking branch, PR state, exact head SHA and tracked/untracked/ignored files for valuable work (preserve unique work as a bundle/patch first). A squash merge changes commit IDs – do not assume commits are redundant because the PR is merged. `--force` only after preservation.
- Python ≥ 3.12, managed by `uv`. Add dependencies with `uv add` (runtime) or `uv add --dev`; keep them minimal, commit `uv.lock`.
- Quality gate (must pass locally and in CI): `uv run ruff check . && uv run pytest`.
- Typing: public functions fully type-annotated. Style: ruff config in `pyproject.toml`.

## Data and tests

- `tests/data/` contains real, intentionally published test data owned by the repo author. **Treat it as immutable**: never modify, regenerate, anonymise or delete files there. Adding a new fixture set = new directory + entry in `tests/data/README.md` + its `SHA256SUMS`.
- Tests may use the real data directly (`tests/conftest.py` fixtures). Prefer real data for parser/adapter/analytics tests; use small synthetic EDFs only for edge cases the real data lacks (truncation, corrupt CRC, DST).
- Expected numbers in tests must come from an independent source (device-reported STR values, OSCAR, manual calculation documented in the test) – never copy the implementation's own output into an assertion without checking it.
- Source data is **read-only** in all code paths: open files for reading only; never write next to the source.
- Never commit secrets, tokens or `.env` files. HTTP bearer tokens come from environment/files at runtime.

## Documentation (same PR, done by whoever changes the code)

Files: `README.md` (entry point), `docs/ARCHITECTURE.md` (living stable shape, no history), `docs/DECISIONS.md` (short append-only log), `docs/plan/` (implementation specs). Roadmap, backlog and status live in Multica, not here.

Filter:
1. Structure, interface/contract, data model/persistence, deployment, security model or a main-workflow invariant changed → edit `docs/ARCHITECTURE.md` in place.
2. A real choice between alternatives with long-term or hard-to-reverse impact → append to `docs/DECISIONS.md` (next free ADR number from `main`).
3. Otherwise change no docs.

Never document local implementation details, config values, limits, styling, texts, minor validation or bug fixes. Test: would someone safely changing another part later need to know this? If not, skip.

Deployment: once `docs/OPERATIONS.md` exists (created by WP-10), read it before changing runtime, `Dockerfile` or deployment, and update it in the same PR when environments, hosts, sync or deploy procedure change.

## Safety

- Health data: this tool reports analytics, never diagnoses. Keep the disclaimer in MCP glossary/prompt output.
- Do not touch the AirCANnect device, its firmware, the SMB share on `hnizdo` or any server unless your work package explicitly includes it. Deployment/infrastructure changes (WP-10) need David's confirmation in the issue.
