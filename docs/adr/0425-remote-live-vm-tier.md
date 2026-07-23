# ADR 0425 — A remote-libvirt `live_vm` family: a typed `qemu+tls://` environment contract, gate, and additive sub-marker

- **Status:** Accepted
- **Date:** 2026-07-23
- **Issue:** #1424
- **Epic:** #1423 (remote-libvirt capability proofs)
- **Builds on:** ADR-0386 (the live-test framework + `require_live_vm_*` gate vocabulary),
  ADR-0353 (`live_vm_tcg` — the additive-sub-marker precedent), ADR-0076 (the remote-libvirt
  provider package + `qemu+tls://` verified-TLS mandate), ADR-0084 (two-phase vmcore retrieve
  through the object store), ADR-0095/ADR-0235 (the reconciler-resident remote console collector),
  ADR-0110 (guest-routable object-store endpoint), ADR-0087 (the `src/` config-env guard that
  keeps test-only `KDIVE_*` reads in `tests/`)

## Context

Remote-libvirt has no `live_vm`-tier proof. It is exercised only through the operator-run
`live_stack` suite (`tests/integration/test_remote_live_stack.py`), which drives the full MCP
HTTP spine — there is no direct provider-op tier analogous to local's `live_vm` /
`live_vm_tcg` families. Every remote-capability entry under epic #1423 needs one to demonstrate
its capability against a real `qemu+tls://` host rather than asserting it from unit tests over
`FakeLibvirtConn` / `FakeDomain`.

The live-tier vocabulary is already factored for this — a fourth instance of an existing pattern,
not new machinery:

- `tests/live_vm/__init__.py` resolves each family's env into a typed contract and exposes the
  `require_live_vm_*` gates. It lives in `tests/` deliberately, so the ADR-0087 config-env guard
  (which reserves `KDIVE_*` reads in `src/` for `kdive.config`) is not tripped by test-only env
  vars.
