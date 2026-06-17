# Local git-clone build lane (#530)

- **Status:** Accepted
- **Date:** 2026-06-17
- **ADR:** [ADR-0161](../adr/0161-local-git-build-lane.md)
- **Issue:** [#530](https://github.com/randomparity/kdive/issues/530) — "Support Multiple Kernel
  Build Repos"

## Problem

An agent cannot point a kernel build at a custom repository URL on the default build path.
`runs.build` already parses a structured git source (`kernel_source_ref: {"git": {"remote":
…, "ref": …}}`), but host selection routes it **only** to a registered *remote* build host:
`services/runs/build_host_selection.py` rejects a git source on the local `worker-local` host
(`a local build host requires a warm-tree kernel_source_ref, not a git ref`). The local host
mirrors a single pre-staged warm tree (`KDIVE_KERNEL_SRC`) — the "default canonical upstream
repo" — and offers no override.

So forked development forces an operator to stand up and register a separate remote build host
(SSH or ephemeral-libvirt). That is heavy for the common case: a developer who just wants to
build a fork's branch on the default worker.

## Goal

Let the `worker-local` build host clone an agent-supplied git remote + ref into the per-run
build workspace, gated by an operator allowlist (deny by default). A bare-string
`kernel_source_ref` keeps using the warm tree, unchanged. No build-profile schema change.

Non-goals: warm-tree-as-cache overlay for forks; per-project allowlists; admission-time
allowlist enforcement (see "Considered & rejected").

## Design

### Provenance already distinguishes the lanes

`profiles/build.py` parses two `kernel_source_ref` forms and `is_git_source(profile)` already
discriminates them:

- bare string → warm-tree provenance (existing local lane).
- `{"git": {"remote", "ref"}}` → git-clone provenance (today: remote hosts only).

### Admission relaxation (`services/runs/build_host_selection.py`)

The local-host rule inverts: a `worker-local` host accepts **either** warm-tree **or** git
provenance. The remote-host rule (a non-local host requires git) is unchanged. No lease row is
inserted for a local host (local builds remain single-slot, as today).

The admission boundary does **not** consult the allowlist. The allowlist is a worker-process
setting the server cannot see at admission — the same constraint that already pushes
`KDIVE_KERNEL_SRC` validation to build time (see the `workspace.py` comment that the warm-tree
source "can only surface here"). A disallowed remote is therefore admitted and then fails the
`BUILD` job with a clear `CONFIGURATION_ERROR`, consistent with how a warm-tree build fails when
`KDIVE_KERNEL_SRC` is unset.

### Local checkout dispatch (`providers/shared/build_host/workspace.py`)

`real_checkout` branches on `is_git_source(profile)`:

- bare string → `sync_tree(kernel_src, …)` — the existing `rsync -a --delete` of the warm tree.
- git → new `clone_tree(remote, ref, workspace, allowlist, …)`.

Both lanes then run the unchanged `merge_config(fragment_bytes, …)` and optional
`apply_patch(patch_ref, …)`. The allowlist is threaded through `make_checkout(kernel_src,
allowlist, secret_registry)`; `LocalLibvirtBuild.from_env` reads it from the environment.

`clone_tree` mirrors the proven `ShellBuildTransport.clone` recipe (ADR-0154), run as local
subprocesses on the worker:

1. `validate_git_arg(remote)`, `validate_git_arg(ref)` — reject a leading `-` (would parse as a
   git option) or a control character.
2. `remote_allowed(remote, allowlist)` — enforce the allowlist (below). Reject otherwise.
3. `shutil.which("git")` — `MISSING_DEPENDENCY` if git is absent (mirrors `apply_patch`).
4. **Clean the workspace first** (`rmtree(workspace, ignore_errors=True)` then `mkdir`), so a
   retried `run_id` whose best-effort cleanup did not run cannot inherit stale content. The
   warm-tree lane gets this for free from `rsync --delete`; `git init` + `checkout FETCH_HEAD`
   does **not** remove pre-existing untracked files, so the clone lane must do it explicitly.
5. `git init <workspace>` → `INFRASTRUCTURE_FAILURE` on non-zero.
6. `git -C <workspace> fetch --depth 1 <remote> <ref>` → `CONFIGURATION_ERROR` on non-zero.
7. `git -C <workspace> rev-parse --verify --quiet FETCH_HEAD` → `TRANSPORT_FAILURE` if absent
   (a fetch whose failure was masked to exit 0 leaves no `FETCH_HEAD`; surface the fetch's own
   stderr rather than a misleading later pathspec error — ADR-0154).
8. `git -C <workspace> checkout FETCH_HEAD` → `CONFIGURATION_ERROR` on non-zero.

`ref` must be a server-advertised tag or branch. A bare commit SHA is **not** guaranteed
fetchable by a shallow `fetch <sha>` (most servers reject it unless `uploadpack.allowAnySHA1InWant`
is set), and a SHA that the server will not serve surfaces as the normal `git fetch` failure
above — the clone lane does not special-case it.

All captured stderr passes through `redacted_tail` before it reaches any error detail. The
remote URL and ref are never echoed into error details (a remote may embed a credential in its
userinfo component, e.g. a token before the `@` host separator).

#### Git egress hardening (the allowlist gates the string, not the connection)

`remote_allowed` validates the *submitted* URL, but a bare `git fetch` would not stay confined
to it: git follows the server's HTTP redirect on the initial request (`http.followRedirects`
defaults to `initial`) and applies any ambient `url.<base>.insteadOf` rewrites from system/global
config — either of which can redirect or rewrite an allowlisted host to an internal target
*after* the allowlist check passed. The clone lane therefore runs every git invocation with the
ambient escape hatches closed:

- `-c http.followRedirects=false` — a redirect is an error, not a silent hop off the allowlisted
  host.
- `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null` — no system/global gitconfig, so an
  `insteadOf` rule cannot rewrite the validated remote.
- `-c protocol.allow=never` plus per-protocol `-c protocol.{https,git,ssh}.allow=always`, and
  `GIT_PROTOCOL_FROM_USER=0` — only the three vetted transports run, so the scheme gate cannot be
  sidestepped by a helper transport (`ext::`, `fd::`).

These are part of the egress contract, not an implementation nicety: without them the allowlist
does not actually bound where the worker connects.

### Egress control — allowlist (deny by default)

New worker setting:

```
KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST   # comma-separated; group "build"; processes={worker}
```

Empty or unset ⇒ no remote is permitted ⇒ the local git lane is effectively off. This is the
deny-by-default posture: an operator opts in by listing the hosts (or host+path prefixes) they
trust the worker to clone from.

`remote_allowed(remote, allowlist)`:

1. **Parse** the remote into `(scheme, host, path)`. Two forms:
   - URL form (`https://host/path`, `ssh://host/path`, `git://host/path`) via `urlsplit`.
   - scp-like ssh form (`git@host:path` / `host:path`) normalized to `scheme="ssh"`,
     `host=host`, `path="/" + path`.
2. **Scheme gate** — only `https`, `ssh`, `git` (and the scp-like ssh form) are eligible. Any
   other scheme (`file`, `http`, `ftp`, …) is rejected. `file://` is rejected explicitly so a
   local path cannot be smuggled in.
3. **Host gate** — the parsed host (lowercased, port and userinfo stripped) must **equal** an
   allowlist entry's host (case-insensitive). A substring/prefix match is *not* used, so
   `github.com` does not admit `github.com.evil.com`.
4. **Path gate** — if the entry carries a path (`github.com/myorg`), the remote's path must
   equal `/myorg` or start with `/myorg/` (a `/` boundary). `github.com/myorg` does not admit
   `github.com/myorg-evil`. An entry with no path (`github.com`) admits any path on that host.

A rejection is `CONFIGURATION_ERROR` whose detail names the setting and the staging doc but
**not** the submitted remote. The detail distinguishes the two reasons an agent most needs to
tell apart, without echoing the URL: *lane disabled* (the allowlist is empty/unset — the
operator has not enabled local git builds at all) versus *remote not allowlisted* (the lane is
on, but this host/path is not permitted). The first tells the agent to ask the operator to
enable the lane; the second tells it to pick an allowlisted remote.

Discoverability (an agent reading the configured allowlist before it submits, rather than
learning at build time) is **out of scope** here and tracked as a follow-up; this spec only
makes the build-time rejection self-explanatory.

### Code organization

