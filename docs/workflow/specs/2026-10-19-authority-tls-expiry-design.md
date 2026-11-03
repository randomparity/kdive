# Authority lane TLS expiry check and bounded restart (#3272)

## Problem and authority

A retained authority lane whose TLS leafs reached `notAfter` restarted
`kdive-external-boot-authority.service` thousands of times without ever entering a failed
state, and no provisioning step reads a certificate's validity period. Issue #3272 (child B
of #3267) asks for a fail-closed expiry check in `live_vm_host` preflight and verification
and a bounded start limit on the authority unit. Scope token `q3272-f44aeb37`; campaign
`2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18`. Full-spec lane, M (250). Exclusions:
the mTLS model, a certificate service and the carrier's one-day lifetime are dropped;
expiry naming in the authority's readiness error is #3271; renewal and CA custody are #3273.

## Design

**Expiry check.** A new role task file `live_vm_host/tasks/tls_expiry.yml` takes
`live_vm_host_tls_expiry_certificates`, a list of `{name, path}`, and:

1. asserts `live_vm_host_authority_tls_expiry_margin_seconds` renders as a non-negative
   decimal integer (`^[0-9]+$`), so an extra-vars string works and garbage fails;
2. runs `openssl x509 -noout -enddate -checkend <margin> -in <path>` per entry
   (`changed_when: false`, `failed_when: false`, `check_mode: false`);
3. asserts each result has `rc == 0` and the line `Certificate will not expire`.

The failure message names the credential and path, then one of: the `notAfter=` line and
the margin with the pointer "renew it before use; see
docs/operating/runbooks/live-testing.md, Candidate evidence and cleanup"; when `openssl`
exits 1 without a `notAfter=` line, that the file is not a readable PEM certificate; for
any other exit (for example `openssl` absent), that it could not be checked, with
`openssl`'s stderr or the module message. Only certificate
files are passed; no key path is read, so these tasks carry no `no_log` (it would hide the
message, and dates are not secret).

Call sites:

- `authority_preflight.yml`, after the existing stat/mode asserts, includes the file with
  `apply: {delegate_to: localhost, become: false}`: the authority `server_certificate`,
  `server_ca`, `worker_client_ca` and `health_client_certificate` sources when the
  authority is enabled; the worker `server_ca` and `client_certificate` sources when fixed
  worker authority is enabled. Nothing on the runner host has been changed yet.
- `verify.yml`, before `Assert the authority service is ready` (an expired installed
  certificate keeps the service from becoming ready, so the named message must come first),
  checks the installed
  `server-certificate`, `server-ca`, `worker-client-ca`, `health-client-certificate` under
  `live_vm_host_authority_credentials_dir`, and `server-ca`, `client-certificate` under
  `live_vm_host_worker_authority_secrets_root` when worker authority is enabled.

Role default `live_vm_host_authority_tls_expiry_margin_seconds: 43200` (12 h, operator
decision 2026-10-19 via the campaign root: the longest planned live run is 7 h). The
disposable carrier provisions through this role with one-day certificates whose lifetime is
excluded from change, so the margin stays below 24 h: 12 h leaves the carrier about 12 h and
refuses a retained lane due to expire during the longest planned run. `openssl` is already
an authority package on the runner host; on the controller it is a prerequisite (the carrier's
PKI preparation already uses it), named in the runbook sentence.

