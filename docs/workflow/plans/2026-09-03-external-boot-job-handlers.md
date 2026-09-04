# Implementation plan — external-boot job payloads and lifecycle handlers

Issue [#2205](https://github.com/randomparity/kdive/issues/2205). Spec:
[2026-09-03-external-boot-job-handlers-design.md](../specs/2026-09-03-external-boot-job-handlers-design.md).
Decision: [ADR-0593](../../adr/0593-external-boot-operations-ride-marked-boot-and-teardown-jobs.md).

## Goal

A `boot` or `teardown` job whose payload carries a validated `ExternalBootAuthorityMarkerV1` is
claimed by a worker, routed to a per-operation handler, allocates authority as `kdive_worker`,
calls the injected `ExternalBootPorts` method, and returns an `ExternalBootAuthorityResultV1`
the worker commits through `commit_external_boot_authority_result`.

## Architecture

Two payload subclasses carry an optional marker under the literal JSONB key
`external_boot_authority_v1`, which is what every schema fence tests. One shared router,
injected as a required keyword into the `runs` and `systems` registrars, sends a marked job to
an operations registry instead of to `boot_handler` / `teardown_handler`. Six operation handlers
share one runner that resolves the provider binding, reads the activation, allocates authority,
obtains an acknowledgement through a one-method seam, calls the port, and returns the result.
The worker commits it; the `SECURITY DEFINER` commit finalizes the `jobs` row.

## Tech stack

Python 3.14, pydantic v2, psycopg 3 (async), pytest + testcontainers Postgres. `uv` for
everything.

## Global constraints

- **Base branch** `main`. **Branch** `feat/external-boot-job-handlers-2205`, cut from
  `54f346f553861f949c7df1957cae6f7915673231`.
- **Guardrails.** `just ci` bare — never piped, never with a trailing `; echo $?`; capture with
  `just ci > FILE 2>&1 < /dev/null` when the output is needed. While iterating: `just lint`,
  `just type`, `just test-changed`. A fresh worktree needs `just install-mermaid-deps` once, or
  `check-mermaid` fails with `ERR_MODULE_NOT_FOUND: jsdom`. A `failed=1` in `test-ansible`'s
  output is an intended negative case, not a red.
- **Ruff** line length 100, lint set `E,F,I,UP,B,SIM`. **`ty`** strict, whole tree including
  `tests/`.
- **Prose rule** (applies to code comments, docstrings, ADRs, commit messages): **Milestone**,
  never "Sprint"; avoid critical, robust, comprehensive, elegant.
- **No new dependency. No migration. No new `JobKind` member.**
- **Do not modify:** `src/kdive/db/schema/0122_external_boot_authority.sql`,
  `src/kdive/db/schema/0127_reopen_external_boot_claim_lane.sql`,
  `src/kdive/reconciler/repairs/jobs.py`, `src/kdive/jobs/worker.py`,
  `src/kdive/jobs/queue.py`, `src/kdive/jobs/models.py`.
- **Never pass a PR/issue body as a shell string**; write it to a file and use `--body-file`,
  scanned first with `just check-pr-body FILE`.
- **Postgres role facts that decide test validity.** There is no async connection or pool
  fixture anywhere in the suite. `migrated_url` is a plain `str`; tests open async resources
  inline. Everything connects as the backend **superuser** by default, and `pg_has_role` is
  true for a superuser against every role — so a test meaning to prove a privilege boundary
  proves nothing unless it connects through a real LOGIN role. The only route is the
  `authority_role_dsns` fixture in `tests/db/external_boot_authority_support.py:85`, which
  creates per-role LOGIN principals and returns a callable mapping a role name to a DSN; pass
  its DSN to `psycopg.AsyncConnection.connect` yourself.
- **Expected implementation size: 1900–2800 changed lines (L)** — derived from the file map
  below and reconciled against it: roughly 1000 lines of `src/` across **ten** new modules and
  **ten** edited files (of which five are one-identifier enqueue swaps), roughly 1300 lines of
  tests across **eleven** new test files plus one package `__init__.py`, and roughly 20 lines
  across the **six** existing test files Task 1 breaks. This agrees with the issue's `effort:L`
  label and with the charter's `complexity: high`.

## File map

### New — `src/kdive/jobs/handlers/external_boot/`

| File | Answerable for |
|---|---|
| `__init__.py` | Package docstring citing ADR-0593; re-exports `register_handlers`, `ExternalBootHandlerPorts`, `ExternalBootOperations` |
| `ports.py` | `ExternalBootAuthorityAcknowledger` protocol, `ExternalBootHandlerPorts` dataclass, `EXTERNAL_BOOT_AUTHORITY_MARKER_KEY` |
| `authority.py` | The two authority SQL calls this package owns: `allocate_authority` (wrapping `allocate_external_boot_authority`) and the `AllocatedAuthority` value |
| `operations.py` | `ExternalBootOperations` registry, `ENQUEUEABLE_OPERATIONS`, `DuplicateExternalBootHandler` |
| `router.py` | `route_marked` — the one place a marked job is diverted |
| `runner.py` | The shared seven-step runner and its failure wrapping |
| `evidence.py` | Evidence composition from persisted rows plus the acknowledgement |
| `lifecycle.py` | The six operation handlers |
| `registrar.py` | `build_operations(ports)` — binds the six handlers, raising on a duplicate |
| `admission.py` | `build_external_boot_payload` — the enqueue-side helper |

### Edited

| File | Change |
|---|---|
| `src/kdive/jobs/payloads.py` | `BootPayload`, `TeardownPayload`, registry entries, union aliases |
| `src/kdive/jobs/assembly.py` | Build the operations registry; pass it to both registrars |
| `src/kdive/jobs/handlers/runs/registrar.py` | Required `external_boot` keyword; wrap `JobKind.BOOT` |
| `src/kdive/jobs/handlers/runs/boot.py` | `load_payload(job, BootPayload)` |
| `src/kdive/jobs/handlers/systems.py` | Required `external_boot` keyword; wrap `JobKind.TEARDOWN`; `load_payload(job, TeardownPayload)` |
| `src/kdive/jobs/service_operations.py` | `TeardownPayload(...)` at the enqueue |
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | `TeardownPayload(...)` at the enqueue |
| `src/kdive/mcp/tools/ops/security/breakglass.py` | `TeardownPayload(...)` at the enqueue |
| `src/kdive/reconciler/repairs/systems.py` | `TeardownPayload(...)` at the enqueue |
| `src/kdive/mcp/tools/lifecycle/runs/steps.py` | `BootPayload(...)` at the enqueue |
| `tests/mcp/systems_support.py` | its `provider_resolver` builder gains an `external_boot` parameter, so a test can bind a port under `ResourceKind.LOCAL_LIBVIRT` |

### Edited — existing tests Task 1 breaks

`_ACTIVE_PAYLOAD_MODELS` naming the subclass breaks every site that passes a *base-class model
instance* to `queue.enqueue` for these kinds, in tests as well as in `src/`.

**This plan does not claim a complete list, because it cannot produce one statically and an
earlier draft got it wrong by trying.** Two of the sites are inside helpers that take `kind` as a
parameter (`tests/adversarial/test_provider_state_races.py::_enqueue`,
`tests/mcp/lifecycle/test_runs_tools.py::_enqueue_job`), so grepping `JobKind.(BOOT|TEARDOWN)`
finds the *call sites* and not the line to edit; and a payload constructed inside a
`parametrize` tuple is invisible to a simple AST sweep of `enqueue(...)` arguments. Instead:

- **The authoritative enumeration is the test run.** Task 1 step 6 makes the `src/` change first
  and then runs the whole suite. Every broken site announces itself as a
  `PayloadValidationError`, by exact node id. Fix precisely those. This is complete by
  construction and needs no list to be right.
- **A candidate list is a search aid only.** An AST sweep for `enqueue(...)` calls whose payload
  argument is a `RunPayload(...)`/`SystemPayload(...)` construction — inline or via a local —
  reports **25 candidate sites across 16 files** on this base. Most are `PROVISION`,
  `FORCE_CRASH`, or `POWER` and are unaffected; only the ones reachable with `BOOT`/`TEARDOWN`
  break. The known-affected ones seen so far are `tests/adversarial/test_provider_state_races.py`
  (`_enqueue`, reached with `TEARDOWN` from three call sites),
  `tests/mcp/lifecycle/test_runs_tools.py` (`_enqueue_job`, reached with `BOOT` from about
  fourteen, plus one parametrized construction),
  `tests/jobs/handlers/test_systems_bootstrap_key.py`, `tests/jobs/test_worker.py`,
  `tests/mcp/jobs/test_jobs_tools.py`, and `tests/mcp/lifecycle/test_systems_tools.py` — six
  files, but treat that as where to look first, not as the answer.

**Why the `src/` enqueue files and the test files are in scope.** `dump_payload` is called from
exactly one place (`src/kdive/jobs/queue.py:90`). Once `_ACTIVE_PAYLOAD_MODELS` names the
subclass, `dump_payload`'s `isinstance(payload, model_class)` is False for a `RunPayload`
instance, and pydantic v2 refuses to validate one model instance as a different model class —
confirmed here with pydantic 2.13.4: `Sub.model_validate(base_instance)` raises
`ValidationError … model_type`. The edit is one identifier per site and is an unavoidable
consequence of the sourced criterion that the boot and teardown payloads round-trip; it is not
new scope. The charter's `surface` field listed none of them, so the PR body must name **both**
groups — the `src/` enqueue files and the test files — and must state the count that the Task 1
step 6 run actually produced, not a count predicted before it.

### New tests

`tests/jobs/test_external_boot_payloads.py`,
`tests/jobs/handlers/external_boot/{__init__.py,conftest.py,test_operations.py,test_router.py,test_admission.py,test_runner.py,test_lifecycle.py,test_role_gate.py,test_prepared_before_admission.py,test_import_closure.py}`,
`tests/integration/test_external_boot_job_lifecycle.py`.

---

## Task 1 — Payload models carry the marker

**Creates/modifies:** `src/kdive/jobs/payloads.py`,
`src/kdive/jobs/handlers/runs/boot.py`, `src/kdive/jobs/handlers/systems.py`,
`src/kdive/jobs/service_operations.py`, `src/kdive/mcp/tools/lifecycle/runs/steps.py`,
`src/kdive/mcp/tools/lifecycle/systems/admin.py`,
`src/kdive/mcp/tools/ops/security/breakglass.py`,
`src/kdive/reconciler/repairs/systems.py`.
**Tests:** `tests/jobs/test_external_boot_payloads.py`.

### Interfaces this task publishes

```python
class BootPayload(RunPayload):
    external_boot_authority_v1: ExternalBootAuthorityMarkerV1 | None = None


class TeardownPayload(SystemPayload):
    external_boot_authority_v1: ExternalBootAuthorityMarkerV1 | None = None


ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"}
)
```

All three live in `src/kdive/jobs/payloads.py`. The frozenset is defined here rather than in the
handler package because the payload validator is its first consumer and `payloads.py` must not
import from `kdive.jobs.handlers` — `operations.py` imports it from here instead.

Later tasks rely on `BootPayload`, `TeardownPayload`,
`ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS`, and on
`_ACTIVE_PAYLOAD_MODELS[JobKind.BOOT] is BootPayload`.

### Steps

1. **Write the failing test** `tests/jobs/test_external_boot_payloads.py` with, at minimum:
   - `test_unmarked_boot_payload_round_trips_unchanged` — `dump_payload(JobKind.BOOT,
     {"run_id": str(uuid4())})` equals `{"run_id": …}` exactly (no marker key), and
     `load_payload` on a `Job` carrying it returns a `BootPayload` whose
     `external_boot_authority_v1` is `None`.
   - `test_marked_boot_payload_round_trips_unchanged` — dump then load returns an equal model,
     and the dumped dict's top-level keys are exactly `{"run_id", "external_boot_authority_v1"}`.
     **The key name is asserted as a literal string**, because it is what
     `0122_external_boot_authority.sql` and `0127_reopen_external_boot_claim_lane.sql` test with
     `payload ? '…'`.
   - `test_marked_payload_rejects_extra_field`.
   - `test_marker_run_id_must_match_payload_run_id` — and the same for
     `TeardownPayload.system_id`.
   - `test_teardown_marker_requires_teardown_purpose` and
     `test_boot_marker_rejects_teardown_purpose`.
   - `test_marker_operation_must_be_permitted_for_purpose` — parametrized over at least
     `("activate", "release")` and `("release", "activate")`, both rejected.
   - `test_marker_operation_must_be_enqueueable` — `deadline`, `recovery-attempt`, and `fail`
     rejected by name.
   - `test_enqueueable_operations_are_exactly_these_six` — one assertion, and it is the only place
     the six names are pinned:

     ```python
     assert ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS == frozenset(
         {"activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"}
     )
     ```

     Written as a **literal**, because every other check compares the constant against something
     the constant already gates: `ExternalBootOperations.register` refuses outside it, the payload
     validator rejects a marker outside it, and so a `registered_operations() == CONSTANT`
     assertion compares the constant to itself. Drop `cleanup` from the constant and that
     assertion still passes while criterion 4's "cleanup … ha[s] a registered handler" silently
     stops holding. This literal is what turns that rename red.
   - `test_run_id_from_payload_returns_run_for_boot_and_none_for_teardown`.
   - `test_every_marked_payload_kind_is_boot_or_teardown` — iterate
     `_ACTIVE_PAYLOAD_MODELS.items()`, select the models that declare the marker field, assert
     the kind set is exactly `{JobKind.BOOT, JobKind.TEARDOWN}`. Criterion 1 in the registry.
   - `test_a_marked_payload_cannot_be_dumped_under_another_kind` — criterion 1 **on the wire**,
     which is where the hole is. `TeardownPayload` is a `SystemPayload`, so
     `dump_payload(JobKind.PROVISION, TeardownPayload(system_id=…,
     external_boot_authority_v1=marker))` passes `isinstance` and would emit the marker key on a
     `provision` job. Parametrize over `JobKind.PROVISION` and `JobKind.FORCE_CRASH`; assert
     `PayloadValidationError`. The registry-shaped test stays green with this hole open, so it
     does not substitute for this one.
2. **Run it and confirm it fails**: `uv run python -m pytest
   tests/jobs/test_external_boot_payloads.py -q` — expect collection errors on
   `BootPayload`/`TeardownPayload` not existing.
3. **Implement** in `payloads.py`: import `ExternalBootAuthorityMarkerV1` from
   `kdive.jobs.models` and `operation_is_permitted` plus `AuthorityOperation` from
   `kdive.providers.external_boot_authority.protocol`; add the `dump_payload` guard that raises
   `PayloadValidationError` when the serialized dict carries `external_boot_authority_v1` and
   `kind` is not `JobKind.BOOT`/`JobKind.TEARDOWN`, since subclassing lets a marked
   `TeardownPayload` satisfy `isinstance(payload, SystemPayload)` for `PROVISION`/`FORCE_CRASH`; add the two classes with a
   `model_validator(mode="after")` each enforcing the cross-field rules above; add them to
   `_ActivePayloadModel`/`ActivePayloadModel`; set
   `_ACTIVE_PAYLOAD_MODELS[JobKind.BOOT] = BootPayload`,
   `[JobKind.TEARDOWN] = TeardownPayload`, and `_RUN_PAYLOAD_MODELS[JobKind.BOOT] = BootPayload`.
   Leave `TEARDOWN` out of `_RUN_PAYLOAD_MODELS` and put the reason in a comment: adding it
   would make the worker's `_compensation_run_id` transition a Run to `failed` on every ordinary
   System-teardown failure.
4. **Run it and confirm it passes.**
5. **Update the two `load_payload` sites and the five `src/` enqueue sites** listed in the file
   map. Find them with `rg -n "load_payload\(job, (RunPayload|SystemPayload)\)" src/` and
   `rg -n "JobKind.(BOOT|TEARDOWN)," -A 2 src/`. Do not touch `provision_handler`
   (`systems.py:393`) or the force-crash handler (`control/control.py:221`) — those are
   `PROVISION` and `FORCE_CRASH`, whose model is unchanged.
6. **Run the whole suite and let the breakage enumerate itself.** Make the `src/` change first,
   then run `just test` — the **whole** suite, not a subset. An earlier draft of this plan named
   four subdirectories and a fixed count; both were wrong, and one affected file
   (`tests/adversarial/`) was outside the named set, so the plan's own verification step would
   have missed it and the break would have surfaced only at Task 7. Do not narrow the command.

   Expect a set of failures, all `PayloadValidationError: invalid teardown payload` /
   `invalid boot payload`, from sites still constructing the base model. **That red is the change
   working, not a regression** — and seeing it first is the cheapest bite proof this task has,
   because it proves the model registry actually changed. Record the exact node ids, swap each to
   `TeardownPayload` / `BootPayload`, re-run `just test`, and expect green. Carry the recorded
   list into the PR body as the observed surface. Any failure that is **not** a
   `PayloadValidationError` on one of these two kinds is a real regression, not this expected
   red — except
   `tests/support/test_xdist_backend.py::test_sweep_reaps_a_real_stranded_container_but_spares_a_live_one`,
   which is a known host-local flake caused by a concurrent kdive suite reaping this test's own
   container (`BACKEND_LABEL` is repo-wide and `_private_sweep_lock` opts these tests out of the
   host-global sweep lock). Report that one; do not re-run to green.
7. `just lint && just type`. Commit:
   `feat(jobs): carry an external-boot authority marker on boot and teardown payloads`.

### Acceptance criteria

- Criterion 1 and criterion 2 of the charter hold, asserted by the tests above.
- An unmarked payload's serialized bytes are unchanged, so every persisted pre-change job still
  decodes.
- `just lint`, `just type`, and the three named suites are green.

---

## Task 2 — Package skeleton: ports, registry, router, wiring

**Creates:** `src/kdive/jobs/handlers/external_boot/{__init__.py,ports.py,operations.py,router.py,registrar.py}`.
**Modifies:** `src/kdive/jobs/assembly.py`, `src/kdive/jobs/handlers/runs/registrar.py`,
`src/kdive/jobs/handlers/systems.py`.
**Tests:** `tests/jobs/handlers/external_boot/{__init__.py,test_operations.py,test_router.py,test_import_closure.py}`.

### Interfaces this task publishes

```python
# ports.py
EXTERNAL_BOOT_AUTHORITY_MARKER_KEY: Final = "external_boot_authority_v1"


class ExternalBootAuthorityAcknowledger(Protocol):
    async def acknowledge(
        self, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1: ...


@dataclass(frozen=True, slots=True)
class ExternalBootHandlerPorts:
    resolver: ProviderResolver
    incarnation_credential: SecretStr
    acknowledger: ExternalBootAuthorityAcknowledger | None = None


# operations.py
type ExternalBootOperationHandler = Callable[
    [AsyncConnection, Job, ExternalBootAuthorityMarkerV1],
    Awaitable[ExternalBootAuthorityResultV1],
]
from kdive.jobs.payloads import ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS  # defined in Task 1


class DuplicateExternalBootHandler(RuntimeError): ...


class ExternalBootOperations:
    def register(self, operation: str, handler: ExternalBootOperationHandler) -> None: ...
    def get(self, operation: str) -> ExternalBootOperationHandler | None: ...
    def registered_operations(self) -> frozenset[str]: ...
    async def run(self, conn: AsyncConnection, job: Job) -> ExternalBootAuthorityResultV1: ...


# router.py
def route_marked(operations: ExternalBootOperations, ordinary: JobHandler) -> JobHandler: ...


# registrar.py
def build_operations(ports: ExternalBootHandlerPorts) -> ExternalBootOperations: ...
```

`runs.register_handlers` and `systems.register_handlers` each gain a **required** keyword
`external_boot: ExternalBootOperations`.

### Steps

1. **Write the failing tests.**
   - `test_operations.py`:
     - `test_production_registry_binds_each_enqueueable_operation_once` — build the operations
       registry the production path builds, assert
       `registry.registered_operations() == ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS`.
     - `test_second_registration_raises` — `DuplicateExternalBootHandler`.
     - `test_register_refuses_a_non_enqueueable_operation` — parametrized over `deadline`,
       `recovery-attempt`, `fail`.
     - `test_run_refuses_an_unregistered_operation` — a `Job` whose marker carries `deadline`
       raises `CategorizedError` with `ErrorCategory.CONFIGURATION_ERROR`, `terminal is True`,
       and the operation name in the message.
     - `test_handler_registry_binds_boot_and_teardown_exactly_once` — build the real registry
       through `build_handler_registry` with a stubbed `WorkerHandlerAssembly`; assert
       `registry.get(JobKind.BOOT)` and `registry.get(JobKind.TEARDOWN)` are both non-`None`,
       and that calling `registry.register(JobKind.BOOT, …)` on that same registry now raises
       `DuplicateHandler`. The second half is what proves the kind is bound once rather than
       merely bound; asserting only non-`None` would pass under a double registration if one
       silently won. This is the "no duplicate registration" half of criterion 4.
     - `test_production_registry_resolves_every_operation_to_one_handler` — criterion 4's other
       half, asserted **through** the production entry point rather than around it.
       `HandlerRegistry` exposes only `register` and `get(kind)`, and the operations registry is
       captured in the router closure, so it cannot be read off the returned object. Parametrize
       over **the literal six names pinned by `test_enqueueable_operations_are_exactly_these_six`**,
       not over `ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS` — parametrizing over the constant would
       reintroduce the same self-comparison one level up, because the constant is what
       `ExternalBootOperations.register` and the payload validator both gate on. Build a marked
       `Job` for each, fetch the handler
       `build_handler_registry(<stub assembly>)` returns for that operation's `JobKind`, await it,
       and assert exactly one operation handler ran and it was the one bound to that operation.
       Use recording doubles for all six so "exactly one" is asserted against the recorder rather
       than inferred. This is what proves `register_all_handlers` passed the *same* registry to
       **both** registrars — `test_production_registry_binds_each_enqueueable_operation_once`
       proves only that `build_operations` is internally complete, which is a weaker claim and
       must not be presented as this one.
   - `test_router.py`, using two recording fakes:
     - `test_unmarked_boot_job_reaches_the_ordinary_handler`.
     - `test_marked_boot_job_does_not_reach_the_ordinary_handler` — assert the ordinary fake was
       never called **and** the operations fake was, so the test cannot pass by both being
       skipped.
     - `test_malformed_marker_does_not_reach_the_ordinary_handler` — payload key present but its
       value is `{"nonsense": 1}`; the ordinary fake is never called.
     - the same three for teardown.
   - `test_import_closure.py` — a **real** transitive closure check, which the existing gate is
     not. Run `uv run python -c` in a **subprocess** that imports every module under
     `kdive.jobs.handlers.external_boot` and prints `sys.modules`; assert no name starts with
     `kdive.providers.local_libvirt` or `kdive.providers.remote_libvirt`, and that `libvirt` is
     absent. A subprocess because `sys.modules` is process-wide and another test's imports would
     pollute an in-process assertion into a false pass.
     `tests/services/external_boot/test_recovery_requests.py` is **not** the walk to copy — its
     `_reachable_names` (`:714-727`) and `_kdive_imports` (`:749-766`) are single-module `ast`
     sweeps, and the first's docstring says outright that no transitive walk "is needed or
     wanted" there. Copy only its second idea: pair the gate with a canary
     (`test_the_import_closure_gate_bites`, `:816-825`) that proves the check fails when a
     forbidden import is present, so a gate that silently stopped checking is caught.
2. **Run and confirm they fail.**
3. **Implement** the five modules. `ExternalBootOperations.run` loads the payload
   (`load_payload(job, BootPayload)` or `TeardownPayload` by `job.kind`), reads
   `external_boot_authority_v1`, raises `CategorizedError(configuration_error, terminal=True)`
   when it is `None` (a job routed here must carry one) or when its operation is unregistered,
   and otherwise awaits the bound handler.
   `route_marked` branches on `EXTERNAL_BOOT_AUTHORITY_MARKER_KEY in job.payload` — **presence,
   not validity**, matching the rule `src/kdive/jobs/worker.py:614-624` already applies, so a
   malformed marker fails closed instead of booting a Run.
4. **Wire it**: in `register_all_handlers`, build
   `operations = external_boot.build_operations(ExternalBootHandlerPorts(...))` before the two
   registrar calls and pass it to both. In each registrar, wrap the existing lambda:
   `registry.register(JobKind.BOOT, route_marked(operations, lambda conn, job: boot_handler(...)))`.
5. **Run and confirm they pass**; then
   `uv run python -m pytest tests/jobs tests/mcp/lifecycle -q` to catch every existing caller of
   the two registrars that now needs the required keyword. Three call sites exist and only one is
   under `tests/jobs`: `tests/jobs/handlers/test_runs_boot.py:183`,
   `tests/mcp/lifecycle/test_runs_tools.py:5150`, and
   `tests/mcp/lifecycle/test_systems_tools.py:2125`. Re-derive them with
   `rg -n 'register_handlers\(' tests/` rather than trusting these line numbers.
6. `just lint && just type && just adr-status-check`. The third is not decoration:
   `scripts/guards/check_adr_status.py` fails any ADR whose status keyword is `Proposed` and that
   is cited from `src/` or `tests/`, and this task is where the first `ADR-0593` citation lands
   (the package docstring). ADR-0593 is already `Accepted`, which is the repo's rule for the PR
   implementing a decision, so this should pass — run it here so a regression surfaces where it
   is introduced rather than at Task 7. Commit:
   `feat(jobs): route authority-marked boot and teardown jobs to an operations registry`.

### Acceptance criteria

- A marked job cannot reach `boot_handler` or `teardown_handler` on any path, including a
  malformed marker.
- The keyword is required, so omitting it is a `TypeError` at registration rather than a silent
  wrong-operation execution.
- The import-closure gate passes.

---

## Task 3 — Authority allocation and the shared runner

**Creates:** `src/kdive/jobs/handlers/external_boot/{authority.py,runner.py,evidence.py}`.
**Tests:** `tests/jobs/handlers/external_boot/{conftest.py,test_runner.py,test_role_gate.py}`.

### Interfaces this task publishes

```python
# authority.py
@dataclass(frozen=True, slots=True)
class AllocatedAuthority:
    authority_id: UUID
    generation: int
    operation_digest: str


async def allocate_authority(
    conn: AsyncConnection,
    job: Job,
    marker: ExternalBootAuthorityMarkerV1,
    *,
    incarnation_credential: SecretStr,
) -> AllocatedAuthority | None: ...  # None means the function returned 'superseded'


# runner.py
@dataclass(frozen=True, slots=True)
class OperationContext:
    job: Job
    marker: ExternalBootAuthorityMarkerV1
    activation: ExternalBootActivation
    binding: ProviderBinding
    port: ExternalBootPorts
    authority: AllocatedAuthority
    acknowledgement: AuthorityAcknowledgementV1


async def run_operation(
    conn: AsyncConnection,
    job: Job,
    marker: ExternalBootAuthorityMarkerV1,
    *,
    ports: ExternalBootHandlerPorts,
    require_activation_state: frozenset[ExternalBootActivationState],
    require_activation_evidence: frozenset[str],
    require_preconditions: Callable[
        [AsyncConnection, ExternalBootActivation, ExternalBootAuthorityMarkerV1],
        Awaitable[None],
    ],
    build_result: Callable[
        [OperationContext, RunningKernelObservation | None], ExternalBootAuthorityResultV1
    ],
    call_port: Callable[[OperationContext], RunningKernelObservation | None],
) -> ExternalBootAuthorityResultV1: ...


# evidence.py
def terminal_evidence(context: OperationContext, outcome: str) -> dict[str, object]: ...
def known_object_refs(activation: ExternalBootActivation) -> tuple[str, ...]: ...
def authority_ref(context: OperationContext) -> OpaqueProviderRef: ...
def authority_result(
    context: OperationContext, result: Mapping[str, object]
) -> ExternalBootAuthorityResultV1: ...
```

`run_operation` performs the seven steps in the spec's §6, in that order:

1. `binding = await ports.resolver.binding_for_system(conn, marker.system_id)`; refuse when
   `binding.kind.value != marker.provider_kind` or `binding.runtime.external_boot is None`
   (`CategorizedError`, `CONFIGURATION_ERROR`, `terminal=True`).
2. `activation = await ExternalBootActivationRepository().get(conn, marker.activation_id)`;
   refuse on absence, or on any of `run_id`/`system_id`/`plan_identity` disagreeing with the
   marker.
2a. Refuse when `activation.state not in require_activation_state`.
2b. Refuse when any attribute named in `require_activation_evidence` is `None` on the activation
   row. This is the spec's §7 *required activation evidence* column and it is checked for every
   operation, not only `activate`.
2c. `await require_preconditions(conn, activation, marker)` — the per-operation callable carrying
   the three footnoted prerequisites the spec's §7 names: the recovery-attempt row state (†), the
   `NOT cleanup_complete` / no-existing-release guards (‡), and `systems.state = 'failed'` (§).
   Each refuses with the same terminal `configuration_error`. The System/Run state conditions are
   deliberately **not** mirrored here — `allocate_external_boot_authority` already checks them
   (`0122…sql:482-501`), so duplicating them in Python would add drift surface for a benefit
   #2203 owns.

   Steps 2a-2c all run **before** step 3, so every refusal happens before an authority exists.
   That matters beyond tidiness: a refusal after allocation, or a `superseded` allocation, cannot
   be committed as a failure and wedges the job (spec §6 step 3, §8).
3. `allocate_authority(...)`; `None` raises `CategorizedError(STALE_HANDLE, terminal=False)` so
   the job requeues.
4. `ports.acknowledger` is `None` → `CategorizedError(CONFIGURATION_ERROR, terminal=True)`
   **before** step 5. Otherwise build `AuthorityTakeoverRequestV1` from the marker plus the
   allocation and await `acknowledge`.
5. `await asyncio.to_thread(call_port, context)`.
6. `build_result(context)`.
7. Return it.

Every raise from steps 1–6 is caught by `run_operation` and re-raised as
`ExternalBootAuthorityFailure` carrying an `ExternalBootAuthorityFailureV1` bound to the same
allocation, **but only once an authority has been allocated** — before allocation there is no
binding to carry, so the original `CategorizedError` propagates and the worker's marked-job
branch logs it (the job then stays `running`; that is the #2203 leak, not a regression this task
introduces). `failure_context.phase` is `admission` for steps 1–3, `preparation` for step 4,
`provider-call` for step 5, and `commit` for step 6.

### Steps

1. **Write `conftest.py`** providing, for the whole package's Postgres tests:
   - **`external_boot_vehicle`** — the fixture the spec's "execution vehicle" subsection
     specifies, and the prerequisite for every other Postgres test in this package.

     **Order is forced and is not a style choice.**
     `0124_external_boot_activation_binding.sql:96-111` requires a persisted `recovery_point` to
     carry a three-key `binding` whose UUIDs equal the activation row's `system_id`, `run_id`,
     and `id`, no `ownership` key, and a matching `plan_identity`; `:92-95` requires the same of
     `materialization.ownership`. Those values come from the port's inputs, so a fixture that
     seeds first and builds second cannot make them agree and the INSERT fails with a
     `CheckViolation`. So: **(1)** mint `system_id`, `run_id`, `activation_id`; **(2)** build the
     synthetic `ExternalBootPlan` with `ownership` from the first two; **(3)** build one
     `FaultInjectExternalBoot` and drive it through `materialize(plan, …)` then
     `prepare(materialization, ExternalBootActivationBinding(system_id, run_id, activation_id), …)`
     — this is what populates `_observations`, which `observe` reads and nothing else writes;
     **(4)** seed the activation row with `plan_identity = plan.identity` and the resulting
     objects' canonical JSON.

     Return the port wrapped in a delegating double that forwards
     `activate`/`observe`/`recover`/`cleanup` and raises `AssertionError` from
     `materialize`/`prepare`.

     **Source the synthetic plan from `tests/providers/remote_libvirt/lifecycle/test_external_boot.py:254`**,
     which is the repo's only existing `ExternalBootPlan(...)` construction (`rg -n
     'ExternalBootPlan\(' src/ tests/` returns exactly that one plus the class definition).
     Do **not** source it from ADR-0583's golden vector: that vector is a libvirt **domain-XML**
     identity under the hash prefix `kdive-libvirt-boot-projection-v1`, whereas
     `ExternalBootPlan.identity` uses `kdive-external-boot-plan-v1`
     (`src/kdive/providers/ports/external_boot.py:213-215`), and ADR-0583 carries no
     `ExternalBootPlan`, `platform_arguments`, or `RootSpecV1` example at all. A hand-rolled plan
     is likely to be rejected: `_validate_composed_plan` (`:190-211`) requires the root arguments
     to occur exactly once in `platform_arguments`, `cmdline` to compose them exactly, unique
     argument keys, and a 2047-byte cap.
   - a `seeded_activation` helper that inserts resource/allocation/system/investigation/run/
     activation/worker_incarnation/job rows in a given activation state and purpose, modelled on
     `tests/db/external_boot_authority_support.py:135` (`_seed_case`) but async and returning
     typed ids. It seeds the resource `kind` and the run `target_kind` as **`local-libvirt`**,
     and writes the vehicle's `RecoveryPoint` and `ExternalBootMaterialization` canonical JSON
     verbatim into `external_boot_activations.recovery_point` and `.materialization` — not the
     `{schema, binding, plan_identity}` stub `_seed_case` uses, which carries no `recovery_ref`
     and does not validate as a `RecoveryPoint`. Where an operation needs one, it also seeds the
     `external_boot_recovery_attempts` row in the state §7's † footnote requires, because nothing
     in this change writes that row.

     **It must seed more than the † footnote names, or three operations cannot reach a commit:**
     - an `external_boot_recovery_attempts` row for **every** state except `abandoned`, not only
       for `recover`/`resolve-conflict`: `external_boot_activation_state_evidence`
       (`0121…sql:38-52`) requires `current_attempt_id IS NOT NULL` in
       `recovering`/`recovered`/`recovery_conflict`/`recovery_failed`, so the activation INSERT
       itself fails with a `CheckViolation` without one;
     - an `external_boot_reservations` row in state `ready` whenever the operation's §7 evidence
       column names one (`release`), because the commit re-checks `store_identity`, `owner_key`,
       and `reserved_bytes` against it with `NOT EXISTS (…)` (`0122…sql:1535-1541`) and raises
       `22023` otherwise;
     - for `cleanup` and `teardown`, either an `external_boot_reservation_releases` row or — the
       better shape — the test runs the `release` handler first and takes the row the release
       commit writes, since `0122…sql:1398` compares the cleanup evidence's `release_identity`
       against `v_release.release_identity` and an absent row makes that comparison
       unconditionally true, raising `22023`.

     A `22023` from the commit is §8's third route, so each of these would otherwise surface as an
     untraceable lane-level warning rather than a clean red — slower to diagnose than an ordinary
     test failure, which is why they are enumerated here rather than left to be discovered.
   - a `provider_resolver` builder binding the vehicle's wrapped port on a `ProviderRuntime`
     under `ResourceKind.LOCAL_LIBVIRT`. The fault-inject composition registers its runtime under
     `ResourceKind.FAULT_INJECT`, a value the marker's `provider_kind` cannot hold, so binding
     the port under the local-libvirt kind is what makes the fault-inject **port** usable without
     the fault-inject **kind**. Extend `tests/mcp/systems_support.py`'s existing builder with an
     `external_boot` parameter rather than writing a second one.
   - a `role_connection` async helper taking `authority_role_dsns` and a role name and yielding
     an `AsyncConnection` for that LOGIN role;
   - a `RecordingAcknowledger` that performs the **real**
     `acknowledge_external_boot_authority` call over a `kdive_provider_authority` connection and
     returns the `AuthorityAcknowledgementV1` built from its returned row. It is a seam
     implementation, not a stub: the database does the acknowledging, so a test that passes
     proves the SQL path, not the fake.
   Import the `authority_role_dsns` fixture explicitly
   (`from tests.db.external_boot_authority_support import authority_role_dsns  # noqa: F401`)
   or re-export it from this conftest.
2. **Write the failing tests** in `test_runner.py`:
   - `test_provider_kind_mismatch_is_refused_before_allocation` — assert the refusal **and**
     that no `external_boot_authorities` row exists afterwards, which is what makes it
     "rejected at validation rather than at `allocate_external_boot_authority`" (charter
     criterion 3). Asserting only the exception would pass even if allocation had run.
   - `test_absent_external_boot_port_is_refused`.
   - `test_absent_acknowledger_fails_before_the_port_is_called` — the port double records calls;
     assert it recorded none.
   - `test_activation_identity_mismatch_is_refused` — parametrized over `run_id`, `system_id`,
     `plan_identity`.
   - `test_superseded_allocation_leaves_the_job_running_without_a_jobs_row` — assert the `jobs`
     row is byte-identical before and after (state, attempt, error_category, worker_id all
     unchanged), **not** that `terminal is False`. `terminal` has no consumer on this path,
     because no authority was allocated and the commit's `fail` branch is the only thing that can
     requeue a marked job. A test asserting the flag would assert a value nothing reads.
   - `test_missing_required_evidence_is_refused_before_allocation` — parametrized over
     (`release`, `recovery_point`), (`cleanup`, `recovery_point`), (`teardown`, `recovery_point`),
     (`activate`, `materialization`): seed the activation with that column `NULL` in a state the
     CHECK admits, assert a terminal `configuration_error`, that the port double recorded no call,
     and that **no `external_boot_authorities` row exists** — the last is what proves the refusal
     preceded allocation rather than followed it.
   - `test_unmet_precondition_is_refused_before_allocation` — one case per footnote: a `recover`
     whose recovery-attempt row is in the wrong state (†), a `cleanup` on an activation with
     `cleanup_complete` already true (‡), and a `teardown` whose System is not `failed` (§). Same
     three assertions. There is no `activate`-whose-Run-is-not-succeeded case, because the runner
     does not check that — `allocate` does.
   - `test_provider_exception_becomes_an_authority_failure_bound_to_the_allocation` — assert
     `_authority_binding_matches(marker, failure.result)` is `True` and
     `failure.result.result.failure_context.phase == "provider-call"`.
3. **Write `test_role_gate.py`** — the same runner call over a `kdive_server` LOGIN connection
   raises `psycopg.errors.InsufficientPrivilege`; assert `exc.sqlstate == "42501"` and
   `"worker authority is required"` in `str(exc)`. Then assert the identical call over the
   `kdive_worker` LOGIN connection **succeeds**, so the test proves the gate rather than proving
   the call is broken for everyone. Do not run either arm as the superuser: `pg_has_role` is
   true for a superuser against every role, so a superuser arm asserts nothing.
4. **Run and confirm they fail.**
5. **Implement** the three modules.
6. **Run and confirm they pass.** `just lint && just type`. Commit:
   `feat(jobs): allocate and acknowledge external-boot authority in a shared runner`.

### Acceptance criteria

- Charter criteria 3 and 8 hold, each asserted against a value the test does not itself produce
  (an authority-row count; a SQLSTATE from Postgres).
- No mutation reaches the provider before the acknowledgement exists.

---

## Task 4 — The six operation handlers

**Creates:** `src/kdive/jobs/handlers/external_boot/lifecycle.py`; completes `registrar.py`.
**Tests:** `tests/jobs/handlers/external_boot/{test_lifecycle.py,test_prepared_before_admission.py}`.

### Interfaces this task publishes

```python
def activate_handler(ports) -> ExternalBootOperationHandler: ...
def recover_handler(ports) -> ExternalBootOperationHandler: ...
def resolve_conflict_handler(ports) -> ExternalBootOperationHandler: ...
def release_handler(ports) -> ExternalBootOperationHandler: ...
def cleanup_handler(ports) -> ExternalBootOperationHandler: ...
def teardown_handler(ports) -> ExternalBootOperationHandler: ...


ACTIVATION_READINESS_WINDOW: Final[timedelta] = timedelta(minutes=15)
```

Per-operation behavior, required activation state, port call, and result variant are the spec's
§7 table, which now carries a **required activation state** column plus its three footnotes —
the recovery-attempt row state for `recover`/`resolve-conflict` (†), the `NOT cleanup_complete`
and no-existing-release guards (‡), and `systems.state = 'failed'` for `teardown` (§). Take
`require_activation_state` from that column, which is sourced from the **commit** preconditions
(`0122…sql:1302-1335`) rather than the looser `allocate` ones; the footnoted prerequisites are
separate checks, not activation states. Each handler's docstring states the five-part limit
contract for any deadline it emits: unit, reference clock, scope, consequence of violation,
recovery action.

### Steps

1. **Write the failing tests** in `test_lifecycle.py`, one per operation, each against a real
   Postgres and the `FaultInjectExternalBoot` port injected as
   `ProviderRuntime.external_boot`:
   - the port method named in the §7 table was called, with the **persisted** `RecoveryPoint`
     (compare against the row read back from the database, not against the object the test
     handed the seeder);
   - the returned `ExternalBootAuthorityResultV1` passes `_authority_binding_matches` against
     the payload marker (charter criterion 7, driven through
     `kdive.jobs.worker._authority_binding_matches` exactly as the criterion names);
   - committing it through `queue.complete_external_boot` returns a `Job`, and the
     `external_boot_activations` row afterwards holds the state in the §7 table (criterion 5);
   - the `jobs` row afterwards is `succeeded` (criterion 6, applied half).
   Plus, once:
   - `test_mismatched_result_is_rejected_before_the_commit` — mutate one binding field of a
     handler's result, assert `_authority_binding_matches` is `False` and that committing is not
     attempted (criterion 7's second half);
   - `test_failure_result_leaves_the_job_failed` — commit an
     `ExternalBootAuthorityFailureV1` with `terminal=True` and assert `jobs.state == "failed"`
     (criterion 6, not-applied half);
   - `test_superseded_commit_leaves_the_job_running` — records the #2203-owned leak, with the
     issue number in the test's docstring so it cannot be mistaken for intended coverage.
2. **Write `test_prepared_before_admission.py`** — ADR-0593 decision 4's pin, plus the
   NULL-evidence refusals §6 step 2 requires for every operation:
   - an activate job whose activation has `recovery_point` NULL is refused, and the port double
     recorded no call;
   - the same for `materialization` NULL;
   - **a `cleanup` job against an activation in state `abandoned` with `recovery_point` NULL**
     (which `0121…sql:38-52` permits: `abandoned` requires only
     `terminal_evidence->>'outcome' = 'abandoned'`) is refused with a terminal
     `configuration_error`, the port double recorded no call, and **no `external_boot_authorities`
     row was created** — so the refusal is proven to happen before allocation, not after it;
   - the same shape for `release` and `teardown` against a state whose CHECK permits a NULL
     `recovery_point`. These are the cases where a `NULL` column and a completed operation would
     otherwise be indistinguishable, so each asserts on the column's absence producing a
     categorized refusal, never on the handler happening not to crash;
   - across every one of the six handlers, the port records no call to `materialize` or
     `prepare` on any path. This is the `external_boot_vehicle` fixture's wrapper, which raises
     `AssertionError` from those two, so the assertion cannot be satisfied by the test forgetting
     to check. The fixture itself calls both **before** it installs the wrapper, which is the
     disposition rather than a hole in it: ADR-0593 decision 4 pins that the *handler* never
     performs them, and the activation row the handler reads is `prepared` precisely because
     something else already did.
3. **Run and confirm they fail.**
4. **Implement** `lifecycle.py` and finish `registrar.build_operations`.
5. **Run and confirm they pass.** `just lint && just type`. Commit:
   `feat(jobs): add the six external-boot lifecycle operation handlers`.

### Acceptance criteria

- Charter criteria 4 (handler half), 5, 6, and 7 hold.
- ADR-0593 decision 4 is pinned by a test that fails if a handler ever calls `materialize` or
  `prepare`.

---

## Task 5 — Enqueue-side helper

**Creates:** `src/kdive/jobs/handlers/external_boot/admission.py`.
**Tests:** `tests/jobs/handlers/external_boot/test_admission.py`.

### Interfaces this task publishes

```python
async def build_external_boot_payload(
    conn: AsyncConnection,
    *,
    activation_id: UUID,
    purpose: ExternalBootPurpose,
    operation: str,
    provider_kind: str,
    authority_instance: str,
    operation_identity: str,
    resolver: ProviderResolver,
) -> tuple[JobKind, BootPayload | TeardownPayload]: ...
```

`run_id`, `system_id`, and `plan_identity` come from the activation row, never from the caller.
`provider_kind` and `authority_instance` are caller-supplied; the helper refuses a
`provider_kind` that disagrees with the `ResourceKind` the resolver binds for that System, and
refuses a bound runtime whose `external_boot` is `None`. The returned `JobKind` is `TEARDOWN`
for `purpose == "teardown"` and `BOOT` otherwise, matching
`0122_external_boot_authority.sql:465`.

### Steps

1. **Write the failing tests**: identity is sourced from the row (seed an activation, pass no
   run/system id, assert the marker carries the row's values); a mismatched `provider_kind` is
   refused **and** no authority row is created; an absent `external_boot` port is refused; the
   kind is `TEARDOWN` exactly for the teardown purpose; the returned payload survives
   `dump_payload`/`load_payload`.
2. Run, confirm failure, implement, run, confirm pass.
3. `just lint && just type`. Commit: `feat(jobs): add the external-boot enqueue payload builder`.

### Acceptance criteria

- Charter criterion 3's "sourced explicitly by the enqueueing caller" holds, and a caller cannot
  construct a marker that disagrees with the activation row.

---

## Task 6 — End to end

**Creates:** `tests/integration/test_external_boot_job_lifecycle.py`.

### Steps

1. **Write the failing test**: seed a `prepared`→`activating` activation and a worker
   incarnation; build the payload with `build_external_boot_payload`; `queue.enqueue` it; assert
   `count_claimable_worker_jobs` counts it (this is what #2201's migration bought, so assert it
   rather than assuming it); claim it with a real `Worker` whose registry is built by
   `build_handler_registry` with the fault-inject runtime and the real acknowledger; let the
   worker dispatch and commit; assert the activation reaches `active` and the `jobs` row is
   `succeeded`.
2. **Add a teardown arm.** Repeat the whole flow for a `teardown`-purpose job on
   `JobKind.TEARDOWN`, so both kinds are exercised on the production path rather than `boot`
   alone. Without it the production wiring is proven for one kind and assumed for the other.
3. Run, confirm failure, implement any wiring gaps, run, confirm pass.
4. Commit: `test(jobs): drive an authority-marked job end to end through a worker`.

### Acceptance criteria

- Charter criterion 9 holds against a real worker claim, not a direct handler call.

---

## Task 7 — Guardrails and bite proofs

### Steps

1. `just install-mermaid-deps` if not already done in this worktree.
2. `just ci > /tmp/.../ci.log 2>&1 < /dev/null`, bare, and read the exit code from the command's
   own status. Expect 0. The baseline on this branch before any change was **exit 0, 16206
   passed**, so any red is this branch's.
3. **Bite-prove every new test.** For each new test file, with the implementation already
   committed: inject one controlled fault in the source under test, run only that file, observe
   a **clean assertion failure** (not a collection error, not a connection error), revert the
   fault, and confirm the file is byte-identical with `sha256sum` before and after. Record the
   fault, the failing assertion, and the two matching hashes. A test whose fault produces a
   collection or connection error proved nothing and must be rewritten.
4. Commit any fixes the bite proofs surface, each as its own commit.

### Acceptance criteria

- **Charter criterion 11** — repository guardrails pass: `just ci` exits 0, run bare, with its
  own exit status read rather than a trailing `echo $?` or a pipeline's.
- Every new test has a recorded bite proof with matching before/after hashes.
- The observed set of existing test sites Task 1 broke is recorded and carried into the PR body,
  as the count the run produced rather than one predicted before it.

---

## Deferrals carried into this plan

- **The release commit credits recovery-store capacity before cleanup deletes the objects.**
  ADR-0584's adapter makes `release` non-mutating and non-deleting, so the reservation `DELETE`
  in the release branch of `commit_external_boot_authority_result` credits `reserved_bytes` back
  while the owned objects still exist — under-charging `recovery_max_bytes` between the release
  and cleanup commits, and permanently if the cleanup job never commits. That departs from
  ADR-0583's stated ordering. Owner:
  [deferral record 0010](../../debt/0010-external-boot-release-credits-capacity-before-cleanup.md),
  tracker #2118. This plan must not make the interval longer or the leak likelier; see that
  record's non-regression boundary.

Any further deferral a `$trial-loop` run on this branch disposes of as `deferred-tracked` is
appended here with its owning record path or tracker issue before the branch ships.

## Constraints only execution found

Recorded after the fact, because the design set does not carry them and every one of them cost a
red run to discover. A reader who trusts the tables above without this section rediscovers all of
them.

- **A `recovering` recovery-attempt row requires `recovery_readiness_deadline`.**
  `external_boot_attempt_deadline` (`0121_external_boot_activations.sql:232-233`). The §7 table's
  † footnote names the attempt row's *state* and nothing else, so a seeder that supplies only the
  state fails at INSERT with a `CheckViolation`, before any handler runs.
- **The attempt row's evidence columns are required *iff* its state says so.**
  `external_boot_attempt_evidence` (`:241-246`) pairs `conflict` with `conflict_evidence` and
  `failed`/`recovered` with `terminal_evidence`, each an equality rather than an implication, and
  `external_boot_attempt_evidence_ownership` (`:253-262`) pins the outcome per state —
  `recovery_failed` for `failed`, `recovered` for `recovered`.
- **A four-key `pre_recovery_evidence` satisfies the table CHECK but not the domain model.**
  `tests/db/external_boot_authority_support.py` seeds `{schema, activation_id, system_id, run_id,
  plan_identity}`, which Postgres accepts; `ExternalBootPreRecoveryEvidenceV1` also requires
  `recovery_object`, `source_composite_state` and `observed_at`. A row seeded the short way fails
  at `ExternalBootActivation.model_validate` the moment a handler reads the activation back — a
  decode failure standing in front of the behaviour under test.
- **`jobs.authorizing` must carry `agent_session`.**
  `queue.commit_external_boot_authority_result` validates the row through `Job` after an applied
  commit, so omitting it makes a **successful** commit surface as a pydantic `ValidationError` —
  the confusing direction, since the commit itself worked.
- **Test modules under `tests/jobs/handlers/` are imported by bare basename** unless the directory
  is a package, and `test_admission.py` and `test_lifecycle.py` already exist elsewhere in the
  tree. Both new directories need an `__init__.py`, which is how `tests/services/external_boot/`
  already avoids the same collision.
- **`ruff format` processes Python inside Markdown fences**, and `pyproject.toml` excludes only
  `docs/adr`. So `docs/workflow/specs/` and `docs/workflow/plans/` are formatted by `just lint`,
  and an unformatted code block in this plan fails the branch gate.

## Known adjacent state this plan does not change

- An authority-marked job whose commit returns `superseded`, or whose worker dies, stays
  `running` with a lapsed lease and nothing reaps it: both generic finalizers
  (`0122_external_boot_authority.sql:304-315`) and `repair_abandoned_jobs` are fenced against
  it. Availability only. Owned by **#2203**; Task 4 records the behavior in a test rather than
  changing it.
- `ExternalBootPorts.materialize` and `.prepare` stay uncalled by any worker handler
  (ADR-0593 decision 4). The preparation path that records `materialization` and
  `recovery_point` is owned by **#2204**.
- The three MCP tools still return `recovery_executor_unavailable`
  (`docs/debt/0003-external-boot-contracts-await-their-executor.md`). Flipping them live is
  **#2204**; this branch supplies the executor half they wait on.
