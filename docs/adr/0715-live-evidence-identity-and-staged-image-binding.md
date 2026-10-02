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

### Amendment (2026-10-01): an installed authority that cannot be read stops the run (#3066)

An amendment rather than a new decision: rules 1 and 3 and the other roles in rule 2 stand. It
qualifies one claim in rule 2, that a role which cannot be read or resolved is omitted, for the
`authority` role only. Omission was safe for the `/readyz` roles because a missing role failed every
native cell. After the Consequences amendment below, no native cell requires `authority`, so an
omitted authority would hide a stale install.

- `authority` is recorded only when `/opt/kdive-provider-authority/revision` exists. An `os.lstat`
  that reports a missing file or path component means no authority is installed; nothing is read.
- Any other `lstat` result means the file is present, including a permission error that leaves its
  absence unproven. A present file must be read through `sudo -n` and resolved to a full SHA, or the
  identity read raises and the cell records nothing.
- A recorded role is still checked against the candidate whether or not the cell requires it, so a
  stale authority fails `deployed-revision-mismatch`. An installed authority can be bound, and a bound
  authority changes local-libvirt routing (ADR-0623). Its involvement in a native cell therefore
  cannot be ruled out once it is installed.

## Consequences

- A rebuilt image needs new bindings and new evidence, as ADR-0688 expects.
- The default `demo-up.sh` lane installs no authority. The contract requires that role for
  every native cell, so cells on that lane fail `deployed-role-missing` until #3066 decides
  between a contract change and a lane change.
- `/readyz` reports a commit, not tree state. Restarting the stack from a dirty tree goes
  undetected; only the test checkout's own state is checked.

### Amendment (2026-10-01): native cells require only the roles their scenario uses (#3066)

An amendment rather than a new decision: it settles the question the second bullet above left to
#3066 and qualifies that bullet's claim that the contract requires `authority` for every native cell.
Native cells now require `server`, `worker` and `reconciler`. No native scenario on the demo-up lane
routes through the provider authority: with no authority binding, local-libvirt behaves as before
(ADR-0623). Tool cells keep their declared `authority = true` flag and `role_overrides`. A native
scenario that does route through the authority adds the role to its own cells in the change that
implements it (#2809, #2810). The matrix digest changes, so earlier bindings are void.

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

### Amendment (2026-10-01): two further rejections (#3066)

An amendment rather than a new decision: it adds the alternatives #3066 weighed. It does not change
the rejection above, which concerned this record's own change.

- **Install the authority on the qualification lane and keep the role on every native cell.**
  judgment: it would record a revision for a component that no native scenario on the lane exercises.
  The cell would then qualify on an identity that has nothing to do with its assertions. Owners of
  authority-routed cells install it (#2812, #2807).
- **Keep omitting an authority whose revision cannot be read.** judgment: once native cells stop
  requiring the role, omission makes "installed but unreadable" look like "not installed". That
  hides a stale revision. Once installed, the authority may be involved, because a bound authority
  changes local-libvirt routing (ADR-0623). #2803 requirement 3 says such a revision must fail.
