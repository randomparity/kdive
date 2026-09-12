# Localhost libvirt host preparation

## Scope and authority

Issue #2393 is the authority. Its frozen scope token is `q2393-1a2cbc02`.
The operator authorized expansion to RHEL and SLES in this session after branch review
found the existing reusable-role preflight rejected both.
The permitted surface is the localhost Ansible composition, its harness, and
the justfile. The example installer is evidence only and is not invoked.

## Design

`local_worker_host` admits RHEL-compatible distributions through its RedHat
package route and SLES through its Suse route. The lifecycle installer selects
the same modular `virtqemud` tuple for SLES as it does for RHEL-compatible hosts.
The regression harness proves those selections structurally; no RHEL/SLES package
apply is claimed.

`local-libvirt-host.yml` targets `localhost` with `connection: local` and
`become: true`. Required variables name the checkout, operator, and witness DSN.
It verifies the checkout manifest, Git worktree and revision, lifecycle installer and manifest
builder, and fixture catalog before any role mutates the host. It composes the three existing
roles, then uses a `uv sync
--locked --group live --dry-run` receipt to report whether the operator's checked-out
project venv changed during `uv sync --locked --group live`. It then uses Ansible modules and one root
installer command to converge the remaining local route. The command receives
the DSN through `stdin`; it is marked no-log. A task walks each existing source
and kernel ancestor up to `/`, granting only traversal permission. A final
binding task links a nonempty discovered system guestfs module set into the
project venv only when Python minors match, then imports `guestfs` from that
venv; otherwise it reports the unavailable capture path and succeeds.

## Failure model

An incomplete, non-Git, or symlinked source checkout, missing operator, or missing DSN input fails before
role mutation. Lifecycle installation fails
when its stdin DSN is absent or invalid. A matching-minor binding discovery or
post-link import failure is fatal; an ABI mismatch reports the system and venv
minors and leaves the binding untouched. The harness renders
the playbook and checks its structural contracts without mutating a host. The
live proof accepts the second apply only when Ansible reports zero changes and
the lifecycle installer's managed units and files match their first-run state.

## Validation

The harness checks localhost targeting, role composition, stdin/no-log use,
and guestfs mismatch behavior. `just lint-ansible` includes syntax checking;
`just test-ansible` runs the harness. The Ubuntu 26.04 host must apply the
playbook twice before merge; other listed families remain structurally checked
until a live host is available.
