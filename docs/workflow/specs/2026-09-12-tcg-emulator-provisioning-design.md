# TCG emulator provisioning

## Scope and authority

Issue #2402 and campaign approval dated 2026-09-12 authorize the role defaults/tasks, its family
harness, and platform-support documentation. Live proof, dependency-checker unification, and
provider/runtime changes remain excluded. [ADR-0646](../../adr/0646-libvirt-stack-foreign-guest-emulators.md)
records the package-selection contract.

## Problem

The role provisions only the host-native QEMU emulator, although the advertised TCG tier runs
ppc64le guests. A provisioned x86_64 host consequently lacks its foreign emulator.

## Design

The role derives its package list from native architecture plus a family-specific foreign guest
list. Debian and SUSE add ppc64le; RedHat adds none because EL has no such package. The harness
evaluates the rendered lists under both architectures. Platform support states the consequence.

## Validation

`deploy/ansible/tests/run-libvirt-stack-families.sh` proves the six fact-driven package routes.
`just lint-ansible` verifies role syntax and style. No live proof is in scope.
