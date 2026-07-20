# check-setup-deps.sh: opt-in auto-fix + native arch visibility

Date: 2026-07-20
Status: approved (brainstorm), pending implementation
ADR: [0393](../../adr/0393-check-setup-deps-opt-in-remediation.md)

## Problem

`scripts/check-setup-deps.sh` is report-only ("never installs, never escalates").
An operator on a fresh/failing host must copy-paste the printed `apt install …`
lines by hand, and — for the libguestfs binding — additionally perform the manual
venv symlink documented in `docs/operating/runbooks/four-method-live-run.md` §4b.
Separately, the cross-arch advisory only reports foreign (TCG) arches and skips the
host arch, so it is not obvious which arch runs natively or under what conditions.

## Goals

1. Let the script optionally remediate what it already detects, using data it
   already computes — without breaking its non-interactive/report-only behavior.
2. Show the full provisionable-arch matrix: host arch identified first, then each
   guest arch with its acceleration (native KVM vs TCG) and any missing package.

## Non-goals

- `check-local-libvirt.sh` / Ansible remain the authoritative live-host gate. This
  script stays a setup-time convenience; it does not replace them.
- No new dependency, no config surface beyond the one flag.

## Design

### Contract change (intentional)

The header's "reports only, never installs, never escalates" invariant is replaced
by: **reports by default; remediates only on an explicit per-tier opt-in (interactive
prompt) or `-y`.** Escalation (`sudo`) happens only for an accepted package install.

### Interaction model

| stdin | flag | behavior |
|-------|------|----------|
| TTY | none | after each tier's report, prompt `Fix now? [y/N]` (default No) |
| any | `-y`/`--yes` | auto-accept every offer, no prompts (scripts / `just setup`) |
| non-TTY | none | **no prompt, no fix — identical to today** (protects CI + existing tests) |

Rationale: the non-TTY-without-`-y` default is what keeps every existing behavioral
test and CI invocation unchanged.

### Fix 1 — distro packages (per tier)

Each tier already accumulates its install package list. On accept, run a
**non-interactive** install command — **not** the human-facing hint verbatim (which
omits the auto-confirm flag and would itself prompt on a piped stdin):

| distro | index refresh (fresh host) | install command |
|--------|---------------------------|-----------------|
| debian | `apt-get update` | `apt-get install -y <pkgs>` (`apt-get`, not `apt`, for scripting) |
| fedora | (dnf refreshes within cache validity) | `dnf install -y <pkgs>` |
| arch | none — see caveat | `pacman -S --noconfirm <pkgs>` |
| opensuse | (zypper refreshes implicitly) | `zypper --non-interactive install <pkgs>` |
| unknown | — | no auto-install — report the manual command and skip |

The Debian index refresh matters because the target is a **fresh host**: `apt-get
install` fails with "Unable to locate package" against an empty `/var/lib/apt/lists`.
The refresh is guarded like the install (a refresh failure is reported, not fatal).

**Arch does not auto-refresh** — deliberately. `pacman -Sy <pkg>` refreshes the sync db
but installs only the named package without a full `-Su`, which can pull a package built
against newer shared libraries than what is on disk — the "partial upgrade" state Arch
declares unsupported, and this autofix must never break a working host. `pacman -Syu`
(full upgrade) is the opposite surprise: an unattended dependency check should not upgrade
the whole system. So Arch uses plain `pacman -S --noconfirm <pkgs>`; if it fails against
a stale/empty db, the guarded handler reports "refresh and upgrade first: `pacman -Syu`"
and skips, leaving the operator to run the system upgrade themselves. (See ADR-0393
Considered & rejected.)

**Escalation — scoped to the invocation mode.** When `EUID≠0` the install needs `sudo`,
and the flavor depends on who is driving:

- **Interactive TTY accept** → **plain `sudo`** (a password prompt is the *desired*
  behavior when a human just typed `y`). A `sudo -v` credential pre-flight runs first so
  an auth failure is distinguishable from a package failure (see below).
