# 0715 — Live evidence identity and staged-image binding

## Status

Accepted (2026-10-01)

## Context

ADR-0686 qualification requires two things of every record:

- its deployed `server`, `worker`, `reconciler` and `authority` revisions equal the candidate;
- a catalog smoke cell's `image_sha256` equals an independent binding.

#2808 adds the first producer, and #2809-#2811 will reuse it. Each app role reports its build on
the aux `/readyz` (ADR-0482). The installed provider authority writes its revision to
`/opt/kdive-provider-authority/revision`. A catalog row builds into a customized qcow2 whose bytes
change on every rebuild (ADR-0688). The `fedora-kdive-ready-43` row pins no source digest.

## Decision

1. The candidate is the full `HEAD` of the test checkout. A dirty checkout is an identity
   problem.
2. Deployed revisions are read again for every cell.
   - `server` and `reconciler` come from their default aux `/readyz` ports.
   - `worker` comes from the `/readyz` of every running `kdive-live-worker@N` slot.
   - `authority` comes from the installed revision file, read through `sudo -n`.
   - Each revision is resolved to a full SHA against the checkout. A role that cannot be read
     or resolved is omitted. When worker slots disagree, the first slot that is not at the
     candidate is recorded.
3. A smoke cell binds the SHA-256 that the operator computes over the staged qcow2 registered for
   the catalog name. The operator computes it before the run, after staging with
   `build-image.sh`. The record carries the digest that `images.describe` reports. For a
   staged-path image, that is the digest build-fs recorded in its sidecar. Equal values mean the
   stack registered the bytes that were bound. Nothing hashes the disk at provision time.

## Consequences

- A rebuilt image needs new bindings and new evidence, as ADR-0688 expects.
- The default `demo-up.sh` lane installs no authority. The contract requires that role for
  every native cell, so cells on that lane fail `deployed-role-missing` until #3066 decides
  between a contract change and a lane change.
- `/readyz` reports a commit, not tree state. Restarting the stack from a dirty tree goes
  undetected; only the test checkout's own state is checked.

## Considered & rejected

- **Bind the catalog source checksum and build inside the test.** judgment: a full build-fs on
  every run binds the upstream base rather than the customized image. The virt-builder row has
  no checksum to bind at all.
- **Use the skew probe's single worker port.** verified: `readyz_urls()` in
  `tests/integration/live_stack/skew.py` at `b6dfb269c` probes only port 9465. Slot N > 1 serves
  on `9468 + N` (`scripts/live-stack/worker-lifecycle.sh:214`).
- **Report a missing authority as the candidate.** judgment: that fabricates identity, which
  #2803 requirement 3 forbids.
- **Install the authority on the proof lane in this change.** judgment: it needs a database
  login, server and client PKI, and the `provider_authority_host` role inputs
  (`deploy/ansible/roles/provider_authority_host/defaults/main.yml`). That is lane provisioning,
  which #2807 owns and #3066 tracks.
