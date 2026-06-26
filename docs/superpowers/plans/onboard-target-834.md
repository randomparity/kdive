# Plan — `just onboard` target (#834)

- **Spec:** [`../specs/2026-06-26-onboard-target-834.md`](../specs/2026-06-26-onboard-target-834.md)
- **ADR:** [`../../adr/0256-onboard-target.md`](../../adr/0256-onboard-target.md)
- **Branch:** `feat/onboard-target-834` (this worktree; no subagent fan-out — the tasks are
  sequential and share `bootstrap.py` / `__main__.py` / the live-stack scripts).

This is one cohesive change implemented directly in this session. Order matters: the Python
`verify_project` + redaction land first (with unit tests), then the `verify-project` CLI, then the
shell recipe that calls them, then the justfile/up.sh/docs wiring.

## Guardrails (run before every commit)

- `just lint` — `ruff check .` + `ruff format --check .`
- `just type` — `ty check` (whole tree, src + tests)
- `just lint-shell` — `shfmt` + `shellcheck` over `scripts/` (the new `onboard.sh` is covered)
- focused tests: `uv run python -m pytest tests/admin/test_bootstrap.py tests/scripts/test_onboard.py tests/test_main_version.py -q`
- doc guards when docs change: `python3 scripts/check_adr_status.py`, `./scripts/check-doc-links.sh`,
  `./scripts/check-doc-paths.sh`, `just check-mermaid`
- full suite once before first push: `just test`

## Conventions

- Absolute imports only; Google-style docstrings on public APIs; ≤100 lines/function; line length
  100; plain factual prose (no "robust"/"comprehensive"/"Sprint").
- Bash scripts start with `set -euo pipefail`; pass `shellcheck` + `shfmt -i 2`. Mirror the existing
  live-stack scripts' style (source `scripts/live-stack/env.sh`, compute `repo_root` from
  `BASH_SOURCE`).
- The token and any DB URL printed at runtime are demo-only secrets; the URL is redacted, and the
  scripts repeat the existing "never against a real deployment" warning. Never commit a token/URL.

## Task 1 — Failing tests: redaction + `verify_project` + result formatting (TDD red)

**Where it fits:** spec §2 (verify-readback), AC #3 and #7.

**Files:** new tests in `tests/admin/test_bootstrap.py` (extend the existing module).

Write tests (no implementation yet):

1. `redact_database_url(url)`:
   - A standard `postgresql://` URL carrying a userinfo password → password replaced with `***`,
     scheme/host/port/dbname intact.
   - The same URL with no password component → returned unchanged (no spurious `***`).
   - A non-URL keyword/value conninfo string containing a `password=…` token → that token masked
     (or, if unparseable, a safe `<redacted>` fallback) — assert the password value is absent from
     the output.
2. `verify_project(project)` against a testcontainer DB (reuse the existing `tests/admin`
   pool/migrate fixtures — check how `test_bootstrap.py` seeds; mirror it):
   - both rows seeded → `ProjectFundingStatus(budget_present=True, quota_present=True,
     limit_kcu=…, spent_kcu=0, max_concurrent_allocations=…, occupancy=0)`.
   - only the quota row → `budget_present=False, quota_present=True`.
   - only the budget row → `budget_present=True, quota_present=False`.
   - neither row → both `False`.
3. `format_verify_result(status, project, redacted_url) -> tuple[str, int]` (pure):
   - both present → exit code `0`, message names the project, the redacted DB, and the figures.
   - either absent → exit code `1`, message names the project, the redacted DB, and which
     row(s) are missing.

**Acceptance:** tests fail with ImportError/AttributeError (nothing implemented yet).

**Run:** `uv run python -m pytest tests/admin/test_bootstrap.py -q` → red.

## Task 2 — Implement redaction + `verify_project` + formatter (TDD green)

**Where it fits:** the verify mechanism.

**Files:** `src/kdive/admin/bootstrap.py`.

1. `@dataclass(frozen=True, slots=True) class ProjectFundingStatus` with
   `budget_present: bool`, `quota_present: bool`, `limit_kcu: Decimal | None`,
   `spent_kcu: Decimal | None`, `max_concurrent_allocations: int | None`, `occupancy: int`, and a
   `funded` property (`budget_present and quota_present`).
2. `async def verify_project(*, project) -> ProjectFundingStatus`: open `create_pool()`; in one
   connection call `budget_snapshot(conn, project)` (→ `(limit, spent) | None`) and
   `quota_status(conn, project)` (→ `(max_alloc | None, occupancy)`); map `None` → `*_present=False`.
   Import `budget_snapshot` from `kdive.services.allocation.idempotency` and `quota_status` from
   `kdive.services.allocation.admission.core` (the deliberate single-source-of-truth coupling the
   ADR records). Close the pool in `finally`, mirroring `seed_project`. Add a one-line docstring/
   comment noting these are reused here as **advisory point-reads outside the PROJECT lock**
   (`quota_status` is documented "read under the held PROJECT lock") — acceptable because verify
   reports state, it does not make an admission decision.
