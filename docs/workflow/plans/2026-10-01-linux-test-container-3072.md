# Native-arch Linux test container for macOS — implementation plan (#3072)

**Goal:** `just test-linux [sha] [ARGS...]` runs the `just test` selection in a native-arch Linux
container, and the capture filter and bootstrap manifest accept `aarch64`, so a clean `main`
exits 0 on Apple silicon.

**Architecture:** A justfile recipe builds `tests/container/Dockerfile` and runs it with the git
common directory mounted read-only and the engine socket mounted. The image's entry script
clones the commit, syncs the locked environment, and calls `just test`. The capture filter gets a
per-arch table of absent syscalls.

**Tech stack:** just, bash (in the Linux image: Debian bookworm bash 5.2), Docker or Podman,
Python 3.14, libseccomp through ctypes, pytest + xdist.

Spec: [design](../specs/2026-10-01-linux-test-container-3072-design.md). Decision:
[ADR-0717](../../adr/0717-native-arch-linux-test-container-and-aarch64-capture-filter.md).

Expected implementation size: 230–330 changed lines (L) — the file map below: Dockerfile ~25,
entry script ~25, recipe ~35, sandbox ~6, manifest maps 2, tests ~30, docs ~80, ADR amendment ~10.

## Global Constraints

- Notation: in snippets for other files, `«TEXT → TARGET»` stands for a Markdown link with
  that text and target. Write it as a real link in the target file; the plan avoids the link
  syntax so the link checker does not resolve targets relative to the plan.

- Python 3.14, `uv` 0.11.31 (root Dockerfile pin), `rust-just==1.58.0` (PyPI latest on
  2026-10-01).
- Base images, exactly as in the root `Dockerfile`:
  `python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30`
  and `ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437`.
  Both indexes list amd64 and arm64 (`docker buildx imagetools inspect`, 2026-10-01).
- Ruff line length 100; `ty` strict; shell passes `shellcheck` and `shfmt -i 2 -d`.
- The host shell for verification is zsh on macOS; recipes run under `bash -euo pipefail`
  (justfile `set shell`). Shebang recipes use `#!/usr/bin/env bash` + `set -euo pipefail`.
- Prose: plain and factual; never "critical", "robust", "comprehensive", "elegant".
- ADR-0717 stays **Proposed** until Task 3, which flips it to **Accepted** and adds the
  citations in `src/` and `tests/` in the same commit (`just adr-status-check`).
- Commit messages: conventional commits, ≤72-char subject.
- Gates run bare, never piped through `tail`/`head`.

## File map

| File | Now owns | Will own | Criterion |
|---|---|---|---|
| `justfile` | `test` with no arguments; `lint-shell` paths | `test *ARGS`; new `test-linux`; `tests/container` in `lint-shell` | 1, 4, 6 |
| `tests/container/Dockerfile` (new) | — | the test image | 4, 5 |
| `tests/container/run-suite.sh` (new) | — | container entry: socket group, clone, sync, `just test` | 1, 2, 3 |
| `.github/dependabot.yml` | docker dirs `/`, mock-oidc, seaweedfs | adds `/tests/container` | — (pin upkeep) |
| `src/kdive/jobs/capture_operations/process/sandbox.py` | arch set | per-arch absent-syscall table | 7 |
| `src/kdive/jobs/capture_operations/bootstrap/manifest_attestation.py` | arch map | adds `aarch64` | 7 |
| `scripts/generate/build-capture-bootstrap-manifest.py` | arch map | adds `aarch64` | 7 |
| `tests/jobs/capture_operations/test_sandbox.py` | x86_64/ppc64le numbers | adds aarch64 numbers | 7 |
| `tests/jobs/capture_operations/test_manifest.py` | first `(supported, searched)` line | `glibc-hwcaps` section only; `aarch64` accepted | 7 |
| `docs/adr/0558-supervised-capture-operation-processes.md` | arch claim | appended amendment linking 0717 | 7 |
| `docs/adr/0717-native-arch-linux-test-container-and-aarch64-capture-filter.md` | Proposed | Accepted | — |
| `AGENTS.md`, `docs/development/cross-platform.md`, solution doc | manual recipe | `just test-linux` as the macOS gate | 8 |

No owner moves; every change extends its current owner.

## Task 1 — The `test-linux` recipe, image, and entry script

