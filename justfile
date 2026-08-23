set shell := ["bash", "-euo", "pipefail", "-c"]

# Pinned git-cliff version — referenced by the changelog recipe and release.yml (one place).
GIT_CLIFF := "git-cliff@2.13.1"

# Import path that puts the working tree's src/ first for every recipe that
# executes repository code (#1987). The project's editable install appends src/
# to sys.path AFTER ambient PYTHONPATH and site-packages, so a stale kdive
# earlier on sys.path -- an exported PYTHONPATH pointing at another checkout,
# or a non-editable install left in the venv when UV_NO_SYNC=1 suppresses uv's
# auto-repair -- silently feeds generators and guards outdated registry data
# whose rendered output matches the stale committed artifact: the gate passes
# locally and fails only in CI. Prepending src/ makes each verdict describe the
# exact tree being checked.
WORKTREE_PYTHONPATH := justfile_directory() + "/src" + "${PYTHONPATH:+:$PYTHONPATH}"

# List available recipes.
default:
    @just --list

# One-command first-time setup: check host deps, sync the venv, install hooks.
setup: check-deps sync build-capture-bootstrap-manifest install-hooks
    @echo "Development environment is ready."

# Stage and verify attestation for the explicitly selected worker interpreter. This is
# intentionally unprivileged and never writes /usr; operators install in a separate step.
build-capture-bootstrap-manifest interpreter=".venv/bin/python" output="build/capture-bootstrap-manifest.json":
    {{interpreter}} scripts/build-capture-bootstrap-manifest.py build --interpreter {{interpreter}} --source-root src --output {{output}}
    {{interpreter}} scripts/build-capture-bootstrap-manifest.py verify --interpreter {{interpreter}} --source-root src --manifest {{output}}

# Privileged operator action. The script requires euid 0, installs atomically as root:root mode
# 0644, and verifies byte identity with the already-staged manifest.
install-capture-bootstrap-manifest staged="build/capture-bootstrap-manifest.json" destination="/usr/share/kdive/capture-bootstrap-manifest.json":
    .venv/bin/python scripts/build-capture-bootstrap-manifest.py install --staged {{staged}} --destination {{destination}}

# Report missing host packages with distro-specific install hints. Report-only in CI / when piped;
# at an interactive terminal it offers a [y/N] install per tier (pass -y to install unattended).
check-deps:
    ./scripts/check-setup-deps.sh

# Preflight: can this host run the local-libvirt provider? (report-only)
check-local-libvirt:
    ./scripts/check-local-libvirt.sh

# Onboard the local-libvirt demo project (preflight + seed budget/quota). See #497.
setup-local-libvirt:
    ./scripts/setup-local-libvirt.sh

# Fund a dev-stack project + mint a token (preflight, migrate, seed, verify; KDIVE_PROJECT=demo). See #834.
onboard:
    ./scripts/live-stack/onboard.sh

# Preflight: can the remote-libvirt provider reach a target host? (report-only)
check-remote-libvirt host user="root" uri="":
    ./scripts/check-remote-libvirt.sh {{host}} {{user}} {{uri}}

# Onboard the remote-libvirt demo project (preflight + token + audited budget/quota). See #497.
setup-remote-libvirt host user="root" uri="":
    ./scripts/setup-remote-libvirt.sh {{host}} {{user}} {{uri}}

# Create the venv and install pinned dependencies from the lockfile.
sync:
    uv sync --locked

# Install the git pre-commit hooks and run them across the tree once.
install-hooks:
    prek install
    prek run -a

# Lint and check formatting (read-only; mirrors CI).
lint:
    uv run ruff check .
    uv run ruff format --check .

# Apply lint fixes and reformat in place.
format:
    uv run ruff check --fix .
    uv run ruff format .

# Type-check the whole tree (src + tests). Whole-tree, not `src`: this is the single
# definition CI and the pre-commit ty hook both invoke, and the only place tests/ is
# type-checked (scoping to src once let a test-tree type error merge green).
type:
    uv run ty check

