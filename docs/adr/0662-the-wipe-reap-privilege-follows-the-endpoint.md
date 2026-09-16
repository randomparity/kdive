# 0662 — The `--wipe` reap's privilege follows the endpoint it was published

## Status

Accepted (2026-09-15)

## Context

`stack-down.sh --wipe` runs every mutating reap call under `sudo` and enumerates bare, so the two
halves do not carry the same identity (#2515 recorded the reporting half of that split and left
the privilege half to #2516). `resolve_libvirt_uri` publishes two daemon scopes to one variable:
the lifecycle contract's operator-owned **session** endpoint, and the `qemu:///system` fallback
on a host without that contract, which is root's. One fixed privilege cannot be right for both.

## Decision

`stack-down.sh` derives **one** privilege for the whole `--wipe` reap from the resolved
`KDIVE_LIBVIRT_URI`, by reading its path component with the query stripped: `/session` is reached
as the invoking account, everything else keeps `sudo`. That decision governs the enumeration that
grades the reap, the `destroy` and `undefine` that perform it, and the overlay `rm`, so
observation and mutation always carry the same identity.

## Consequences

- On a provisioned host the reap needs no root. The operator reaches the mode-`0770` socket and
  the mode-`2770` overlay directory as their **owner**: `install-live-worker-lifecycle.sh`
  installs the socket `operator_uid:group_gid:770` and `/var/lib/kdive/rootfs`
  `operator:kdive-live-libvirt` mode `2770`. `kdive-live-libvirt` membership is how the *worker*
  accounts reach the same two paths; both are access paths the contract publishes, and neither
  is root's.
- On a bare host the up-front `--wipe` gate now enumerates under `sudo`, so an operator who
  cannot escalate is refused before anything is stopped rather than mid-wipe. That moves the
  run's first escalation ahead of the irreversibility warning and the `Type 'wipe'` confirmation,
  so a host configured to prompt asks for the password before the operator has confirmed, and the
  refreshed sudo timestamp outlives an abort at the confirmation. Kept, but not because the
  ordering is forced: the confirmation block itself precedes every destructive step, so moving
  the gate below it would still refuse before anything is stopped. The reason to keep the gate
  first is narrower — refusing before asking spares the operator a confirmation on a run that
  cannot proceed, and `--wipe --yes` skips the prompt entirely, so the ordering only ever shows
  on the interactive bare-host path. Two alternatives were weighed and declined as
  confirmation-UX changes independent of this decision: moving the gate below the confirmation,
  and printing the irreversibility warning ahead of the gate while leaving the prompt where it
  is (which splits the warning from the confirmation it qualifies, and warns about
  irreversibility on runs the gate then refuses).
- Classification is textual: an endpoint whose scope is not in its path would be misclassified.
  Both published URIs and the bare-host default carry it there.
- The overlay `rm` becomes the invoking account's on the session branch, so the operator must be
  able to **write** the overlay directory, not merely list it. The installed `2770` directory
  grants that; a host whose directory has drifted away from it gets one denied `rm` per overlay,
  reported by the existing end-state grading. No precondition is added for it here — see the
  unresolved finding carried out of this change's review round.
- Which *daemon* answers is unchanged for the published `?socket=` endpoints, where the socket
  path selects it; for a plain `qemu:///session` it changes from root's per-uid daemon to the
  invoking account's, which is the point.

## Considered & rejected

- **Keep `sudo` on every call.** verified: `_reconcile_libvirt_tuple` in
  `deploy/systemd/install-live-worker-lifecycle.sh` adopts an endpoint only when the live pid's
  uid equals `$operator_uid`, and the same installer creates `/var/lib/kdive/rootfs` as
  `operator:kdive-live-libvirt` mode `2770`. Root owns neither, so escalation bypasses the ownership
  gate rather than satisfying it.
- **Drop `sudo` from every call.** verified: `resolve_libvirt_uri` in
  `scripts/live-stack/libvirt-uri.sh` resolves `qemu:///system` when the contract file is absent,
  and that daemon is root-owned.
- **Restate the ownership model so root is correct** (#2516's second acceptance). judgment: it
  grants the reap a privilege the rest of the contract spends real effort withholding — worker
  accounts are kept out of `sudo` entirely.
- **Match the two published URIs exactly instead of classifying.** verified:
  `resolve_libvirt_uri` honours a caller-supplied `KDIVE_LIBVIRT_URI` without consulting
  `LIBVIRT_SOCKET_URIS`, so a plain `qemu:///session` reaches the reap and an exact-match list
  would escalate against it.
- **Ask libvirt which scope the URI names.** judgment: a round trip to the daemon whose
  reachability is the question under test, for something the URI string already states.