Files: modify `justfile`, `.github/dependabot.yml`; create `tests/container/Dockerfile`,
`tests/container/run-suite.sh`.

Interfaces: produces `just test-linux [sha] [ARGS...]`, used by Tasks 2 and 4 for every
container run. `just test` accepts `*ARGS`.

**Verification**

- `test *ARGS` — Mode: focused-test. `just --dry-run test` prints
  `PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest -m "not live_vm and not live_stack and not agent_smoke" -n auto --maxprocesses=16 --dist worksteal -q --tb=short`
  (with a trailing space). `just --dry-run test --maxprocesses=8` prints the same line followed
  by `--maxprocesses=8`. Red before: `just --dry-run test --maxprocesses=8` exits 1 with
  `Justfile does not contain recipe `--maxprocesses=8``.
- engine selection — Mode: focused-test. `CONTAINER_ENGINE=bogus just test-linux` exits 2 and
  prints `test-linux: CONTAINER_ENGINE must be docker or podman, not 'bogus'`. Red before: no
  such recipe.
- image + entry script — Mode: focused-test.
  `just test-linux HEAD tests/domain/test_errors.py` exits 0 and its last line ends with
  `39 passed in …`. Red before: no such recipe.
- shell lint — Mode: focused-test. `just lint-shell` exits 0 and its `shellcheck` run includes
  `tests/container/run-suite.sh` (`shfmt -f tests/container` lists it).

Steps:

1. In `justfile`, change the `test:` line and its command to:

   ```just
   test *ARGS:
       PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest -m "{{_TEST_MARKERS}}" {{_TEST_XDIST}} -q --tb=short {{ARGS}}
   ```

   Add one line to the comment above `test:`: `# Extra arguments go to pytest unchanged;
   pytest keeps the last value of a repeated option such as `--maxprocesses`.`

2. In `justfile`, add `tests/container` to both path lists of `lint-shell`, after
   `.github/scripts`.

3. Create `tests/container/Dockerfile`:

   ```dockerfile
   # syntax=docker/dockerfile:1
   # Test image for `just test-linux` (ADR-0717). It builds at the engine's native architecture,
   # so it has no --platform. The bases reuse the root Dockerfile's pins; dependabot keeps both.
   FROM ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 AS uv

   FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30
   COPY --from=uv /uv /uvx /usr/local/bin/
   # libvirt-dev and the build tools compile libvirt-python; iproute2 gives `ss` to the
   # live-stack script tests; the Docker CLI and compose plugin reach the host engine socket.
   RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git gnupg iproute2 libvirt-dev pkg-config \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin
   RUN UV_TOOL_BIN_DIR=/usr/local/bin UV_TOOL_DIR=/opt/uv-tools uv tool install rust-just==1.58.0 \
    && useradd --create-home --uid 1000 tester \
    && install -d -o tester -g tester /work /home/tester/.cache/uv
   COPY run-suite.sh /usr/local/bin/run-suite
   ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
   WORKDIR /work
   ENTRYPOINT ["/usr/local/bin/run-suite"]
   ```

4. Create `tests/container/run-suite.sh` (mode 0755):

   ```bash
   #!/usr/bin/env bash
   # Entry point of the `just test-linux` image (ADR-0717). Usage: run-suite <commit> [ARGS...]
   # As root, it only gives the non-root tester the group that owns the engine socket, because
   # several tests expect a permission refusal that root would not get.
   set -euo pipefail

   socket=/var/run/docker.sock
   if [[ $(id -u) == 0 ]]; then
     gid=$(stat -c %g "$socket")
     group=$(getent group "$gid" | cut -d: -f1) || true
     if [[ -z $group ]]; then
       group=enginesock
       groupadd --gid "$gid" "$group"
     fi
     usermod --append --groups "$group" tester
     exec runuser -u tester -- "$0" "$@"
   fi

   commit=$1
   shift
   git clone --quiet --no-checkout /repo /work/src
   cd /work/src
   git checkout --quiet --detach "$commit"
   uv sync --locked --quiet
   KDIVE_REQUIRE_DOCKER=1 exec just test --maxprocesses=8 "$@"
   ```

