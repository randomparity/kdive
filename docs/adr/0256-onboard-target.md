# ADR 0256 — A reliable `just onboard` target that seeds funding and binds the project (#834)

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** `seed-project` (`src/kdive/admin/bootstrap.py`), the live-stack
  env (`scripts/live-stack/env.sh`), `mint_local_token` (`src/kdive/cli/login.py`),
  [ADR-0007](0007-metering-budgets-admission.md) (fail-closed funding invariants),
  [ADR-0044](0044-mcp-wire-harness-oidc-token-issuance.md) (the project/role JWT claims).
- **Related:** [ADR-0255](0255-aggregate-funding-gate-denials.md) (#833 — the server-side aggregate
  denial for the same onboarding friction).
- **Issue:** [#834](https://github.com/randomparity/kdive/issues/834).
- **Spec:** [`../superpowers/specs/2026-06-26-onboard-target-834.md`](../superpowers/specs/2026-06-26-onboard-target-834.md).

## Context

A fresh agent's first `allocations.request` is funding-walled unless **three strings are identical**:
the seeded `budgets`/`quotas` key, the token's `projects`/`roles` JWT claim, and the `project`
argument. `seed-project` controls only the first. The two existing seed entry points each miss a
piece: `scripts/setup-local-libvirt.sh` seeds but neither migrates nor sources
`scripts/live-stack/env.sh` (so `KDIVE_DATABASE_URL` may be unset or point at a different Postgres
than the server reads — the seed lands in the wrong DB), and `scripts/live-stack/up.sh` migrates but
never seeds. Nothing reads the rows back to prove the seed is durable in the DB the server uses, and
nothing makes the project-name binding explicit. The binding (seeded string == token claim ==
`project` arg, and whether the recipe mints/prints the token) was deferred `needs-design`.

## Decision

Add a `just onboard` recipe (`scripts/live-stack/onboard.sh`) that composes the existing primitives
into one idempotent, self-verifying, project-binding-explicit funding step for the dev/live stack.

1. **One project string, single source of truth.** The recipe reads one `PROJECT`
   (`KDIVE_PROJECT`, default `demo`) and threads the same variable into the seed
   (`seed-project --project "$PROJECT"`), the verify (`verify-project --project "$PROJECT"`), the
   minted token's claims (`projects=["$PROJECT"]`, `roles={"$PROJECT": <role>}`), and the printed
   token-contract block. The three strings are equal by construction.

2. **Verify-readback is the reliability gate.** A new token-less `kdive verify-project --project P`
   re-opens a fresh `KDIVE_DATABASE_URL` connection and asserts both a `budgets` and a `quotas` row
   exist for `P`, printing the figures, reusing the *exact* reads admission control uses
   (`budget_snapshot`, `quota_status`) so "verified" means "the admission funding reads return rows"
   with no second lookup to drift. Either row absent → non-zero exit; the recipe fails loud, naming
   the DB. verify resolves `KDIVE_DATABASE_URL` the same way the seed did in the same run, so its
   guarantee is that the rows are durably present and readable in the **targeted** DB (catching a
   rolled-back seed, a missing schema, or a silent no-op) — not that the targeted DB is the server's.
   That identity comes from §5; the recipe echoes the credential-redacted resolved URL (password
   masked) so a skew is visible without printing a secret.

3. **Token: a real 24 h token, best-effort, loud expiry warning.** The recipe mints a bearer token
   via the existing `mint_local_token` with `ttl_seconds=86400`, role `admin` (`KDIVE_ROLE`
   override) + `platform_admin`/`platform_operator` (mirroring `examples/local-libvirt/mint-token.sh`
   so the demo token reaches every tool), and prints it as `export KDIVE_TOKEN=…` plus a loud "this
   token expires in 24 h" warning. The seed + verify are already durable, so a mint failure (issuer
   down) **warns and the recipe still exits 0**; the token-contract block prints unconditionally.

4. **Advisory provider preflight.** `scripts/check-local-libvirt.sh` runs so a later "no schedulable
   resource" denial is not mistaken for a funding wall, but a FAIL only WARNs — funding is
   independent of libvirt readiness, so a missing `/dev/kvm` must not block funding. This is the
   deliberate divergence from `setup-local-libvirt.sh` (which hard-fails preflight).

