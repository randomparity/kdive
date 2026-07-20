set shell := ["bash", "-euo", "pipefail", "-c"]

# Pinned git-cliff version — referenced by the changelog recipe and release.yml (one place).
GIT_CLIFF := "git-cliff@2.13.1"

# List available recipes.
default:
    @just --list

# One-command first-time setup: check host deps, sync the venv, install hooks.
setup: check-deps sync install-hooks
    @echo "Development environment is ready."

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

# Run the test suite, excluding the gated live_vm and live_stack suites.
# (oidc_issuer-marked tests stay selected; they skip cleanly without the issuer container.)
#
# `-n auto` runs the suite across all cores via pytest-xdist; each worker gets its own
# session-scoped Postgres/MinIO container, so there is no cross-worker DB contention.
# PYTHONHASHSEED is pinned so every xdist worker collects parametrized tests in the same
# order — a parametrize source backed by a set is ordered by the hash seed, which differs
# per worker, and xdist then aborts with "Different tests were collected". It defaults to 0
# but is overridable: the weekly test-ordering workflow sets PYTHONHASHSEED=random to
# surface any new ordering-dependent test the pinned seed would otherwise mask.
test:
    PYTHONHASHSEED="${PYTHONHASHSEED:-0}" uv run python -m pytest -m "not live_vm and not live_stack" -n auto -q

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
    docker compose up -d --wait postgres minio oidc
    docker compose run --rm minio-init
    ./scripts/live-stack/apply-migrations.sh
    @echo "Backends healthy and schema migrated."
    @echo "Start the app tier with: docker compose up -d migrate server worker reconciler"
    @echo "(or, for the full local-libvirt host path: scripts/live-stack/up.sh)"
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
    docker compose up -d

# Stop the operator backing services and remove their volumes.
compose-down:
    docker compose down -v

# Lint and format-check the shell scripts (recursively under scripts/).
lint-shell:
    shfmt -f scripts deploy/remote-libvirt-guest-helpers deploy/ansible/tests | xargs shellcheck
    shfmt -i 2 -d scripts deploy/remote-libvirt-guest-helpers deploy/ansible/tests

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

# Run the Ansible role regression harness (gdbstub_acl ufw prune, #616).
test-ansible:
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-github-runner-preflight.sh

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

# Guard the ADR status lifecycle: valid status, index in sync, no shipped-but-Proposed
# drift (docs/adr/README.md ratification rule). Stdlib-only (plain python3, no uv sync).
adr-status-check:
    python3 scripts/check_adr_status.py

# M2 portability gate: cumulative core-touch measurement vs the pre-M2 tag (ADR-0076).
# Stdlib-only (plain python3, no uv sync); needs the pre-M2 tag fetched.
m2-gate:
    python3 scripts/m2_portability_gate.py

# Regenerate the committed milestone-end M2 portability report (ADR-0076).
m2-report:
    python3 scripts/m2_portability_gate.py --report > docs/archive/reports/m2-portability.md

# Audit runtime dependencies for known vulnerabilities.
audit:
    #!/usr/bin/env bash
    set -euo pipefail
    reqs="$(mktemp)"
    trap 'rm -f "$reqs"' EXIT
    uv export \
      --no-emit-project \
      --no-dev \
      --no-default-groups \
      --group live \
      --format requirements-txt > "$reqs"
    uv run --with 'pip-audit==2.10.0' pip-audit --no-deps --strict -r "$reqs"

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
    current="$(uv version --short)"
    [[ "$current" == "{{VERSION}}" ]] || { echo "pyproject version $current != {{VERSION}}" >&2; exit 1; }
    git tag -a "v{{VERSION}}" -m "Release v{{VERSION}}"
    git push origin "v{{VERSION}}"
    echo "Pushed tag v{{VERSION}}. NEXT: open a 'chore(release): begin <next>-dev' PR"
    echo "(just set-version <next>) — CHANGELOG auto-syncs on merge; see docs/development/releasing.md."

# Regenerate the agent-facing tool reference from the live registry (mutating).
docs:
    uv run python scripts/gen_tool_reference.py

# Verify the committed tool reference matches a fresh generation (CI gate).
docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    uv run python -c "from scripts.gen_tool_reference import write_reference; from pathlib import Path; write_reference(Path('$tmp'))"
    # config.md is generated separately (just config-docs-check); exclude it from the
    # tool-reference directory diff so the two generators can share docs/guide/reference/.
    if ! diff -ru --exclude=config.md docs/guide/reference "$tmp"; then
        echo "tool reference is stale — run 'just docs' and commit" >&2
        exit 1
    fi

# Regenerate the committed config reference from the registry (mutating).
config-docs:
    uv run python scripts/gen_config_reference.py

# Verify the committed config reference matches a fresh generation (CI gate).
config-docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    uv run python -c "from pathlib import Path; from scripts.gen_config_reference import write_reference; write_reference(Path('$tmp'))"
    if ! diff -u docs/guide/reference/config.md "$tmp"; then
        echo "config reference is stale — run 'just config-docs' and commit" >&2
        exit 1
    fi

# Regenerate the packaged MCP doc-resource snapshots from canonical docs/ (ADR-0151).
resources-docs:
    uv run python scripts/gen_doc_resources.py

# Verify the committed doc-resource snapshots match canonical docs/ (CI gate, ADR-0151).
resources-docs-check:
    uv run python scripts/gen_doc_resources.py --check

# Regenerate the role->tool visibility matrix in docs/guide/safety-and-rbac.md (#347).
rbac-matrix:
    uv run python scripts/gen_rbac_tool_matrix.py

# Verify the committed role->tool visibility matrix is current (also gated by `just test`).
rbac-matrix-check:
    uv run python scripts/gen_rbac_tool_matrix.py --check

# Structural guard: no KDIVE_* env read outside kdive.config (ADR-0087). Stdlib-only.
config-guard:
    uv run python scripts/config_env_guard.py

# Coverage guard: every KDIVE_* token is documented (registry or external_env.py). Stdlib-only.
env-docs-check:
    uv run python scripts/check_env_documented.py

# Immutability guard: no modify/delete/rename of an existing src/kdive/db/schema/*.sql
# (only new migrations may be added). Applied migrations are byte-immutable (ADR-0015);
# a cosmetic edit breaks upgrades of any DB migrated by an earlier build (#1218). Diffs
# against HEAD, so a clean tree passes and a staged edit fails. Stdlib-only (git only).
schema-guard:
    python3 scripts/schema_immutable_guard.py

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
    pyproject="$(uv version --short)"
    chart="$(grep -E '^appVersion:' deploy/helm/kdive/Chart.yaml | sed -E 's/^appVersion:[[:space:]]*"?([^"]+)"?[[:space:]]*$/\1/')"
    if [[ "$chart" != "$pyproject" ]]; then
        echo "::error::Chart.yaml appVersion ($chart) != pyproject version ($pyproject)." >&2
        echo "Run 'just set-version $pyproject' or align Chart.yaml appVersion." >&2
        exit 1
    fi
    echo "appVersion == pyproject == $pyproject"

# Run the full gate that PR CI runs, reproducible locally.
ci: lint type lock-check lint-shell lint-ansible test-ansible lint-workflows check-mermaid docs-links docs-paths adr-status-check docs-check config-docs-check config-guard env-docs-check schema-guard container-arch-check resources-docs-check chart-version-check test
