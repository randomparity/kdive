# ADR 0489 — The in-guest install helper's exit code names transience, and unknown codes stay permanent

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** kdive maintainers
- **Issue:** #1653 (follow-up to #1631)
- **Amends:** [ADR-0082](0082-remote-install-in-guest-kernel.md) §1 (the helper contract's "exit
  non-zero on any failure") and §2.3 (the worker's "map a non-zero helper exit to
  `INSTALL_FAILURE`"). Everything else in ADR-0082 — the single allowlisted helper, the fixed
  argv, the single deterministic grub slot, boot-id readiness — stands unchanged.
- **Builds on:** [ADR-0483](0483-non-retryable-category-dead-letters-a-job.md), which made the
  failure category decide whether the job queue retries an attempt at all, and disclosed this
  conflation as a named residual cost; [ADR-0118](0118-wait-on-resource-mechanisms.md), the
  category → `retryable` table both retry seams read.

## Context

`deploy/remote-libvirt-guest-helpers/kdive-install-kernel` had exactly one failure path — `die()`,
hardcoded `exit 1` — and all twelve of its call sites collapsed onto that code.
`RemoteLibvirtInstall.install` therefore mapped **any** non-zero helper exit to `INSTALL_FAILURE`.

That was harmless while the queue retried every category regardless. ADR-0483 made
`RETRYABLE_BY_CATEGORY` (`src/kdive/domain/errors.py`) load-bearing at the queue: a non-retryable
category dead-letters the job on its **first** attempt. `INSTALL_FAILURE` is non-retryable and
`INFRASTRUCTURE_FAILURE` is retryable, so that single exit code now decides whether a Run gets a
second chance.

The helper's failures are not one kind. Its `curl` of the ADR-0081 bundle from a presigned GET
fails when the object store blips, when the guest network has not settled, or when the presigned
URL — minted with a bounded expiry — has already expired. Every one of those is healed by attempt
2, which mints a *fresh* URL and re-runs; curl's own `--retry 3` covers seconds, not a MinIO
restart or an expiry that has already passed. Its `dracut` and `grubby` failures are the opposite:
deterministic, and dead-lettering them on attempt 1 is exactly right. Before this ADR the two were
indistinguishable, so the transient one lost its self-heal.

The constraint that shapes the fix is that **the helper and the worker are separately versioned
artifacts**. The helper is baked into an operator-built guest base image
(`deploy/ansible/roles/guest_base_image`); a worker taught new codes will still meet images built
before this contract, which exit `1` for everything.

## Decision

**1. The helper's exit code names transience, additively.**

`kdive-install-kernel` exits `75` (sysexits' `EX_TEMPFAIL`) from a new `die_tempfail` for the one
condition it can honestly call transient — the bundle did not arrive from the presigned URL — and
keeps `1` for every failure it decides deterministically. The contract is documented in the
helper's own header and in `deploy/remote-libvirt-guest-helpers/README.md`, alongside the
already-per-condition exit contracts of `kdive-capture-vmcore` and `kdive-drgn`.

| exit | condition | worker category | retryable |
|---|---|---|---|
| `0` | success | — | — |
| `75` | bundle fetch failed (`curl`) — store down, network unsettled, URL expired | `infrastructure_failure` | yes |
| `1` | bad argv; bundle contents wrong; `tar`, `depmod`, `dracut`, `grubby`, `grub2-reboot` | `install_failure` | no |
| other | a `set -e` propagation from an unguarded command | `install_failure` | no |

**`tar` extract stays deterministic**, not tempfail. `curl -f` already fails a short transfer, so
bytes that arrived whole and still will not extract are a bad object, and re-downloading yields
the same bytes. This follows the taxonomy's stated bias (`errors.py`: non-retryable when
transience is ambiguous).

**2. An unrecognised code is permanent.**

`_category_for_helper_exit` maps a known code to its category and **every other non-zero code to
`INSTALL_FAILURE`** — precisely the pre-#1653 behaviour. An old base image degrades to what it
already did rather than being misclassified, a `set -e` propagation from an unguarded command is
not mistaken for a named code, and a code from a *newer* helper this worker has never heard of is
never assumed retryable. The fallback direction is the conservative one in both time directions.

**3. Both sites that read a helper exit share the one mapping.**

`install()` and `_read_boot_id()` both call `_category_for_helper_exit`, so the two cannot drift
into disagreeing about what a code means.

## Consequences

- **A transient install self-heals again.** An object-store blip or an expired presigned GET is
  `infrastructure_failure`, so the queue re-dispatches, `install()` mints a fresh URL, and the Run
  completes instead of dead-lettering on attempt 1.
- **A vanished `kernel_ref` now costs the full retry budget.** The worker only mints the URL and
  never fetches, so an object that is genuinely gone surfaces as the same in-guest `curl` failure
  as an expired URL (S3 answers both with a 4xx) and is retried before dead-lettering. Accepted:
  it is a bounded number of attempts, paid to heal the far commoner case. The helper does not
  inspect the HTTP status to split them (see Alternatives).
- **A guest base image must be rebuilt to gain the new behaviour.** Until an image carries the new
  helper its installs are classified exactly as before — no regression, no benefit. The
  obligation is documented where images are built (`guest_base_image/tasks/build_one.yml`) and
  where the helper contract lives (the helpers README).
- **The helper's exit code is now a versioned contract**, not an implementation detail. Adding a
  code is safe (old workers fall back); *reusing* an existing code for a different condition is
  not, and would need its own ADR.
- **`boot-id`'s failure classification changes shape but not behaviour** — the `boot-id`
  subcommand has no tempfail path, so it keeps producing `install_failure`; it shares the mapping
  so a later addition cannot leave it behind.

## Alternatives considered

- **Have the worker classify by scraping the helper's transcript** (match `bundle download failed`
  in the captured stderr). Rejected: it makes a human-readable message load-bearing, it is exactly
  the fragile string-matching ADR-0483 accepted only as a one-way *upgrade* for a condition with no
  code available, and here a code is available. An exit code is the helper's own structured
  statement about its failure.
- **Make the helper distinguish HTTP 404 (vanished object, permanent) from 403/5xx (expired or
  transient)** with `curl -w '%{http_code}'`. Rejected for now: an expired presigned GET and a
  denied one both answer 403, so the split only separates 404 — a condition that barely occurs,
  since the build that produced `kernel_ref` just wrote it — at the cost of parsing curl output in
  the guest and a third code to keep in sync. The retry budget already bounds the cost.
- **Add a `configuration_error` code for helper usage errors** (bad argv = a worker/helper
  contract mismatch). Rejected: `configuration_error` and `install_failure` are both non-retryable,
  so the code would change only the label on a failure that cannot happen without a worker bug —
  new surface for no behaviour.
- **Widen the retryable set instead, making `install_failure` retryable.** Rejected: it re-runs
  dracut and grubby failures that cannot succeed, undoing ADR-0483's point. The category is right;
  what was missing was the helper's ability to say which failure it had.
- **Version the helper explicitly** (a `kdive-install-kernel --contract-version` the worker probes
  before each install) rather than relying on the unknown-code fallback. Rejected: an extra
  guest-agent round-trip on every install to learn something the fallback already handles safely,
  and older images would not answer the probe anyway.
