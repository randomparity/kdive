# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## What this is

KDIVE (Kernel Debug, Inspect, Validate, Explore) is an MCP platform that gives agentic
coding environments a full Linux kernel build → boot → debug lifecycle across
heterogeneous resources. Local VMs are the default; remote libvirt is an
operator-configured opt-in provider; cloud, bare-metal, and PowerVM remain future targets.
It is a greenfield rewrite of a single-user stdio PoC into a multi-user HTTP service.
Python 3.14, managed with `uv`.

Read `docs/design/top-level-design.md` first — it is the authoritative architecture. The
current milestone plans are `docs/archive/plans/m0-implementation.md` and
`docs/archive/plans/m1-implementation.md`.

## Commands

The `justfile` is the **single source of truth** for build/lint/type/test commands. CI
(`.github/workflows/ci.yml`) and the pre-commit `ty` hook both invoke `just` recipes, so
run the same recipes locally rather than reinventing the underlying command:

| task | runs |
|------|------|
| `just setup` | check host deps, `uv sync --locked`, install + run git hooks |
| `just lint` | `ruff check` + `ruff format --check` |
| `just format` | `ruff check --fix` + `ruff format` (mutating) |
| `just type` | `ty check` — **whole tree (src + tests)**, not `src` alone |
| `just test` | the suite, excluding the gated `live_vm` marker |
| `just test-verbose` | same selection as `just test` with full error output (`-vv --tb=long`); optional path arguments scope the run, and passing any argument makes it serial |
| `just test-live` | the native `live_vm` suite (needs a KVM/libvirt host + kdump guest image) |
| `just test-live-tcg` | the emulated foreign-arch (`live_vm_tcg`) tier: the four ppc64le proofs; needs the foreign qemu emulator + a running stack, skips cleanly without either |
| `just ci` | the full PR gate: lint, type, lock-check, shell/workflow/Ansible lint, mermaid + doc-link guards, all generated-artifact checks, then the suite |
| `just compose-up` / `compose-down` | Postgres + MinIO + mock-OIDC backing services for a live run |
| `just stack-up` | bring the live-stack backends up healthy + print host-process env (see runbook) |
| `just test-live-stack` | the `live_stack` suite; skips cleanly when the stack/fixtures are absent |

