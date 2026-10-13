# 0770 — The installed fixed-worker authority route is published and forwarded

## Status

Accepted (2026-10-13)

## Context

A fixed worker reaches the local provider authority through five settings,
`KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_{INSTANCE,REQUEST_SOCKET,SERVER_CA_REF,CLIENT_CERT_REF,
CLIENT_KEY_REF}`. The `live_vm_host` role installs the socket and the referenced client files, but
`scripts/live-stack/worker-lifecycle.sh` sends the values in its start request only from its own
environment. No bring-up path supplies them, so after an ordinary stack bring-up the worker has
no route, and every restart drops the exports an operator added by hand (#3215). The libvirt
endpoint had the same shape and was solved by a root-published `/etc/kdive/live-worker-libvirt.env`
(ADR-0659, ADR-0661).

## Decision

With `live_vm_host_worker_authority_enabled`, the role writes `/etc/kdive/live-worker-authority.env`,
root:root `0644`, holding exactly the five `NAME=value` lines from its own variables. It removes
the file when the client is disabled, and authority teardown removes it with the other endpoint
artifacts. `worker-lifecycle.sh start` reads the file when it exists: exact metadata, exactly the
five allowlisted names once each with non-empty values, parsed as literal data. It exports
them into the start request. An environment value that differs from the file is refused before
the request. An absent file changes nothing.

## Consequences

- A standard bring-up and every restart give the fixed workers the installed route, with no
  operator exports.
- Unlike ADR-0661's libvirt rule, a contradicting preset is an error, not a report. To use another
  route, change the role inputs and converge.
- The lifecycle request schema and the witness are unchanged; the route values are still
  validated there.
- The file carries no credential bytes. The registry marks the client-key ref `secret=True` for
  redaction (`src/kdive/providers/external_boot_authority/settings.py`), but its value is a
  root-relative path to a `0440` file under a `0750` root, and that path is the role default.

## Considered & rejected

- **Document manual exports in the lane's backend environment.** verified: issue #3215 records the
  #3184 lane's route gate failing (`installed worker authority route is incomplete`) after a
  bring-up whose backend environment carried no exports. judgment: a hand-maintained copy drifts
  from the role inputs that own the values.
- **Have the lifecycle witness fill the route from installed configuration.** judgment: it widens the
  lifecycle request contract and the root-run witness to supply values the client can already
  read from a root-owned file.
- **Honour a contradicting preset with a report, as ADR-0661 does.** judgment: an overridden
  libvirt endpoint still names a working daemon. An overridden route names an authority identity
  the installed client files do not match, so honouring it only defers the failure to the
  authority.
- **Add the five lines to `/etc/kdive/live-worker-libvirt.env`.** verified:
  `load_published_libvirt_uri` in `scripts/live-stack/libvirt-uri.sh` refuses any file that does
  not hold exactly one `KDIVE_LIBVIRT_URI` line, so every existing reader would fail closed.