# Shared selection for `test` / `test-verbose`: the gated-tier exclusion, xdist parallelism,
# and the worker cap described below, defined once so the quiet and verbose invocations
# cannot drift apart.
_TEST_SELECT := '-m "not live_vm and not live_stack and not agent_smoke" -n auto --maxprocesses=16 --dist worksteal'

# Run the test suite, excluding the gated live_vm, live_stack, and agent_smoke suites.
# (oidc_issuer-marked tests stay selected; they skip cleanly without the issuer container.)
#
# `-n auto` runs the suite in parallel via pytest-xdist; workers share one Postgres and one
# MinIO container per run (xdist_backend). `--maxprocesses=16` caps the worker count on
# high-CPU-count machines (e.g. 128-logical-CPU ppc64le POWER hosts where -n auto falls back
# to multiprocessing.cpu_count()=128 when psutil is absent): saturating a single shared
# container causes timing-sensitive tests to flap. The flag lives here, not in addopts,
# because --maxprocesses is a pytest-xdist option and bare pytest (used by the image-smoke
# CI step with --no-project) would reject it as unrecognised. CI runners have ≤8 CPUs so
# the cap is never reached there (#1921).
# `--dist worksteal` lets an idle worker pull queued tests from a busy worker's queue
# instead of running its up-front chunk to completion (the `load` default); durations here
# range from ~2ms to hundreds of ms per test, so worksteal shortens the straggler tail
# (#1332). It only changes execution order, not collection order, so it doesn't interact
# with the ordering guard below. PYTHONHASHSEED is pinned so every xdist worker collects
# parametrized tests in the same order — a parametrize source backed by a set is ordered by
# the hash seed, which differs per worker, and xdist then aborts with "Different tests were
# collected". It defaults to 0 but is overridable: the weekly test-ordering workflow sets
# PYTHONHASHSEED=random to surface any new ordering-dependent test the pinned seed would
# otherwise mask.
test:
    PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest {{_TEST_SELECT}} -q

# Same selection as `test:` with full error output for inspection: `-vv` restores complete
# assertion introspection and diffs, `--tb=long` keeps every frame, where `-q` trims both.
# Optional path arguments scope the run to the failing files, so a failure can be escalated
# to readable output without re-running the whole suite loudly or hand-assembling flags.
test-verbose *PATHS:
    PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest {{_TEST_SELECT}} -vv --tb=long {{PATHS}}

# Rerun the tests that failed on the previous run, failures first — the fast inner loop
# (#1334, ADR-0420). Additive: `just test` stays the full pre-push gate and this never runs
# in CI. Same marker exclusion and pinned PYTHONHASHSEED as `test:`, so a stale/empty --lf
# cache (pytest then runs everything) still skips the gated live tiers and collects stably.
test-lf:
    PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest -m "not live_vm and not live_stack and not agent_smoke" --lf -n auto --maxprocesses=16 -q

# Run only the tests your working changes touch — the fast inner loop (#1334, ADR-0420).
# scripts/select_changed_tests.py maps each changed src file to every tests/**/test_<stem>.py
# and lists changed test files directly; it prints `__ALL__` when a change is unmappable (a
# src file with no named test, a conftest, pyproject.toml, this justfile), which falls back
# to the full suite here. The map is by name, not import graph, so a change to a widely
# imported module runs only its own named test — `just test` stays the gate for the rest.
test-changed:
    #!/usr/bin/env bash
    set -euo pipefail
    marks="not live_vm and not live_stack and not agent_smoke"
    run_full() {
      PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest -m "$marks" -n auto --maxprocesses=16 --dist worksteal -q
    }
    # Command substitution (not `< <(...)`) so a selector crash is caught, not read as "no
    # changed tests" — a false green is the one failure this recipe must never produce.
    if ! output="$(python3 scripts/select_changed_tests.py)"; then
      echo "changed-test selector failed — running the full suite" >&2
      run_full
      exit
    fi
    if [[ "$output" == "__ALL__" ]]; then
      echo "changed set is broad or unmappable — running the full suite"
      run_full
    elif [[ -z "$output" ]]; then
      echo "no changed tests detected — nothing to run (use 'just test' for the full suite)"
    else
      mapfile -t targets <<< "$output"
      printf 'running %d changed-test target(s):\n' "${#targets[@]}"
      printf '  %s\n' "${targets[@]}"
      # Exit 5 ("no tests collected") means every selected test was gated out by $marks
      # (a changed live_vm/live_stack/agent_smoke test) — report it, don't abort as a
      # false-red under set -e. Other non-zero codes are real failures and propagate.
      rc=0
      PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest "${targets[@]}" -m "$marks" -n auto --maxprocesses=16 --dist worksteal -q || rc=$?
      if [[ "$rc" -eq 5 ]]; then
        echo "all selected tests are gated (live_vm/live_stack/agent_smoke) — none ran; use 'just test' or a live recipe"
        exit 0
      fi
      exit "$rc"
    fi

