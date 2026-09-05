# Remote domain metadata round-trip implementation plan

Goal: retain remote System and storage ownership through real-libvirt inactive XML readback.

Architecture: ADR-0598 groups both records beneath one KDIVE namespace root. Shared parsing keeps
the renderer, admission gate, teardown, and reaper aligned without changing local-libvirt output.

Tech stack: Python 3.14, ElementTree/defusedxml, pytest, libvirt.

Expected implementation size: 45–80 changed lines (S) — one renderer, two helpers, direct readers,
and focused regressions.

## Global constraints

- Preserve exact System, pool, volume, and overlay-path matching.
- Preserve legacy standalone System discovery only for reaping existing domains.
- Do not add a dependency or change local-libvirt metadata.
- Exclude native ppc64le testing; run the authorized native x86_64 Ubuntu/libvirt carrier.
- Guardrails are `just lint`, `just type`, focused `just test-verbose <path>`, and pre-push
  `just ci > <file> 2>&1 < /dev/null`.

## Task 1: Group and parse remote ownership metadata

Files: `src/kdive/providers/shared/libvirt_xml.py`,
`src/kdive/providers/remote_libvirt/lifecycle/xml.py`,
`src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`,
`src/kdive/providers/remote_libvirt/reaping/domains.py`, and focused provider tests.

Interfaces: add pure helpers that locate the grouped KDIVE root and return System text or storage
attributes from a parsed full-domain root. `render_domain_xml(...) -> str` emits that shape;
`require_disk_grub_source(...) -> None`, `disk_pool[_strict](...)`, and reaper ownership consume it.

Verification:

- Mode: focused-test. Contract: one namespaced root retains System and storage identity. Tests:
  rendering/parser assertions in provider lifecycle and shared XML tests. Expected red: current
  sibling shape fails the grouped-root assertion. Green command:
  `just test-verbose tests/providers/test_libvirt_xml.py tests/providers/remote_libvirt/lifecycle`.
- Mode: focused-test. Contract: ownership admission rejects changed pool, volume, and path. Test:
  `test_admission_rejects_a_source_that_is_not_the_owned_baseline`. Expected red: the new wrong-
  volume case is not exercised. Green command:
  `just test-verbose tests/providers/remote_libvirt/lifecycle/test_external_boot.py`.
- Mode: focused-test. Contract: legacy standalone System metadata remains reapable without storage
  authority. Tests: remote reaper and XML parser regressions. Expected red: grouped-only parsing
  would lose legacy ownership. Green command:
  `just test-verbose tests/providers/test_libvirt_xml.py tests/providers/remote_libvirt/reaping/test_domains.py`.

Steps:

1. Add the grouped-shape and wrong-volume tests; confirm their expected failures.
2. Add the minimal shared parsing helpers and grouped renderer.
3. Migrate every direct remote reader and make the focused commands pass.
4. Run `just lint` and `just type`; commit the implementation with ADR-0598 accepted and cited.

Acceptance: all direct remote readers consume one representation, unchanged ownership passes, each
changed identity fails, and existing standalone System tags remain discoverable by the reaper.

## Task 2: Prove the real-libvirt round trip

Files: the existing #2121 native carrier under `tests/live_vm/`, if a reusable assertion belongs
there; otherwise no tracked file is required.

Interfaces: production `render_domain_xml` output enters libvirt `defineXML`; inactive `XMLDesc`
feeds the same ownership parser and admission gate.

Verification:

- Mode: focused-test. Contract: real libvirt retains both children and the production gate accepts
  only the exact readback. Test: authorized native Ubuntu/libvirt carrier. Expected red: the old
  sibling shape loses storage. Green command: invoke the existing carrier with its private
  environment, without logging secret values.

Steps:

1. Run the carrier against the clean native Ubuntu host.
2. Confirm unchanged inactive XML passes and independent pool, volume, and path mutations fail.
3. Clean every domain, overlay, boot artifact, and isolated pool created by the carrier.

Acceptance: the live proof passes and leaves no test resource behind.