Run a single test: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py::test_request_under_cap_grants -q`

Smaller-than-suite selections, all direct pytest against a path:

- **One block** — any node-ID prefix: a parametrized family
  (`tests/domain/test_errors.py::test_name` runs every parameter), a class
  (`.../test_mod.py::TestClass`), one file, or one directory.
- **Keyword expression** across files — `-k "taxonomy and not round_trips"`.

Both compose with the recipes below (`just test-verbose <path>::<block>` for full error
output on just that block).

**Choosing how to run the suite (agent guidance):** iterate on changed code with
`just test-changed`, rerun failures with `just test-lf`, and treat `just test` as the
pre-push gate. Default recipes run quietly (`-q` plus `-ra` from `addopts`), but that only
drops the per-test progress line and the header — it bounds nothing on the failure path.
What bounds it is `--tb=short`, carried by `just test`, `just test-lf`, and
`just test-changed` alike (ADR-0577): a `file:line: in func` entry and its source line for
every frame, in place of the full source context and argument values pytest's default
`--tb=auto` prints for the first and last frame, plus the failing expression and the
assertion message — so every failure stays individually diagnosable at roughly half the
bytes. When a failure needs a full frame or an assertion diff, escalate only the affected
paths (`just test-verbose tests/<dir>` or a single file) instead of re-running everything;
passing `test-verbose` any argument drops xdist, so its output is serial and readable top to
bottom. Name paths when you can: any argument drops xdist, so one that does not narrow the
run (`-x`, `--pdb`, a broad `-k`) runs the whole suite single-process. Serial is also a
different topology from the gate's, so a failure caused by xdist itself can vanish under
escalation — reproduce those with direct pytest carrying the gate's marker exclusion and
parallelism flags (`_TEST_MARKERS` / `_TEST_XDIST` in the justfile, where it is described).
For one known test, direct pytest stays fine (see above). Never pipe a gate recipe through
`tail`/`head`: a pipeline reports the *last* command's exit code, so the gate's own status is
lost. Redirection does not have that problem — `just ci > <file> 2>&1; echo $?` reports the
recipe's exit code faithfully — so capture output that way when you need to read it.

Agents should capture rather than inherit the harness's streams. `just ci` runs `lint-ansible`,
and ansible-core aborts with `ERROR: Ansible requires blocking IO on stdin/stdout/stderr` when
any of the three is non-blocking, which is how an agent harness commonly supplies them. Run it
as `just ci > <file> 2>&1 < /dev/null; echo $?`: the redirects give blocking regular files for
stdout and stderr, `< /dev/null` gives a blocking stdin, and the exit code stays truthful.

Reserve `just ci` for pre-push parity; while iterating, invoke the specific recipe you
need (`just lint`, `just type`, `prek run`).

**Before `git commit` (agent guidance):** let the mutating hooks rewrite the tree *before* the
commit attempt rather than during it. Four hooks in `.pre-commit-config.yaml` rewrite files in
place — `ruff-check` (which runs with `--fix`) and `ruff-format` (Python only),
`end-of-file-fixer` and `trailing-whitespace` (any text file). When one of them changes
something, `git commit` aborts with the tree modified and the same commit has to be re-staged
and retried, so that round trip is guaranteed rather than occasional for any commit touching a
file a hook rewrites.
`just format` settles the ruff pair, which is all a Python-only change needs. For anything
else (Markdown, YAML, shell), stage first and run `prek run`: it builds its file list from
the staged paths, stashes everything unstaged while it works, rewrites the staged files in
the working tree, and exits non-zero when it changed one — a rewrite, not a gate failure.
Record the staged set first (`git diff --cached --name-only`), then re-add exactly those
paths — `git add -- <the paths you staged>` — and commit, which now has nothing left to
rewrite. Not `git add -A` or `git add -u`: `prek run` restored every unrelated unstaged
file when it finished, and both would sweep those into this commit.

**Running the live tiers** — before re-deriving how to run a live test, read
[`docs/operating/runbooks/live-testing.md`](docs/operating/runbooks/live-testing.md).
It is the canonical map of the three live test tiers (`live_stack`, `live_vm`,
`live_vm_tcg`) and the `live_vm` families: each tier's `just` recipe, its
environment contract, and the hard-won quirks (`qemu:///session` vs system, a
short session-mode socket path via `XDG_CONFIG_HOME`, modular daemons, per-mode
confinement).

`just type` is whole-tree on purpose: scoping `ty` to `src` once let a test-tree type
error merge green, so `tests/` is type-checked only here. Don't narrow it back.

## Host prerequisites

- `libvirt-dev` and `python3-dev` system headers — `libvirt-python` has no wheels and
  compiles against both the libvirt and Python headers; `uv sync` fails without them. CI
  apt-installs them; the README lists the distro command. `drgn` and `psycopg[binary]`
  need nothing extra.