# Run the doc-driven agent-smoke tier (#1370, ADR-0411): a deterministic walker drives the
# agent-index.md golden path over the served surface and fails on any stall. Non-PR-gate like
# the live tiers and NOT in `ci` — the harness for the deferred nightly live-LLM agent. It is
# infra-free (built app over a closed pool + dummy KDIVE_S3_* test env), so it needs no stack.
# --strict-markers fails a mis-marked test; exit 5 ("no tests collected") is tolerated as a
# clean skip, other codes propagate.
test-agent-smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest -m agent_smoke --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no agent_smoke tests collected — skipping cleanly (marked suite absent)"
      exit 0
    fi
    exit "$rc"

# The emulated foreign-arch tier is `just test-live-tcg`, excluded here so the native run stays fast.
# Run the native live_vm suite (needs a KVM/libvirt host with a kdump-enabled guest).
test-live:
    uv run python -m pytest -m "live_vm and not live_vm_tcg" -q

# --strict-markers fails a mis-marked test; pytest exit 5 ("no tests collected") is tolerated as a
# clean skip, other codes propagate. Needs the foreign qemu emulator (e.g. qemu-system-ppc64) AND a
# running stack (`just stack-up` + fixtures); the tests skip cleanly without either.
#
# Run the emulated foreign-arch (TCG) tier: the four ppc64le provision→boot→crash→retrieve proofs.
test-live-tcg:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest -m live_vm_tcg --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no live_vm_tcg tests collected — skipping cleanly (marked suite absent)"
      exit 0
    fi
    exit "$rc"

# --strict-markers fails a mis-marked test. Needs an operator-provided qemu+tls:// host
# (KDIVE_LIVE_VM_REMOTE_URI + base-image volume + KDIVE_S3_* + a running reconciler); the
# require_live_vm_remote gate skips cleanly with no remote env and fails loud on a partial one
# (docs/operating/runbooks/remote-live-stack.md).
#
# pytest exit 5 ("no tests collected") is a FAILURE here, not a clean skip: it means no
# live_vm_remote test ran, so the run proved nothing either way. Today it means the family has zero
# carriers (ADR-0425 shipped the marker and gate ahead of the first remote proof). It would also
# fire if a future carrier skipped at MODULE level, which likewise yields exit 5 — hence the gate
# belongs inside the test, where an absent remote env is a reported skip and exit 0 (#1627).
#
# Run the remote-libvirt live_vm family: direct provider ops against a genuinely remote qemu+tls:// host.
test-live-remote:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest -m live_vm_remote --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no live_vm_remote test ran — this recipe proved nothing, so it is not a pass." >&2
      echo "pytest collected nothing. Either no test carries the marker (ADR-0425 shipped the" >&2
      echo "gate ahead of the first remote proof), or a carrier skipped at module level." >&2
      echo "Mark the remote-libvirt proofs and call require_live_vm_remote() INSIDE the test —" >&2
      echo "an absent remote env is then a reported skip, not an empty run (#1627)." >&2
      exit 1
    fi
    exit "$rc"

# Apply database migrations using the live-stack default environment.
stack-migrate:
    ./scripts/live-stack/apply-migrations.sh

