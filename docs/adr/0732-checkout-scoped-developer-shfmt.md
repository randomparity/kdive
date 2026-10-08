# 0732 — Checkout-scoped developer shfmt

## Status

Accepted (2026-10-07).
Partially supersedes ADR-0694 only for shfmt installation and developer-check selection.

## Context

Issue #3070 records a retained Rocky toolchain requiring its digest-verified system shfmt3.14.1
and exact operator-login selection path. Developer setup instead installs3.13.1 into an earlier
shared user-bin directory. The real source helper reproduces the selection change while leaving
the original binary digest intact. KDIVE CI and its isolated Go hook pin3.13.1 deliberately.

## Decision

Install only KDIVE shfmt3.13.1 in ignored checkout-local build/dev-tools/bin. Do not change
ambient PATH, profiles, system formatter or shared user-bin formatter. Other developer tools
retain ADR-0694's existing installation policy.

The lint-shell recipe and --setup preflight use one checkout formatter dispatcher. Prefer the
private binary; if absent, accept ambient shfmt only when its --version is exactlyv3.13.1.
A present but invalid private binary fails with an actionable setup remedy, without ambient
fallback. Setup verifies/reuses the exact private destination, independent of PATH selection.
No generic accept-newer rule. The existing shfmt-src hook remains independently pinned and
native-Go-built; no hook PATH rewrite or binary download is introduced.

## Consequences

Each checkout can retain its own formatter without changing another repository's login tools.
Manually prepared and CI environments with exact ambient3.13.1 remain valid before setup.
A missing or wrong selected formatter fails developer verification instead of silently choosing
another version. Existing shared-user shadowing is not deleted or overwritten by this repair;
retained fixtures must pass their own baseline verification before candidate setup.

Native POWER still builds the same pinned Go module. No downstream fixture revision, generic
toolchain framework, new dependency or version bump is part of this change.

## Considered & rejected

- Align KDIVE with the fixture's newer version. judgment: requires separate compatibility and
  pin changes rather than solving coexistence; downstream changes need separate approved scope.
- Isolate every developer tool. judgment: adds unrelated shellcheck/hook, Helm/promtool and
  credential-scanner callers when the reproduced conflict is shfmt.
- Keep shared-user installation but temporarily reorder PATH. verified: downstream
  guest_verify.py operator_command uses a fresh login and check_rocky_operator_shfmt checks the
  exact system path; changing only KDIVE's caller environment does not preserve that invariant.
