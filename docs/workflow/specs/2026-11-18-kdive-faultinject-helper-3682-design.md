# The kdive-faultinject guest helper

## Problem

#918 needs an in-guest way to read and set the `failslab` and `fail_page_alloc` debugfs knobs.
No reviewed helper touches debugfs, and the guest exec allowlist (ADR-0079, ADR-0085) names only
`kdive-drgn`. [ADR-0822](../../adr/0822-guest-exec-allowlist-adds-kdive-faultinject.md) amends it;
this spec is the helper and its staging. Charter: WORK:SCOPE q3682-33e072a6 on #3682.

## Scope

In: `deploy/remote-libvirt-guest-helpers/kdive-faultinject` (bash) with the ADR's subcommands, knob
set, bounds, JSON output and exit codes 0/2/3/4/5; a staging step `faultinject_helper_steps()`
beside `drgn_helper_steps()` in `src/kdive/images/families/_fedora_customize.py`, called wherever
`drgn_helper_steps()` is called (Debian debug images, Fedora/RHEL via the shared customizer, SUSE
when `drgn` is in the packages); the helper name in the `guest_base_image` helper list in
`deploy/ansible/inventory/group_vars/all.yml`; the README entry. No ownership transition: the
Ansible role already loops over the list, and the allowlist is a convention, not a code set.

Out (owners): MCP tools (#3683, #3684); other knobs, providers, free-form writes (unowned); live
image rebuild or guest proof (#3683).

The helper reads `FAULTINJECT_DEBUGFS_ROOT` (default `/sys/kernel/debug`) so tests can use a fake
tree. A caller must not forward it; #3683 owns that.

## Failure model

- Actors and deployments: a kdive worker running the helper as root in a local-libvirt debug
  guest over SSH with fixed argv; a contributor running the unit tests on a Linux host with bash 4 or later.
- Invariants: no caller text reaches a path or a write beyond the closed knob and parameter names
  and a bounded integer; a rejected `set` writes nothing; stdout is valid JSON or empty.
- Accepted: a mid-sequence write error leaves earlier parameters written (exit 5 reports it; the
  knobs are ephemeral guest state); `reset` restores kernel defaults, not prior values; a kernel
  whose knob directory lacks a parameter file reports exit 5 (an absent directory is exit 4); fault injection can break the guest's own
  tasks, which is the feature and is scoped by the caller.
- Covered elsewhere: who may invoke the helper (SSH bootstrap key, ADR-0289); mapping exits to
  tool errors and any guest proof (#3683).

## Success

1. `read`, `set` and `reset` behave per the ADR on a fake tree for both knobs, including the
   per-knob parameter sets and every bound at its min and max.
2. A knob, parameter or value outside the allowlist exits 2 and writes nothing: path separators,
   `..`, whitespace, shell metacharacters, `+5`, `-0`, leading zeros, over ten digits, a repeated
   parameter, `set <knob>` with no pair, in every `set` argument position. The value pattern is
   `^(-1|0|[1-9][0-9]{0,9})$` and then the range. `set` writes `probability` last; `reset` first.
3. Missing or empty debugfs exits 3; a missing knob directory exits 4 for a named knob and reports
   `present:false` for `read` with no knob.
4. A Debian debug image, a Fedora/RHEL debug image and a SUSE image with `drgn` stage the helper
   `0755` at `/usr/local/sbin/kdive-faultinject`; a missing source fails the build loudly.
5. The Ansible `guest_base_image` helper args copy it with the same chown and chmod as `kdive-drgn`.
6. The README lists it and `just ci` is green.

## Validation

- Helper contract (criteria 1-3): focused-test `tests/scripts/test_kdive_faultinject_helper.py`,
  red because the helper is absent; green `just test-verbose tests/scripts/test_kdive_faultinject_helper.py`.
- Image staging (criterion 4): focused-test additions in `tests/images/families/test_debian.py`,
  `test_suse.py`, `test_fedora_customize.py` (and the rhel family path), red because no step is
  emitted; green `just test-verbose tests/images/families`.
- Ansible list (criterion 5): focused-test `tests/deploy/test_guest_base_image_helpers.py`
  parametrization over the new helper; green `just test-verbose tests/deploy/test_guest_base_image_helpers.py`.
- Lint (criterion 1, shell): focused `just lint-shell` covers the new file.
- README (criterion 6) and the ADR: task-test-not-applicable, prose with no executable consumer;
  `just records` and the doc checks in `just ci` gate their shape.