# Bring up the live-stack backing services healthy, then migrate the schema and print the
# host-process startup step. Reuses the compose backends; host processes stay outside compose.
#
# `--wait` is scoped to the three long-running backends: it treats ANY container exit as a wait
# failure, so the one-shot `minio-init` (creates the bucket, then exits 0) would make a healthy
# stack report exit 1. Run that init separately to completion — its exit code still propagates,
# so a real bucket-creation failure fails the recipe.
stack-up:
    # Pre-build oidc when using the local build path (KDIVE_OIDC_IMAGE unset). ADR-0357
    # has compose build kdive-mock-oidc:dev from ./deploy/mock-oidc; without this pre-build,
    # `compose up` first tries to PULL that local-only tag and prints a confusing "pull
    # access denied" warning before falling back to build. Skip the build entirely when the
    # image already exists — the Dockerfile inputs (pom.xml + Dockerfile) change rarely and
    # `docker compose build` re-contacts the registry on every call even when fully cached.
    # The skip is announced (not silent) so an operator editing deploy/mock-oidc knows to
    # `docker rmi kdive-mock-oidc:dev` to force a rebuild. Skipped entirely when
    # KDIVE_OIDC_IMAGE is set (that's the pull path, ADR-0358).
    if [ -z "${KDIVE_OIDC_IMAGE:-}" ]; then if docker image inspect kdive-mock-oidc:dev > /dev/null 2>&1; then echo "using cached kdive-mock-oidc:dev — run 'docker rmi kdive-mock-oidc:dev' to force a rebuild after editing deploy/mock-oidc"; else docker compose build oidc; fi; fi
    # --wait-timeout is required now the backends carry `restart: on-failure` (ADR-0449):
    # a container that keeps failing cycles Exited -> Restarting instead of settling, so
    # without a bound the convergence poll can block indefinitely rather than reporting.
    docker compose up -d --wait --wait-timeout 120 postgres minio oidc
    docker compose run --rm minio-init
    ./scripts/live-stack/apply-migrations.sh
    @echo "Backends healthy and schema migrated."
    @echo "App tier, for IN-NETWORK clients: just compose-up"
    @echo "For the live suites, the CLI, or any local-libvirt VM: scripts/live-stack/up.sh"
    @echo "  (compose containers get a different OIDC issuer identity than a host-minted token"
    @echo "   carries -> 401, and no /dev/kvm or libvirt socket -> no local VM. See the runbook.)"
    @echo "MCP URL: http://127.0.0.1:8000/mcp"
    @echo "Full runbook: docs/operating/runbooks/live-stack.md"

# Print a bearer token from the bundled Helm-demo mock-OIDC issuer (Kubernetes):
#   export KDIVE_TOKEN=$(just demo-token)                  # full admin grant (default)
#   export KDIVE_TOKEN=$(just demo-token --role viewer)    # narrowed, to test an RBAC denial
# Demo-only. KDIVE_DEMO_{NAMESPACE,FULLNAME,CONTEXT} override the target release.
demo-token *ARGS:
    @./scripts/demo-token.sh {{ARGS}}

# Run the live_stack suite (needs `just stack-up` + VM fixtures). --strict-markers fails a
# mis-marked test instead of silently deselecting; pytest exit 5 ("no tests collected", e.g.
# the marked driver not yet present) is tolerated as a clean skip, other codes propagate.
test-live-stack:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest -m live_stack --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no live_stack tests collected — skipping cleanly (stack/fixtures or marked suite absent)"
      exit 0
    fi
    exit "$rc"

# `test-live-stack` also collects the local spine, which needs local guest-image fixtures this
# host may not carry; scoping to the remote module lets an operator proving the remote tier read
# one signal. NOT the same tier as `test-live-remote`, which selects the live_vm_remote
# (direct-provider) family by marker. See docs/operating/runbooks/remote-live-stack.md.
# Run ONLY the remote-libvirt arm of the live_stack suite (needs a genuinely remote qemu+tls host).
test-live-stack-remote:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest tests/integration/test_remote_live_stack.py \
      -m live_stack --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no remote live_stack tests collected — skipping cleanly (remote config absent)"
      exit 0
    fi
    exit "$rc"

