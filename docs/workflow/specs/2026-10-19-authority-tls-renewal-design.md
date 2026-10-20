# Renew retained authority lane TLS credentials (#3273)

## Authority and status

The frozen charter is issue #3273, `WORK:SCOPE` token `q3273-bbefbb02`. Campaign
`2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18` supplied the operator-approved
exclusions: changing the authority's mTLS model, adding a certificate service, and changing
the disposable proof carrier's one-day lifetime (all dropped, no owner). The operator approved
this design and its operator-facing interface on 2026-10-19; see the operator decisions
section. PR #3279 implements it.
The decision record is [ADR-0803](../../adr/0803-retained-authority-tls-custody-and-renewal.md).

**Built on #3272.** PR #3278 merged at `b2d93172e`. The implementation is based on that merge,
because it calls `tls_expiry.yml` and edits the runbook paragraph #3278 extends.

Siblings under #3267: #3271 (merged) names expiry in the authority's readiness error; #3272
(PR #3278, merged) adds the `live_vm_host_authority_tls_expiry_margin_seconds` check (default
43200) to preflight and verify, and `StartLimitIntervalSec=600`/`StartLimitBurst=5` to the
authority unit. This design reuses that check and counts against that start budget. It does
not duplicate either.

## Problem and evidence

At base `bb85e0eba`, the only in-repo issuance is inside the disposable proof carrier:
`scripts/operations/prove-external-boot-authority-host.sh:233-265` derives the server name,
creates `ca.key` in the carrier's disposable `$secrets` directory, and issues the CA and the
`server`, `health-client` and `proof-client` leaves with `-days 1`. A retained lane therefore
runs on a hand-adapted private copy of that subset. When its certificates expire the authority
fails its readiness self-handshake and stops serving, and nothing in the repository says
where the CA key should be, which lifetime to use, or how to install and restart.
`docs/operating/runbooks/live-testing.md:557-559` defers all of this to an unspecified
separate review.

Facts the design relies on, checked against source at the base:

- The authority loads its seven credentials through `LoadCredential=`, so it reads them only
  when it starts. A change takes effect only on restart
  (`deploy/ansible/roles/live_vm_host/templates/external-boot-authority.service.j2`).
- The fixed worker resolves its authority client certificate, key and server CA from the
  secrets root on every request: `providers/assembly/authority.py:48-50` builds the
  transport per call, `network_client.py:38-65` resolves the three refs, and
  `security/secrets/secrets.py:87` reads the file with no cache. A worker needs no restart to
  use renewed files.
- The three worker files are installed by one looped `copy` task
  (`live_vm_host/tasks/main.yml:432-445` at `bb85e0eba`). Each copy is atomic, but the loop is not a
  transaction. ADR-0762 records the same property.
- An out-of-band systemd restart of a fixed worker wedges its slot. The worker-slot wedge
  recovery note records this, and the operator's 2026-10-19 manual renewal left the worker
  running for that reason.

## Scope

In:

1. A reusable issuance script, `scripts/operations/authority-pki.sh`. The proof carrier's
   PKI block moves into it unchanged in key type, extensions and server-name derivation.
2. A durable custody rule for the CA key and leaf keys, and operator-chosen lifetimes.
3. A narrow renewal playbook. It installs through the existing `live_vm_host` credential tasks
   (same task names, file names, owners and modes) and restarts only the authority (operator decision Q5).
4. A runbook procedure replacing the PKI half of the "review separately" sentence.
5. An ADR on custody, lifetime policy, and renewal versus CA rotation.

