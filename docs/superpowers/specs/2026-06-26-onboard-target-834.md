# A reliable `just onboard` target that seeds funding and binds the project (#834)

- **Issue:** [#834](https://github.com/randomparity/kdive/issues/834) (Black-box review follow-up,
  `status:needs-design`).
- **ADR:** [ADR-0256](../../adr/0256-onboard-target.md).
- **Status:** Accepted.
- **Related:** [#833](https://github.com/randomparity/kdive/issues/833) / ADR-0255 (the server-side
  denial UX that surfaces quota + budget together) — the same onboarding friction from the other
  side.
- **Builds on:** `seed-project` (`src/kdive/admin/bootstrap.py`), the live-stack env
  (`scripts/live-stack/env.sh`), and `mint_local_token` (`src/kdive/cli/login.py`) — the existing
  primitives this recipe composes.

## Problem

A fresh agent on a new dev stack hits the zero-quota / zero-budget wall on its first
`allocations.request`, even after the maintainer "seeded" the project. A successful first request
needs **three strings to be identical**: the seeded `budgets`/`quotas` key, the token's
`projects`/`roles` JWT claim, and the `project` argument the agent passes. Nothing today makes that
binding explicit or verifies it landed, and the two seed entry points are each missing a piece:

- `scripts/setup-local-libvirt.sh` seeds but **never migrates and never sources
  `scripts/live-stack/env.sh`**, so `KDIVE_DATABASE_URL` may be unset (`create_pool` raises,
  `src/kdive/db/pool.py:24-29`) or point at a *different* Postgres than the server reads — the seed
  silently lands in the wrong DB.
- `scripts/live-stack/up.sh` migrates but **never seeds**.
- There is **no `onboard` recipe** in the justfile, and no step reads the rows back to prove the
  seed is durable in the DB the server uses.

The project-name binding (guaranteeing seeded string == token claim == `project` arg, and whether
the recipe mints/prints the token) is the part the issue flagged `needs-design`; the
migrate → seed → verify ordering itself is straightforward.

## Goal

One idempotent `just onboard` recipe that funds a project against the **same DB the server uses**,
makes the project string the **single source of truth** threaded through seed + token + the printed
contract, **verifies** the rows are durably present before claiming success, and hands the agent a
ready-to-use token. After `just stack-up` then `just onboard`, a first `allocations.request` with
the printed token is granted, not funding-walled.

## Design decisions (the `needs-design` part)

### 1. One project string, single source of truth

The recipe takes one `PROJECT` value (`KDIVE_PROJECT`, default `demo`) and threads the *same*
variable into every place the three strings live:

- the seed: `seed-project --project "$PROJECT"`;
- the verify readback: `verify-project --project "$PROJECT"`;
- the minted token's claims: `projects=["$PROJECT"]`, `roles={"$PROJECT": <role>}`;
- the printed **token contract** block.

Because all four read one shell variable, the seeded key, the JWT claim, and the `project` arg are
equal *by construction* — the binding cannot drift within a run.

### 2. Verify-readback is the reliability gate (the new mechanism)

A new token-less `kdive verify-project --project P` subcommand re-opens a fresh connection to
`KDIVE_DATABASE_URL` and asserts **both** a `budgets` row and a `quotas` row exist for `P`, printing
the figures and the resolved DB. It reuses the *exact* reads admission control uses —
`budget_snapshot` (`services/allocation/idempotency.py`) and `quota_status`
(`services/allocation/admission/core.py`) — so "verified" means "the funding reads admission
performs will return rows," with no second copy of the lookup to drift. Either row absent →
non-zero exit naming the DB, and the recipe fails.

**What verify does and does not prove.** verify-project resolves `KDIVE_DATABASE_URL` the same way
`seed-project` did, in the same recipe run, so the two cannot disagree about *which* DB. Its
guarantee is therefore: the funding rows are **durably present and readable** in the DB this recipe
targeted — it catches a seed transaction that rolled back, a `migrate` that did not create the
`budgets`/`quotas` tables (the `SELECT` raises), and a silent seed no-op. It does **not**
independently prove that targeted DB is the one the *server* reads; that identity comes from both
processes resolving the same env (see §5), and the recipe makes any skew visible by echoing the
resolved `KDIVE_DATABASE_URL` it targeted.

### 3. Token: mint a real 24 h token, best-effort, with a loud expiry warning

The recipe mints an actual bearer token via the existing `mint_local_token` (the same primitive
`examples/local-libvirt/mint-token.sh` uses) with `ttl_seconds=86400` (24 h) and prints it as an
`export KDIVE_TOKEN=…` line plus a **loud** warning that it expires in 24 h and how to re-mint.
Minting is **best-effort**: the seed + verify are already done and durable, so a mint failure (e.g.
the mock-OIDC issuer is down) **warns and still exits 0** — it prints the contract and the re-mint
command so the agent can mint when the issuer is up. The contract block (the three strings) is
printed **unconditionally**, whether or not the mint succeeds.

- **Role:** `admin` (`KDIVE_ROLE` override) + `platform_admin`/`platform_operator`, mirroring
  `examples/local-libvirt/mint-token.sh`, so the demo token reaches every tool including the
  admin-gated funding tools (`accounting.set_quota`/`set_budget`) and `control.force_crash`. The
  minted role must be **≥ contributor** for the token to pass the `allocations.request` gate; if
  `KDIVE_ROLE` resolves below contributor (`viewer`), the recipe prints a WARN that the token will
  be funding-walled (it does not fail — the seed is still valid).
- **Demo-only:** the bundled mock issuer mints a valid token for any caller; the script repeats the
  existing scripts' "never against a real deployment" warning.

### 4. Advisory provider preflight