**Start limit.** All three renderings of `kdive-external-boot-authority.service` gain, in
`[Unit]`, `StartLimitIntervalSec=600` and `StartLimitBurst=5` (operator-approved 2026-10-19):
`provider_authority_host/templates/authority.service.j2`,
`live_vm_host/templates/external-boot-authority.service.j2` (what the observed retained lane
runs) and `deploy/systemd/system/kdive-external-boot-authority.service`. With
`RestartSec=5s`, the budget is five starts of any kind (automatic, manual or Ansible) in ten
minutes, so transient failures recover while fewer than five starts have been spent in the
window; a deterministic failure reaches `failed` after five starts; and
5 × (`TimeoutStartSec=90s` + 5 s) = 475 s < 600 s, so a start that hangs to its timeout
still trips the limit. The existing `active/running` assertion in `verify.yml` then fails.
Within ten minutes of tripping, systemd also refuses a manual or Ansible restart until
`systemctl reset-failed`; the runbook paragraph says so. No reset task is added to the
provisioning path. The budget is shared with Ansible-driven starts and with the installed-authority live proofs
(`tests/live_vm/installed_*_authority_support.py` restart the unit, about three starts a pass): the proof-only drift block
in `verify.yml` (run when `live_vm_host_authority_proof_enabled`, which the carrier sets)
deliberately drives the unit through a TLS-drift restart loop, which now reaches
start-limit-hit, and then restarts it in its `always` block. That block therefore runs
`systemctl reset-failed kdive-external-boot-authority.service` immediately before
`Restore authority readiness after drift proofs`; the reset exists only inside the proof.
Earlier role starts and the ACL-drift restarts may exhaust the budget before the TLS-drift
restart; that restart is already `failed_when: false` and its assertion only requires the unit
not to be active, so the proof holds either way and the reset still precedes the restore.

**Considered and rejected.**

- `community.crypto.x509_certificate_info`. judgment: it needs Python `cryptography` in the
  target interpreter, which `live_vm_host_authority_packages` does not install; `openssl`
  is already there.
- A margin of 24 h or more. verified: `prove-external-boot-authority-host.sh` signs the CA
  and every leaf with `-days 1` and provisions through `live_vm_host`, so every carrier
  run would fail its own preflight.
- Keep the systemd defaults. verified: `systemctl show -p DefaultStartLimitIntervalUSec
  -p DefaultStartLimitBurst` prints `10s` and `5` (systemd 259); a 5 s restart cycle never
  makes five starts within 10 s, which is the observed endless loop.

## Failure model

- Actors and deployments: an operator provisioning a retained or disposable authority lane
  with `installed-local-authority.yml`, `runner.yml` or the carrier; CI running the role
  harness and unit tests on Linux x86_64.
- Invariants and assets: no key material is read or printed; preflight fails before host
  mutation; a lane whose checked certificates expire within the margin cannot pass
  verification; a deterministic authority start failure ends `failed`.
- Accepted failure classes: only the first certificate in a PEM bundle is checked (each
  credential here is one certificate); a credential expiring after verification but during
  use is not caught here (the start limit then surfaces it); key/certificate mismatch and
  chain validity stay with the authority's own readiness probe.
- Covered elsewhere: readiness-error naming #3271; renewal and CA custody #3273; the
  provider_authority_host remote role has no expiry check (outside this issue).

## Threat model

- Boundaries: added — `openssl` parses operator-supplied certificate files on the
  controller (preflight) and root-owned installed copies on the runner host (verify); the
  path reaches `openssl` as an argv element, never through a shell. None widened.
- Actors: the provisioning operator, who already controls these files and the role inputs;
  no untrusted party supplies a path or certificate.
- Controls: paths come from existing role variables already required to be protected
  regular files (preflight) or asserted `0400`/root-owned (verify); argv form prevents
  injection; output echoed on failure is the `notAfter=` line, `openssl`'s own error or the module's
  execution error, never
  file content or key material.
- Out of scope: a malicious operator; certificate chain, revocation and hostname
  validation, which remain the authority's mTLS handshake.

## Validation and rollback

- `deploy/ansible/tests/run-authority-tls-expiry.py` drives the real `tls_expiry.yml`
  against localhost with generated certificates: valid passes; inside-margin and expired
  fail naming the credential and `notAfter`; a non-certificate fails as unreadable; a
  negative or non-numeric margin fails the margin assert. Wired into `just test-ansible`.
- `tests/deploy/` unit tests: the start limit sits in `[Unit]` of all three renderings with
  `burst × (TimeoutStartSec + RestartSec) < interval`; preflight and verify include the
  expiry file with exactly the certificate lists above, preflight delegated to localhost,
  verify's includes before the service-ready assert, and no key path passed; the drift
  proof's `always` block resets the failed unit immediately before its restore restart.
- Not run: the carrier and live systemd start-limit behaviour need a lab host, which this
  change has no grant for; the drift-proof interaction is held by the structural test.
- Regenerate `runner-tasks-2391.txt` and its count. Rollback is reverting the commit; no
  persisted state changes.