Out (operator-approved exclusions): the mTLS model, any certificate service, and the
carrier's one-day lifetime. Also out because nothing in the charter asks for them: certificate
revocation, automatic renewal scheduling, and the dedicated remote composition
(`provider_authority_host` plus the worker's `remote-authority` client files). The last of
these is out of scope by operator decision Q1.

## Design

### Issuance script

`scripts/operations/authority-pki.sh` is a Bash script (`set -euo pipefail`, `umask 077`) that
uses `openssl` and, for server-name derivation only, `python3`. It is an operator tool, not a
service. It never prints key material. Its two issuing subcommands each take an explicit
`--key` mode, so that a mistyped directory can never silently become a new trust root. A third
subcommand only prints the derived server name, so that the playbook's cross-check uses the
same derivation.

```text
authority-pki.sh ca   --dir DIR --subject CN --days N --key new|existing
authority-pki.sh leaf --dir DIR --name NAME --usage server|client --days N --key new|existing
                      [--authority-instance INSTANCE]   # required for server, refused for client
authority-pki.sh server-name --authority-instance INSTANCE
```

- `ca --key new` refuses if `DIR/ca.key` exists. It generates a prime256v1 key and self-signs
  `ca.pem` with `basicConstraints=critical,CA:TRUE,pathlen:0` and
  `keyUsage=critical,keyCertSign,cRLSign`. These are the carrier's current values.
- `ca --key existing` refuses unless `ca.key` exists and `--subject` equals the subject of the
  current `ca.pem`. It re-self-signs `ca.pem` from the same key. The key is unchanged, so the
  issuer name and authority key identifier of existing leaves still match, and those leaves
  verify against the new `ca.pem`. A test proves this.
- `leaf --key new` refuses if `DIR/NAME.key` exists; `--key existing` refuses unless it
  exists. The leaf carries `basicConstraints=critical,CA:FALSE`,
  `keyUsage=critical,digitalSignature`, `extendedKeyUsage=serverAuth|clientAuth`, and for a
  server `subjectAltName=DNS:<authority_server_name(INSTANCE)>`. These are the carrier's
  current values. The server name uses the carrier's existing Python derivation, which equals
  `kdive.providers.external_boot_authority.protocol.authority_server_name`; a test proves the
  equality.
- `leaf` refuses when `ca.pem` expires within `N` days (`openssl x509 -checkend`).
  `ca --key existing` refuses when any other `*.pem` in `DIR` would outlive the new CA
  certificate. Together these keep every certificate in the directory within its CA's lifetime
  at every issuance.
- The CSR and extension file live in a private `mktemp -d` directory that is removed on exit.
  Each new certificate is written to a temporary file in `DIR`. Before an atomic `mv` over
  `NAME.pem` or `ca.pem`, the script checks it with `openssl verify -purpose sslserver` or
  `sslclient` against `ca.pem`, and checks that the certificate's public key equals the key
  file's. A failed issuance never replaces a good file. The serial file is `DIR/ca.srl`.
- `--days` must be a positive decimal integer. There is no default and no maximum; the
  operator chooses, as the ADR records.
- Directory gate, run before any read or write: `DIR` must be an existing directory and not a
  symlink, owned by the invoking user, with mode `0700` (no group or other bits). It must not
  be inside a Git work tree. That check fails closed. With `GIT_DIR`, `GIT_WORK_TREE` and
  `GIT_CEILING_DIRECTORIES` removed from the environment, the directory passes only when
  `git -C DIR rev-parse --is-inside-work-tree` prints `false`, or exits 128 reporting
  "not a git repository". It also refuses when `git rev-parse --is-inside-git-dir` prints
  `true` (a directory inside a `.git` admin area, which `git worktree remove` can delete).
  A missing `git`, a `safe.directory` refusal, or any other error refuses, and git's message
  is shown. Files are written `0600`.
- Output is one line per action: the file name, `notAfter`, and the public-key SHA-256. That is
  enough for a private receipt and contains no secret.

The proof carrier replaces lines 233-265 with four calls that use the script from its exact
source clone (`$source_root/scripts/operations/authority-pki.sh`): `ca --key new --days 1`
and three `leaf --key new --days 1` calls (`server`, `health-client`, `proof-client`), all with
`--dir "$secrets"`. The file names (`ca.pem`, `server.pem`, `server.key` and the rest) and
the generated `vars.yml` stay the same, and the lifetime stays one day. The `.csr` and `.ext`
files no longer appear in `$secrets`. Nothing reads them there today.

### Custody and lifetime

- One private lane PKI directory per retained lane, on the operator's control host (the
  machine that runs Ansible and already holds the lane's protected vars and credential
  sources). It holds `ca.key`, `ca.pem`, `ca.srl` and each leaf's `.key` and `.pem`. The
  runbook's example location is `~/.local/share/kdive/authority-pki/<lane>/`; the script takes
  the directory as an argument and has no built-in default.
- The lane's protected vars file names this directory's files as the existing
  `live_vm_host_authority_*_source` and `live_vm_host_worker_authority_client_*_source`
  inputs. Renewal rewrites those files in place, and rotation recreates the same path, so
  the vars file does not change.
- `ca.key` is never copied to a lane host. No role task reads it, because no role input names
  it.
