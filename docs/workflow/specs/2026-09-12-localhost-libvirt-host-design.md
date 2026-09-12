# Localhost libvirt host preparation

## Scope and authority

Issue #2393 is the authority. Its frozen scope token is `q2393-1a2cbc02`.
The permitted surface is the localhost Ansible composition, its harness, and
the justfile. The example installer is evidence only and is not invoked.

## Design

`local-libvirt-host.yml` targets `localhost` with `connection: local` and
`become: true`. Required variables name the checkout, operator, and witness DSN.
It composes the three existing roles, then uses Ansible modules and one root
installer command to converge the remaining local route. The command receives
the DSN through `stdin`; it is marked no-log. A task walks existing source and
kernel ancestors and grants only traversal permission. A final binding task
links system guestfs modules into the project venv only when Python minors match;
otherwise it reports the unavailable capture path and succeeds.

## Failure model

Missing required inputs fail before host mutation. Lifecycle installation fails
when its stdin DSN is absent or invalid. A guestfs ABI mismatch reports the
system and venv minors and leaves the binding untouched. The harness renders
the playbook and checks its structural contracts without mutating a host.

## Validation

The harness checks localhost targeting, role composition, stdin/no-log use,
and guestfs mismatch behavior. `just lint-ansible` includes syntax checking;
`just test-ansible` runs the harness. The Ubuntu 26.04 host must apply the
playbook twice before merge; other listed families remain structurally checked
until a live host is available.
