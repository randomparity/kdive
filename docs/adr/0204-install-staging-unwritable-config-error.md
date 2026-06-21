# ADR 0204 — Unwritable install-staging root is a configuration_error, not infrastructure_failure

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** kdive maintainers

## Context

`LocalLibvirtInstall.install` (ADR-0030) stages the kernel and optional initrd to a
per-Run host path under `KDIVE_INSTALL_STAGING` (default `/var/lib/kdive/install`), created
with `mkdir(parents=True, exist_ok=True)`. The default's parent (`/var/lib/kdive`) is
root-owned, so on a source-checkout host the worker (running as the operator user) cannot
create the staging dir: the `mkdir` raises `PermissionError: [Errno 13]`.

Today every `OSError` on that `mkdir` is mapped to `INFRASTRUCTURE_FAILURE` with a message
of "failed to create the per-Run staging directory". That is the wrong tier:
`infrastructure_failure` reads as a transient host fault, but an unwritable staging root
never becomes writable on its own — it is an operator misconfiguration that mislabels the
fault for whoever (operator or observability) triages it. Worse, the surfaced detail names
only the attempted leaf path and gives no remedy, so the #115 live verification hit an
opaque failure and the operator had to guess that repointing `KDIVE_INSTALL_STAGING` at an
already-prepared writable directory was the fix.

A `PermissionError` here is categorically a configuration problem; other `OSError`s on the
same `mkdir` (e.g. `ENOSPC` disk-full, a file in the path, a read-only filesystem) are not
necessarily operator-fixable config and stay infrastructure-tier.

## Decision

Split the `mkdir` failure mapping by errno:

- A `PermissionError` (the staging root is not writable by the run user) becomes a
  `CONFIGURATION_ERROR`. Its `details` name the env var (`KDIVE_INSTALL_STAGING`), the
  configured staging **root** that was tried, the per-Run path, and a `remedy` string telling
  the operator to pre-create the root writable by the run user and, on SELinux hosts, give it
  the `virt_image_t` label (the standard libvirt-image label, consistent with ADR-0052).
- Every other `OSError` keeps the existing `INFRASTRUCTURE_FAILURE` mapping and detail
  (`op=mkdir`, `dest=<per-Run path>`), so disk-full / path-in-the-way / read-only-fs faults
  are unchanged.

The `KDIVE_INSTALL_STAGING` help text gains a one-line writability note so the failure's
remedy is also discoverable in `config.md` before an install is ever run.

## Consequences

- An unwritable staging root now self-describes: category, env var, path, and fix, so the
  operator does not have to reverse-engineer it from a bare `PermissionError`.
- The fault carries the right tier for triage: `configuration_error` points the operator at
  the env var instead of suggesting a transient host fault to retry. (Job requeue is gated on
  the `terminal` flag and `max_attempts`, not on the category, so the retry count itself is
  unchanged by this change — re-tiering is a labeling/actionability fix, not a dead-letter
  change. Making the staging fault `terminal` is a separate decision left out of scope.)
- The discrimination is errno-based (`PermissionError` only), so the pre-existing
  file-in-the-path test stays `INFRASTRUCTURE_FAILURE`; only the writability fault re-tiers.
- The default `KDIVE_INSTALL_STAGING` is unchanged — re-homing the default off a root-owned
  parent (and host-script create-or-verify) is left to #658; this change makes the existing
  default's failure actionable rather than papering over it.

## Considered & rejected

- **Map every `mkdir` OSError to `configuration_error`.** Over-broad: a disk-full or
  read-only-fs fault during staging is infrastructure, not a config typo, and re-tiering it
  would mislead the operator toward the env var.
- **Change the default to a writable path (e.g. under `$HOME`/tmp).** A multi-process,
  packaged deployment wants a stable system path; silently relocating it risks staging
  artifacts somewhere unexpected and is out of this change's scope (owned by #658).
- **Pre-create the dir from the provider at startup.** The provider is DB-free and seam-
  injected; creating system directories as a side effect of building it is the host setup
  scripts' job (#658), not the install plane's.
- **Leave it `infrastructure_failure` and only enrich the message.** The retry semantics are
  wrong for a permission fault; the category, not just the prose, has to change.