- The operator chooses the lifetimes at each issuance. ADR policy: a leaf never outlives the
  CA; a routine renewal re-issues leaves only, while the CA certificate still covers the new
  leaf lifetime; the CA certificate is re-issued from the same key when it does not. The
  runbook example uses 30-day leaves and a 365-day CA (operator decision Q2). Every lifetime is at least one day,
  which keeps every leaf above #3272's 12-hour margin at issuance.

### Renewal versus CA rotation

- **Renewal** keeps the CA key and every leaf key and issues new certificates (`--key
  existing`). Only certificate files change on the host. Because keys and trust anchors keep
  their identity, the install order cannot create a mismatched pair or a trust gap. This is
  the routine operation.
- **CA rotation** uses a new CA key and new leaf keys. It is needed when the CA key is lost or
  exposed, or when a leaf key is exposed: there is no revocation, so trust can be withdrawn
  only by replacing the CA.
  - The operator moves the old lane directory aside (`<dir>.retired-<date>`) and recreates
    the same path at mode `0700`. That keeps every `*_source` path in the vars file valid.
  - They then run `ca --key new` and `leaf --key new` for all three leaves.
  - The retired directory is kept until the rotated lane verifies. If the reason for rotation
    was an exposed key, it is then destroyed; otherwise it is archived like any backup.
  - Rotation changes `server-ca` and `worker-client-ca` on the authority side and `server-ca`
    on the worker side. While the authority has restarted on the new CA and the worker still
    holds old files, or while the worker's three files are copied one after another, a worker
    request fails closed with a TLS error.
  - Rotation therefore requires a quiet lane, with no running job using the authority. The
    operator enforces this precondition; the playbook does not check it.
  - Rotation uses the same playbook as renewal.

### Install and restart: `deploy/ansible/playbooks/renew-local-authority-tls.yml`

The playbook targets `live_vm_runners`, runs with `become: true`, and takes the lane's existing
protected inventory and vars file, unchanged. Every role entry it uses is a static
`import_role`, so `--list-tasks` shows each task it runs. In order:

1. **Preconditions on the controller and host, before any mutation:**
   - The lane assertions from `installed-local-authority.yml`: authority enabled, worker
     authority enabled.
   - `live_vm_host` `authority_preflight.yml`, unchanged. It includes #3272's expiry check over
     the sources. It also validates the System manifest and base digests. That means the lane's
     bases must still be present on the controller, and each renewal pays one digest pass;
     the runbook states both.
   - A new chain cross-check, run on the controller (`delegate_to: localhost`, read-only),
     mirroring what the authority and worker check at runtime (`transport.py:719-736`,
     `network_client.py:53-65`), with `-x509_strict`:
     - `openssl verify -purpose sslserver -verify_hostname <authority_server_name>` of the
       server certificate source, once against the worker's server CA source and once against
       the authority's own server CA source (the startup self-check trusts that one). The name
       comes from `authority-pki.sh server-name` in the controller checkout;
     - `-purpose sslclient` of the worker client certificate source and the health client
       certificate source against the authority's worker-client CA source;
     - each certificate's public key equals its key source's.

     This proves before any change that each side will accept the other's certificate. The
     post-install handshake cannot prove it in TLS 1.3, because the server judges the client
     certificate after the client's handshake completes. These are tasks in the renewal
     playbook itself. They print only names and OpenSSL verdicts, never key material.
   - `/opt/kdive-provider-authority/revision` exists and equals `live_vm_repo_version`, and
     the controller checkout is clean with `HEAD` equal to that revision (operator decision Q3). Tasks from one
     revision are not run against an authority installed from another.
2. **Authority files:** `live_vm_host` `tasks_from: authority_credentials.yml`. This is the
   existing "Install external-boot authority credentials" task, moved verbatim into its own
   file. `main.yml` imports it at the same position, so the role's task list, names, owners,
   modes, `no_log`, `register` and restart notification do not change.
3. **Exactly one authority restart, whatever the copy reported:**
   - `systemctl reset-failed kdive-external-boot-authority.service`, unconditionally. This
     clears both a `failed` state and the start-limit counter, so recent provisioning or proof
     starts cannot make #3272's limit refuse this planned start.
   - `meta: flush_handlers`, which runs the existing restart handler when a file changed.
   - The same restart, explicitly, when `live_vm_host_authority_credentials` reports no change.
     A rerun after an interrupted or failed run therefore still restarts onto the files on disk,
     and the authority never keeps serving a certificate it loaded before them.

   The restart is `Type=notify`. It returns only after `READY=1`, which follows the startup TLS
   self-check (`host.py:1927-1930`). A failed restart stops the play here.