5. **Order / env / gating.** `source live-stack/env.sh` → advisory preflight → `migrate`
   (idempotent) → `seed-project` → `verify-project` → mint + contract. **Hard gates: `migrate` and
   `verify-project`** (the funding rows are present); **advisory: preflight, the seed's
   resource-discovery side effect, and the mint.** `seed_project` commits the budget/quota upserts
   before it registers discovered resources, and `register_all_discovery` re-raises an unreachable
   composed provider's failure — so the recipe runs the seed capturing its exit and lets verify
   decide: rows present → a non-zero seed is a WARN (discovery failed, funding committed), rows
   absent → fail. verify, not the seed exit code, is the funding source of truth, so a
   libvirt-unreachable host still funds its project. The targeted DB equals the server's DB only
   when the server is
   brought up from the same env (the live-stack convention: `lib.sh restart_host_processes` sources
   `env.sh`); a server started with an overriding `KDIVE_DATABASE_URL` absent at onboard time is a
   skew the token-less recipe cannot detect, so it echoes the credential-redacted resolved URL
   (password masked — the override case is when the URL likely holds a real secret) rather than
   asserting identity. The minted role must be ≥ contributor to pass the `allocations.request` gate; a
   sub-contributor `KDIVE_ROLE` (e.g. `viewer`) WARNs (the seed stays valid) rather than failing.

The two existing seed paths are left unchanged: `setup-local-libvirt.sh` is the package-install /
audited path (different audience, no `just`); `examples/local-libvirt/up.sh` is the heavier guided
bring-up that also starts the processes. `onboard` is the lightweight, process-agnostic seed +
**verify** + contract step the others lack.

## Consequences

- A fresh stack reaches a granted first allocation in two commands (`just stack-up; just onboard`)
  with the project binding printed, not inferred. The verify step converts a seed that did not
  persist (rollback, missing schema, silent no-op) into a loud failure naming the targeted DB.
- `verify-project` becomes a reusable, scriptable, token-less funding check for any project, not just
  a recipe internal.
- `bootstrap.py` gains a deliberate import of two service-layer reads (`budget_snapshot`,
  `quota_status`) so the verify checks exactly what admission checks. The coupling is intentional:
  one source of truth for "is this project funded," accepted over a second copy of the SQL that
  could drift from admission.
- A new `onboard` path adds a third onboarding-shaped entry point; the spec's "Out of scope" section
  records why each existing one stays (distinct audiences), so the surface is legible rather than
  redundant.
- The demo token is printed to the terminal (a secret). It is mock-issuer-only and short-lived; the
  script repeats the existing "never against a real deployment" warning.

## Considered & rejected

- **Print only the claim contract, no token.** Lower friction-removal: the whole issue is a fresh
  agent hitting the wall, and `mint_local_token` already exists. Minting closes the loop; the
  contract still prints so the binding is explicit either way.
- **Hard-fail on the provider preflight** (as `setup-local-libvirt.sh` does). Conflates provider
  readiness with funding; a host without `/dev/kvm` could still legitimately want its project funded.
  Advisory keeps the two concerns separately legible.
- **Make `up.sh` seed automatically.** Folds funding into every stack bring-up and changes a
  sudo-elevated script's behavior; keeping `onboard` a separate idempotent recipe (suggested from
  `up.sh`'s status output) is better scoped.
- **Replace `setup-local-libvirt.sh`/`examples` with `onboard`.** They serve the package-install and
  full-bring-up audiences respectively; collapsing them would regress those paths.
- **Raw SQL readback in the recipe (or duplicated in `bootstrap`).** A second copy of the
  budget/quota lookup can drift from what admission actually reads; reusing `budget_snapshot` /
  `quota_status` guarantees the verify matches the gate.
- **A `kdive mint-local-token` CLI subcommand.** `mint_local_token` is already the public entry
  point and the heredoc pattern is established in `examples/local-libvirt/mint-token.sh`; a new
  subcommand is surface the recipe does not need.
