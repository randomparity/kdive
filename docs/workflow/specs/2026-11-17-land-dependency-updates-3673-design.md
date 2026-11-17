# Land the #3567 and #2096 dependency updates (#3673) — design

## Problem

Dependabot PRs #3567 (python-dependencies, `pyproject.toml` + `uv.lock`, 15 updates) and
#2096 (docker-images, `Dockerfile`) each fail CI for code reasons: ruff 0.17 `SIM102`,
the cryptography policy test, two pydantic 2.14 failures, the uv pin parity guard, and
the image build. `uv_build` and the Dockerfile uv pins move together, so the two PRs land
as one branch.

## Design

**Versions.** Merge both PR heads (`git fetch origin pull/3567/head pull/2096/head`) onto
a branch from `main`. `uv lock --check` passes against the merged `pyproject.toml`.

**uv pins.** Every uv pin moves to 0.13.7: the amd64/arm64 `ghcr.io/astral-sh/uv` image
digests, the ppc64le `uv==` wheel pin (Dependabot rewrites `FROM` lines only, which is
what `tests/image/test_dockerfile_uv_pins.py` guards), and `tests/container/Dockerfile`.

**Python base image — deliberate deviation from #2096.** #2096 also moves the base image
from `python:3.14.6-slim-bookworm` to `python:3.15.0-slim-bookworm`. `requires-python` is
`==3.14.*` and ADR-0817 keeps 3.15 as an informational-only lane, so a 3.15.0 image cannot
install the project. The 3.14.6 digests stay. Moving the interpreter major is a separate
decision owned by ADR-0817.

**pydantic 2.14.3.** Cause: 101 `BaseModel` classes declare `schema_: ... = Field(...,
alias="schema")` under `extra="forbid"`. Through 2.14.0 an input key equal to the *field
name* of an aliased field was silently dropped (neither used nor reported as extra); from
2.14.3 it raises `extra_forbidden`. The behaviour is the documented one (validate by alias
only), so no model changes. Wire and persistence paths already serialize with
`by_alias=True` (`dump_payload`, `AuthorityRequestSender`, the transport envelope, the
system-authority repository). The two failing tests dumped the unaliased form by accident
and relied on the silent drop; they now dump by alias, and a new test pins the rejection of
the unaliased key. No wire contract changes. Residual risk: a persisted or in-flight
payload containing a literal `schema_` key would now be rejected instead of ignored; no
production writer produces one.

**ruff 0.17.** The two `SIM102` sites fold the nested `if` into the preceding `elif`
condition; neither chain has a trailing `else`, so behaviour is unchanged.

**ty 0.0.99.** `ty check` exits 1 on any diagnostic, so warnings gate. Changes:
`@contextmanager`/`@asynccontextmanager` functions annotate `Generator`/`AsyncGenerator`
instead of `Iterator`/`AsyncIterator` (247 sites, scripted from ty's own locations);
the libvirt monitor aliases and drgn `follow_phys` seam take `Any` handles; renamed
`unresolved-import` ignores become `import-not-found`; ignores ty no longer needs are
removed; Literal-narrowing field overrides in pydantic models and test doubles get
per-site ignores; always-true assertions and redundant casts are removed or made explicit.

## Failure model

- A pin drifts between arches: `test_all_uv_pins_agree_across_arches`.
- The lock disagrees with `pyproject.toml`: `uv lock --check` in `just ci`.
- An unaliased payload reaches a closed model: `extra_forbidden`, now a loud failure.