# Mutation-test ONE module against an explicit test path (see docs/development/mutation-testing.md).
# Reports surviving mutants — code changes no test caught. mutmut runs ephemerally (not a locked dep).
#   just mutate src/kdive/domain/errors.py tests/domain/test_errors.py
mutate source *tests:
    uv run --with 'mutmut==3.6.0' python scripts/mutate.py {{source}} {{tests}}

# Build wheel + sdist with build info baked in, then remove the stamp so it never lingers
# in the editable checkout (a leftover would shadow live-git version reporting). Pass
# release=true only when building from a release tag.
build release="false":
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'rm -f src/kdive/_buildinfo.py' EXIT
    ./scripts/stamp-buildinfo.sh "{{release}}"
    uv build

# Regenerate CHANGELOG.md from conventional-commit history (Keep a Changelog).
changelog:
    uvx {{GIT_CLIFF}} --output CHANGELOG.md

# Start the operator backing services (Postgres + MinIO + mock OIDC) for a live run.
compose-up:
    KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:-postgresql://kdive-witness-member:kdive-witness-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}" KDIVE_WORKER_DATABASE_URL="${KDIVE_WORKER_DATABASE_URL:-postgresql://kdive-worker-member:kdive-worker-local@postgres:5432/kdive}" uv run python -m kdive.processes.lifecycle.compose_worker_lifecycle up # pragma: allowlist secret — local dev only

# Stop the stack after recording worker termination, preserving named volumes for an upgrade.
compose-stop:
    KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:-postgresql://kdive-witness-member:kdive-witness-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}" uv run python -m kdive.processes.lifecycle.compose_worker_lifecycle down # pragma: allowlist secret — local dev only

compose-recreate-worker:
    KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:-postgresql://kdive-witness-member:kdive-witness-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}" KDIVE_WORKER_DATABASE_URL="${KDIVE_WORKER_DATABASE_URL:-postgresql://kdive-worker-member:kdive-worker-local@postgres:5432/kdive}" uv run python -m kdive.processes.lifecycle.compose_worker_lifecycle recreate # pragma: allowlist secret — local dev only

# Stop the operator backing services and remove their volumes.
compose-down:
    KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:-postgresql://kdive-witness-member:kdive-witness-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}" uv run python -m kdive.processes.lifecycle.compose_worker_lifecycle down --volumes # pragma: allowlist secret — local dev only

# Run the isolated executable Compose/Docker lifecycle proof. The explicit environment makes
# unavailable Docker a failure and guarantees that the sole carrier cannot report a skip as proof.
test-compose-lifecycle:
    command -v bwrap >/dev/null || { echo "bubblewrap is required: install bwrap" >&2; exit 2; }
    KDIVE_RUN_COMPOSE_LIFECYCLE_PROOF=1 KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/compose/test_compose_worker_lifecycle_live.py -m live_stack --strict-markers -q

# Run the isolated executable proof that a plain `docker compose down` preserves the named data
# volumes and `down --volumes` still drops them (ADR-0552). Same fail-loud contract as above:
# the explicit environment makes unavailable Docker a failure rather than a reported skip.
test-compose-volumes:
    KDIVE_RUN_COMPOSE_VOLUME_PROOF=1 KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/compose/test_compose_volume_persistence_live.py -m live_stack --strict-markers -q

# Lint and format-check the shell scripts (recursively under scripts/).
lint-shell:
    shfmt -f scripts deploy/compose deploy/remote-libvirt-guest-helpers deploy/ansible/tests examples | xargs shellcheck
    shfmt -i 2 -d scripts deploy/compose deploy/remote-libvirt-guest-helpers deploy/ansible/tests examples

# Lint and syntax-check the Ansible automation (deploy/ansible).
lint-ansible:
    uv run --with 'ansible-lint==26.4.0' --with 'ansible-core==2.21.1' \
        yamllint -c deploy/ansible/.yamllint deploy/ansible
    ANSIBLE_CONFIG=deploy/ansible/ansible.cfg \
        uv run --with 'ansible-lint==26.4.0' --with 'ansible-core==2.21.1' \
        ansible-lint -c deploy/ansible/.ansible-lint deploy/ansible
    cd deploy/ansible && for p in site.yml playbooks/pki.yml playbooks/image.yml; do \
        uv run --with 'ansible-core==2.21.1' \
        ansible-playbook "$p" --syntax-check -i inventory/hosts.yml; done

