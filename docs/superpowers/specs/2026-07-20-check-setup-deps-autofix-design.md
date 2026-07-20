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

Each tier already accumulates its install package list. On accept, run the distro
install command, prefixing `sudo` when `EUID≠0`. If not root and `sudo` is absent,
report that privileges are required and skip (cannot self-fix). Separate accept per
tier (Required / Recommended / Future) so an operator can install required deps and
decline live-only ones.

### Fix 2 — guestfs venv link (separate prompt)

The guestfs probe already tests `"${PY}" -c "import guestfs"`. When it fails and the
system binding exists but is not importable in the venv, offer a **separate** fix: the
ABI-checked symlink of `guestfs.py` + `libguestfsmod*.so` into the venv site-packages
(same operation as the runbook / Ansible role). No sudo. Skipped when the venv does not
exist yet (e.g. first `just setup` before `uv sync`) or the system/venv Python minor
versions differ (fail loud rather than create a broken link).

### After fixing

Re-verify the Required tier; exit `0` if now satisfied (today it exits `1` on missing
Required deps).

### Native arch visibility

The advisory leads with a positive host-arch line, then lists each supported guest
arch — host/native arch first, then foreign — with its condition. Probes `/dev/kvm`
via `KDIVE_KVM_NODE` (as `check-local-libvirt.sh` does). Existing foreign-arch line
wording is unchanged (additive), so current assertions hold.

```
Host architecture: x86_64 (supported kdive provisioning arch)
  guest arch x86_64:  available natively (KVM-accelerated) via qemu-system-x86_64
  guest arch ppc64le: available via TCG only (qemu-system-ppc64)
```

Native-line variants: native emulator present but `/dev/kvm` inaccessible → "runs
under TCG until KVM is enabled"; native emulator absent → "not available; install
<pkg> for native guests". Unsupported host arch keeps today's single explanatory line.

## Testing

- `-y` path: `apt`/`dnf`/`sudo`/`ln`/python stubs → assert install + symlink ran.
- non-TTY default: assert **no** install/symlink ran (report-only contract).
- guestfs link: ABI-mismatch → fail loud, no symlink; venv absent → skip.
- native advisory: KVM-present, native-qemu-absent, `/dev/kvm`-absent; existing
  foreign-line assertions unchanged.

## Commits

1. Native arch visibility in the advisory (host-first matrix).
2. Opt-in `-y`/interactive package install (per tier, sudo when non-root).
3. Opt-in guestfs venv-symlink fix (separate prompt, ABI-checked).