5. In `justfile`, after `test-shard`, add:

   ```just
   # Run the `test` selection in a Linux container at the container engine's native
   # architecture (ADR-0717). This is the unit gate on macOS, where a host `just test` is not
   # regression evidence. It tests the committed `sha` (default HEAD), never uncommitted edits;
   # a dirty tree gets a warning. The git common directory is mounted read-only, so the run works
   # from a worktree and changes nothing in the checkout. `CONTAINER_ENGINE` selects `docker`
   # (default) or `podman`; only Docker is verified on macOS. Extra arguments go to `just test`,
   # with the same shell splitting as `test-verbose`.
   test-linux $sha="HEAD" *ARGS:
       #!/usr/bin/env bash
       set -euo pipefail
       engine="${CONTAINER_ENGINE:-docker}"
       case "$engine" in
         docker) socket=/var/run/docker.sock ;;
         podman) socket="$(podman info --format '{{{{.Host.RemoteSocket.Path}}')" ;;
         *)
           echo "test-linux: CONTAINER_ENGINE must be docker or podman, not '$engine'" >&2
           exit 2
           ;;
       esac
       commit="$(git rev-parse --verify --end-of-options "$sha^{commit}")"
       if [[ -n "$(git status --porcelain)" ]]; then
         echo "test-linux: warning: uncommitted changes are not tested; testing $commit" >&2
       fi
       common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
       "$engine" build --quiet --tag kdive-test-linux tests/container >/dev/null
       exec "$engine" run --rm --init \
         --volume "$common_dir:/repo:ro" \
         --volume "$socket:/var/run/docker.sock" \
         --volume kdive-test-linux-uv-cache:/home/tester/.cache/uv \
         --env TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
         --add-host host.docker.internal:host-gateway \
         kdive-test-linux "$commit" {{ARGS}}
   ```

6. In `.github/dependabot.yml`, add `- "/tests/container"` to the docker ecosystem's
   `directories`, and extend the comment above it to name the test image.
7. Run the four verifications above. Then run `just lint` (exit 0).
8. Commit: `feat(test): add just test-linux native-arch container gate`.

If `git clone` in step 4 fails with `detected dubious ownership`, add
`git config --global --add safe.directory /repo` before the clone, rerun the third verification,
and record the observed error in the commit message.

## Task 2 — aarch64 in the capture filter and the bootstrap manifest

Files: modify `src/kdive/jobs/capture_operations/process/sandbox.py`,
`src/kdive/jobs/capture_operations/bootstrap/manifest_attestation.py`,
`scripts/generate/build-capture-bootstrap-manifest.py`,
`tests/jobs/capture_operations/test_sandbox.py`, `tests/jobs/capture_operations/test_manifest.py`.

Interfaces: consumes `just test-linux` from Task 1. `install_capture_filter()` keeps its
signature `() -> None`.

**Verification**

- aarch64 filter rules — Mode: focused-test. Red: after steps 1–2 and a commit,
  `just test-linux HEAD tests/jobs/capture_operations/test_sandbox.py` on Apple silicon exits 1
  with four failures that contain `RuntimeError: unsupported audit architecture: aarch64`.
  Green after step 4: same command exits 0, `5 passed`.
- manifest on aarch64 — Mode: focused-test. Red at the same commit:
  `just test-linux HEAD tests/jobs/capture_operations/test_manifest.py` shows errors with
  `unsupported architecture: aarch64` and the hwcaps test failing with
  `DID NOT RAISE RuntimeError`. Green after step 5: exit 0; the hwcaps test is `SKIPPED` with
  `runtime loader exposes no supported glibc-hwcaps directory`.
- unknown arch fails closed — Mode: focused-test.
  `test_filter_refuses_unsupported_architecture` passes in the green run above.
- capture child on aarch64 — Mode: focused-test.
  `just test-linux HEAD tests/jobs/capture_operations` exits 0 after step 6.

Steps:

1. In `test_sandbox.py`, change the three number tables:
   - `"numbers = {'x86_64': 435, 'ppc64le': 435}\n"` →
     `"numbers = {'x86_64': 435, 'ppc64le': 435, 'aarch64': 435}\n"`
   - after the line `" 'ppc64le': {'clone': 120, 'execveat': 362},\n"` add
     `" 'aarch64': {'clone': 220, 'execveat': 281},\n"`
   - `"clone_number = {'x86_64': 56, 'ppc64le': 120}[platform.machine()]\n"` →
     `"clone_number = {'x86_64': 56, 'ppc64le': 120, 'aarch64': 220}[platform.machine()]\n"`