# Run the Ansible role regression harnesses (gdbstub_acl ufw prune #616; image admission
# + staged-volume confirmation #1629).
test-ansible:
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-github-runner-preflight.sh
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-guest-base-image-admission.sh
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-remote-libvirt-facts-render.sh

# Lint and security-scan the GitHub Actions workflows.
# actionlint-py bundles a prebuilt actionlint and upstream ships no ppc64le binary, so its
# install fails there. On ppc64le use a PATH actionlint (build from Go source:
# `go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12`); elsewhere keep the
# pinned wrapper for a reproducible version.
lint-workflows:
    uv run --with 'zizmor==1.25.2' zizmor .github/workflows
    if [ "$(uname -m)" = "ppc64le" ]; then actionlint; else uv run --with 'actionlint-py==1.7.12.24' actionlint; fi

# Browserless syntax check of every mermaid block in tracked Markdown.
# -z/-0 keeps paths with spaces intact; -r skips the run when nothing matches.
check-mermaid:
    git ls-files -z '*.md' | xargs -0 -r node .github/scripts/mermaid-check/mermaid-check.mjs

# Resolve relative markdown links in tracked *.md against the filesystem.
docs-links:
    ./scripts/check-doc-links.sh

# Fail when a concrete docs/<path> reference in code/recipes/markdown is missing.
docs-paths:
    ./scripts/check-doc-paths.sh

# Scan a pull-request or issue body file for credentials and pasted process environments,
# before `gh pr create --body-file` publishes it. The pr-body-scan workflow runs this same
# script against the live body, so local and CI share one definition. Needs gitleaks.
check-pr-body +FILES:
    ./scripts/check-pr-body.sh {{ FILES }}

# Fail when a served doc (DOC_RESOURCES) cites another served doc by a relative link instead
# of its resource:// URI — a relative link is filesystem-valid but unfetchable over MCP.
served-doc-links:
    ./scripts/check-served-doc-links.sh

# Guard the ADR status lifecycle: valid status, index in sync, no shipped-but-Proposed
# drift (docs/adr/README.md ratification rule); record shape/anti-erasure is the `records`
# workflow (ADR-0504). Stdlib-only (plain python3, no uv sync).
adr-status-check:
    python3 scripts/check_adr_status.py

# Audit runtime dependencies for known vulnerabilities. The script retries only a run that
# produced no verdict (unreachable PyPI), never a run that found something — pip-audit exits 1
# for both, so retrying on exit status alone would re-run genuine advisories (ADR-0553, #1913).
audit:
    ./scripts/audit-deps.sh runtime

# Pull the images the db/store testcontainer fixtures start, ahead of the suite, with a bounded
# retry. CI runs this as its own step so an unreachable registry is one red step naming one
# image instead of thousands of downstream fixture errors (ADR-0553, #1913). Not part of `ci:`:
# a developer's daemon already holds these layers, and `just test` still pulls lazily without it.
pull-test-images:
    ./scripts/pull-test-images.sh

# Install Debian/Ubuntu system packages under a hard per-call timeout and a bounded retry
# (3 attempts, 5s/15s). CI's `Install libvirt build headers` step wedged for 13 and 33 minutes
# on two runs in one afternoon against a ~15s normal (#1978); the failure is a stall rather than
# a non-zero exit, so the timeout is the half that makes it fail and the retry is the half that
# keeps a slow-but-working mirror from going red (ADR-0566). Raise KDIVE_APT_TIMEOUT_S for a
# large package set, as live.yml does. Not part of `ci:` — this installs system packages, and
# the workflows invoke the script directly because they run before `just` is set up. The recipe
# is where the command text lives (AGENTS.md).
apt-install +PACKAGES:
    ./scripts/apt-install.sh {{PACKAGES}}

