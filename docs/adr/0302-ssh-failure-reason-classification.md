# ADR-0302: classify ssh stderr into a closed failure-reason vocabulary (#1008)

- Status: Accepted
- Date: 2026-07-02
- Builds on [ADR-0271](0271-system-direct-ssh-access.md) (`authorize_ssh_key`, the direct-SSH
  handler), [ADR-0289](0289-per-system-ssh-bootstrap-key.md) (the per-System bootstrap key and the
  bounded connect-retry it motivated), [ADR-0240](0240-live-drgn-script-introspection.md) (the
  drgn-live SSH path), and [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the `Redactor`
  that the failure-context path already applies). Sibling of the fast-fail work (#1012) and the
  reachability-verdict work (#1011).

## Context

When `systems.authorize_ssh_key` fails, the worker preserves only ssh's **exit code**. ssh reports
connection-refused, banner-exchange timeout, host-key mismatch, and auth rejection all as exit
`255`, so every distinct failure mode collapses to one opaque number. `jobs.get`/`jobs.wait` on a
failed authorize return exactly `{"failure_message": "…failed in the guest",
"failure_detail_exit_status": "255"}`.

The one string that disambiguates those modes — ssh's stderr — **is** captured. `run_ssh_with_retry`
(`ssh_connect_retry.py`) already reads it to decide retryability, matching phrases like
`connection refused`, `too many authentication failures`, and `host key`. But the caller's
`CategorizedError.details` keeps only `exit_status` and the stderr is discarded.

The consequence (from the black-box agent review, `BLACK_BOX_REVIEW.md` Finding 1): an agent
blocked by an SSH failure could only report "255 / ssh failed in the guest". It could not
root-cause, and neither could a developer reading the job record. The contrast is
`check_ssh_reachable`, which correctly named "no SSH banner" in seconds. The durable defect is not
that SSH breaks — SSH works on current `main`, verified live — it is that **when it fails, the
failure is undiagnosable.**

## Decision

Add a single, shared classifier in `ssh_connect_retry.py` (the module that already owns the ssh
stderr phrase tables) and route the failing call sites through it.

1. **Closed reason vocabulary.** `classify_ssh_failure(returncode, stderr)` returns one of a
   fixed `SshFailureReason` literal set — `connection_refused | banner_timeout | unreachable |
   auth_rejected | host_key_mismatch | remote_command_failed | unknown`. It never returns
   free-form text, so the reason itself cannot leak a secret or a hostname. A non-`255` exit is
   always `remote_command_failed` (ssh connected; the remote command exited non-zero); a `255`
   exit is classified by ordered stderr-phrase match (fatal auth/host-key phrases first, so they
   win over any co-occurring transient phrase), falling back to `unknown`.

2. **Single phrase table.** The old two-bucket `_STARTING_MARKERS`/`_FATAL_MARKERS` split is
   replaced by one ordered `(phrase, reason)` table. Retryability is derived from it:
   `is_sshd_starting` now returns whether the classified reason is in the retryable set
   (`connection_refused`, `banner_timeout`, `unreachable`). This preserves the existing retry
   behavior exactly — no phrase list is duplicated.

3. **Leak-safe details helper.** `ssh_failure_details(returncode, stderr)` returns
   `{"exit_status", "reason", "stderr_tail"}`. `stderr_tail` is the last `512` chars of ssh's
   stderr, length-capped at the source and redacted downstream unchanged by the worker's
   `_failure_context` → `Redactor` path (ADR-0027). The closed `reason` is the leak-free primary
   signal; the tail is best-effort context.

4. **Route the call sites.** `authorize_ssh_key`'s `_real_ssh_exec` and the two drgn-live SSH
   exec sites (`introspect.py`) swap their `details={"exit_status": …}` for
   `details=ssh_failure_details(…)`. `exit_status` is retained; `reason` and `stderr_tail` are
   purely additive. No message, category, migration, schema, RBAC, or config change.

## Consequences

- A forced banner-timeout authorize failure now surfaces `failure_detail_reason` (e.g.
  `banner_timeout`) plus a redacted `failure_detail_stderr_tail`, not just `255`. The agent and
  the developer can name the layer.
- The vocabulary is closed and validated by unit tests that drive each stderr shape. Adding a new
  reason is a one-line table edit plus a test.
- The stderr tail carries a residual leakage surface (e.g. a hostname ssh prints). It relies on
  the same `Redactor` every other failure detail already passes through, and is length-capped; the
  closed `reason` remains the leak-free field an agent should key on. Host-key pinning and
  endpoint redaction are named future hardening, unchanged by this ADR.