- `just` and `prek` must be installed before `just setup` (it can't bootstrap its own
  runner): `uv tool install rust-just && uv tool install prek`. On arches without prebuilt
  wheels/binaries (e.g. `ppc64le`), these plus `pydantic-core` build from source, so a
  Rust toolchain ([rustup](https://rustup.rs)) must be on `PATH` first. `just check-deps`
  enforces this per-arch; the [cross-platform guide](docs/development/cross-platform.md)
  covers the `ppc64le` prerequisites, container images, and POWER stack bring-up.
- The db/integration tests need a reachable Docker daemon (disposable Postgres via
  testcontainers). They **skip** when Docker is absent — unless `KDIVE_REQUIRE_DOCKER=1`
  (set in CI), which turns the skip into a hard failure so a broken runner can't mask the
  schema tests. The daemon is probed **once per process** and that verdict is latched
  (ADR-0580): a run where Docker answered keeps every gated test, so a daemon that dies
  mid-run reddens the suite instead of quietly shrinking it, and a run where Docker never
  answered skips them as a set. Ryuk is disabled (ADR-0401), so a run killed outright
  cannot reap its own shared backend container; the next run that starts one sweeps it
  instead, keyed to a lock the owning run holds while alive (ADR-0551). Containers
  started before ADR-0551 carry no `kdive.test-backend` label, and the sweep will not
  touch what it cannot prove it owns. Clear that backlog once, with no test run in flight on the host:

  ```sh
  docker ps -aq --filter "label=org.testcontainers=true" | xargs -r docker rm -fv
  ```

  This is **host-wide and not scoped to kdive** — it removes every testcontainers
  container from every project on the daemon, including any a concurrent run owns. The
  `-r` keeps it a no-op once the backlog is clear rather than an error.

## Architecture

### Four runtime roles, one codebase

`python -m kdive {server|worker|reconciler|lifecycle-witness}` (`src/kdive/__main__.py`):
- **server** — the FastMCP streamable-HTTP app; owns state machines, authz, admission
  control. Thin and fast; never blocks on a long provision.
- **worker** — pulls durable jobs from the Postgres-backed queue and runs provider
  operations. Long ops (provision/build/install/capture-vmcore) are jobs; the tool returns
  `{job_id, status: running}` and the agent polls `jobs.*`.
- **reconciler** — periodic drift-repair loop (ADR-0021): tears down orphaned Systems,
  fails Runs on torn-down Systems, reclaims expired leases, detaches dead DebugSessions.
- **lifecycle-witness** — deployment-specific worker-termination witness. Kubernetes runs it
  as a fourth long-running workload; the portable Compose and systemd core uses only the first
  three roles and handles lifecycle evidence through operator-side gates.

State of record is **Postgres**; bulk artifacts (vmcores, transcripts) live in an
**S3-compatible object store**, referenced by row. Postgres advisory locks replace the
PoC's flock.

### Six durable objects

`Resource ──< Allocation ──< System ──< Run ──< DebugSession`, plus a cross-cutting
`Investigation` that groups Runs across Allocations/resource kinds. Each is a Postgres row
with an explicit state machine. Lower layers outlive higher ones; a System never outlives
its Allocation. See the design doc's "Domain model" section for the precise lifecycles.

### The provider runtime seam

The active M0/M1 provider seam is `ProviderRuntime` typed ports (ADR-0063). Production
assembly happens in `providers/assembly/composition.py`, which builds a `ProviderResolver` over the
registered runtimes. The default production resolver registers local-libvirt; fault-inject is
a concrete test/failure-path opt-in provider; remote-libvirt is an operator-configured
opt-in provider wired through the same resolver/runtime seam. A provider still implements
narrow port protocols for the planes it supports (Discovery, Provisioning, Build, Install,
Connect, Debug, Control, Retrieve; Allocation is core, not a provider plane), but runtime
code calls those typed ports directly.

The old `CapabilityRegistry` / `OpContract` dispatch design now exists only in historical
ADRs and planning records (ADR-0066 removed the in-tree prototype). It is not the current
production assembly path. Production defaults to `providers/local_libvirt/`; fault-injection
deployments also register `providers/fault_inject/`, and remote deployments register
`providers/remote_libvirt/` when the operator supplies remote-libvirt configuration.

The falsifiable design hypothesis held for remote-libvirt: adding that provider was mostly
a provider implementation plus `ProviderRuntime` wiring. Future provider families such as
cloud, bare-metal, or PowerVM should follow that path unless a new ADR justifies broader
registry-based dispatch.

### Two registrar seams keep the entrypoint stable

`mcp/assembly/app.py` is the assembly facade. Tool/resource/prompt registrars live in
`mcp/assembly/tool_registration.py`; worker job-handler registration lives in
`jobs/assembly.py` as `register_all_handlers`. A new plane adds its direct registration there,
so `build_app` and `build_handler_registry` stay stable. MCP tools
(`mcp/tools/*.py`) are thin FastMCP wrappers over plain async handlers that take an injected
pool + `RequestContext`, so they are tested directly without a transport.

**The wrapper docstring is the agent-facing contract.** FastMCP serializes only the
`@app.tool`-decorated wrapper's docstring and its `Field(description=...)` text into the
tool schema — the inner handler's docstring, module docstrings, and `docs/` are invisible
to the agent at call time. When a tool's contract changes (parameters, returned `data`
fields, poll/retry/timeout semantics), update the **wrapper** docstring and `Field` text,
not only the handler. Guidance that lives only on the inner handler or in an ADR is a
discoverability defect even when the behavior is correct: the agent acts on the schema it
sees, so a contract it can't read it won't follow. When reviewing an MCP tool change,
verify the agent-facing text (wrapper docstring + `Field` descriptions) names every field
and constraint an agent must know, and does not invite a pattern the behavior discourages.

### Cross-cutting invariants (apply on every plane)

- **Uniform response envelope** — every tool returns a `ToolResponse` (`mcp/responses.py`):
  object id, status, `suggested_next_actions` (literal next tool names), artifact `refs`,
  and an `error_category` **iff** the status is a failure (enforced at construction).
  References, never log dumps.
- **State a limit's full contract** — For any limit you hand an agent, state all five:
  unit, reference clock, scope (per-request vs per-flow), consequence of violation, and
  recovery action. An agent has no wall clock and fills unspecified space with worst-case
  assumptions, so a bare relative limit (`expires_in: 3600`) reads as a hard wall it must
  route around — it will invent workarounds a fully-specified contract would have prevented.
  Prefer absolute deadlines plus a `server_time` reference clock over relative durations,
  surface the real (enforced) deadline rather than an incidental one, and name the recovery
  tool in `suggested_next_actions`. Every workaround an agent improvises marks a missing
  sentence in a contract (#1336).
- **State transitions are guarded data** — `domain/capacity/state.py` is a nested adjacency table;
  the repository layer (`db/repositories.py`) calls `can_transition` before persisting any
  state change. An illegal edge raises `IllegalTransition` (a programming error, distinct
  from operational `ErrorCategory` failures).
- **Stable error taxonomy** — `domain/errors.py` `ErrorCategory`. Pick the most specific
  existing value; never invent strings.
- **Secrets by reference + mandatory redaction** — secrets resolve at the worker boundary
  and register into the redaction registry for the op's lifetime; only `(present,
  source-ref)` persists. All guest/console/gdb output passes the redactor before
  persistence or any response snippet (`security/`).
- **Destructive-op gate** — `security/authz/gate.py`: `force_crash` requires both the
  allocation project's required RBAC role and an explicit profile opt-in. The former
  `capability_scope` factor was removed by ADR-0130; power, teardown, and reprovision now
  use their own lifecycle/RBAC paths.
- **Concurrency** — serialize per-Allocation and per-System via advisory locks; admission
  control's check-then-debit is atomic under a per-project lock. Idempotent steps keyed by
  `run_id` + step. The `tests/adversarial/` suite stress-tests these races.

## Conventions

- **Architecture decisions are ADRs** (`docs/adr/`, `NNNN-kebab-title.md`, monotonic
  numbers never reused). Don't change an accepted decision in place — write a new ADR that
  supersedes it. Most source modules cite the ADR(s) they implement in their docstring;
  follow the citation when changing behavior. Spec → plan → implementation cycles live
  under `docs/design/`, `docs/archive/plans/`, and `docs/archive/superpowers/`.
- **Releasing** — see [`docs/development/releasing.md`](docs/development/releasing.md) and
  [ADR-0041](docs/adr/0041-versioning-release-process.md) (SemVer, milestone→minor,
  tag-driven release).
- **Doc-style guard** (enforced in CI / `check-mermaid` is mermaid-only, but the prose
  rule is project-wide): use **Milestone**, never "Sprint"; keep prose plain and factual —
  avoid "critical", "robust", "comprehensive", "elegant". This applies to ADRs, specs,
  commit messages, and code comments.
- **Never pass a PR or issue body as a shell string.** Write the body to a file and use
  `gh pr create --body-file FILE` (same for `gh pr edit`, `gh issue create/comment`,
  `gh pr comment`). A body inside double quotes is shell source, not text: every
  `` `…` `` span in it runs as a command substitution. PR #2037 was written that way, so
  its markdown code spans executed — one of them was `env`, and five live API keys landed
  in a public pull-request body. Editing the body afterwards does **not** unpublish it;
  GitHub keeps every prior revision in `userContentEdits`, and a public repository mirrors
  the original to third parties. Never build a body with `--body "$(…)"` or a heredoc
  spliced into an outer quoted string. Scan the file before you publish it:
  `just check-pr-body FILE`. The `pr-body-scan` workflow runs the same checker against the
  live body, but it runs *after* GitHub has the text — it shortens exposure, it does not
  prevent it. The `detect-secrets` hook does not cover this at all: it scans repository
  files, and a body never becomes one.
- **`live_vm` tests** are skipped by default (marker in `pyproject.toml`); they need an
  operator-provided KVM/nested-virt host with libvirt and a kdump-enabled guest image, and
  run only as a manually-dispatched self-hosted CI job. Unit/service tests depend only on
  disposable Postgres + MinIO + mock OIDC.
- **`live_stack` tests** drive the spine over the real MCP HTTP transport against the portable
  three-role host stack (`server`/`worker`/`reconciler`) + the compose backends; they do not run
  the Kubernetes-only `lifecycle-witness`. Operator bring-up is in
  [`docs/operating/runbooks/live-stack.md`](docs/operating/runbooks/live-stack.md) (ADR-0042). `just
  test-live-stack` skips cleanly when the stack/fixtures (or the marked suite) are absent.
- **Three live tiers** (ADR-0353): `live_vm` (native, direct-provider ops against a
  pre-provisioned System); `live_vm_tcg` (the emulated foreign-arch spine — the four ppc64le
  proofs, run over the `live_stack` vehicle, selected by `just test-live-tcg`); `live_stack`
  (full HTTP transport). `just test-live` is native-only (`-m "live_vm and not live_vm_tcg"`);
  the TCG tier skips cleanly on a host without the foreign qemu emulator (`require_guest_arch`).
  The self-hosted native-KVM runner host is codified in `deploy/ansible/playbooks/runner.yml`
  (+ the `self-hosted-kvm-runner.md` runbook under `docs/operating/runbooks/`), built to the
  `live_vm` environment contract in `tests/live_vm/__init__.py` (ADR-0387, #1291).
- **Provisioning parity is the extender's job.** The runner is a cattle host, reprovisioned
  from these Ansible roles — nothing is hand-installed. So when you extend the live stack with
  a new host tool or system package (a `subprocess`-invoked binary, a build-fs/libguestfs
  dependency, an introspection tool), you MUST declare it in the role that owns that layer in
  the same change: general build-host FS/virt tools in `libvirt_stack`, the `live_vm` debug
  toolchain and store-script deps in `live_vm_host`. An undeclared host dep passes on your
  already-warmed dev box and silently breaks the next clean runner reprovision. If a live proof
  makes you `apt install` something by hand, that package is a role edit you still owe.
- Tests mirror the package tree under `tests/`; `tests/adversarial/` holds concurrency /
  property-based (hypothesis) race tests, `tests/integration/` holds the end-to-end
  milestone exercises.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults (no
  project-wide relaxations); the unstubbed C-extension deps (`libvirt-python`, `drgn`)
  suppress `unresolved-import` with a scoped per-site ignore.
- Runtime env vars are `KDIVE_*` (`KDIVE_DATABASE_URL`, `KDIVE_OIDC_*`, `KDIVE_S3_*`,
  `KDIVE_HTTP_HOST/PORT`, `KDIVE_LOG_LEVEL`); see `docker-compose.yml` for a working set.