- **`-y` / non-TTY** → `sudo -n` (never block). A `sudo -n true` pre-flight decides
  up front: on failure (no NOPASSWD / `requiretty`), emit "re-run as root or with
  passwordless sudo to install: `<cmd>`" and skip — never hang. A `-y` provisioning
  caller is thus expected to run as root or with passwordless sudo (stated in the header
  and ADR). If `sudo` is absent entirely, same skip message.

**Routable failure messages.** The credential pre-flight (`sudo -v` interactive /
`sudo -n true` non-interactive) separates the two failure causes the spec must report
distinctly: a pre-flight failure → the "escalation" message and skip; only on pre-flight
success is the install run, and its non-zero exit → the "package set `<pkgs>` failed to
install" message.

**Failure handling under `set -euo pipefail`:** guard every refresh/install (`if !
<cmd>; then report+continue; fi`) so a failure (package absent on this distro/version,
network, disk) does not abort the whole script. Report which package set failed,
continue to remaining tiers and the advisory, and preserve a non-zero exit when the
Required tier is still unsatisfied at the end.

**Only distro-package tiers and the guestfs symlink are auto-fixable.** Manual-hint
tooling — `uv` (`curl … | sh`), `rustc/cargo` (rustup), `just`/`prek` (`uv tool
install`) — is **never** auto-installed, even under `-y`: running a piped-shell
installer unprompted is a supply-chain surprise the ADR's opt-in framing rejects. These
stay report-only, so the Required tier's exit code may remain non-zero when such a tool
(e.g. `uv`) is still missing.

Separate accept per tier (Required / Recommended / Future) so an operator can install
required deps and decline live-only ones.

### Fix 2 — guestfs venv link (separate prompt, after Fix 1)

The guestfs future-tier probe tests `"${PY}" -c "import guestfs"`. The symlink source
files exist only once `python3-guestfs` is installed, so this fix is evaluated **after**
the Future-tier package install (re-probe), and offered only when the binding is present
system-wide but still not importable in the venv. On accept it does the ABI-checked
symlink of `guestfs.py` + `libguestfsmod*.so` into the venv site-packages (same operation
as the runbook §4b / Ansible role). No sudo.

- **Source discovery:** locate the binding at `/usr/lib/python3/dist-packages` (Debian
  dpkg path), falling back to the owning interpreter's `purelib` (Fedora) — the exact
  logic in runbook §4b. If zero `guestfs.py` matches → report "binding not found, is
  python3-guestfs installed?" and skip. If multiple `libguestfsmod*.so` match, link all.
- **`${PY}` must be a real venv first.** `${PY}` resolves to the repo `.venv` python only
  when it exists (else it falls back to **system** python3, and the `.venv` path variable
  is unset). Symlinking into a system `${PY}` is the exact pollution this design rejects,
  so before anything Fix 2 asserts `${PY}` is a venv:
  `"${PY}" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)'`.
  If `${PY}` is not a venv (or does not exist), take the **skip** path — this subsumes
  "venv absent" and handles `KDIVE_PYTHON` correctly (a host-services `KDIVE_PYTHON`
  that points at a real venv passes and is the intended symlink target).
- **Owning interpreter / ABI check:** the binding is built for the **system**
  interpreter (`/usr/bin/python3`, as the Ansible role pins). Compare its `X.Y` minor
  version against the venv `${PY}`'s; on mismatch, fail loud (report the two versions)
  and do **not** create a broken link.
- **Idempotency:** link with `ln -sf` (or skip when the correct link already exists) so a
  re-run after a partial prior attempt does not abort on "File exists".

### After fixing

Re-verification re-runs the affected tier's probes after `hash -r` (bash caches
command lookups, so a just-installed binary is otherwise not found — a false "still
missing"). Re-run all three probe kinds a tier uses: PATH command (`command_exists`),
`pkg-config --exists`, and the venv import. **Re-verification rebuilds the accumulator
arrays** (`*_commands`, `*_packages`, `manual_hints`) from the post-fix probe results —
not just a separate status flag — so the **entire** terminal summary renders from the
current state: the per-tier report, the `manual_hints` "Tooling not provided by your
distribution" block, the "Install the required dependencies … then rerun: just setup"
trailer, and the "Setup dependencies are present." line. Otherwise a just-fixed host
would print the stale "rerun: just setup" trailer immediately before `exit 0` —
contradictory. Exit `0` if the Required tier is now satisfied (today it exits `1` on
missing Required deps); otherwise exit `1`. A fully-fixed Required tier prints no
"Install the required dependencies … rerun" trailer.

