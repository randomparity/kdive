# Remote module appliance supervision implementation plan

Goal: isolate the ADR-0585 supervisor from the authorized lineage and close issue #2169's three
defects. The supervisor remains a provider-private boundary over libvirt, #2167 attachment proof,
#2170 prepared volumes, durable module documents, and secret redaction.

Tech stack: Python 3.14, libvirt-python, pytest, `uv`, and `just`.

## Global constraints

- Support x86_64 and ppc64le contracts; native ppc64le live testing is excluded.
- Preserve the 300-second invocation cap, 30-second idle cap, and 10-millisecond would-block yield.
- Preserve categorized conflicts, secret redaction, bounded evidence, and fail-closed teardown.
- Do not duplicate #2167 attachment or #2170 volume contracts.
- Run `just lint`, `just type`, focused tests, and `just ci` before delivery.

Expected implementation size: 1,560–1,680 changed lines (L) — the authorized 470-line runtime and
1,108-line test suite plus three focused corrections and package export reconciliation.

## Task 1: Extract the appliance supervisor and establish red evidence

Files: create `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_appliance.py` and
`tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_appliance.py`; modify
`src/kdive/providers/remote_libvirt/lifecycle/rootfs/__init__.py` only if its existing export style
requires it.

Interfaces: consume `RemoteModuleOperationV1`, `RemoteModuleResultV1`, #2167's
`AttachmentInspection`, `ExpectedAppliance`, `ExpectedAttachmentState`,
`normalized_appliance_devices`, and `validate_appliance_xml`, and #2170's `PreparedVolume`.
Provide `ApplianceRequest`, `ApplianceOutcome`, `render_remote_module_appliance`,
`run_or_adopt_appliance`, and `teardown_remote_module_appliance` with the lineage signatures.

Verification:

- Mode: focused-test — a foreign same-name domain preserves `ErrorCategory.CONFLICT`; add
  `test_foreign_same_name_domain_preserves_conflict_category`, observe failure because the lineage
  wraps it in `RuntimeError`, then run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_appliance.py -q` and expect pass.
- Mode: focused-test — progress re-arms the idle deadline but not the total deadline; add tests
  whose controlled clock receives bytes at 20-second intervals beyond 30 seconds and at the
  300-second cap, observe the fixed-deadline failure, then run the same focused command and expect
  pass.
- Mode: focused-test — repeated `-2` results yield without re-arming; inject or patch the bounded
  sleeper, observe zero calls against the lineage, then run the same focused command and expect
  pass.
- Mode: focused-test — a stream returning only `-2` reaches exactly the 30-second idle boundary,
  times out, and attempts teardown; observe that the fixed lineage loop retries without a yield and
  does not expose this boundary, then run the same focused command and expect pass.
- Mode: focused-test — a synchronous `openConsole` error aborts its allocated stream, while an
  `UnresolvedCallError` from `openConsole` issues no competing abort; observe the lineage leaking
  the synchronous-error stream, then run the same focused command and expect both cases pass.
- Mode: focused-test — extracted domain, durable-result, redaction, stream-release, and teardown
  behavior; retain the lineage tests and run the same focused command, expecting all pass.

Steps:

1. Copy only the two authorized appliance files from the read-only lineage.
2. Add the three named regression tests and run them against the unchanged extraction, recording
   their expected failures.
3. Verify #2167 and #2170 expose the exact imported interfaces before running the extraction.
4. Re-raise `CategorizedError` before the unclassified identity exception handler.
5. Re-arm the local idle deadline after each non-empty byte chunk, capped by the immutable outer
   deadline.
6. Yield for `min(0.01, remaining idle time)` after `-2` without re-arming.
7. Put synchronous `openConsole` completion and failure inside the release boundary, while handling
   unresolved `openConsole` outside it without abort.
8. Run the focused test file and expect all tests to pass.
9. Run `just lint` and `just type`, then commit the implementation separately from this design.

Acceptance: all public-safe supervision behavior from the authorized lineage remains; the three
issue defects have direct red/green proof; no sibling-owned implementation is introduced.

Rollback: revert the implementation commit; no schema or persisted state changes.
