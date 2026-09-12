# Reusable local worker host preparation

## Scope and authority

Issue #2391 is a child of #2388. The operator approved a separate role and
openSUSE Tumbleweed as the SUSE target on 2026-09-12. The frozen issue scope is
`q2391-2beef9a7`. [ADR-0642](../../adr/0642-reusable-local-worker-host-preparation.md)
records the role boundary. #2390, #2392, #2393, and #2394 own package-map
reconciliation, SUSE libvirt-stack, the localhost install path, and install
documentation respectively.

## Problem

`live_vm_host` asserts the Ubuntu runner base before worker-host preparation.
The worker package, fixed-account, and data-directory tasks therefore cannot be
used by a Fedora or openSUSE Tumbleweed play without also adopting the runner
service account, Docker, checkout, and Python/guestfs system-ABI contract.

## Components and flow

`local_worker_host` owns explicit OS-family package lists and the existing
worker-host package installation, fixed groups/accounts, protected worker
directories, slot/recovery roots, and shared provider directories. Its public
entry point is `tasks/main.yml`. It requires
`local_worker_host_operator_user`, the name of an existing account that owns
shared provider data; it creates no runner account. A preflight rejects an
empty/nonexistent operator or a distribution outside Debian, Ubuntu, Fedora,
and openSUSE Tumbleweed before any mutation. Broader RedHat and Suse support
needs package verification in a later issue.

`live_vm_host` keeps its authority source preflight, Ubuntu assertion, runner
account, Docker, checkout/venv, guestfs ABI binding, fixed live-test fixture
catalog reconciliation, service units, and host verification. Static
`import_role` tasks invoke the reusable task files at the
old positions with `local_worker_host_operator_user: "{{ github_runner_user }}"`.
The old worker variables remain named `live_vm_host_*` to preserve inventory
overrides; their defaults move to the reusable role, as do the authority opt-in
and client-group defaults needed by the fixed-account tasks. Ansible 2.21's
`import_role` exposes those defaults at parse time. The `runner.yml` roles and
the effective ordered task names remain unchanged after normalizing the role
prefix introduced by extraction.

The shell harness checks the real `runner.yml --list-tasks` sequence against
the recorded baseline and probes family selection and validation without
pretending local Fedora can run Tumbleweed's package manager. A Fedora
package-only check-mode run exercises dnf; Tumbleweed has syntax and structural
coverage until a SUSE host is available. It also checks no Docker or runner
account, the early Ubuntu assertion, and tagged recovery tasks. Paired
`--check --diff` traces use the runner inventory and skip only the existing
check-mode-incompatible recovery capacity step. A trace must reach the last
moved task and preserve its ordered outcomes to count as parity evidence;
otherwise it is inconclusive. A full unmodified check-mode run and the live
Ubuntu apply are reported separately.

## Success

- The standalone reusable role exposes the named package, account, and
  directory preparation to Fedora and openSUSE Tumbleweed without ADR-0387's
  Ubuntu assertion or runner/Docker/fixture-pruning tasks.
- On Ubuntu 26.04, `runner.yml` preserves the existing package list, account
  ownership, unit set, and ordered task names. Its role-local assertion
  precedes `live_vm_host` mutation and names ADR-0387, Python 3.14, and
  `python3-guestfs`.
- The role harness, `just lint-ansible`, syntax check, and `just test-ansible`
  pass. The Ubuntu host's live apply outcome is reported separately.

## Failure model

- **Actors and deployments:** operator applying `local_worker_host` on Fedora
  or openSUSE Tumbleweed; automation applying `runner.yml` on Ubuntu 26.04.
- **Invariants and assets:** root-owned slots and protected paths retain modes;
  fixed workers remain isolated; the runner's package/task order and units stay
  stable; unsupported standalone distributions fail before mutation.
- **Accepted failure classes:** full runner check mode may stop at an existing
  task that reads a path check mode only reports it would create; this is
  reported as a failed check, not parity proof. Package repository unavailability
  fails through the package manager with its package name; a host with missing
  configured repositories is outside the named deployment contract.
- **Covered elsewhere:** SUSE libvirt daemon setup is #2392; the canonical
  host install play is #2393; non-Ubuntu live proof is outside the operator's
  approved host scope in parent #2388.

## Threat model

- **Added boundary:** an operator-controlled account name and OS facts reach
  account/group, package, and path actions; standalone preflight validates the
  account and named distribution before mutation. Ansible modules pass values
  as data.
- **Existing boundary widened:** a new standalone role can create fixed worker
  identities and protected paths without the runner assertion; the play's
  privileged operator is trusted and the same file modes and no-follow checks
  govern the paths.
- **Out of scope:** untrusted tenant input does not call Ansible; package
  repository trust follows the operator's configured distribution repositories.

## Validation

The regression harness compares the normalized runner task sequence and exact
Ubuntu package list, and exercises family selection plus the unsupported and
missing-account paths. Existing recovery-root tests cover symlink refusal and
slot ownership. `--syntax-check` and `ansible-lint` cover role parse and style;
the Ubuntu check-mode trace and live apply cover real inventory behavior.
