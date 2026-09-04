# External-boot command-line readiness implementation plan

Goal: enforce ADR-0583's exact running command-line proof in core for both external-boot providers.

Architecture: widen the existing provider-neutral observation, keep guest reads at provider
boundaries, and make the core lifecycle the sole equality authority. The shared plan validator
rejects XML-illegal bytes before either renderer receives them.

Tech stack: Python 3.14, Pydantic models, pytest, local and remote libvirt provider seams.

Expected implementation size: 520–900 changed lines (M) — derived from a persistence-safe type
split, one core comparator, a behavior-preserving shared executor move, two provider reads, local
channel preparation, one bounded failure-carrier migration, fixture updates, and focused/live tests.

## Global constraints

- Support x86_64 and ppc64le contracts; this host is x86_64 and live ppc64le testing is excluded.
- Keep `external-boot-materialization-v1` canonical JSON byte-identical; no persisted field changes.
- Remove exactly one trailing newline from `/proc/cmdline`; perform no other normalization.
- Compare the remaining bytes with `plan.cmdline.encode("utf-8")` byte-for-byte.
- Use terminal `READINESS_FAILURE` for changed bytes and preserve retry behavior for unavailable
  evidence.
- Redact diagnostic strings with the process `SecretRegistry` before constructing or persisting the
  authority result; cap each rendered value at 8,192 UTF-8 bytes and the offset at 2,048.
- Use assigned migration `0130`; do not add a dependency, public MCP surface, or new ADR.
- Run `just lint`, `just type`, focused tests, and `just ci > <file> 2>&1 < /dev/null` before push.

## Task 1: Widen and validate the shared observation contract

Files: `src/kdive/providers/ports/external_boot.py`,
`tests/providers/ports/test_external_boot.py`, and provider contract bindings/tests.

Interfaces:

- Produces persisted `KernelIdentity(architecture, release, gnu_build_id)` and transient
  `RunningKernelObservation(identity, cmdline, expected_cmdline)` for both providers and core.
- Keeps `ExternalBootMaterialization.kernel_observation` and its canonical JSON shape unchanged,
  changing only the Python value type to `KernelIdentity`.
- Keeps `ExternalBootPorts.observe(...) -> RunningKernelObservation` unchanged.
- Tightens `_validate_platform_argument(value: str) -> str` for XML 1.0 representability.

Verification:

- Mode: focused-test. Transient command-line bytes fail existing constructors first; green command:
  `uv run python -m pytest tests/providers/ports/test_external_boot.py tests/providers/contract/test_external_boot_contract.py -q`.
- Mode: focused-test. A pre-change canonical materialization fixture must retain its exact bytes and
  identity after the type split; the same focused command proves it.
- Mode: focused-test. XML-illegal C0 arguments are accepted before the change; the same command
  rejects them after the validator change.

Steps:

1. Add tests constructing and serializing an observation with exact bytes and rejecting C0 values.
2. Run the focused command and observe missing-field and accepted-control failures.
3. Split persisted `KernelIdentity` from transient `RunningKernelObservation`, and add an XML 1.0
   character predicate to the shared port model.
4. Update contract fixtures and run the focused command green.

Acceptance: every observation contains bytes and every XML-illegal platform token is rejected at
the shared boundary.

## Task 2: Enforce the plan command line in core

Files: `src/kdive/jobs/handlers/external_boot/lifecycle.py` and
`tests/jobs/handlers/external_boot/test_lifecycle.py`.

Interfaces:

- Consumes `context.activation.materialization.kernel_observation`, plus transient
  `RunningKernelObservation.identity`, `.cmdline`, and `.expected_cmdline`.
- Produces a terminal `CategorizedError` with category `ErrorCategory.READINESS_FAILURE` carrying
  bounded deterministically escaped expected/observed strings and
  `first_differing_byte`.

Verification:

- Mode: focused-test. Exact, truncated, reordered, and appended observations exercise the common
  lifecycle comparator; red is acceptance or configuration classification, green command:
  `uv run python -m pytest tests/jobs/handlers/external_boot/test_lifecycle.py -q`.

Steps:

1. Add parametrized lifecycle tests for exact and three mismatch shapes, including prefix offsets.
2. Run the focused command and observe that mismatches lack command-line enforcement.
3. Split kernel-identity comparison from command-line comparison and add one escaped diagnostic.
4. Run the focused command green and confirm existing recovery-operation cases still pass.

Acceptance: core alone decides exact equality and every mismatch is terminal readiness failure with
both escaped strings and the first differing byte.

## Task 3: Carry a bounded redacted diagnostic through authority persistence

Files: `src/kdive/jobs/models.py`, `src/kdive/jobs/handlers/external_boot/ports.py`,
`src/kdive/jobs/handlers/external_boot/runner.py`, `src/kdive/jobs/assembly.py`,
`src/kdive/db/schema/0130_external_boot_cmdline_failure_diagnostics.sql`,
`tests/jobs/test_external_boot_authority_models.py`,
`tests/jobs/handlers/external_boot/test_runner.py`, and the existing external-boot authority
database integration tests.

Interfaces:

- Adds closed `ExternalBootCmdlineMismatchV1(schema, expected_cmdline, observed_cmdline,
  first_differing_byte)` nested optionally in `_FailureContext`.
- Adds `secret_registry: SecretRegistry` to `ExternalBootHandlerPorts`; production assembly supplies
  its existing registry.
