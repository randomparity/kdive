# ppc64le gdbstub tracker reference

## Problem

The native gdbstub skip and the kernel-case operator notes point to #2678. That issue covers
broader native POWER testing, while #2736 tracks the still-unproven ppc64le gdbstub capability.
The wrong reference sends a reader to the wrong owner when the skip fires.

## Scope

Change the #2678 reference to #2736 in `tests/mcp/debug/session_support.py` (comment,
docstring, and emitted skip reason), `docs/development/kernel-test-cases/README.md`,
`docs/development/kernel-test-cases/11-hugetlb-boot-param-null-deref.md`, and
`docs/development/kernel-test-cases/18-kpageflags-ksm-false-positive.md`. Update the expected
issue number in
`tests/mcp/debug/test_session_support.py`. Clarify that #2736 tracks ppc64le, while other
architectures remain unproven. Keep the helper's accepted-architecture set, profile
generation, and skip behavior unchanged. The helper owns the skip reason; the unit test
owns its assertion; the three Markdown pages own operator guidance. This is a text
correction with no owner transition or caller migration.

Native pseries gdbstub discovery and proof belong to #2739. ppc64le support belongs to #2740.

### Failure model

1. **Actors and deployments:** CI runs the focused debug helper test; operators read the
   kernel-case README, case 11, case 18, and native live skip reason.
2. **Invariants and assets:** The ppc64le skip must still occur and identify #2736; x86_64
   must still pass through the architecture guard.
3. **Accepted failure classes:** A native ppc64le debug proof still skips; #2739 and #2740
   own the absent capability, and this reference change has no means to establish it.
4. **Covered elsewhere:** Native POWER gdbstub investigation is #2739; support is #2740.

## Success

The ppc64le helper test observes a skip reason containing #2736. The three Markdown pages
and helper text cite #2736 for that gap. The existing x86_64 and unrelated-architecture
guard tests continue to pass; the latter identifies other architectures as unproven.

## Validation

- `Mode: focused-test` — the ppc64le skip tracker is observable in
  `tests/mcp/debug/test_session_support.py::test_require_live_gdbstub_arch_skips_with_a_reason_on_ppc64le`.
  Changing only its expectation to #2736 fails against the old helper; after the helper edit,
  `uv run python -m pytest tests/mcp/debug/test_session_support.py -q` passes.
- `Mode: focused-test` — the unrelated-architecture skip text is observable in
  `tests/mcp/debug/test_session_support.py::test_require_live_gdbstub_arch_skips_rather_than_fails_on_an_unrelated_arch`.
  Expecting "other architectures unproven" fails against the old text; the same focused
  command passes after the wording edit.
- `Mode: task-test-not-applicable` — the three Markdown pages and helper comment/docstring
  prose have no executable consumer of their issue-number wording. Review their diff and run
  the repository documentation guardrails through `just ci`.
