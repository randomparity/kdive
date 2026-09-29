# 0708 — Authority retirement restarts a user manager that outlived its memlock drop-in

## Status

Accepted (2026-09-28)

## Context

The ppc64le unblock (#2767, PR #2886) installs
`/etc/systemd/system/user@<uid>.service.d/kdive-memlock.conf` (`LimitMEMLOCK=infinity`) for the
authority account and restarts `user@<uid>.service` so the running manager takes the ceiling.
Both retirement paths — `deploy/ansible/playbooks/authority_host_teardown.yml` and the
`provider_authority_host` role's `tasks/disable.yml` — delete the drop-in and only reload systemd.
A reload changes the unit's configured limit, not the running manager's: the authority account
lingers, so its manager keeps an unlimited locked-memory ceiling until the next restart (#2891).

## Decision

After the existing service stops and a system reload, each retirement path reads the authority
user manager's `MainPID`, its running hard limit (`prlimit --memlock --output=HARD`), and its
configured limit (`systemctl show --property=LimitMEMLOCK --value`). It restarts
`user@<uid>.service` only when a manager is running (`MainPID` not `0`), its running hard limit is
`unlimited`, and its configured limit is no longer `infinity`. Absent account or absent manager
skips all of it. The role path runs the restart before it disables linger.

## Consequences

- A restart stops every unit that manager runs, including anything the earlier stops left. It is
  ordered after those stops and their assertions, so it only ends what retirement already ends.
- The condition reads live state, so a re-run after an interrupted retirement still restarts, and a
  converged host never restarts again.
- A host whose own configuration grants the manager `LimitMEMLOCK=infinity` (a
  `DefaultLimitMEMLOCK` or another drop-in) keeps it: the configured limit stays `infinity`.

## Considered & rejected

- **Leave the ceiling until the next reboot and document it.** judgment: a lingering account's
  manager can run for the host's whole uptime, which is the gap #2891 names.
- **Restart from a handler notified by the drop-in removal.** verified: the removal task reports
  `ok`, not `changed`, once the file is gone, so a re-run after a retirement interrupted between
  removal and restart never notifies; `provider_authority_host/tasks/libvirt.yml` keys its install
  restart on the running limit for the same reason (commit `a699f2774`).
- **Restart whenever the running limit is unlimited.** verified: `systemd-system.conf(5)`
  `DefaultLimitMEMLOCK` can set `infinity` host-wide; the manager would then restart on every run.
- **Stop the user manager instead.** judgment: leaves a lingering account without its manager
  until reboot, which is linger behaviour the operator owns (#2891 exclusions).