4. **Worker files:** `live_vm_host` `tasks_from: worker_authority_tls.yml`. These are the
   existing four tasks (inspect the secrets root, refuse a substitute, create the root, install
   the three files), moved verbatim. `main.yml` imports them at the same position. They run
   only after the authority is ready, so an authority that fails to start leaves the worker's
   files untouched.
5. **Verification:**
   - #3272's `tls_expiry.yml` over the six installed certificate files.
   - The authority's `ActiveState` is `active`.
   - An installed-file chain check as root:
     - the worker's installed `client-certificate` verifies (`sslclient`) against the
       authority's installed `worker-client-ca`;
     - the authority's installed `server-certificate` verifies (`sslserver`) against the
       worker's installed `server-ca`.
   - For each account in `live_vm_host_worker_accounts`, a TLS 1.3 handshake as that account
     over the request socket. It uses `openssl s_client -unix`, the worker's own installed
     files, and `-verify_hostname` set to the server name. Root derives that name with the
     installed authority venv's `authority_server_name`. The check requires
     `Verify return code: 0 (ok)`, and requires the served certificate's SHA-256 fingerprint
     to equal the installed `server-certificate`'s. That proves the restarted authority is
     serving the new file.
   - The handshake proves the worker's view of the server. The chain checks prove the server's
     view of the worker's certificate. A real worker-to-authority request is part of the live
     proof only, not of this playbook, because only a running worker incarnation holds the
     request credential.

The playbook installs only credentials. It does not check out the repository, build the venv,
or touch the unit, environment, libvirt, database or System state.

Two inputs of the moved tasks, `live_vm_host_authority_client_group` and
`live_vm_host_worker_accounts`, are `local_worker_host` role defaults. `main.yml` gets them
through its `import_role`. The playbook loads them at role-default precedence with a static
`import_role: {name: local_worker_host, tasks_from: worker_install_dirs.yml}` carrying
`when: false`. The defaults load and every imported task is skipped, so the lane's inventory
overrides still win. A test proves that an inventory override of
`live_vm_host_authority_client_group` wins.

**Restart order and start budget.**

- **Authority:** it restarts exactly once per playbook run, after all of its files are in
  place. Because of the `reset-failed` before it, that restart always fits #3272's budget.
  A failed restart can spend at most #3272's five automatic starts before the unit stays
  `failed`. The runbook says to correct the source before rerunning, so a broken renewal is
  never looped.
- **Fixed workers:** they are not restarted (operator decision Q5). They pick up the new files on their next
  request.

### Runbook

In `docs/operating/runbooks/live-testing.md`, under "Candidate evidence and cleanup", the
sentence becomes "Review any reuse of its disposable DSN preparation separately, including
database grants." The DSN half is unchanged and outside this charter. A one-line link to the
new subsection takes the PKI half's place in that paragraph. #3278's `tls_expiry.yml` message
names "Candidate evidence and cleanup", so the link sits where that message sends the
operator. The new subsection, "Renewing retained-lane TLS credentials", follows. It covers:

- creating the lane PKI directory and the first issuance for a new lane;
- routine renewal: `ca --key existing` when needed, then `leaf --key existing` for `server`,
  `health-client` and `worker-client`, with chosen lifetimes, and a private receipt of the
  printed lines;
- running the playbook, and its prerequisites: the lane's protected vars, bases and a
  clean checkout at the installed revision;
- what it verifies, and why the worker is not restarted;
- CA rotation: the same-path directory swap, the operator-enforced quiet-lane precondition,
  and disposal of the retired directory;
