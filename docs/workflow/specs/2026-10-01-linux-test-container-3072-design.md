# Native-arch Linux test container for macOS (#3072)

Issue: [#3072](https://github.com/randomparity/kdive/issues/3072). Decision record:
[ADR-0717](../../adr/0717-native-arch-linux-test-container-and-aarch64-capture-filter.md), which
amends the architecture claim of
[ADR-0558](../../adr/0558-supervised-capture-operation-processes.md).

## Problem

On macOS, `just test` fails hundreds of tests for host reasons, so it cannot show a regression.
The manual container recipe in
`docs/solutions/2026-09-25-macos-linux-container-test-baseline.md` has no `just` recipe and a
baseline of 50 failures. Container runs of `main` at `2c4df02fd` in this cycle (Docker Desktop
29.8.1, Apple silicon) gave these causes:

| Failures | Cause | Arch |
|---|---|---|
| 36 capture tests | `seccomp_load failed: 125` under Rosetta | amd64 |
| about 49 capture tests + 37 errors | `unsupported audit architecture: aarch64` (`sandbox.py:87`); `unsupported architecture: aarch64` (`build-capture-bootstrap-manifest.py:109`) | arm64 |
| 11 `test_live_stack_scripts.py` | `env: '/work/src/.venv/bin/python': No such file or directory`: the recipe put the venv at `/venv` | both |
| 1 `test_bounded_command_timeout_terminates_and_reaps_the_process_group` | no init process reaps the orphan, so it stays as a zombie | both |
| 2 `test_unexecutable_depmod_is_missing_dependency` | Rosetta exec errors | amd64 |
| 1 `test_runtime_verifier_rejects_new_higher_priority_hwcaps_selection` | the test reads a legacy hwcaps line from `ld.so --help` as a `glibc-hwcaps` entry | arm64 |

A throwaway prototype on arm64 (in-clone `.venv`, `--init`, the aarch64 changes below) ran the
affected files with 2 failures left: the hwcaps test and the process-group test. A second run
with `--init` passed the process-group test.

## Scope

The charter in the `WORK:SCOPE` comment on #3072 governs. In summary:

1. **Recipe.** `just test-linux [sha] [ARGS...]` in the `justfile`. `ARGS` (for example test
   paths) go to `just test` unchanged, with the same shell splitting as `test-verbose`. It resolves `sha` (default `HEAD`) to a
   full commit on the host, warns when `git status --porcelain` is not empty, builds
   `tests/container/Dockerfile`, and runs the image with:
   - `--rm --init`;
   - the git common directory (`git rev-parse --path-format=absolute --git-common-dir`) at
     `/repo`, read-only;
   - the engine socket at `/var/run/docker.sock`;
   - the named volume `kdive-test-linux-uv-cache` at `/home/tester/.cache/uv`;
   - `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal` and
     `--add-host host.docker.internal:host-gateway`.

   `CONTAINER_ENGINE` is `docker` (default) or `podman`. Any other value exits 2 with a message.
   The Docker socket is `/var/run/docker.sock`; the Podman socket is
   `podman info --format '{{.Host.RemoteSocket.Path}}'`. `sha` reaches the shell as an exported
   recipe parameter, not as interpolated source.
2. **Image.** `tests/container/Dockerfile` copies `/uv` and `/uvx` from the root Dockerfile's
   `ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4…` pin onto its
   `python:3.14.6-slim-bookworm@sha256:86f9…` pin. Both are multi-arch indexes (amd64, arm64).
   It installs `build-essential`, `ca-certificates`, `curl`, `git`, `gnupg`, `iproute2`,
   `libvirt-dev`, `pkg-config`, `docker-ce-cli`, `docker-compose-plugin`, and
   `rust-just==1.58.0`, and adds the user `tester` (uid 1000). It has no `--platform`, so it
   builds at the engine's native arch. Dependabot's docker ecosystem lists `/tests/container`.
3. **Entry script.** `tests/container/run-suite.sh <commit>`. As root, it adds `tester` to the
   group that owns the socket, then re-executes as `tester`. As `tester`, it clones `/repo` into
   `/work/src`, checks out the commit detached, runs `uv sync --locked`, and runs
   `KDIVE_REQUIRE_DOCKER=1 just test --maxprocesses=8 <ARGS>`. The script's exit code is the suite's.
4. **`test` recipe.** `test *ARGS:` appends `ARGS` to its pytest command. Without arguments the
   command is unchanged. pytest keeps the last `--maxprocesses`, so `8` replaces `16`.
5. **Capture filter (`sandbox.py`).** `_SUPPORTED_ARCHITECTURES` becomes `_ABSENT_SYSCALLS`, a
   mapping from arch to the deny-list syscalls that do not exist there: `x86_64` and `ppc64le`
   map to an empty set; `aarch64` maps to `{"fork", "vfork"}`. The install loop skips only those
   names. An arch outside the mapping raises `unsupported audit architecture` as before.
6. **Manifest.** `_ARCHITECTURES` in `manifest_attestation.py` and in
   `build-capture-bootstrap-manifest.py` add `"aarch64": "aarch64"`.
7. **Tests.** `test_sandbox.py` adds aarch64 syscall numbers (clone 220, execveat 281, clone3
   435). `test_manifest.py` accepts `aarch64` in its architecture assertion and reads only the
   `glibc-hwcaps` section of `ld.so --help`.
8. **Docs.** `AGENTS.md` and `docs/development/cross-platform.md` name `just test-linux` as the
   macOS unit gate and say a host `just test` on macOS is not regression evidence. The solution
   doc's manual recipe becomes a pointer. `lint-shell` adds `tests/container`. ADR-0558 gets an
   amendment block that links to ADR-0717.

Deferral: end-to-end Podman verification is a follow-up issue that this run files (charter
exclusion 6).

## Failure model

1. **Actors and deployments**
   - A developer at a terminal on macOS (Apple silicon or Intel) with Docker Desktop. Verified.
   - The same developer with Podman (`podman machine`). Supported, not verified.
   - A Linux developer who runs the recipe. Supported, not verified.
   - Worker hosts that run the capture filter: `x86_64` and `ppc64le` deployments. `aarch64` is
     reached only by the test suite.
2. **Invariants and assets at stake**
   - The capture filter on `x86_64` and `ppc64le` installs exactly the rules it installs today.
   - On `aarch64`, no capture child can fork, vfork, exec, or create a non-thread clone.
   - The host checkout does not change: the working tree, `.venv`, `__pycache__`, and the `.git`
     directory or file.
   - The gate's exit code is the suite's exit code.
3. **Accepted failure classes**
   - Uncommitted edits are not tested. The recipe warns; operator decision.
   - The hwcaps drift test skips on glibc 2.36 aarch64, where the loader searches no
     `glibc-hwcaps` subdirectory. The verifier uses the loader's own trace, so the skip does not
     hide a selection the loader makes.
   - A Podman-specific failure (socket path, socket group, `host-gateway`). Bounded to Podman
     users until the follow-up issue verifies it.
   - Docker Hub or the Docker apt repository is unreachable: the image build fails with the
     engine's message. The cost is a failed local run.
4. **Covered elsewhere**
   - CI on `ubuntu-latest` runs the suite natively; it does not use this recipe (exclusion 2).
   - aarch64 worker support (provisioning, catalog, live proof): exclusion 5.
   - Base image pin updates: dependabot docker group.

## Threat model

1. **Boundary inventory**
   - Added: the test container gets the host engine's API socket. Code in the cloned commit can
     start containers on the host engine.
   - Added: the image build downloads Debian packages, the Docker apt key, and `rust-just`.
   - Widened: the capture filter now installs on `aarch64`.
2. **Actor model**
   - The developer, who runs the recipe on their own commit. Trusted: the same person can run
     the code with `just test` on a Linux host.
   - Package sources (Debian, Docker apt, PyPI). Trusted through apt signatures and HTTPS, and
     through `uv sync --locked` hashes for Python dependencies.
   - A capture provider on an aarch64 host, held by the filter. Untrusted, same as on x86_64.
3. **Control per boundary**
   - Socket: no new control. The suite already has this access on a Linux host with Docker
     (`KDIVE_REQUIRE_DOCKER=1` in CI). The tests run as the non-root `tester`.
   - Image inputs: the bases are digest-pinned; `rust-just` is version-pinned; apt verifies
     signatures; the Docker key comes over HTTPS.
   - aarch64 filter: the default action for a non-native syscall ABI stays the libseccomp
     default (kill). `fork` and `vfork` do not exist on aarch64; `clone` without the thread bits
     returns `EPERM`; `clone3` returns `ENOSYS`; `execve` and `execveat` return `EPERM`. The
     existing tests `test_filter_denies_fork_and_allows_real_threads`,
     `test_filter_denies_vfork_execveat_and_clone_missing_thread_bits`,
     `test_filter_returns_enosys_for_clone3_and_denies_later_exec`, and
     `test_filter_enforces_complete_raw_clone_flag_matrix` prove each rule on aarch64.
4. **Explicitly out of scope**
   - AArch32 compat syscalls on an aarch64 host: the libseccomp bad-arch default kills them,
     the same as the i386 ABI on x86_64 today.
   - Malicious code in the tested commit: the developer chose to run it.

## Success

1. On Apple silicon with Docker Desktop, `just test-linux` on a clean `main` that contains this
   change exits 0. Measured with `just test-linux > <log> 2>&1; echo rc=$?`.
2. The run uses the same `_TEST_MARKERS`, `_TEST_XDIST`, `PYTHONHASHSEED`, and `--tb=short` as
   `just test`, because it calls `just test`.
3. After a run from a worktree, `git status --porcelain` in that worktree and in the main
   checkout is the same as before, and the mtimes of `.venv`, the worktree `.git` file, and every
   `__pycache__` directory in the worktree do not change.
4. `CONTAINER_ENGINE=bogus just test-linux` exits 2 with a message that names the variable.
5. In the aarch64 container, the four seccomp tests named above pass, and
   `test_filter_refuses_unsupported_architecture` still passes.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| aarch64 filter rules | focused-test | the four seccomp tests in `test_sandbox.py`, red on arm64 before the change |
| unknown arch fails closed | focused-test | `test_filter_refuses_unsupported_architecture` |
| manifest accepts aarch64 | focused-test | `tests/jobs/capture_operations/test_manifest.py` on arm64 (37 errors before) |
| hwcaps section parsing | focused-test | `test_runtime_verifier_rejects_new_higher_priority_hwcaps_selection`: red on arm64 before; skips on arm64 and passes on amd64 after |
| `test *ARGS` | focused-test | `just --dry-run test` prints the unchanged command; `just --dry-run test --maxprocesses=8` appends the flag |
| recipe, image, entry script | focused-test | Success 1, 3, and 4 on this host |
| Podman path | task-test-not-applicable | no Podman on the verification host; the follow-up issue owns it |
| docs | task-test-not-applicable | prose; `just docs-links` and `just docs-paths` check the links and paths |
