# Retained host formatter coexistence (#3070)

Approved design (2026-10-07): the operator approved the formatter policy.
Frozen scopeq3070-a2fbf5ea, full-specM250, base95e28bcac.

## Problem and evidence

Developer setup's source Go-tool helper installs shfmt3.13.1 into uv's shared user-bin and
requires it to win PATH. A controlled source-helper reproduction with system3.14.1 and an
earlier empty user-bin failed the retained-selection assertion after setup; system bytes
were unchanged but user-bin3.13.1 won lookup. Only the external Go install boundary was stubbed.
Downstream guest_verify.py separately checks system binary ownership/mode/digest and exact
fresh-login path/version. The existing isolated shfmt-src hook already pins3.13.1-1.

## Policy and ownership

[ADR0732](../../adr/0732-checkout-scoped-developer-shfmt.md) narrows ADR0694 for shfmt only.
Developer setup owns installation at build/dev-tools/bin/shfmt, already covered by build/ ignore.
A short scripts/shfmt.sh dispatcher owns KDIVE recipe/preflight selection: use that private file
when present, otherwise command-v shfmt; require exact --version v3.13.1 before forwarding args.
An invalid present private file fails closed; do not hide it by falling back to ambient tools.
Installation verifies the exact private destination and reuses it on repeat setup.
The ordinary report-only check-deps keeps its existing host-tool behavior. --setup uses the
same dispatcher as lint-shell. Existing shfmt-src isolated hook and CI's exact ambient pin
remain unchanged. No global PATH export, profile edit, arbitrary user-bin deletion or downstream
verification adjustment. No new tool dependency or version is introduced.

## Success and compatibility

A valid retained system3.14.1 remains selected by the same parent/fresh-login environment before,
after and after repeated setup, with byte identity preserved. KDIVE lint-shell and developer
preflight select3.13.1; ordinary git/prek formatting uses the existing pinned Go hook.
The dispatcher forwards arguments and exit status. Missing binary, nonexecutable/corrupt private
binary, wrong version and failed installation cannot report success. Paths with spaces work.
Absent private binary plus exact ambient3.13.1 continues to support manually prepared/CI hosts.
Other developer installers and their error behavior remain unchanged. Native POWER builds
shfmt through the same Go module rather than receiving a foreign-architecture executable.
A previously drifted retained fixture must be reconciled through its own owner before this
repair can be proved; setup does not erase an existing shared-user formatter to manufacture green.

### Failure model

- Actors/deployments: trusted developer checkout on Linux, retained toolchain host with an
  earlier user-bin PATH, manually prepared exact-pin CI; native x86_64 and ppc64le targets.
- Invariants/assets: tested formatter selection, retained host binary/path/version, unchanged
  hook isolation, exact private installation/reuse, clear failure and cross-repository ownership.
- Accepted failures: unavailable Go/network/build toolchain or invalid selected executable fails
  setup/checks; pre-existing retained-fixture drift blocks its proof. Neither permits newer-tool
  acceptance, destructive cleanup or a passing verification claim.
- Covered elsewhere: downstream provisioning revisions require its fixture owner's separate
  scope; unrelated tool installation remains ADR0694; native POWER qualification remains#2818.

## Validation

Execute the real helper and dispatcher with external Go builds stubbed: system3.14.1 plus earlier
empty user-bin; verify host path/hash unchanged, private3.13.1, repeated setup reuse and no shared
writes. Run the actual lint-shell recipe with boundary stubs to prove both formatter calls use
that selection. Test missing/private-wrong/ambient-exact/ambient-newer and exit/argument handling.
Exercise --setup preflight with that same selection; preserve ordinary report-only behavior.
Existing POWER, archive checksum, installation failure and Helm build-flag tests remain gates.
Before/after/repeat candidate setup on an exclusively authorized Rocky fixture, run its unchanged
retained-toolchain verifier using fresh-login selection. Record exact candidate and fixture
source identity. Run KDIVE lint-shell and the existing formatter hook at that candidate.
Focused tests, lint/type/shell/docs precede commit. Mandatory managed prepush owns fulljustci;
CI and native fixture proof remain separate. No POWER or release qualification claim follows.
