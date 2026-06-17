# ADR 0157 — Local git-clone build lane with operator remote allowlist

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0029](0029-build-plane-local-make.md) (the
  `kernel_source_ref` provenance forms), [ADR-0101](0101-local-libvirt-remote-build-host.md)
  (the local builder and its checkout seam), [ADR-0154](0154-clone-verify-fetch-head.md)
  (the `git init` + shallow fetch + `FETCH_HEAD` verify + checkout recipe reused here),
  [ADR-0019](0019-tool-response-envelope.md) (the response envelope / error taxonomy).
- **Spec:** [`../design/local-git-build-lane.md`](../design/local-git-build-lane.md)

## Context

`runs.build` already accepts a structured git source (`kernel_source_ref: {"git": {"remote":
…, "ref": …}}`), but host selection (`services/runs/build_host_selection.py`) routes a git
source only to a registered *remote* build host and rejects it on the local `worker-local`
host. The local host mirrors one pre-staged warm tree (`KDIVE_KERNEL_SRC`) — the default
canonical repo — with no per-build override. Forked development (issue #530) therefore forces
an operator to register a separate remote build host, which is heavy for the common case of
building a fork's branch on the default worker.

The worker runs in the control plane (unlike an isolated remote build host), so letting it
clone an agent-chosen URL is an SSRF/egress surface: an arbitrary remote could be an internal
git server, a link-local metadata endpoint, or a `file://` path.

## Decision

1. **Add a local git-clone lane.** The `worker-local` build host accepts either warm-tree
   (bare-string `kernel_source_ref`) or git (`{"git": …}`) provenance. Admission inverts the
   local-host rule; the remote-host "requires git" rule is unchanged. No schema change.

2. **The local checkout seam dispatches on provenance.** `real_checkout` runs the existing
   `sync_tree` for a bare string and a new `clone_tree` for a git source, then the unchanged
   `merge_config` + optional `apply_patch`. `clone_tree` reuses ADR-0154's recipe (`git init`
   + `fetch --depth 1` + verify `FETCH_HEAD` + `checkout`) as local subprocesses, with the same
   error taxonomy and stderr redaction. It cleans the workspace before `git init` (the warm-tree
   lane gets this from `rsync --delete`; `git init` + checkout does not).

   Because the worker runs in the control plane, `clone_tree` closes git's ambient escape
   hatches so the allowlist actually bounds the connection: `http.followRedirects=false` (a
   server redirect off the allowlisted host is an error, not a silent hop), `GIT_CONFIG_NOSYSTEM`
   + `GIT_CONFIG_GLOBAL=/dev/null` (no `insteadOf` rewrite of the validated remote), and a
   protocol-allow restriction to the three vetted transports.

3. **Gate the lane with an operator allowlist, deny by default.** A new worker setting
   `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST` (comma-separated) lists trusted hosts or host+path
   prefixes. Empty/unset ⇒ no remote permitted ⇒ the lane is off. Matching parses the remote,
   admits only `https`/`ssh`/`git` schemes (incl. scp-like ssh), requires an exact
   case-insensitive host match, and — when an entry carries a path — a `/`-boundary path-prefix
   match. A rejection is `CONFIGURATION_ERROR` that names the setting but not the submitted
   remote.

4. **Enforce the allowlist at build time on the worker, not at admission.** The allowlist is a
   worker-process setting the server cannot see at admission (the same constraint as
   `KDIVE_KERNEL_SRC`). A disallowed remote is admitted and then fails the `BUILD` job, matching
   the existing warm-tree-unset failure timing.

## Consequences

- Forked development works on the default build path once an operator allowlists the fork's
  host — no remote build host required.
- The worker gains a bounded, operator-controlled egress surface. With no allowlist configured
  the behavior is unchanged (git + local still fails), so the default deployment is unaffected.
- `validate_git_arg` moves to a shared `git_source.py` imported by both the remote transport and
  the local lane; the clone recipe and error taxonomy are shared, not forked.
- A disallowed or malformed remote costs an admitted-then-failed `BUILD` job rather than an
  admission-time rejection; the failure detail points at the setting and the staging doc.

## Considered & rejected

- **Scheme/host validation only (no allowlist).** Still lets the worker clone any reachable
  public host an agent names; weaker than deny-by-default for a control-plane process.
- **No gate (URL parity with the remote-host path).** The remote path clones on an isolated
  host; the worker is not isolated, so parity would widen SSRF exposure.
- **Warm-tree overlay (fetch the fork ref onto the warm tree).** Faster but requires the warm
  tree to be a real git repo and assumes shared upstream history; more moving parts for a
  narrower case.
- **Admission-time allowlist enforcement.** Would fail fast but requires leaking worker-only
  config to the server; inconsistent with the existing `KDIVE_KERNEL_SRC` timing.
