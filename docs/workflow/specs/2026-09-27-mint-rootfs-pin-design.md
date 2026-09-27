# Digest-pinned live VM mint fixture

## Scope and authority

Issue #2851 asks that the ordinary minted System bind verified catalog provenance when its
warm rootfs matches, while retaining the ordinary fixture. Campaign approval on 2026-09-27
covers an empty exclusion set. Scope token: q2851-b18a34e9.

## Problem and design

`mint-system.sh` stages a warm qcow2, then sends an unpinned local component reference.
`resolve_root_provenance` deliberately cannot bind that reference (ADR-0583).
Hash the staged file with stdlib `hashlib.file_digest`, using `sha256:<hex>`, before any
MCP call. Add that digest to the existing local reference. System admission remains the
single owner of registered-state, project visibility, inspection, architecture and ambiguity
validation. No new CLI option, catalog client, database access, or runtime seam is needed.

The provider still validates the pin when consuming the image. Catalog absence keeps the
ordinary fixture usable but does not qualify it for installed authority proofs. Operators
must use matching registered verified bytes and confirm the persisted provenance row before
running that carrier. Read/hash errors stop before allocation rather than creating a partial
fixture. The existing staging and polling contracts stay unchanged.

## Success

- The emitted pin describes staged bytes, including the copy fallback, before allocation.
- The fixture still reaches ready without a matching catalog authority.
- A registered matching local image produces a System-project provenance snapshot; a private
  image belonging only to another project does not qualify, even for a broader caller.
- Runbook instructions explain automatic pinning and the separate carrier qualification.

## Failure model

Supported deployments are the existing single-operator native x86_64 and ppc64le mint path.
Unreadable staged files are fatal before MCP calls. Catalog absence is accepted for ordinary
fixtures. Existing admission rejects ambiguous or invalid matching authorities; this change
activates those existing checks for previously unpinned fixtures. Concurrent writes to a staged
base remain invalid operator behavior: the provider's checksum verification detects changed
bytes; the helper adds neither shared-path locking nor a new immutability contract.

## Verification and rollback

Execute the real script with onboarding and MCP transport stubbed at external boundaries;
assert the provisioned profile pin equals actual staged bytes and stdout remains one System id.
Force a copy fallback and distinct staged content to prove the source is not hashed instead.
Extend real-Postgres provenance coverage to local-libvirt and missing/foreign/private/public
catalog cases. Run the registered-image mint and installed carrier only when their documented
host and revision contract is available, recording any absent prerequisite explicitly.
Revert the fixture pin and its tests to roll back; no schema or persistent format changes.
