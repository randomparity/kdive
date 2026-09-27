# Implement local-libvirt module label policy

Issue: #2783. Base: `main`. Branch: `feat/guest-policy-module-labels-2783`.
Guardrails: `just lint`, `just type`, focused provider tests, `just ci` before push.
Fixed design denominator: 1000 changed lines (L), from recovery identity, fencing, and SELinux
live proof in the approved scope packet.
Expected implementation size: 250–500 changed lines (L) — policy evaluator, local lifecycle
integration, provisioning, and focused tests.

## Task 1: Evaluate the inactive guest policy

Files: new `src/kdive/providers/local_libvirt/lifecycle/boot/selinux_policy.py`; focused tests in
`tests/providers/local_libvirt/lifecycle/boot/test_selinux_policy.py`.

Interface: `guest_policy(guest: InactiveGuest) -> ContextManager[ModuleLabelPolicy | None]`;
`ModuleLabelPolicy.label(path: str, mode: int) -> bytes` returns the xattr bytes including NUL.
The evaluator reads bounded guest policy files, validates the SELinux type, opens libselinux's
file-context backend with an explicit temporary `file_contexts` path, and looks up final module
paths with their file type. Temporary files and native handles close on all exits. Missing policy
for enabled SELinux and failed lookups raise before provider mutation. The output is consumed by
Tasks 2 and 3.

Verification: `Mode: focused-test` — a temporary file-context fixture distinguishes module
files from `modules.*` indexes; a controlled invalid spec or missing match fails. Run
`uv run python -m pytest tests/providers/local_libvirt/lifecycle/boot/test_selinux_policy.py -q`;
expect all cases pass after observing the pre-fix failure. Do not run guest binaries.

## Task 2: Bind and apply the target identity

Files: `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; focused tests in
`tests/providers/local_libvirt/test_external_boot.py`.

Interface: the existing `LocalExternalBootOperation.prepare` computes a labeled target manifest
from `recovery._validate_archive` and `_manifest`, and
`_complete_preparation_metadata(intent, materialization, capture, target_manifest)` stores it.
`LibguestfsAuthenticatedGuestTree` takes an optional `ModuleLabelPolicy`, applies the context
after normalized metadata for every entry kind, and `_SessionModulePublicationIO.staging_tree`
passes it only during target activation. The activation caller observes the staged tree and
compares it to metadata before moving the live tree. Recovery restores captured bytes unchanged.

Verification: `Mode: focused-test` — a labeled archive must produce a distinct target digest;
staging mismatch blocks publication; a changed live label remains a conflict. Run
`uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q`; expect all cases
pass after observing the pre-fix failure.

## Task 3: Provision and record the native prerequisite

Files: `deploy/ansible/roles/local_worker_host/defaults/main.yml`, this plan's ADR-0691, and
the native host verification task when needed. Add the distribution libselinux package to each
admitted local worker package list, including the Ubuntu live-VM runner's shared list. Keep
package names native to each family and make missing library errors actionable.

Verification: `Mode: focused-test` — provisioned package lists must include libselinux for the
Debian, RedHat, and SUSE worker families; run the repository's Ansible lint recipe from `justfile`.
`Mode: task-test-not-applicable` — ADR prose has no executable consumer of its wording; doc-link
and record guards validate references and record structure.

## Task 4: Assemble and prove

Run focused tests, `just lint`, `just type`, and `just ci` with blocking standard streams. Review
the final diff against the approved exclusions, perform adversarial and security review, then
push for CI. Use the live-testing runbook before the native VM proof; record the actual host/guest
architectures and exact tier results. A failed or unavailable live prerequisite is reported
rather than represented as a pass. The quest handoff names the commit and PR head SHA.
