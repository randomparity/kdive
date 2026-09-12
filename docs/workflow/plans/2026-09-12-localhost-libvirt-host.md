# Localhost libvirt host preparation (#2393)

**Goal.** Provide the supported declarative route for preparing an operator's
own local-libvirt host.

**Architecture.** A localhost playbook composes existing virtualization,
network, and reusable worker roles, then owns only workstation-specific setup.
It passes the lifecycle DSN on stdin and makes guestfs ABI degradation explicit.

**Tech stack.** Ansible Core 2.21.1 through uv; Python harness.

**Spec:** `docs/workflow/specs/2026-09-12-localhost-libvirt-host-design.md`
**Decision:** `docs/adr/0644-localhost-libvirt-host-preparation.md`

Expected implementation size: 180–260 changed lines (L) — one playbook, one
structural harness, and justfile wiring.

## Global Constraints

- Python 3.14, managed with `uv`; Ansible remains uv-supplied.
- Use `hosts: localhost`, `connection: local`, and idempotent Ansible modules.
- The DSN never appears in an argument, task output, or repository file.
- Guardrails: `just lint-ansible`, `just test-ansible`, and the relevant syntax check.

## File map

| File | Action | Responsibility |
|---|---|---|
| `deploy/ansible/playbooks/local-libvirt-host.yml` | create | localhost composition and remaining setup |
| `deploy/ansible/tests/run-local-libvirt-host.py` | create | structural regression harness |
| `justfile` | modify | operator recipe, syntax check, and harness wiring |

## Task 1 — localhost playbook

**Interfaces.** Inputs are `local_libvirt_host_source`,
`local_libvirt_host_operator_user`, `local_libvirt_host_witness_dsn`, and
`local_libvirt_host_kernel_source`; the roles consume the operator value.

**Verification.** Mode: focused-test. The new harness must fail before the
playbook exists and pass with `uv run --with 'ansible-core==2.21.1' python3
deploy/ansible/tests/run-local-libvirt-host.py`.

Create the playbook with localhost/local connection, role order
`libvirt_stack`, `libvirt_pool_net`, `local_worker_host`, a no-log command task
for `install-live-worker-lifecycle.sh --operator <operator> --source <source>`
with `stdin`, a `2770` operator-owned `/var/lib/kdive/rootfs/local` task,
ancestor traversal tasks, and guestfs mismatch/report/link tasks.

## Task 2 — regression wiring

**Verification.** Mode: focused-test. The harness parses YAML and fails if the
target, connection, roles, stdin/no-log contract, or guestfs mismatch branch
changes. Green command: `just test-ansible`.

Add the recipe `prepare-local-libvirt-host` beside local-libvirt preflight,
add the playbook to `lint-ansible` syntax checks, and add the harness to
`test-ansible`.

## Task 3 — live proof

**Verification.** Mode: task-test-not-applicable. Host privilege, package
repositories, and the witness database are operator-owned external state; a
structural test cannot establish an apply result.

On the configured Ubuntu 26.04 operator host, run the recipe twice and retain
the changed count from each run. Report that RHEL, Fedora, SLES, and openSUSE
were not live-proven when no corresponding host is reachable.
