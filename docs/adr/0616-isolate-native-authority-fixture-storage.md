# 0616 — Isolate native authority fixture storage

## Status

Accepted (2026-09-06)

## Context

The native authority proof needs an actual guest and its root disk on the authority's private
libvirt daemon. Changing a disk's owner does not protect it when a worker can replace its directory
entry. Sharing the worker's writable rootfs or console parent therefore defeats the authority
boundary even if the files themselves belong to the authority account.

## Decision

Provision the explicitly selected disposable native fixture on the authority's private daemon.
Its rootfs, baseline, console, recovery records, and journal reside under authority-owned private
directories. Workers receive neither directory write permission nor access to the private provider
socket. The authority service's writable-path list names only its required private roots.

The fixed process settings `KDIVE_LIBVIRT_ROOTFS_ROOT` and `KDIVE_LIBVIRT_CONSOLE_ROOT` select
absolute runtime roots before provider imports. Their defaults retain ordinary worker behavior.
The rootfs upload area and catalog cache keep their existing independent locations; changing the
authority runtime root does not silently relocate either shared surface. Provisioning must arrange
the fixture's input image before starting the installed authority proof.

The fixture command takes one exact System UUID and an explicit provisioning profile. Before
changing anything it must refuse an already present authority domain, including an inactive one.
It may replace only the workflow-owned disposable worker fixture and its exact regular artifacts.
It must validate directory types before removing the selected baseline. A failed or interrupted
fixture setup requires inspection of that exact fixture, not automatic broad cleanup.

This is not a migration of ordinary worker-owned Systems. Existing deployments and unrelated
provider objects remain unchanged. An authority operation against a worker-owned System fails
closed; no compatibility fallback grants workers access to protected state.

## Consequences

The installed proof can test the authority boundary against real storage and a real private
daemon. Acceptance requires attempted worker write, replacement, and unlink operations to fail,
as well as successful authority lifecycle operations. Template assertions alone do not prove the
permissions or installed path work. The installed source revision must match the tested commit.

## Considered & rejected

- **Grant the authority access to worker-writable parents.** Workers could still replace protected
  files and bypass the authority's ownership checks.
- **Transfer every existing System into authority ownership.** The native proof does not authorize
  or require a general deployment migration.
- **Relocate upload and catalog caches with the private runtime root.** These have independent
  consumers and access contracts; implicit relocation breaks unrelated lifecycle operations.