The recipe runs `scripts/check-local-libvirt.sh` so a later "no schedulable resource" denial is not
mistaken for a funding wall — but **advisory**: a preflight FAIL prints a warning and the recipe
continues to seed + verify. Funding is independent of libvirt readiness; a missing `/dev/kvm` must
not block funding the project. (This is the deliberate difference from `setup-local-libvirt.sh`,
which hard-fails on preflight.)

### 5. Order and env

`source live-stack/env.sh` → advisory preflight → `migrate` (idempotent) → `seed-project` →
`verify-project` → mint + print token + contract. Sourcing `env.sh` resolves `KDIVE_DATABASE_URL`
to the live-stack default when the caller left it unset, so the recipe never seeds an ambient/unset
DB. Hard gates: migrate, seed, verify. Advisory (warn, non-fatal): preflight, token mint.

**Prerequisite (named, not assumed):** the DB onboard targets equals the DB the server reads **only
when the server is brought up from the same env** — which the live-stack convention satisfies
(`lib.sh restart_host_processes` sources `env.sh` before starting the host `server`). An operator
who starts the server with an overriding `KDIVE_DATABASE_URL` not present when `just onboard` runs
will fund a different DB than the server reads, and the agent stays walled despite a green onboard.
The recipe therefore echoes the resolved `KDIVE_DATABASE_URL` it targeted so such a skew is visible
rather than silent; reconciling it is the operator's job, not something the token-less recipe can
detect.

## Scope

### In scope

- `src/kdive/admin/bootstrap.py` — add `verify_project(project) -> ProjectFundingStatus` reusing the
  canonical budget/quota reads; a small frozen result type carrying `(budget_present, quota_present,
  limit_kcu, max_concurrent_allocations, max_concurrent_systems)`. (No `database_url_present` field:
  `create_pool` raises when the URL is unset, so verify never reaches the result with it absent.)
- `src/kdive/__main__.py` — register a `verify-project` command (`--project`, default `demo`) that
  prints the figures and exits non-zero when either row is absent.
- `scripts/live-stack/onboard.sh` — the recipe body (sources `env.sh` + `lib.sh`; advisory preflight;
  `migrate`; `seed-project`; `verify-project`; mint + contract).
- `justfile` — `just onboard` calling the script.
- `scripts/live-stack/up.sh` — its status output suggests `just onboard` (up.sh still does not seed;
  onboard stays the seed step).
- Docs: a short `just onboard` note in `docs/operating/runbooks/live-stack.md` and the live-stack
  `README.md`; the project-binding contract framing.
- Tests: `tests/scripts/test_onboard.py` (PATH-stub behavioral, mirroring
  `test_setup_local_libvirt.py`) and `tests/admin/test_bootstrap.py` additions for `verify_project`
  (testcontainer DB: both rows, missing budget, missing quota).

### Out of scope (explicit)

- `scripts/setup-local-libvirt.sh` — unchanged. It serves the **package-install** path
  (`KDIVE_PYTHON` override, the `KDIVE_SETUP_AUDITED` audited MCP path) and a different audience;
  `onboard` is the dev-stack / live-stack convenience. Two entry points, distinct audiences (the
  issue note: operator walkthroughs intentionally avoid `just`).
- `examples/local-libvirt/up.sh` — unchanged. It is the heavier guided bring-up that also starts the
  three processes and mints a token (project default `local`). `just onboard` is the lightweight,
  process-agnostic seed + **verify** + contract step; the verify-readback is the piece neither
  existing path has.
- The audited production onboarding (`accounting.set_budget`/`set_quota` admin tools,
  `docs/operating/project-onboarding.md`) — `onboard` is the token-less dev/demo path, as
  `seed-project` already is.
- Helm: out of scope for this recipe. The contract note points at `demo.oidc.claims` and warns that
  customizing `projects` there must match the seeded `$PROJECT` — documentation only, no code.

## Acceptance criteria

1. `just onboard` after `just stack-up` seeds `demo` and prints a contract showing
   `projects:["demo"]`, `roles:{"demo":"admin"}`, `project arg: "demo"` and an
   `export KDIVE_TOKEN=…` line; a subsequent `allocations.request(project="demo")` with that token is
   granted (not quota/budget-walled).
2. Re-running `just onboard` is a no-op upsert (idempotent) and still verifies + prints.
3. `verify-project --project P` against a migrated-but-**unseeded** DB exits non-zero naming the DB
   (the reachable form of "seed did not persist"); a seed step that errors aborts the recipe before
   it claims success. (Pointing seed *and* verify at the same wrong DB is not this check — both write
   and read it; see §2 "what verify does and does not prove".)
4. A provider-preflight FAIL prints a WARN but does **not** abort the seed (recipe still seeds,
   verifies, exits 0).
5. A token-mint failure (issuer down) prints a WARN, the contract, and the re-mint command, and the
   recipe still exits 0 (seed + verify already succeeded).
6. The printed token carries a 24 h TTL and a loud expiry warning.
7. `verify_project` returns `budget_present=False` when only the quota row exists, and vice versa
   (covered by the testcontainer unit tests).

## Failure modes / edges

- `KDIVE_DATABASE_URL` unreachable → `migrate`/`seed` raise and the recipe aborts before verify
  (fail loud).
- Migration drift (ADR-0015 immutable-migration guard) → `migrate` fails; the recipe surfaces the
  `up.sh --reset-db` recovery hint.
- Quota row present but `limit_kcu == 0` (a mis-seed) → verify reports presence and prints the
  figure `0`, so the zero is visible rather than masked; presence (not value) is the pass condition,
  matching the issue.
- `seed-project` registers discovered resources inside `seed_project`; if discovery surfaces no
  schedulable resource, the advisory preflight WARN is the signal — keeping the funding seed and the
  provider readiness concerns separately legible.