- New `providers/shared/build_host/git_source.py`: `parse_remote()`, `remote_allowed()`,
  `local_build_remote_allowlist_from_env()`, and `validate_git_arg()` relocated here as the
  shared validator. `shell_transport.py` imports it instead of holding its own copy (two call
  sites — the remote transport and this local lane — justify one shared home).
- `workspace.py`: `clone_tree()` + provenance dispatch in `real_checkout`; allowlist threaded
  through `make_checkout`.
- `local_libvirt/build.py`: `from_env` reads the allowlist and passes it to `make_checkout`.
- `build_host_selection.py`: relax the local-host rule.
- `config/core_settings.py`: declare and register the setting.

## Error contract

| Condition | `ErrorCategory` |
|---|---|
| git binary absent on worker | `MISSING_DEPENDENCY` |
| `remote`/`ref` starts with `-` or holds a control char | `CONFIGURATION_ERROR` |
| remote not on the allowlist (incl. empty allowlist, bad scheme, `file://`) | `CONFIGURATION_ERROR` |
| `git init` non-zero | `INFRASTRUCTURE_FAILURE` |
| `git fetch` non-zero | `CONFIGURATION_ERROR` |
| fetch reported success but no `FETCH_HEAD` | `TRANSPORT_FAILURE` |
| `git checkout FETCH_HEAD` non-zero | `CONFIGURATION_ERROR` |

## Testing

TDD; the live toolchain is not needed for the orchestration/contract tests (real network clone
and `make` stay under the `live_vm` gate):

- **Allowlist matching** table: exact host match, host+path-prefix boundary, case-insensitivity,
  `github.com.evil.com` and `github.com/myorg-evil` rejection, scp-like form, scheme rejection,
  `file://` rejection, empty allowlist ⇒ every remote denied.
- **Provenance dispatch**: a git profile invokes `clone_tree` (injected runner), a bare string
  invokes `sync_tree`; existing warm-tree tests stay green.
- **Admission**: git + local host now admitted; warm-tree + remote host still rejected; git +
  remote host still admitted.
- **Clone error mapping** via an injected subprocess seam: git missing, `init`/`fetch`/`checkout`
  non-zero, missing `FETCH_HEAD`.
- **Egress hardening**: the git argv/env the clone lane builds carries
  `http.followRedirects=false`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, and the
  protocol-allow restriction (asserted on the injected runner's captured argv/env, no network).
- **Clean workspace**: clone_tree removes pre-existing workspace content before `git init` (a
  stale file planted in the workspace is gone after the clone seam runs).
- **Rejection reason**: an empty allowlist yields the *lane-disabled* detail; a non-matching
  remote against a non-empty allowlist yields the *remote-not-allowlisted* detail.
- **Redaction**: a disallowed-remote error and a clone-failure error never contain the remote
  URL, credentials, or ref.

## Docs

- ADR-0161 (decision + rejected alternatives).
- `mcp/resources/_content/build-source-staging.md`: the two-lane table gains a git-on-local
  (allowlisted) row; the "a bare string never overrides `KDIVE_KERNEL_SRC`" caveat stays; the
  `_BUILD_LANE_GUIDANCE` message in `workspace.py` and the config reference gain the new setting.

## Considered & rejected

- **Scheme/host validation only (no operator allowlist).** Blocks `file://` and obvious
  internal targets but still lets the worker clone any reachable public host an agent names.
  Rejected: the worker shares the control plane; deny-by-default with an explicit operator
  opt-in matches the repo's gate philosophy (`security/gate.py`, `KDIVE_*` opt-ins).
- **No gate (parity with the remote-host path).** The remote-host path clones arbitrary URLs,
  but on an isolated build host. The worker is not isolated, so URL parity would widen SSRF
  exposure. Rejected.
- **Warm-tree overlay (fetch the fork ref on top of the warm tree).** Faster (warm tree caches
  upstream objects) and natural for forks, but requires the warm tree to be a real git repo (it
  may be an unpacked tarball) and assumes the fork shares upstream history. More moving parts for
  a narrower case. Rejected for now; the clean-clone lane is simpler and self-contained.
- **Admission-time allowlist enforcement.** Would fail fast, but the allowlist is worker-only
  config invisible to the server at admission (like `KDIVE_KERNEL_SRC`). Rejected to avoid
  leaking worker config into the server and to stay consistent with the existing warm-tree
  failure timing.