3. `def redact_database_url(url: str) -> str`: `urllib.parse.urlsplit`; if `parsed.password`,
   rebuild netloc with the password replaced by `***`; for a non-URL conninfo, regex-mask a
   `password=<...>` token, else return `<redacted>`. Stdlib only.
4. `def format_verify_result(status, project, redacted_url) -> tuple[str, int]`: build the
   human-readable line(s) + exit code per Task 1.3.

**Acceptance:** Task 1 tests pass. `just lint`, `just type`, focused tests green.

## Task 3 — `verify-project` CLI command (TDD red → green)

**Where it fits:** spec "In scope" — `src/kdive/__main__.py`.

**Files:** `src/kdive/__main__.py`; tests in `tests/test_main_version.py` (or a sibling
`tests/test_main_verify_project.py` if cleaner — check where CLI-registration tests live).

1. Red: (a) a test asserting the parser registers `verify-project` with `--project` (default `demo`)
   — mirror how an existing command's registration is asserted (`_COMMAND_BY_NAME`/`build_parser`);
   (b) **required** exit-code propagation tests — the whole gate rests on this wire, and Task 1.3
   (pure `format_verify_result`) and Task 4 (stubbed verify-project) do not exercise the real CLI →
   process-exit path. Drive the real handler against the testcontainer `migrated_url`:
   migrated-but-**unseeded** DB → `_handle_verify_project` raises `SystemExit` with a non-zero code
   (or a `python -m kdive verify-project` subprocess returns non-zero); both rows seeded → exit 0.
   This closes the `format_verify_result` code → `SystemExit` seam, so a handler that prints but
   forgets to propagate the code cannot regress onboard into claiming success on an unseeded DB.
2. Green: add `_add_verify_project_arguments` (`--project`, default `demo`), `_handle_verify_project`
   (runs `asyncio.run(verify_project(project=args.project))`, computes
   `redact_database_url(database_url())`, calls `format_verify_result`, prints the message, and
   `raise SystemExit(code)` on the returned exit code), and the `_Command("verify-project", …)`
   entry in `_COMMANDS`. Keep the handler ≤ the wire described; all logic is in `bootstrap`.

**Acceptance:** `python -m kdive verify-project --help` lists `--project`; registration test green.
`just lint`/`just type` green.

## Task 4 — `onboard.sh` + behavioral shell tests (TDD red → green)

**Where it fits:** spec §1–§5 — `scripts/live-stack/onboard.sh`.