2. In `test_manifest.py`:
   - change `assert manifest["architecture"] in {"x86_64", "ppc64le"}` to
     `assert manifest["architecture"] in {"x86_64", "ppc64le", "aarch64"}`;
   - add this module-level helper above the first test that uses it:

     ```python
     def _supported_glibc_hwcaps(help_text: str) -> str | None:
         """First supported entry of the loader's glibc-hwcaps section, not its legacy section."""
         in_section = False
         for line in help_text.splitlines():
             if line.startswith("Subdirectories of glibc-hwcaps directories"):
                 in_section = True
             elif in_section and not line.strip():
                 return None
             elif in_section and "(supported, searched)" in line:
                 return line.strip().split()[0]
         return None
     ```

   - in `test_runtime_verifier_rejects_new_higher_priority_hwcaps_selection`, replace the
     `supported = next(...)` expression with `supported = _supported_glibc_hwcaps(help_result.stdout)`.
3. Commit locally (`test: cover the capture filter and manifest on aarch64`) and run the two red
   verifications.
4. In `sandbox.py`, replace `_SUPPORTED_ARCHITECTURES = frozenset({"x86_64", "ppc64le"})` with:

   ```python
   # Deny-list syscalls that the architecture's syscall table does not have (ADR-0717). On
   # aarch64, glibc implements fork and vfork with clone, which the thread-bit rules deny.
   _ABSENT_SYSCALLS = {
       "x86_64": frozenset[str](),
       "ppc64le": frozenset[str](),
       "aarch64": frozenset({"fork", "vfork"}),
   }
   ```

   In `install_capture_filter`, change `if architecture not in _SUPPORTED_ARCHITECTURES:` to
   `if architecture not in _ABSENT_SYSCALLS:`, and the deny loop to:

   ```python
           for syscall_name in ("fork", "vfork", "execve", "execveat"):
               if syscall_name not in _ABSENT_SYSCALLS[architecture]:
                   seccomp.deny(syscall_name, errno.EPERM)
   ```

5. In `manifest_attestation.py` and `build-capture-bootstrap-manifest.py`, change
   `_ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "ppc64le": "ppc64le"}` to
   `_ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "ppc64le": "ppc64le", "aarch64": "aarch64"}`
   (split over lines if ruff requires).
6. `just format`, `just lint`, `just type` (on macOS, only host-stdlib `has no member`
   diagnostics are expected). Amend the local commit from step 3 to include the implementation
   and retitle it `feat(capture): accept aarch64 in the capture filter and manifest`. The commit
   is not pushed yet, so the amend is local. Run the green verifications.

## Task 3 — Decision records and docs

Files: modify `docs/adr/0558-supervised-capture-operation-processes.md`,
`docs/adr/0717-native-arch-linux-test-container-and-aarch64-capture-filter.md`,
`src/kdive/jobs/capture_operations/process/sandbox.py` (docstring),
`tests/jobs/capture_operations/test_sandbox.py` (docstring), `AGENTS.md`,
`docs/development/cross-platform.md`,
`docs/solutions/2026-09-25-macos-linux-container-test-baseline.md`.

Interfaces: none for code.

**Verification**

- ADR status — Mode: focused-test. `just adr-status-check` exits 0. Red check: with 0717 still
  Proposed and the citation added, it exits 1 naming 0717.
- records shape — Mode: focused-test. `git fetch origin main && just records` exits 0
  (append-only amendment to 0558).
- links and paths — Mode: focused-test. `just docs-links` and `just docs-paths` exit 0 (run with
  Homebrew bash and gnubin on `PATH`).
- prose — Mode: task-test-not-applicable: human-readable guidance; no consumer parses it beyond
  the link/path checks above.

Steps:

1. Append to the `## Decision` section of ADR-0558, after its last paragraph:

   ```markdown
   ### Amendment (2026-10-01): the filter also supports aarch64 (#3072)

   This qualifies the claim above that the filter fails closed unless the architecture is the
   x86_64 or ppc64le form. «ADR-0717 → 0717-native-arch-linux-test-container-and-aarch64-capture-filter.md»
   adds aarch64. aarch64 has no `fork` or `vfork` syscall; glibc implements both with `clone`,
   which the thread-bit rules deny. The other rules and the fail-closed behavior for an unlisted
   architecture are unchanged.
   ```

   Under ADR-0558's status line add:
   `> **Amended by «ADR-0717 → 0717-native-arch-linux-test-container-and-aarch64-capture-filter.md» (#3072):** the capture filter also supports aarch64.`
