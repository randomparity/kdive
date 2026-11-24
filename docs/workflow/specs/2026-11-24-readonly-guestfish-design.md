# Shared read-only guestfish invocation

## Problem

Five provenance probes repeat the same guestfish argv, subprocess options and two error
translations. Their messages, output types and fallback rules differ and must remain local.

## Scope

In `images/planes/provenance_probes.py`, extract `_run_readonly_guestfish(path, command,
guest_path, *, budget_s, text, missing_message, timeout_message)` in the existing module.
It invokes fixed `guestfish --ro -a <path> -i <command> <guest_path>` with captured output,
`check=False`, the supplied text mode and timeout. Minimal Literal overloads preserve typed
text/bytes results. Translate only FileNotFoundError and TimeoutExpired with each caller's
exact message, existing category/details and chained cause.
Migrate both marker readers, kernel-config reader, boot listing and os-release reader;
remove their duplicate process/error blocks. Preserve signatures, DEFAULT aliases, the
bounded inspector and advisory callers. No module ownership, public or policy change occurs.
Calculate host-scaled budget in each probe; os-release calculates once before its fallback
loop and gives each invocation that full budget. Keep returncode/content/fallback handling
in callers. Correct the makedumpfile docstring's obsolete `download` wording to `cat`.

### Failure model

- Assets: image provenance, verbatim config bytes, read-only image access and bounded probes.
- Fixed argv has no shell; preserve `--ro`, capture, timeout, exception causes and categories.
- Missing tools/timeouts propagate without os-release fallback. Other process exceptions remain
  uncaught. Nonzero exits/empty content retain each caller's established absence behavior.
- Accept existing captured-output buffering and per-invocation budgets, including a second full
  budget for os-release fallback. No new output cap, shared deadline or native-tool claim.

## Success

One process/error block serves five probes. Markers strip text; config preserves nonempty
bytes including whitespace, CRLF and non-UTF8; boot entries strip/filter and preserve empty
lists; os-release preserves successful raw text and skips fallback after `/etc` success.

## Validation

- focused-test: extend existing five-probe missing/timeout cases with exact message/cause and
  argv/options assertions; `test_guestfish_process_arguments` covers all five fixed invocations.
  Before extraction, a controlled timeout-option fault must fail that test; restore baseline
  GREEN. Post-change GREEN: `just test-verbose tests/images/planes/test_provenance_probes.py`.
- focused-test: existing config/output/fallback cases gain binary and whitespace edges;
  `test_probe_os_release_reuses_one_budget` verifies calculation count, both paths, raw text
  and per-invocation timeout. Process exception tests prove no fallback.
- focused-test: preservation GREEN: `just test-verbose tests/images/planes/test_provenance_probes.py
  tests/images/test_stage_volume.py tests/providers/local_libvirt/test_rootfs_build.py`.
  Compare signatures/aliases, untouched AST and migration hashes; run `just lint`, whole-tree
  `just type`, hooks and fresh scoped code/security reviews.
- task-test-not-applicable: pure extraction introduces no new runtime behavior requiring a
  baseline defect; do not add private-name/shape tests. Doc wording needs no prose snapshot.
  Boundary doubles verify invocation policy; this task makes no native-appliance/hardware claim.
