# Localhost libvirt host preparation

## Scope and authority

Issue #2393 is the authority. Its frozen scope token is `q2393-1a2cbc02`.
The permitted surface is the localhost Ansible composition, its harness, and
the justfile. The example installer is evidence only and is not invoked.

## Design

`local-libvirt-host.yml` targets `localhost` with `connection: local` and
`become: true`. Required variables name the checkout, operator, and witness DSN.
It composes the three existing roles, syncs the checked-out project with `uv
sync --group live` as the operator, then uses Ansible modules and one root
installer command to converge the remaining local route. The command receives
the DSN through `stdin`; it is marked no-log. A task walks each existing source
and kernel ancestor up to `/`, granting only traversal permission. A final
binding task links a nonempty discovered system guestfs module set into the
project venv only when Python minors match, then imports `guestfs` from that
venv; otherwise it reports the unavailable capture path and succeeds.

## Failure model

Missing required inputs or `uv` fail before lifecycle installation. Lifecycle installation fails
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