### Native arch visibility

The advisory leads with a positive host-arch line, then lists each supported guest
arch — host/native arch first, then foreign — with its condition. Probes `/dev/kvm`
via `KDIVE_KVM_NODE` (as `check-local-libvirt.sh` does). Existing foreign-arch line
wording is unchanged (additive), so current assertions hold.

```
Host architecture: x86_64 (supported kdive provisioning arch)
  guest arch x86_64:  available natively via qemu-system-x86_64 (/dev/kvm accessible — KVM)
  guest arch ppc64le: available via TCG only (qemu-system-ppc64)
```

The native line reports only what the `/dev/kvm` probe proves (device accessible), not
that KVM will accelerate — CPU virt flags / nested-virt can still fail, which
`check-local-libvirt.sh` (the authoritative gate) validates. Native-line variants:
native emulator present but `/dev/kvm` inaccessible → "native emulator present, `/dev/kvm`
not accessible — runs under TCG until KVM is enabled"; native emulator absent → "not
available; install <pkg> for native guests". Unsupported host arch keeps today's single
explanatory line.

## Testing

Stub tests verify **command invocation and output wording**, not a real host bootstrap
(that is a manual/live step the ADR notes). Concretely:

- **non-TTY default:** with no `-y` and no TTY, assert **no** install/symlink/sudo ran —
  the report-only contract every existing test and CI run depends on.
- **`-y` package install:** `apt-get`/`dnf`/`sudo`/python stubs → assert the executed
  command carries the index refresh (`apt-get update` / `pacman -Sy`) and the
  non-interactive flag (`-y`/`--noconfirm`/`--non-interactive`), and `sudo -n` when
  non-root; a `sudo -n true` pre-flight that fails → assert the actionable skip message,
  no hang, and no install attempted.
- **interactive install:** simulated TTY accept, non-root → assert **plain** `sudo`
  (not `sudo -n`) is used, so a password-sudo developer is not refused.
- **routable failures:** sudo pre-flight fail → escalation message; pre-flight ok but
  install stub exits non-zero → "package set failed" message (distinct paths).
- **manual-hint safety:** under `-y`, assert `uv`/rustup/`just`/`prek` piped-shell
  installers are **not** executed (report-only), and Required stays non-zero when `uv`
  is missing.
- **install failure:** an install stub exiting non-zero → assert the script does not
  abort, reports the failed set, and exits non-zero when Required stays unsatisfied.
- **re-verify:** a binary stub that appears only after the install stub runs → assert
  exit `0` (proves `hash -r` + re-probe).
- **guestfs link:** ordered after install in one `-y` run (install then link);
  ABI-mismatch → fail loud, no symlink; pre-existing link → no abort (idempotent);
  glob-miss → skip with message; venv absent → skip.
- **venv identity:** when `${PY}` is a non-venv (system) interpreter, assert Fix 2 takes
  the skip path and creates **no** symlink (never pollutes system site-packages).
- **trailer consistency:** after a `-y` fix that satisfies Required, assert the output
  contains **no** "Install the required dependencies … rerun: just setup" trailer and
  exits `0` (accumulators rebuilt from post-fix state).
- **native advisory:** exact host-first line ordering and per-arch wording for
  KVM-present, native-qemu-absent, and `/dev/kvm`-inaccessible (via `KDIVE_KVM_NODE`);
  existing foreign-line assertions unchanged.

## Commits

1. Native arch visibility in the advisory (host-first matrix).
2. Opt-in `-y`/interactive package install (per tier, sudo when non-root).
3. Opt-in guestfs venv-symlink fix (separate prompt, ABI-checked).
