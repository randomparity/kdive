# Implement libguestfs kernel-readability remediation

Goal: make the shared diagnostic and local preflight point to existing durable provisioning without
widening `/boot`. The runtime constant and preflight remain presentation-only; ADR-0669 replaces
only ADR-0222's kernel-remediation choice. Python 3.14, `uv`, Bash, and existing tests are used.

Expected implementation size: 55–85 changed lines (S) — two remediation strings, focused assertions,
one status pointer, and a narrow ADR from the file map below.

## Global Constraints

- Preserve the existing kernel matcher, CONFIGURATION_ERROR classification, stderr passthrough, and
  passt remediation and Debian/Ubuntu qualification.
- Local-libvirt names `KDIVE_LIFECYCLE_WITNESS_DATABASE_URL=... just prepare-local-libvirt-host`;
  other workers name their owning provisioning play.
- State `root:kvm 0640` and require the worker in `kvm`; fallback uses `/boot/vmlinu?-*`, `sudo
  chgrp kvm`, then `sudo chmod 0640`; only provisioning installs the durable hook.
- Do not edit relabel roles, Fedora/RHEL behavior, existing hosts, or `.github/workflows/live.yml`.

## File map

- `src/kdive/images/planes/_build_common.py` owns shared matched-error remediation text.
- `scripts/operations/check-local-libvirt.sh` owns local preflight remediation text.
- Focused tests own the corresponding operator-visible contracts.
- ADR-0669 owns the new remediation decision; ADR-0222 gets the permitted Status pointer.

## Task 1 — Specify the accepted remediation decision

**Files:** add `docs/adr/0669-supported-libguestfs-kernel-remediation.md`; modify ADR-0222 Status.

**Interfaces:** consumes ADR-0222's classification decision and ADR-0668's durable-hook evidence;
later tasks use the ADR's exact scope.

**Verification:** Mode: task-test-not-applicable — ADR prose has no independently executable
consumer; `just records origin/main` checks record structure.

1. Record ADR-0669 as accepted, superseding only the unsafe kernel remediation choice.
2. Add the one-line ADR-0222 Status pointer; retain its body.
3. Run `git fetch origin main && just records origin/main`; expect exit 0.

## Task 2 — Pin the operator-visible contract

**Files:** modify `tests/images/planes/test_build_common.py` and
`tests/scripts/test_check_local_libvirt.py`.

**Interfaces:** consumes the existing `CategorizedError.details["remediation"]` and preflight
stderr; later source changes make these assertions pass.

**Verification:** Mode: focused-test — the old `0644` assertion fails after replacement; focused
commands below pass after implementation.

1. Assert the runtime hint retains Debian/Ubuntu scope and has the supported path, worker `kvm`
   membership, group/mode, hook, deployment qualification, safe `sudo` glob, and no
   `0644`/`dpkg-statoverride`.
2. Assert the preflight hint has the hook-qualified fallback and no unsafe suggestions.
3. Run both focused node IDs with `just test-verbose`; expect exit 0 after Task 3.

## Task 3 — Replace only stale remediation wording

**Files:** modify `_KERNEL_REMEDIATION` and the unreadable-kernel `note_fail` hint.

**Interfaces:** preserves `_remediation_for_stderr(stderr) -> tuple[str, str] | None` and
`run_guestfs_tool(...)->str`; callers retain their current error behavior.

**Verification:** Mode: focused-test — run the two node IDs in Task 2; expect exit 0 and unchanged
category assertions.

1. Make the shared hint lead with the supported local and deployment-owned provisioning routes.
2. State `root:kvm 0640`, required worker membership, durable hook, and only the safe temporary
   both-architecture `sudo` fallback.
3. Make the preflight say the local recipe installs the hook and only the fallback needs reapply.
4. Run `just format`, `just test-changed`, `just lint`, `just type`, and `just test`; expect exit 0.