- `_bound_failure` copies only a terminal readiness error's exact recognized scalar keys, decodes
  bytes with `surrogateescape`, redacts through a fresh `Redactor`, deterministically escapes every
  control, invalid byte, and literal backslash, bounds the result, and otherwise emits phase-only
  output.
- Migration `0130` widens only the commit function's failure-context validation and retains the
  existing `jobs.failure_context` column and old result compatibility.

Verification:

- Mode: focused-test. Model tests reject missing, extra, oversized, wrong-schema, and out-of-range
  diagnostic fields while accepting the legacy phase-only shape; red is extra-field rejection,
  green command: `uv run python -m pytest tests/jobs/test_external_boot_authority_models.py -q`.
- Mode: focused-test. Runner tests register a secret occurring in both values and prove the carrier
  contains only redacted bounded strings and the correct offset; NUL, another C0 byte, invalid UTF-8,
  and a literal backslash remain distinct PostgreSQL-safe text. Green command:
  `uv run python -m pytest tests/jobs/handlers/external_boot/test_runner.py -q`.
- Mode: focused-test. Database tests prove migration `0130` accepts the closed v1 diagnostic,
  rejects malformed shapes, and preserves legacy commits; use the exact test file that already
  exercises `commit_external_boot_authority_result`.

Steps:

1. Add failing model and runner tests for versioning, bounds, redaction, legacy compatibility, and
   unknown-detail refusal.
2. Implement the nested model, injected registry, and allowlisted redaction transfer; run focused
   model/runner tests green.
3. Add migration `0130` with source-shape guards around the exact commit-function validation change.
4. Add database acceptance/refusal tests and run their exact file green.

Acceptance: no raw command-line diagnostic crosses authority persistence, every accepted payload is
bounded and versioned, SQL independently validates it, and phase-only failures remain valid.

## Task 4: Return exact guest bytes from both provider paths

Files: `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`,
`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`,
`src/kdive/providers/local_libvirt/lifecycle/boot/session.py`,
`src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py`,
`src/kdive/providers/local_libvirt/lifecycle/xml.py`, relevant provider tests, and test bindings.
The behavior-preserving executor move also changes
`src/kdive/providers/remote_libvirt/guest/agent.py`, creates
`src/kdive/providers/shared/guest_agent.py`, and moves or extends its existing tests under
`tests/providers/shared/`.

Interfaces:

- Remote `observe_guest_identity(...) -> RunningKernelObservation` returns live and plan-derived
  expected bytes and no longer compares them locally.
- The hardened `GuestAgentExec` moves to the shared provider module without changing its fixed
  program allowlist, two-phase polling, timeout, base64, or error classification contracts; both
  providers import it there. Each observation reader applies the 2,048-byte content bound after the
  executor returns.
- Local `RunningObserver` receives the opened domain, performs bounded qemu-guest-agent reads, and
  returns live and target-XML-derived expected bytes. The standard guest-agent channel is rendered
  for newly provisioned local domains; absence names reprovisioning as recovery.
- Local `LocalExternalBootOperation.observe_running(...) -> RunningKernelObservation` validates
  only the returned kernel identity against metadata; core owns command-line comparison.

Verification:

- Mode: focused-test. Provider tests cover one newline, missing newline, two newlines, invalid UTF-8,
  the 2,048-byte content bound, exact preservation, local channel XML, and missing-channel recovery;
  red is missing carriage or production observer, green command: `uv run python -m pytest
  tests/providers/remote_libvirt/lifecycle/test_external_boot.py
  tests/providers/local_libvirt/test_external_boot.py
  tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py
  tests/providers/local_libvirt/lifecycle/test_xml.py -q`.
- Mode: focused-test. The moved executor's existing suite passes from its shared test location and a
  local fixed-program case proves the same allowlist and classification behavior.

Steps:

1. Add provider tests for exact byte carriage and newline behavior.
2. Run the focused command and observe missing carriage or premature provider comparison.
3. Move the executor and tests to the shared provider module without behavior changes; run its suite.
4. Populate the transient observation on remote reads; widen the local observer seam to receive the
   domain, implement its bounded guest-agent reads through the shared executor, and render the
   standard channel.
5. Update fixtures and run the focused command green.

Acceptance: both provider implementations perform the guest read and return exact bytes for the
common core check. Existing local Systems without the newly rendered channel fail with a bounded,
actionable reprovision diagnostic rather than producing false readiness.

## Task 5: Prove the local path on native x86_64

Files: the smallest applicable case under `tests/live_vm/` and, only if its environment contract
changes, `docs/operating/runbooks/live-testing.md`.

Interfaces:

- Consumes the production local domain renderer and shared guest-agent executor on this host.
- Observes a connected `org.qemu.guest_agent.0` channel and exact command-line bytes through the
  assembled local observation mechanism.

Verification:

- Mode: focused-test. The new `live_vm` node runs with the canonical environment from the live-test
  runbook; absence of the configured fixture skips under the tier contract, while this provided host
  must execute and pass before completion.

Steps:

1. Read the live-testing runbook and select the existing local external-boot-compatible fixture.
2. Add the smallest live case proving channel connection and exact command-line observation.
3. Run that node on this x86_64 host and record its non-skipped passing result.

Acceptance: real local libvirt and the guest agent return the exact running command line through the
production observer. No ppc64le live run is required.

## Final verification and rollback

Run `just lint`, `just type`, all focused commands above, the native x86_64 live node, and then the
captured full `just ci`. Rollback is a normal git revert; migration `0130` only adds an accepted
failure-context shape and requires no data rollback.