- `pyproject.toml` declares the marker family: `live_vm` plus the additive `live_vm_throwaway` /
  `live_vm_provisioned` sub-markers, each carried *alongside* the bare `live_vm` marker (#1290).
- `docs/operating/runbooks/remote-live-stack.md` already codifies the remote operator contract
  this tier's env resolves: a `qemu+tls://` URI with x509 mutual TLS (`no_verify` forbidden), an
  operator-staged base-image qcow2 volume, and object-store reachability for the two-phase vmcore
  upload.

Two structural facts constrain the contract's shape:

- **Skip-vs-fail discipline is load-bearing.** Required env unset → the gate *skips* (this host
  isn't set up for the tier); env set but wrong → the gate *fails loud*, so a mis-provisioned host
  cannot masquerade as "no environment" and read green.
- **The contract must cover more than the libvirt URI.** Two dependents are otherwise unprovable:
  the remote vmcore retrieve is two-phase through the object store (ADR-0084) whose endpoint must
  be guest-routable (ADR-0110), and remote's console collector is reconciler-resident (ADR-0095,
  ADR-0235), so any console-dependent proof needs a running reconciler.

## Decision

**Add a fourth `live_vm` family — remote-libvirt — as an additive `live_vm_remote` sub-marker over
a typed `RemoteContract`, resolved and gated in `tests/live_vm/__init__.py`.** It mirrors the three
existing families exactly; nothing new is invented.

- **A typed `RemoteContract`** carrying `libvirt_uri`, `base_image`, `s3_endpoint_url`, `s3_bucket`,
  and `reconciler`, resolved by `resolve_remote_contract()` into the shared `EnvResolution[T]`
  (`AVAILABLE` / `ABSENT` / `MISCONFIGURED`).
- **The trigger is `KDIVE_LIVE_VM_REMOTE_URI`.** Unlike the local families, `KDIVE_LIBVIRT_URI` is
  *not* the lever: there is no default remote host, so a remote run must name its own
  `qemu+tls://` host, and the URL's presence is what declares intent to run the tier. Once set,
  every companion is required — `KDIVE_LIVE_VM_REMOTE_BASE_IMAGE` (the operator-staged base-image
  volume), `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` (reusing the existing `_S3_REQUIRED_ENV`),
  and `KDIVE_LIVE_VM_REMOTE_RECONCILER` (a presence marker for the reconciler daemon) — and a
  missing one is `MISCONFIGURED`, mirroring the provisioned family, where a set `SYSTEM_ID` with a
  partial `KDIVE_S3_*` fails loud.
- **Remote mandates verified mutual TLS.** A URI that is not `qemu+tls://`, or one carrying
  `no_verify`, is `MISCONFIGURED` (ADR-0076; the remote-live-stack runbook forbids `no_verify`) —
  not "no environment". This is the family's own analogue of the throwaway family's
  `session_required` fail-loud.
- **The reconciler and the object-store endpoint are presence-checked, not probed.** The resolver
  reads env and returns; it does no network I/O, exactly as the provisioned family checks the S3
  endpoint present, not reachable (S3 *credentials* stay file-based under `KDIVE_SECRETS_ROOT`, not
  env). This keeps the resolver deterministic and unit-testable with `monkeypatch` alone.
- **`require_live_vm_remote()`** is the gate a remote test threads: skip on `ABSENT`, `pytest.fail`
  on `MISCONFIGURED`, return the contract on `AVAILABLE` — the `live_vm` analogue of `require_stack`
  / `require_issuer`.
- **Marker + selection.** Register `live_vm_remote` in `pyproject.toml`; a remote test carries it
  *and* the bare `live_vm` marker (#1290). The existing non-gated additivity guard
  (`test_family_markers.py`) is extended to include `live_vm_remote`, so a carrier that forgets the
  bare marker fails ordinary CI. `just test-live-remote` (`-m live_vm_remote`, `--strict-markers`,
  exit-5-is-a-clean-skip) is a focused selector alongside `test-live` / `test-live-tcg`;
  `just test-live` (`-m "live_vm and not live_vm_tcg"`) still collects the remote tests too, each
  skipping cleanly without remote env.

No new host system package: the remote family connects out over `qemu+tls://` using the libvirt TLS
client stack (`libvirt-clients` / `gnutls-bin`) the `libvirt_stack` role already declares, and its
introspect leg uses the worker-side vmcore postmortem (`drgn`) already in the `live_vm` venv. The
(nil) delta is recorded as a comment in `live_vm_host` defaults so the "declare host deps in the
same change" discipline (AGENTS.md) is auditable rather than silent. Test-only + docs + one marker;
no production code, no migration, no dependency.

## Consequences

- Remote-libvirt capabilities under epic #1423 get a direct provider-op proof tier
  (`just test-live-remote`) instead of being asserted only through the `live_stack` HTTP spine.
- The tier skips cleanly on a host with no remote env, and a partial/wrong env fails loud — a
  mis-provisioned host cannot read green.
- The contract single-sources the remote env in `tests/live_vm/__init__.py`, so the base-image,
  object-store, and reconciler obligations a remote proof depends on are checked in one place.
- `just test-live` runtime is unchanged (the remote tests skip without env), and the additivity
  guard prevents a remote test from silently escaping `-m live_vm`.

## Rejected alternatives

- **Reuse `KDIVE_LIBVIRT_URI` as the remote trigger.** Rejected: that env is the local families'
  per-family override on top of a sensible default (`qemu:///system` / `qemu:///session`). Remote
  has no default host, so overloading the same var would make "is the remote tier requested?"
  ambiguous and could silently route a local override into the remote gate.
- **Probe reconciler/object-store reachability in the resolver.** Rejected: it would make the
  resolver do network I/O, breaking the deterministic-`monkeypatch` unit-test property every other
  family's resolver has, and it duplicates readiness checks the spine already performs. Presence,
  not reachability, is the contract — matching the provisioned family's S3-endpoint check.
- **A separate `require_live_vm_remote_console` / `_kdump` gate per dependent.** Rejected as
  premature: one `RemoteContract` carries every companion, and a proof reads the field it needs.
  Split the gate only if a real remote proof needs a strictly narrower env.
- **Add the remote env reads to a `src/` module.** Rejected: it would trip the ADR-0087 config-env
  guard. Test-only env belongs in `tests/live_vm/`, where the other three families already live.
- **Make `live_vm_remote` a top-level tier disjoint from `live_vm` (like `live_vm_tcg`).** Rejected:
  `live_vm_tcg` is disjoint because it rides a different *vehicle* (the `live_stack` provision
  spine) and a 10×-slower emulated boot that must not leak into the native tier. The remote family
  is the same vehicle as the other `live_vm` families (direct provider ops), so it is an additive
  sub-marker under `live_vm`, not a disjoint tier.