2. In ADR-0717, change `Proposed (2026-10-01)` to `Accepted (<merge-prep date>)`. Change the
   docstrings: `sandbox.py` → `"""Minimal pre-gate seccomp installation for capture children (ADR-0558, ADR-0717)."""`;
   `test_sandbox.py` → `"""Native seccomp matrix for the capture-operation child boundary (ADR-0558, ADR-0717)."""`.
3. `AGENTS.md`: add a commands-table row after `just test`:
   `| `just test-linux` | the `just test` selection in a native-arch Linux container (Docker or Podman); the unit gate on macOS; tests a commit, not uncommitted edits | — |`.
   In `## Host prerequisites`, after the paragraph that ends `cannot install the runner that
   invokes it.`, add: `On macOS, a host `just test` is not regression evidence: the suite needs
   Linux behavior. Run `just test-linux` instead («cross-platform guide → docs/development/cross-platform.md#macos-run-the-suite-in-a-linux-container»).`
4. `docs/development/cross-platform.md`: add before `## Container images`:

   ```markdown
   ## macOS: run the suite in a Linux container

   On macOS, a host `just test` is not regression evidence. The suite needs Linux behavior
   (`/proc`, pidfd, seccomp, GNU tool output), so hundreds of tests fail on every branch.
   Use `just test-linux`, which runs the same selection, hash seed and `--tb=short` output in a
   Linux container at the engine's native architecture (arm64 on Apple silicon;
   «ADR-0717 → ../adr/0717-native-arch-linux-test-container-and-aarch64-capture-filter.md»).

   - `just test-linux` tests `HEAD`; `just test-linux <sha>` tests another commit. Commit first:
     uncommitted edits are not tested, and the recipe warns when the tree is dirty.
   - `just test-linux HEAD tests/jobs` runs only the given paths.
   - The recipe works from a worktree. It mounts the git directory read-only and changes nothing
     in the checkout. A named volume, `kdive-test-linux-uv-cache`, keeps downloaded packages.
   - `CONTAINER_ENGINE=podman just test-linux` uses Podman. Only Docker Desktop is verified on
     macOS.
   - The container uses the engine's socket to start the test PostgreSQL and SeaweedFS
     containers, the same as `just test` on a Linux host with Docker.
   ```

5. Solution doc: replace the body of `## Solution` with one paragraph: `Use `just test-linux`
   («cross-platform guide → ../development/cross-platform.md#macos-run-the-suite-in-a-linux-container»).
   It runs natively on arm64, and a clean `main` exits 0, so no baseline comparison is needed.`
   In `## Prevention`, change the first bullet to name `just test-linux` and remove the bullet
   about the `rm -rf` hook (it described the scratch Dockerfile, which no longer exists).
6. Commit: `docs: name just test-linux as the macOS unit gate` (ADR-0717 Accepted + amendment
   + docs in one commit, so the citation and the status flip land together).

## Task 4 — End-to-end proof and the Podman follow-up

Files: none changed, unless the proof finds a failure; then the fix goes in the file that owns
it, as its own commit.

**Verification**

- clean gate — Mode: focused-test. On Apple silicon with Docker Desktop, from the worktree:
  `just test-linux > <scratch>/e2e.log 2>&1` exits 0. Red evidence: the arm64 baseline on
  `2c4df02fd` (`28 failed … 37 errors`).
- checkout unchanged — Mode: focused-test. Before and after the run, record
  `git status --porcelain` in the worktree and in the main checkout, and
  `stat -f '%m %N'` of the worktree `.git` file and of every `__pycache__` directory under the
  worktree. Both records must be equal.
- worker cap — Mode: focused-test. `just test-linux HEAD tests/domain/test_errors.py -v` prints
  `created: 8/8 workers` (`-v` cancels the recipe's `-q`, so xdist prints its worker line).

Steps:

1. Run the three verifications. Record the duration and the summary line.
2. File the Podman follow-up with `gh issue create --body-file <file>`: title
   `Verify just test-linux with Podman on macOS`, labels `type:test`, `priority:P3`,
   `status:needs-triage`; body: the socket path, socket group and `host-gateway` points to verify,
   and a link to #3072. Run `just check-pr-body <file>` first.