# Set the project version in pyproject.toml AND uv.lock together. `--no-sync` re-locks
# (updates uv.lock) WITHOUT rebuilding the virtual environment — so a version bump does not
# require libvirt-dev to compile libvirt-python; the editable install refreshes on the next
# `uv run`. Used at a Milestone start and for the post-release "begin <next>-dev" bump.
# Commit the result on a branch — never directly on main.
set-version VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{VERSION}}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
      echo "VERSION must be MAJOR.MINOR.PATCH, got '{{VERSION}}'" >&2
      exit 1
    fi
    uv version --no-sync "{{VERSION}}"
    # Keep the Helm chart's appVersion locked to the pyproject version (spec A3 /
    # chart-version-check). Done here so a version bump never trips the CI guard.
    sed -i.bak -E 's/^appVersion:.*/appVersion: "{{VERSION}}"/' deploy/helm/kdive/Chart.yaml
    rm -f deploy/helm/kdive/Chart.yaml.bak
    echo "Set version to {{VERSION}} (pyproject.toml + uv.lock). Commit on a branch."

# Fail if uv.lock is out of date relative to pyproject.toml (a forgotten re-lock).
lock-check:
    uv lock --check

# Cut a release: verify state, then push the annotated tag only (never a commit to main).
# The version must already equal VERSION (it was bumped at Milestone start / post-release).
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ "$(git branch --show-current)" == "main" ]] || { echo "not on main" >&2; exit 1; }
    [[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
    git fetch --quiet origin main
    [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo "HEAD is not at origin/main (behind, ahead, or diverged) — sync first" >&2; exit 1; }
    # scripts/pyproject-version.sh reads a colour-free version regardless of the caller's
    # environment (#1883, #1886) — see its header for why.
    current="$({{justfile_directory()}}/scripts/pyproject-version.sh)"
    [[ "$current" == "{{VERSION}}" ]] || { echo "pyproject version $current != {{VERSION}}" >&2; exit 1; }
    git tag -a "v{{VERSION}}" -m "Release v{{VERSION}}"
    git push origin "v{{VERSION}}"
    echo "Pushed tag v{{VERSION}}. NEXT: open a 'chore(release): begin <next>-dev' PR"
    echo "(just set-version <next>) — CHANGELOG auto-syncs on merge; see docs/development/releasing.md."

# Regenerate the agent-facing tool reference from the live registry (mutating).
docs:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_tool_reference.py

# Verify the committed tool reference matches a fresh generation (CI gate).
docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python -c "from scripts.gen_tool_reference import write_reference; from pathlib import Path; write_reference(Path('$tmp'))"
    # config.md is generated separately (just config-docs-check); exclude it from the
    # tool-reference directory diff so the two generators can share docs/guide/reference/.
    if ! diff -ru --exclude=config.md docs/guide/reference "$tmp"; then
        echo "tool reference is stale — run 'just docs' and commit" >&2
        exit 1
    fi

# Regenerate the committed config reference from the registry (mutating).
config-docs:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_config_reference.py

# Verify the committed config reference matches a fresh generation (CI gate).
config-docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python -c "from pathlib import Path; from scripts.gen_config_reference import write_reference; write_reference(Path('$tmp'))"
    if ! diff -u docs/guide/reference/config.md "$tmp"; then
        echo "config reference is stale — run 'just config-docs' and commit" >&2
        exit 1
    fi

# Regenerate the packaged MCP doc-resource snapshots from canonical docs/ (ADR-0151).
resources-docs:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_doc_resources.py

# Verify the committed doc-resource snapshots match canonical docs/ (CI gate, ADR-0151).
resources-docs-check:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_doc_resources.py --check

# Regenerate code-derived doc constants (tool count, upload ceiling) from source (ADR-0410).
doc-constants:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python -m scripts.gen_doc_constants

# Verify code-derived doc constants match their source of truth (CI gate, ADR-0410).
doc-constants-check:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python -m scripts.gen_doc_constants --check

# Regenerate the role->tool visibility matrix in docs/guide/safety-and-rbac.md (#347).
rbac-matrix:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_rbac_tool_matrix.py

# Verify the committed role->tool visibility matrix is current (also gated by `just test`).
rbac-matrix-check:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_rbac_tool_matrix.py --check

# Regenerate the committed kdivectl verb descriptors from the live tool schemas (mutating, #1447).
cli-verbs:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_cli_verbs.py

# Verify the committed kdivectl verb descriptors match a fresh generation (CI gate, #1447).
cli-verbs-check:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/gen_cli_verbs.py --check

# Structural guard: no KDIVE_* env read outside kdive.config (ADR-0087). Stdlib-only.
config-guard:
    uv run python scripts/config_env_guard.py

# Coverage guard: every KDIVE_* token is documented (registry or external_env.py). Stdlib-only.
env-docs-check:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/check_env_documented.py

# Fail when the pinned mcp library no longer advertises the protocol range src/kdive/mcp
# declares (ADR-0537). Offline by design — the upstream half runs on a weekly cron
# (mcp-spec-drift.yml), so no PR depends on github.com being reachable.
mcp-spec-check:
    PYTHONPATH="{{WORKTREE_PYTHONPATH}}" uv run python scripts/check_mcp_spec_version.py

# Immutability guard: no modify/delete/rename of an existing src/kdive/db/schema/*.sql
# (only new migrations may be added). Applied migrations are byte-immutable (ADR-0015);
# a cosmetic edit breaks upgrades of any DB migrated by an earlier build (#1218). Diffs
# against the base ref, so a committed edit fails on a clean checkout too — diffing against
# HEAD passed every PR CI ever ran (#1723). CI passes the PR's base commit; locally the
# default origin/main is the closest stand-in. Offline by design (ADR-0505): it reads a local
# ref, so `git fetch origin main` first. Stdlib-only (git only).
schema-guard base_ref="origin/main":
    python3 scripts/schema_immutable_guard.py {{base_ref}}

# Ordering guard: a migration this branch adds must be numbered strictly above the highest
# version already on origin/main. Pre-assigned numbers stop filename collisions but not
# out-of-order merges (#1553 landed 0085 after 0086), which apply cleanly on a fresh DB and
# out of order on an existing one (#1720). Offline by design (ADR-0505): it reads the local
# origin/main, so `git fetch origin main` first. Stdlib-only (git only).
migration-order-check:
    python3 scripts/migration_ordering_guard.py

# Drift guard: the docker-compose image set matches the ADR-0356 arch-support matrix, and each
# handling token meets its ppc64le obligation (ADR-0356). Parses compose via yaml.safe_load.
container-arch-check:
    uv run python scripts/check_container_arch_matrix.py

# Assert the Helm chart's appVersion tracks the pyproject version (spec A3). A drift
# would let a cut release point the chart's default image tag at a tag that was never
# published. Run in CI and `just ci`.
# Scope is deliberately appVersion ONLY. Chart.yaml `version` is the chart-package version
# on its own SemVer track and is NOT constrained here (ADR-0365) — coupling the two would
# turn every legitimate chart-only bump into a CI failure.
chart-version-check:
    #!/usr/bin/env bash
    set -euo pipefail
    # scripts/pyproject-version.sh reads a colour-free version regardless of the caller's
    # environment (#1883, #1886) — see its header for why.
    pyproject="$({{justfile_directory()}}/scripts/pyproject-version.sh)"
    chart="$(grep -E '^appVersion:' deploy/helm/kdive/Chart.yaml | sed -E 's/^appVersion:[[:space:]]*"?([^"]+)"?[[:space:]]*$/\1/')"
    if [[ "$chart" != "$pyproject" ]]; then
        echo "::error::Chart.yaml appVersion ($chart) != pyproject version ($pyproject)." >&2
        echo "Run 'just set-version $pyproject' or align Chart.yaml appVersion." >&2
        exit 1
    fi
    echo "appVersion == pyproject == $pyproject"

# Run the full gate that PR CI runs, reproducible locally.
ci: lint type lock-check lint-shell lint-ansible test-ansible lint-workflows check-mermaid docs-links docs-paths served-doc-links adr-status-check docs-check config-docs-check config-guard env-docs-check mcp-spec-check schema-guard migration-order-check container-arch-check resources-docs-check doc-constants-check chart-version-check cli-verbs-check test
