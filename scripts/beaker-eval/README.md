# Beaker evaluation probes (issue #1792)

Scripts for evaluating [Beaker](https://github.com/beaker-project/beaker) as the M4
provisioning control plane, using only its **system-level HTTP API** — never the
job/recipe scheduler. That distinction is the point of the evaluation: used at the system
level Beaker sits underneath KDIVE's Allocation/System/Run planes; used via the scheduler
it competes with them.

These are evaluation tooling, not product code. Issue #1792 produces findings, not a
KDIVE integration.

Stage 1 ran against a local x86_64 lab and is
[recorded on the issue](https://github.com/randomparity/kdive/issues/1792). These scripts
exist for stage 2, against the ppc64le PowerVM fleet.

## What stage 1 already settled

Arch-independent results that need no re-testing:

| Question | Result |
| --- | --- |
| Q1 provision without the harness | **Yes.** The harness block in `snippets/rhts_post` is gated on `{% if recipe %}`; a scheduler-free install renders none of it. |
| Q4 pool access policy | **Yes.** Minimum grant set is `view`, `view_power`, `reserve`, `control_system`, `loan_self`. A system also needs its `active_access_policy` repointed at the pool — adding it to the pool alone grants nothing. |
| Q5 reservation vs loan | **Not alternatives.** A manual reservation on an `Automated` system requires a loan first. Neither has an expiry, and nothing reaps either — a dead worker leaks its hold permanently. |
| Q6 command states | `Queued` / `Running` / `Completed` / `Failed` / `Aborted`. No idempotency key, and no server-side timeout: a command stays `Running` indefinitely while its lab controller is down. |
| Q2 console | **Not in the HTTP API at all**, at 29.1 or upstream `master`. `has_console` is a hardcoded `False` stub (`# IMPLEMENTME`) and reports nothing. Console capture is conserver's; Beaker's upload half is recipe-scoped. |

Stage 1 also found the bundled `lpar` power script rejects `interrupt` unconditionally,
before invoking `fence_lpar`, byte-identical at 29.1, `beaker-29.3`, `python-3` and
`master`. Since NMI is how a crash dump is forced, confirming what *can* deliver a
diagnostic interrupt on PowerVM is the highest-value remaining item.

## Scripts

| Script | Mutates? | Purpose |
| --- | --- | --- |
| `beaker-api.sh` | — | Sourced helpers: auth, redacted evidence logging, command polling. |
| `fleet-probe.sh` | **No** | Fleet version, lab controllers, registered power types, ppc64le distro trees and systems. Optionally inspects a lab controller over SSH. |
| `fleet-exercise.sh` | **Yes, gated** | loan → reserve → power → interrupt → release against one named system. |

`fleet-exercise.sh` refuses to run without both `--target FQDN` and
`--confirm-destructive`, and aborts if the target is reserved by someone else. It always
releases its hold via an `EXIT` trap.

**Provisioning is deliberately not scripted.** Stage 1 provisioned a disposable VM, but
reinstalling a real LPAR is a larger commitment than a script should make on its own. To
test it, `POST /systems/{fqdn}/installations/` by hand with a `distro_tree` id, holding a
reservation. `beaker-create-kickstart -f FQDN -d TREE_ID -m KS_META` renders the kickstart
that *would* be used without touching hardware, which answers most questions for free.

## Usage

```bash
export BEAKER_URL=https://beaker.example.com/bkr    # no trailing slash
export BEAKER_COOKIE_JAR="$(mktemp)"
export BEAKER_USERNAME=your-account
export BEAKER_PASSWORD_FILE=~/.config/beaker/password   # mode 0600
export BEAKER_EVIDENCE=./stage2-evidence.log            # optional

./fleet-probe.sh --sample-system some-lpar.example.com
./fleet-probe.sh --lab-controller-ssh root@lc.example.com

./fleet-exercise.sh --target some-lpar.example.com --confirm-destructive
```

If your Beaker uses Kerberos or another scheme, authenticate however you normally would
and point `BEAKER_COOKIE_JAR` at the resulting jar; leave `BEAKER_PASSWORD_FILE` unset and
the login step is skipped.

`BEAKER_EVIDENCE` accumulates redacted request/response records suitable for pasting into
the issue. Redaction covers `password`, `root_password`, `power_password` and
`power_passwd` — Beaker echoes power credentials in the system representation, so an
unredacted log should not be shared. **Never commit an evidence log**; treat it as a
secret-bearing artifact regardless.

## API traps worth knowing

Found the hard way in stage 1:

- **Release keys differ.** Reservations take `{"finish_time": "now"}`; loans take
  `{"finish": "now"}`. Sending `finish_time` to the loan endpoint returns HTTP 400
  `Loan durations are not yet configurable` and silently keeps the loan.
- **`ks_meta packages=` drops `--default`.** `%packages` only gets `--default` when
  `not recipe and packages is undefined`, so injecting prerequisites *replaces* the distro
  default set. Include `@core` explicitly or install from `%post`.
- **Collection endpoints return HTML**, not JSON, even with
  `Accept: application/json` — `/distrotrees/`, `/systems/` and `/free/` all do. Only the
  per-system `/systems/{fqdn}/` is JSON. Use the `bkr` client to enumerate anything.
- **Pool access-policy rules 500 on a missing `everybody` key.** `pools.py:399` does
  `everybody=rule['everybody']`, so omitting it raises `KeyError` and returns HTTP 500
  instead of 400. Always send it, including `false`. Present in upstream `master`.
- **`POST /users/` silently discards `password`.** The account is created but cannot be
  logged into; set the password with a follow-up `PATCH /users/{username}`.

The last two are upstream Beaker defects, unreported as of this writing.
