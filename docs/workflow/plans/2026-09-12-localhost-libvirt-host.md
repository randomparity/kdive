# Localhost libvirt host preparation (#2393)

**Goal.** Provide the supported declarative route for preparing an operator's
own local-libvirt host.

**Architecture.** A localhost playbook composes existing virtualization,
network, and reusable worker roles, then owns only workstation-specific setup.
It passes the lifecycle DSN on stdin and makes guestfs ABI degradation explicit.

**Tech stack.** Ansible Core 2.21.1 through uv; Python harness.

**Spec:** `docs/workflow/specs/2026-09-12-localhost-libvirt-host-design.md`
**Decision:** `docs/adr/0644-localhost-libvirt-host-preparation.md`

Expected implementation size: 310–390 changed lines (L) — one playbook with
explicit lifecycle, traversal, and guestfs guards; reusable-family routing coverage; one
structural harness; and justfile wiring.

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
| `deploy/ansible/roles/local_worker_host/tasks/preflight.yml` | modify | RHEL/SLES admission |
| `deploy/ansible/tests/run-local-worker-host.py` | modify | RHEL/SLES route coverage |
| `deploy/systemd/install-live-worker-lifecycle.sh` | modify | SLES modular daemon selection |
| `tests/deploy/test_live_worker_provisioning.py` | modify | SLES lifecycle selection coverage |

## Task 1 — localhost playbook

**Interfaces.** Inputs are `local_libvirt_host_source`,
`local_libvirt_host_operator_user`, `local_libvirt_host_witness_dsn`, and
`local_libvirt_host_kernel_source`; the roles consume the operator value.

**Verification.** Mode: focused-test. The new harness must fail before the
playbook exists and pass with `uv run --with 'ansible-core==2.21.1' python3
deploy/ansible/tests/run-local-libvirt-host.py`.

Extend `local_worker_host` preflight to accept RHEL-compatible distributions and SLES,
reusing the existing RedHat and Suse package task routes. Extend its harness to prove the
new facts select only that route without a live package operation. Make the lifecycle installer
select SLES's modular `virtqemud` tuple, and add its focused regression test.

Create the playbook with localhost/local connection, role order
`libvirt_stack`, `libvirt_pool_net`, `local_worker_host`, a no-log command task
for `install-live-worker-lifecycle.sh --operator <operator> --source <source>`
with `stdin`, an operator-owned locked `uv sync --group live` task before venv access
whose change receipt comes from a locked dry-run, pre-role Git checkout/source sentinel and
getent validation, and operator-home kernel-source derivation,
a `2770` operator-owned `/var/lib/kdive/rootfs/local` task, an existing-directory
ancestor walk from each source root through `/` that adds only `o+x`, and guestfs
mismatch/report/link/import tasks. The matching-minor path asserts a nonempty
`guestfs.py` plus `libguestfsmod*.so` discovery and imports `guestfs` from the venv.

## Task 2 — regression wiring

**Verification.** Mode: focused-test. The harness parses YAML and fails if the
target, connection, roles, pre-role checkout sentinel, stdin/no-log contract, locked uv sync
ordering/change receipt, rootfs ownership, ancestor-walk
shape, guestfs mismatch branch, nonempty discovery, or post-link import
changes. Green command: `just test-ansible`.

Add the recipe `prepare-local-libvirt-host` beside local-libvirt preflight,
add the playbook to `lint-ansible` syntax checks, and add the harness to
`test-ansible`.

## Task 3 — live proof

**Verification.** Mode: task-test-not-applicable. Host privilege, package
repositories, and the witness database are operator-owned external state; a
structural test cannot establish an apply result.

On the configured Ubuntu 26.04 operator host, run the recipe twice and retain
the changed count from each run; the second count must be zero. Record the
lifecycle-managed unit/file state after each run and require it to match. Report that RHEL, Fedora, SLES, and openSUSE
were not live-proven when no corresponding host is reachable.