- failure handling:
  - a refused precondition changes nothing;
  - an authority start failure leaves the authority not ready and the worker files
    untouched, and the journal names the cause (#3271);
  - correct the source, then rerun the playbook, which resets the failed state itself;
  - do not rerun without a correction.
- how to back up the PKI directory, since losing it forces rotation.


## Failure model

1. **Actors and deployments**
   - A local operator on the control host runs the script and the playbook against one
     retained installed-local-authority lane (Ubuntu 26.04 x86_64, as
     `installed-local-authority.yml` asserts).
   - The disposable proof carrier runs the script on its carrier host.
2. **Invariants and assets at stake**
   - The CA private key: it never leaves the control host, never sits in a Git work tree, and
     is never overwritten (only `--key new` creates one, and only in a directory without one).
   - Existing leaf keys are never replaced by renewal.
   - At each issuance, no certificate in the lane directory outlives its CA certificate.
   - Installed file names, owners and modes are unchanged (the same tasks).
   - Each playbook run restarts the authority exactly once, after all seven of its files are
     in place.
   - A completed run leaves the authority serving the installed server certificate (the
     fingerprint check).
   - The worker's files are untouched unless the authority became ready.
   - No run starts when any of the cross-checked pairs fails: the server certificate against
     both server CA sources with the derived name, the two client certificates against the
     worker-client CA source, or a certificate against its key.
   - No key or DSN appears in output (`no_log` is kept; the script prints no secret).
3. **Accepted failure classes**
   - Authority unavailability during its restart (seconds). This is bounded and is the point of
     the restart. It happens on every run, including a run that changed nothing.
   - Between provisioning runs, nothing renews or warns. A missed renewal takes the authority
     out of service when its certificate expires, as on 2026-10-19. #3272 refuses it at the
     next provisioning or verification; ongoing monitoring is outside this charter.
   - The worker-to-authority request path is proven only by the live proof (a real request
     needs a running worker incarnation's credential). The playbook proves each side's
     certificate acceptance statically and the server side by handshake.
   - Failed worker requests during a CA-rotation window. Bounded by the quiet-lane
     precondition, and they fail closed.
   - A partially applied copy loop after an interrupted run. This is ADR-0762's accepted
     property; rerun with the same inputs.
   - Concurrent script runs on one directory racing on `ca.srl`. There is one operator per lane
     and no lock is added.
   - Loss of the lane PKI directory forces CA rotation. The operator backs it up.
   - Compromise of the operator's control host: it already holds every leaf key and the lane
     DSN.
4. **Covered elsewhere**
   - The expiry margin in preflight and verify, and the start limit: #3272 / PR #3278.
   - The readiness error naming expiry: #3271.
   - Remote-composition renewal: out of scope (operator decision Q1); an unfiled follow-up
     candidate.
   - DSN and database-grant reuse: the unchanged runbook sentence.

### Threat model

- **Boundaries added:**
  - the lane PKI directory, which the issuance script reads (CA and leaf keys);
  - the controller-side chain cross-check, which reads the lane's certificate and key sources
    and prints only verdicts;
  - the playbook's worker-identity handshake, which reads worker files the worker already
    owns.
- **Boundaries widened:** none. The controller-to-host copy and its `no_log` are unchanged.
- **Actors:** the operator, who is trusted; other local accounts on the control host, which are
  untrusted readers; and Git tooling, which can publish a file or delete an ignored one (`git
  add -f`, `git worktree remove`).
- **Controls:**
  - The owner/mode/no-symlink/not-in-a-work-tree directory gate.
  - `umask 077` and `0600` files.
  - Explicit `--key` modes.
  - Temporary-then-verify-then-rename writes.
  - Output limited to names, dates and public-key hashes.
  - The existing `no_log` on both install tasks.
- **Out of scope:** revocation (rotation is the remedy), hardware key storage, a compromised
  control host, and automatic renewal.

## Validation

Focused tests (no live host):

- `tests/scripts/test_authority_pki.py` uses real `openssl` in pytest temporary directories.
  It covers:
  - `ca`/`leaf` `--key new` produce the carrier's key type, extensions and EKUs, and verify
    under the matching purpose;
  - the server SAN and the `server-name` output both equal `authority_server_name(instance)`;
  - the lifetime equals `--days`;
  - file mode `0600`;
  - each refusal: an existing key with `new`, an absent key with `existing`, a subject
    mismatch, a CA expiring before the leaf, non-positive or non-integer `--days`, a server
    without an instance, a client with one, a group-readable directory, a symlinked directory,
    a directory not owned by the caller (when it can run), and a directory inside a Git work
    tree;
  - renewal keeps public keys and changes certificate bytes;
  - an old leaf verifies against a re-issued `ca.pem`;
  - a failed issuance leaves the previous `NAME.pem` unchanged;
  - no output line contains `PRIVATE KEY`.
- `tests/deploy/test_live_worker_provisioning.py`:
  - the carrier calls the script with `--days 1` for the CA and three leaves and no longer
    contains its own `openssl req -x509`;
  - both moved task files keep the exact task names, destinations, owners, modes and `no_log`;
  - `main.yml` imports them at the original positions.
- The existing text-scanning tests that read `main.yml` for these tasks must be repointed at
  the imported files. Their assertions are kept, and the ordered-position check follows the
  import lines. The tests are `tests/deploy/test_live_worker_provisioning.py:776-797` (the
  ordered `tasks.index("- name: Install external-boot authority credentials")`),
  `tests/deploy/test_live_worker_provisioning.py:1035-1050` (`register:
  live_vm_host_authority_credentials` in `main.yml`), and
  `tests/deploy/test_provider_authority_local_provisioning.py:836-844`. The implementation
  first searches `tests/deploy` with `rg` for every read of `MAIN_TASKS` or `main.yml` that
  touches moved text, and repoints each one.
- `ansible-playbook --list-tasks` on the renewal playbook (the role entries are static, so the
  listing is complete) covers:
  - the order: assertions, preflight, chain cross-check, revision check, authority files,
    reset-failed, flush, conditional restart, worker files, then verification;
  - no repository checkout, venv, unit, template or package task.
- A static YAML assertion on the playbook: the `local_worker_host` `import_role` carries
  `when: false`. `--list-tasks` cannot show conditions.
- A local play over `localhost` that imports the renewal playbook's precondition tasks proves
  that an inventory override of `live_vm_host_authority_client_group` wins over the role
  default.
- A local Ansible harness runs the chain cross-check against generated certificates. It is
  wired into `just test-ansible` like #3278's `run-authority-tls-expiry.py`. Its cases:
  - a matching set passes;
  - a server certificate from another CA is refused, with the credential named;
  - a worker client certificate from another CA is refused;
  - a key that does not match its certificate is refused;
  - no key bytes appear in the output.
- `deploy/ansible/tests/run-local-worker-host.py` and the runner-task fixture: regenerate the
  fixture, never hand-edit it, and expect the task list to be unchanged because the imports
  are static.
- `ansible-playbook --syntax-check` of the renewal playbook within `just lint-ansible` or
  `just test-ansible`, whichever already enumerates playbooks.
- Controlled faults (checked red, then reverted):
  - drop the CA-outlives-leaf check;
  - let `--key existing` create a key;
  - move the worker-file include before the flush;
  - drop the conditional restart, which makes a rerun after an interrupted copy leave the
    authority on its old certificate; the `--list-tasks` order test and the conditional's
    static test must go red;
  - make the Git check fail open.

Live proof (needs a separate operator grant after hosted CI is green; operator decision Q4):

- On the retained local lane, after this change is installed at that exact revision:
  1. Issue renewal certificates with operator-chosen lifetimes.
  2. Run the playbook.
  3. Observe:
     - a new authority InvocationID, `active/running`, and no further restarts after a soak of
       at least 65 seconds;
     - the worker handshake succeeding, with the served fingerprint equal to the installed
       file's;
     - one real worker-to-authority request succeeding through the fixed worker, for example
       the first authority-backed operation of the downstream carrier;
     - checkend passing on all six installed certificate files;
     - fixed-worker InvocationIDs unchanged;
     - `/readyz` unchanged.
- Negative arm, no host change: a source inside #3272's margin is refused at preflight.
- Carrier arm: one `prove-external-boot-authority-host.sh` run on a disposable carrier host
  proves the refactored issuance end to end.

## Operator decisions (2026-10-19)

The campaign root relayed these decisions from the design gate. They are recorded in the
issue's latest `WORK:SCOPE` annotation.

- The design and the operator-facing interface are approved as presented. That covers the
  `authority-pki.sh` commands and flags, `renew-local-authority-tls.yml`, the playbook-level
  chain check, and ADR-0803.
- **Q1.** Renewal of the dedicated remote composition (`provider_authority_host` and the
  worker's `remote-authority` client files) is out of scope. It is an unfiled follow-up
  candidate.
- **Q2.** The runbook example uses 30-day leaves under a 365-day CA.
- **Q3.** The playbook requires a clean controller checkout whose `HEAD` equals the installed
  revision. A lane installed before this change must be reprovisioned once, at a revision that
  contains the playbook, before it can be renewed this way.
- **Q4.** The live arms are decided once the PR's hosted CI is green. No host access until then.
- **Q5.** Completion criteria 4 and 5 are amended to "the authority restarts; the fixed worker
  needs no restart". The fixed worker is not restarted. Source shows that it re-reads its TLS
  files on every request: `providers/assembly/authority.py:48-50`,
  `network_client.py:38-65`, and `security/secrets/secrets.py:87`. An out-of-band worker
  restart is also a known way to wedge a slot.
