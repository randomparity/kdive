# 0803 Retained authority lane CA custody, lifetime and renewal

## Status

Accepted (2026-10-20)

## Context

A retained installed-authority lane runs on TLS credentials that only the disposable proof
carrier can issue. The carrier keeps its CA key in a directory it tears down and fixes every
lifetime at one day (`scripts/operations/prove-external-boot-authority-host.sh:233-265` at
`bb85e0eba`). When a hand-adapted copy of that PKI expired on a retained lane, there was no CA
key custody rule, no lifetime policy, and no supported way to install renewed files (#3267,
#3273).

[ADR-0762](0762-remote-worker-tls-provisioning.md) keeps worker TLS delivery in the existing
installation tasks and adds no new installer. The authority reads its credentials only at
start, through `LoadCredential=`. The fixed worker re-reads its client certificate, key and
server CA from the secrets root on every request (`providers/assembly/authority.py:48-50`,
`network_client.py:38-65`, `security/secrets/secrets.py:87`).

## Decision

We will issue authority and fixed-worker TLS credentials with one script,
`scripts/operations/authority-pki.sh`, from a private lane PKI directory on the operator's
control host. Both the disposable carrier and retained lanes use it. Its rules:

- **Custody.** The CA key and leaf keys live in an operator-chosen directory. That directory
  is owned by the operator, has mode `0700`, is not a symlink, and is outside every Git work
  tree. The CA key is never installed on a lane host. Creating a CA key or a leaf key needs an
  explicit `--key new`, and the script refuses to overwrite one.
- **Lifetime.** The operator states every lifetime in whole days, at least one, at each
  issuance. The script has no default and no maximum. At each issuance it refuses any
  certificate in the lane directory that would outlive its CA certificate. The runbook's
  example uses 30-day leaves under a 365-day CA. The carrier keeps its one-day lifetime.
- **Renewal.** Renewal issues new certificates for the existing CA key and leaf keys. It
  re-issues the CA certificate from the same key only when that certificate no longer covers
  the new leaf. A narrow playbook installs the files through the existing `live_vm_host`
  credential tasks:
  1. On the controller, it cross-checks the certificate chains the authority and worker verify
     at runtime, including the server name, and each key pair.
  2. It installs the authority files.
  3. It runs `reset-failed`, then restarts the authority exactly once, whether or not a file
     changed, so a rerun repairs an interrupted run.
  4. It installs the worker files only after the authority is ready.
  5. It verifies expiry and the installed chains, then makes a handshake with the worker's own
     files, which must see the installed server certificate being served.

  The fixed workers are not restarted.
- **CA rotation.** Rotation uses a new CA key and new leaf keys at the same lane path. The
  old directory is retired, and destroyed if a key was exposed. Rotation is the only remedy
  for a lost or exposed key, because there is no revocation. It uses the same playbook and
  requires a quiet lane, with no running job using the authority. The operator enforces that
  precondition.

## Consequences

- Routine renewal changes only certificate files. Key and trust-anchor identity are
  preserved, so ADR-0762's non-transactional copies cannot pair a new certificate with an old
  key or leave a trust gap.
- The operator must keep, and back up, a durable private directory. Losing it forces CA
  rotation.
- Nothing renews or warns automatically. #3272 refuses a missed renewal at the next
  provisioning or verification. Between those, an expired certificate takes the authority out
  of service.
- Each renewal restarts the authority once. Its `reset-failed` clears #3272's start counter,
  so recent provisioning starts cannot make that restart fail.
- Renewal requires the lane's full provisioning inputs on the controller, because it reuses
  the provisioning preflight.
- The playbook requires a clean controller checkout at exactly the installed revision, so it
  never runs tasks from one revision against an authority installed from another. A lane
  installed before this change has to be reprovisioned once before it can use the playbook.
- Rotation has a short window in which worker requests fail closed. The quiet-lane
  precondition bounds it.

## Considered & rejected

- **Keep the CA key in the lane's disposable or campaign working directory.** verified: the
  carrier deletes `$secrets` in `cleanup_remote_proof`. `git worktree remove` deletes ignored
  files, so a key kept in an ignored path inside a checkout is lost with the worktree.
- **Reuse `deploy/ansible/playbooks/pki.yml`.** verified: it keeps the CA key under
  `{{ playbook_dir }}/../artifacts`, inside the checkout. It issues the remote-libvirt
  profile, with host FQDN and IP SANs and no authority server-name derivation, not the
  carrier's (`pki.yml` at `bb85e0eba`).
- **Install the CA key on the authority host.** judgment: no host consumer reads it, and it
  would widen what a lane-host compromise exposes.
- **Rerun the full `installed-local-authority.yml` playbook to renew.** judgment: it
  reconverges the checkout, venv, units and System state to change certificate files, which
  is the reinstall #3273 rules out.
- **New leaf keys on every renewal.** verified: the worker reads its certificate and key as
  separate files per request (`network_client.py:41-65`). The install loop copies them one at
  a time (`live_vm_host/tasks/main.yml:432-445` at `bb85e0eba`, now
  `worker_authority_tls.yml`), so a new key opens a window with a
  mismatched pair.
- **An overlapping old-plus-new CA bundle during rotation.** judgment: it needs a second
  installation pass and a bundle lifecycle to remove a seconds-long window on a lane that can
  be quiesced.
- **A default or capped lifetime in the script.** judgment: #3273 asks for a deliberate
  operator choice. The runbook gives the recommended values, and the ADR does not fix them.
- **Restart only when a credential file changed (handler only).** verified: Ansible keeps a
  handler notification only for the run that made it (ansible-core 2.21 handler documentation),
  so the notification does not survive an interrupted or failed run. A rerun would then leave the
  authority serving the certificate it loaded earlier, and a same-CA handshake would still
  pass.
- **Restart the fixed workers after renewal.** verified: the worker reads its TLS files per
  request (above), so a restart gains nothing. An out-of-band worker restart is a known way
  to wedge a slot.