**Files:** new `scripts/live-stack/onboard.sh`; new `tests/scripts/test_onboard.py` (mirror
`tests/scripts/test_setup_local_libvirt.py`'s PATH-stub harness).

1. Red: write `tests/scripts/test_onboard.py` first. Stub `uv` (logs `"$@"`, routes by subcommand)
   and the preflight bins (`virsh`/`id`/`qemu-img`/`python3`) so the real `check-local-libvirt.sh`
   can be driven pass/fail. Assert:
   - happy path → runs `migrate`, then `seed-project --project demo`, then `verify-project --project
     demo`, then the mint heredoc with `86400`; exit 0; prints the contract block
     (`projects:["demo"]`, `roles:{"demo":"admin"}`, `project arg: "demo"`).
   - `KDIVE_PROJECT=acme` → all three subcommands carry `--project acme` and the contract shows
     `acme`.
   - preflight FAIL (stub makes `check-local-libvirt.sh` exit 1) → WARN printed, **seed still runs**,
     exit 0 (the advisory-vs-hard-fail divergence from `setup-local-libvirt.sh`).
   - `verify-project` exit 1 (stub routes that subcommand to exit 1) → recipe exits non-zero, mint
     does **not** run. (verify is the hard funding gate.)
   - `seed-project` exit 1 but `verify-project` exit 0 (stub routes seed→1, verify→0; models a
     post-commit discovery failure — see below) → WARN about the seed/discovery failure, recipe
     **continues** to mint, exit 0.
   - mint failure (stub routes the mint call to exit 1) → WARN printed, contract + re-mint command
     printed, exit 0.
   - `KDIVE_ROLE=viewer` → a sub-contributor WARN is printed; exit 0.
2. Green: write `onboard.sh`:
   - `set -euo pipefail`; compute `repo_root`; `source scripts/live-stack/env.sh`.
   - `PROJECT="${KDIVE_PROJECT:-demo}"`, `ROLE="${KDIVE_ROLE:-admin}"`,
     `TTL="${KDIVE_TOKEN_TTL:-86400}"`, `LIMIT_KCU`/`MAX_ALLOC`/`MAX_SYS` defaults matching
     `seed-project`.
   - advisory preflight: `if ! "${repo_root}/scripts/check-local-libvirt.sh"; then echo "WARN …" >&2;
     fi` (never aborts).
   - `migrate` (hard): `uv run python -m kdive migrate` (no schema → no rows; a failure aborts).
   - `seed-project` (its **funding rows** are the hard part, its **discovery** is advisory):
     `seed_project` commits the budget/quota upserts in a `conn.transaction()` block *before*
     `register_discovered_resources` runs (`src/kdive/admin/bootstrap.py`), and
     `register_all_discovery` re-raises a composed-but-unreachable provider's registration failure
     (`providers/core/resolver.py`). So run seed capturing its exit
     (`if ! uv run python -m kdive seed-project --project "$PROJECT" …; then seed_rc=1; fi`) rather
     than letting `set -e` abort — the committed rows survive a discovery raise.
   - `verify-project` (the **hard funding gate**): `uv run python -m kdive verify-project --project
     "$PROJECT"` (its output is the redacted-DB echo + figures; a non-zero exit — rows absent —
     aborts via `set -e`). If `seed_rc=1` but verify passed, print a WARN that seed's
     resource-discovery step failed (provider likely unreachable; preflight has the detail) while
     the funding rows are committed, and continue. This makes verify, not seed's exit code, the
     funding source of truth — the spec's "verify is the reliability gate" thesis — and keeps a
     libvirt-unreachable host from blocking funding (consistent with the advisory preflight).
   - role floor: `case "$ROLE" in viewer) echo "WARN: role '$ROLE' is below contributor; the token
     will be funding-walled" >&2 ;; esac`.
   - best-effort mint: `if token=$(uv run python - "$PROJECT" "$TTL" "$ROLE" <<'PY' … mint_local_token
     … PY); then print "export KDIVE_TOKEN=$token"; else echo "WARN: token mint failed (issuer
     down?)"; print the re-mint command; fi`.
   - print the contract block unconditionally + the loud 24 h expiry warning + the demo-only/never
     against production warning.
   - `chmod +x`.

**Acceptance:** `tests/scripts/test_onboard.py` green; `just lint-shell` clean.

## Task 5 — justfile recipe + up.sh suggestion + docs

**Where it fits:** spec "In scope" — wiring + discoverability.

**Files:** `justfile`, `scripts/live-stack/up.sh`, `docs/operating/runbooks/live-stack.md`,
`scripts/live-stack/README.md`.

1. `justfile`: add an `onboard` recipe near `setup-local-libvirt` calling
   `./scripts/live-stack/onboard.sh`, with a one-line doc comment (mention `KDIVE_PROJECT` default
   `demo`).
2. `up.sh`: in the final status output, add a line suggesting `just onboard` (up.sh still does not
   seed; onboard is the seed step). Keep `shfmt`/`shellcheck` clean.
3. Docs: a short "Onboard a demo project" subsection in the live-stack runbook and a one-liner in
   `scripts/live-stack/README.md` — the `just stack-up; just onboard` sequence, the printed
   token-contract framing, the 24 h token + re-mint note, and the env==server-DB prerequisite. No
   `just` in the *operator* walkthroughs (`docs/operating/local-stack.md` / `project-onboarding.md`
   stay as-is); this is the dev-stack convenience.

**Acceptance:** `just --list` shows `onboard`; doc guards green; `just lint-shell` clean.

## Rollback / cleanup

- Pure-additive: new CLI subcommand, new script, new recipe, new tests, doc additions. No schema,
  no migration, no change to `setup-local-libvirt.sh` / `examples/local-libvirt/*`. Reverting the
  branch removes the recipe with no residue.
- `verify-project` and `onboard.sh` are idempotent and read-only except for the seed they delegate
  to. The seed is an idempotent budget/quota upsert (committed before its resource-discovery side
  effect, which is itself an idempotent re-registration), so a re-run or a half-finished run — even
  one where discovery raised — leaves the funding rows consistent and re-runnable.

## Verification (live, where the host allows)

Per repo memory the dev host runs KVM/libvirt directly. After unit/shell tests pass, optionally
prove end-to-end: `just stack-up` then `just onboard`, confirm the contract + token print and
`verify-project` reports both rows, then mint the token and confirm a first `allocations.request`
is granted. The unit + stubbed-shell tests are the gating evidence; the live run is confirmation.
