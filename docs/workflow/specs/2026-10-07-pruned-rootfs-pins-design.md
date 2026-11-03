# Refresh three pruned rootfs pins

Issue #3064; frozen scope q3064-061ed59e. Complexity M; full-spec denominator 250.
Existing decision: [ADR-0251](../../adr/0251-local-multidistro-rootfs-catalog.md).

## Problem

The x86_64 CentOS Stream 9/10 and openSUSE Tumbleweed source pins no longer download.
On 2026-10-07 their existing URLs returned HTTP 502, 502 and 404 respectively.
Their vendor directories advertise newer images. The catalog owns these inputs (ADR-0251);
checksum verification already fails closed in the existing acquisition path.

## Scope and design

Refresh only `centos-stream-kdive-ready-9`, `centos-stream-kdive-ready-10`, and
`opensuse-tumbleweed-kdive-ready`. Keep their names, architectures, families and kinds.
Use vendor versioned URLs and independently match downloaded bytes to vendor SHA-256 metadata.
Candidates observed on 2026-10-07 are CentOS GenericCloud 20261006.0 for both releases and
Tumbleweed Minimal VM Cloud Snapshot20261005. If a candidate disappears before acquisition,
repeat vendor discovery and byte verification rather than substituting a floating URL.
Update Tumbleweed's version to its verified snapshot; keep CentOS release versions 9 and 10.

Revalidate makedumpfile and drgn annotations from the actual customized guest's package
inventory and executable version markers. Update the existing curated test expectations only
where verified versions changed. Package availability/build errors remain blocking findings.

Add a bounded manual refresh procedure to the existing image-lifecycle runbook: identify the
versioned vendor image, obtain its checksum, verify bytes, update only the corresponding row,
build and smoke it, then record candidate/image identities and actual package versions.
Explain that versioned rolling-vendor paths can disappear and require this procedure again.
No refresh service, downloader change, mirror infrastructure, or catalog schema is introduced.

## Success

For the three named rows: source bytes match the new pin; documented `build-fs` completes;
package annotations match observed installed versions; the built image reaches ready in a
real x86_64/KVM boot. Run the existing image-smoke carrier when its candidate-matched runtime
is available and report its acquire/access/identity/reboot/cleanup outcomes separately.
Missing runtime prerequisites or failed required proofs stay explicit and block completion;
a direct boot does not stand in for the release image-smoke cell.
The runbook makes refreshing pruned sources repeatable without weakening checksum checks.

## Failure model

1. Actors/deployment: a maintainer refreshing these three x86_64 rows on a disposable KVM
   workspace; HTTPS vendor origins and guest package repositories are external dependencies.
2. Invariants/assets: checksum-bound inputs, truthful package annotations and proof identity;
   preserve existing images/VMs, native POWER obligations and unrelated catalog rows.
3. Accepted classes: future vendor pruning after this verified refresh remains possible;
   the documented refresh procedure addresses recurrence, not perpetual archival availability.
   No present download/build/boot failure is accepted as success.
4. Covered elsewhere: POWER qualification #2818; unrelated catalog expansion outside scope.
   Existing runtime/authority prerequisites retain their owners and are not implemented here.

## Validation

The current old-URL probes are the red regression. Green requires downloading each selected
image and comparing SHA-256 with its exact vendor checksum filename, then the existing real
builder's checksum validation. No unit test may substitute a mock response for that proof.
Run `tests/images/test_rootfs_catalog.py`, relevant image-smoke binding tests, `just lint`,
whole-tree `just type`, and doc checks. The mandatory pre-push hook owns full `just ci`.
Capture candidate commit, source/output digests, KVM identity, guest OS/kernel/package facts,
exit codes, durations and owned cleanup; preserve failures and never count skipped cells.
