# ppc64le gdbstub tracker reference

## Problem

The native gdbstub skip and the kernel-case operator notes point to #2678. That issue covers
broader native POWER testing, while #2736 tracks the still-unproven ppc64le gdbstub capability.
The wrong reference sends a reader to the wrong owner when the skip fires.

## Scope

Change the #2678 reference to #2736 in `tests/mcp/debug/session_support.py` (comment,
docstring, and emitted skip reason) and `docs/development/kernel-test-cases/README.md`. Update
the expected issue number in `tests/mcp/debug/test_session_support.py`. Keep the helper's
accepted-architecture set, profile generation, and skip behavior unchanged. The helper owns
the skip reason; the unit test owns its assertion; the README owns operator guidance. This
is a clean text correction with no owner transition or caller migration.

Native pseries gdbstub discovery and proof belong to #2739. ppc64le support belongs to #2740.

### Failure model

1. **Actors and deployments:** CI runs the focused debug helper test; operators read the
   kernel-case README and native live skip reason.
2. **Invariants and assets:** The ppc64le skip must still occur and identify #2736; x86_64
   must still pass through the architecture guard.
3. **Accepted failure classes:** A native ppc64le debug proof still skips; #2739 and #2740
   own the absent capability, and this reference change has no means to establish it.
4. **Covered elsewhere:** Native POWER gdbstub investigation is #2739; support is #2740.

## Success

The ppc64le helper test observes a skip reason containing #2736. The README and helper text
cite #2736 for the same gap. The existing x86_64 and unrelated-architecture guard tests
continue to pass.

## Validation

- `Mode: focused-test` — the ppc64le skip tracker is observable in
  `tests/mcp/debug/test_session_support.py::test_require_live_gdbstub_arch_skips_with_a_reason_on_ppc64le`.
  Changing only its expectation to #2736 fails against the old helper; after the helper edit,
  `uv run python -m pytest tests/mcp/debug/test_session_support.py -q` passes.
- `Mode: task-test-not-applicable` — README and helper comment/docstring prose has no
  executable consumer of its issue-number wording. Review their diff and run the repository
  documentation guardrails through `just ci`.
