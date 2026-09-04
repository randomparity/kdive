# External-boot command-line readiness implementation plan

Goal: enforce ADR-0583's exact running command-line proof in core for both external-boot providers.

Architecture: widen the existing provider-neutral observation, keep guest reads at provider
boundaries, and make the core lifecycle the sole equality authority. The shared plan validator
rejects XML-illegal bytes before either renderer receives them.

Tech stack: Python 3.14, Pydantic models, pytest, local and remote libvirt provider seams.

Expected implementation size: 180–320 changed lines (M) — derived from one port-model change, one
core comparator, two provider adjustments, fixture updates, and focused tests.

## Global constraints

- Support x86_64 and ppc64le contracts; this host is x86_64 and live ppc64le testing is excluded.
- Remove exactly one trailing newline from `/proc/cmdline`; perform no other normalization.
- Compare the remaining bytes with `plan.cmdline.encode("utf-8")` byte-for-byte.
- Use terminal `READINESS_FAILURE` for changed bytes and preserve retry behavior for unavailable
  evidence.
- Do not add a dependency, schema migration, public MCP surface, or new ADR.
- Run `just lint`, `just type`, focused tests, and `just ci > <file> 2>&1 < /dev/null` before push.

## Task 1: Widen and validate the shared observation contract

Files: `src/kdive/providers/ports/external_boot.py`,
`tests/providers/ports/test_external_boot.py`, and provider contract bindings/tests.

Interfaces:

- Produces `RunningKernelObservation(..., cmdline: bytes)` for both providers and core.
- Keeps `ExternalBootPorts.observe(...) -> RunningKernelObservation` unchanged.
- Tightens `_validate_platform_argument(value: str) -> str` for XML 1.0 representability.

Verification:

- Mode: focused-test. Required command-line bytes fail existing constructors first; green command:
  `uv run python -m pytest tests/providers/ports/test_external_boot.py tests/providers/contract/test_external_boot_contract.py -q`.
- Mode: focused-test. XML-illegal C0 arguments are accepted before the change; the same command
  rejects them after the validator change.

Steps:

1. Add tests constructing and serializing an observation with exact bytes and rejecting C0 values.
2. Run the focused command and observe missing-field and accepted-control failures.
3. Add required `cmdline: bytes` and an XML 1.0 character predicate to the shared port model.
4. Update contract fixtures and run the focused command green.

Acceptance: every observation contains bytes and every XML-illegal platform token is rejected at
the shared boundary.

## Task 2: Enforce the plan command line in core

Files: `src/kdive/jobs/handlers/external_boot/lifecycle.py` and
`tests/jobs/handlers/external_boot/test_lifecycle.py`.

Interfaces:

- Consumes `context.activation.materialization.kernel_observation.cmdline` (sourced from the plan
  at materialization) and the live `RunningKernelObservation.cmdline`.
- Produces a terminal `CategorizedError` with category `ErrorCategory.READINESS_FAILURE` carrying
  escaped expected/observed strings and `first_differing_byte`.

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

## Task 3: Return exact guest bytes from both provider paths

Files: `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`,
`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`, relevant provider tests, and
test bindings.

Interfaces:

- Remote `observe_guest_identity(...) -> RemoteGuestIdentity` returns a
  `RunningKernelObservation` containing the stripped bytes and no longer compares them locally.
- Local `LocalExternalBootOperation.observe_running(...) -> RunningKernelObservation` preserves
  the injected observer's bytes and compares only its kernel identity fields with metadata.

Verification:

- Mode: focused-test. Provider tests cover one newline, missing newline, two newlines, and exact
  byte preservation; red is missing `cmdline` or provider-local mismatch, green command:
  `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_external_boot.py tests/providers/local_libvirt/test_external_boot.py -q`.

Steps:

1. Add provider tests for exact byte carriage and newline behavior.
2. Run the focused command and observe missing carriage or premature provider comparison.
3. Populate the widened observation on remote reads and preserve the local observer's bytes.
4. Update fixtures and run the focused command green.

Acceptance: both provider implementations return exact bytes for the common core check; local
production reachability remains an explicit #2212 dependency rather than hidden scope growth.

## Final verification and rollback

Run `just lint`, `just type`, the three focused commands above, and then the captured full
`just ci`. Rollback is a normal git revert; no persisted schema or external state changes.
