# Hot-plug runbook: remote provider SSH prerequisite (#3738)

## Problem

The hot-plug section of `live-testing.md` sends the remote arm to the remote runbook's section 7
but never names `REMOTE_PROVIDER_SSH`. The remote cells read the provider's domain XML and
volumes over the test's own `qemu+ssh` connection (`remote_lifecycle.py`, `remote_host`), so
without it each cell records `blocked`. The section also has no remote command line.

## Scope

In: that section of `docs/operating/runbooks/live-testing.md` only, using placeholders.
Out: test or harness changes; the recovery-configuration proof (#3719, merged).

The section states only what the remote hot-plug arm reads: `REMOTE_PROVIDER_SSH`,
`KDIVE_SYSTEMS_TOML` (one `[[remote_libvirt]]` instance), `KDIVE_DATABASE_URL`, and the stack
variables the demo scripts already provide. It records that no kernel is booted, so
`KDIVE_FIXTURE_ROOT` and `KDIVE_S3_ENDPOINT_URL` are not needed.

### Failure model

The documented command is not re-run for this change (no live run granted). The variable list is
read from the test code, so a later harness change can drift from it; no gate checks it.

## Success

The section names `REMOTE_PROVIDER_SSH` with its requirements, links the section 7 definition,
and gives a `-k remote-libvirt` command with placeholders. No host, address or user name appears.

## Validation

- `task-test-not-applicable`: documentation only; no behavior changes.
- `task-test-not-applicable`: the runbook command is not run; links and paths are checked by the
  docs gates.
