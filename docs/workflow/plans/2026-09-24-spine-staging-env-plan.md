# Implement isolated spine module staging

Goal: Make the shared live-stack module installer depend on explicit target
arguments and its selected tree rather than exported Kbuild settings.

Architecture: Retain `combined_kernel_tar` as the owner. Add an explicit
environment to its one `make modules_install` call and a target-to-Kbuild
architecture mapping. Keep the current tar and boot member work untouched.

Expected implementation size: 25–65 changed lines across three test files.
Scope: #2751 frozen `WORK:SCOPE` q2751-193f1477. Base: main. Commands:
`uv run python -m pytest tests/integration/live_stack/test_spine.py -q`,
`just lint`, `just type`, pre-push `just ci`.

## Task 1: Prove and isolate the make call

Files: `tests/integration/live_stack/test_spine.py`,
`tests/integration/live_stack/test_harness_unit.py`,
`tests/integration/live_stack/spine.py`.

Criterion: exported Kbuild inputs do not choose module staging; explicit
`arch` chooses the Kbuild target. The current owner remains the shared spine
helper, so local and remote callers need no migration. The existing boot
member and tar paths remain because they implement separate bundle contracts.

Verification: focused-test — create a built boot member for x86_64 and
ppc64le, inject `INSTALL_MOD_STRIP`, `ARCH`, `CROSS_COMPILE`, and another
Kbuild variable, intercept subprocess calls, and assert that make receives
only the allowed environment and the appropriate command-line `ARCH`.
The prior code fails because it passes no `env`. Green command:
`uv run python -m pytest tests/integration/live_stack/test_spine.py
tests/integration/live_stack/test_harness_unit.py -q`.

Implementation: build a local dictionary from present `PATH`, `HOME`, and
locale keys, and pass it as `env` to this make call. Map the two validated
target names to `ARCH=x86` and `ARCH=powerpc` in argv. Preserve the existing
`INSTALL_MOD_PATH` and any merged #2744 `INSTALL_MOD_STRIP=1` argument.
Update the existing ppc64le subprocess double to accept the make call's
`env` keyword without weakening its boot and tar assertions.

## Integration and handoff

Run focused tests, `just lint`, and `just type`; review the scoped diff and
commit. The mandatory pre-push hook runs `just ci` on the pushed checkout.
Review the complete branch, then create a PR and wait for GitHub checks.
Do not merge from this worker.
