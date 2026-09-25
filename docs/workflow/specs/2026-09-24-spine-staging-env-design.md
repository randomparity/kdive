# Isolate the live-stack module staging environment

Status: implementation design for #2751.

## Problem

`combined_kernel_tar` invokes `make modules_install` without `env=`, so exported
Kbuild variables such as `ARCH`, `CROSS_COMPILE`, `INSTALL_MOD_STRIP`, and
`INSTALL_MOD_DIR` can change the uploaded kernel bundle without a harness choice.
The helper is shared by local and remote live-stack proofs. PR #2744 separately
owns the decision to pass `INSTALL_MOD_STRIP=1` on the make command line.

## Scope and contract

Keep the existing shared helper and callers. Pass `make` an explicit environment
containing only available `PATH`, `HOME`, and locale variables. Pass Kbuild's
`ARCH` on the command line, mapped from the helper's target `arch` argument:
`x86_64` to `x86`, and `ppc64le` to `powerpc`. Keep the existing
`INSTALL_MOD_PATH` argument; retain `INSTALL_MOD_STRIP=1` if #2744 merges first.
The boot member resolver already rejects unknown targets before invoking make.

This change does not alter the boot ELF strip or tar subprocess environments,
production build/install behavior, or generic subprocess environment policy.
No new configuration knob or dependency is needed. Cross-compiling the bundle
on a different host still needs a separately designed explicit toolchain input;
the current supported native paths use the host's tools. This change does not
claim a new cross-host packaging capability.

## Success

- Ambient `ARCH`, `INSTALL_MOD_STRIP`, `CROSS_COMPILE`, `KBUILD_*`, and module
  install variables are absent from the make environment.
- The selected target supplies the Kbuild architecture explicitly for x86_64
  and ppc64le; the selected tree supplies the module contents.
- `PATH`, `HOME`, and locale needed to execute make and its recipes survive.
- Existing x86_64 and ppc64le bundle shape and the #2744 strip flag survive.

## Validation

- Focused test: intercept `subprocess.run`, set adversarial Kbuild variables,
  and assert the make call's argument and environment for both targets. With
  the prior implementation this test fails because no `env` exists; with a
  wrong mapping or leaked variable it also fails. Run `uv run python -m pytest
  tests/integration/live_stack/test_spine.py -q`.
- Focused test: preserve the existing make argument expected by #2744 if its
  branch lands; no production tests or API contract changes are needed.
- Run the existing ppc64le bundle test in `test_harness_unit.py`; its
  subprocess double must accept `env=` while retaining its tar assertions.
- Run `just lint`, `just type`, and the pre-push `just ci` gate before handoff.

## Failure model

An absent boot member fails in `boot_member_source` before make with an
actionable path. A malformed build tree can fail in make or tar. A missing
required executable path causes `subprocess.run(check=True)` to fail visibly.
Unexpected caller Kbuild
settings must not silently affect the bundle. Locale and `HOME` are retained
solely for command execution and do not select a kernel target.
