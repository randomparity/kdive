"""The container arch-support matrix drift guard (ADR-0356).

The guard fences the ``docker-compose.yml`` image set against the arch-support matrix embedded
in ADR-0356, and asserts a per-handling-token ppc64le obligation so the ppc64le core-loop
invariant is machine-checkable, not a prose promise. These tests drive ``evaluate`` with
crafted compose + matrix strings (one per contract clause) plus a live pin against the real
repo files.
"""

from __future__ import annotations

import pytest
import yaml

from scripts.check_container_arch_matrix import (
    ADR_PATH,
    COMPOSE_PATH,
    evaluate,
    parse_compose,
)

# A minimal-but-representative compose: a YAML anchor + merge key, an opt-in `obs` profile, a
# locally-built app image, and a top-level `volumes:` block whose child must NOT be read as a
# service. Image set matches GOOD_MATRIX below.
#: A digest-pinned kdive-published mirror reference, used by the publish-mirror fixtures.
_PUB_MIRROR = "ghcr.io/randomparity/mock-oauth2-server@sha256:" + "a" * 64

GOOD_COMPOSE = f"""\
x-common: &common
  restart: unless-stopped
services:
  db:
    image: postgres:17
    <<: *common
  oidc:
    image: ghcr.io/navikt/mock-oauth2-server:3.0.3
  oidcpub:
    image: {_PUB_MIRROR}
    build: ./deploy/mock-oidc
  prometheus:
    image: prom/prometheus:v3.12.0
    profiles: ["obs"]
  grafana:
    image: grafana/grafana:13.0.3
    profiles: ["obs"]
  app:
    build: .
    image: kdive:dev
volumes:
  postgres_data:
"""

# All five handling tokens exercised: rely-on-upstream (ppc64le published), mirror (cites an
# issue), publish-mirror (digest-pinned, ppc64le ✅), accept-gap (opt-in only), build-local
# (— arch cells, built by a service).
GOOD_MATRIX = f"""\
intro prose
<!-- arch-matrix:begin -->
| Image | Role | amd64 | arm64 | ppc64le | Handling |
|---|---|:---:|:---:|:---:|---|
| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |
| `ghcr.io/navikt/mock-oauth2-server:3.0.3` | oidc #1183 | ✅ | ✅ | ❌ | mirror |
| `{_PUB_MIRROR}` | oidc published #1184 | ✅ | ❌ | ✅ | publish-mirror |
| `prom/prometheus:v3.12.0` | obs | ✅ | ✅ | ✅ | rely-on-upstream |
| `grafana/grafana:13.0.3` | obs | ✅ | ✅ | ❌ | accept-gap |
| `kdive:dev` | app image | — | — | — | build-local |
<!-- arch-matrix:end -->
trailing prose
"""


def _has(violations: list[str], needle: str) -> bool:
    return any(needle in v for v in violations)


def test_clean_pair_has_no_violations() -> None:
    assert evaluate(GOOD_COMPOSE, GOOD_MATRIX) == []


def test_real_repo_files_pass() -> None:
    """The live pin: the shipped compose file and ADR-0356 matrix are self-consistent."""
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    adr = ADR_PATH.read_text(encoding="utf-8")
    assert evaluate(compose, adr) == []


def test_top_level_volumes_child_is_not_a_service() -> None:
    """`postgres_data` under the top-level `volumes:` must not be parsed as an image-less
    service (yaml.safe_load reads only the services mapping)."""
    images = parse_compose(GOOD_COMPOSE)
    assert "postgres:17" in images
    assert set(images) == {
        "postgres:17",
        "ghcr.io/navikt/mock-oauth2-server:3.0.3",
        _PUB_MIRROR,
        "prom/prometheus:v3.12.0",
        "grafana/grafana:13.0.3",
        "kdive:dev",
    }


def test_merge_key_service_still_carries_its_image() -> None:
    """A service using `<<: *anchor` is parsed normally (safe_load resolves the merge)."""
    images = parse_compose(GOOD_COMPOSE)
    assert not images["postgres:17"].built
    assert images["postgres:17"].default_profile


def test_env_default_interpolation_resolves_to_default() -> None:
    """The app image is parameterized `${KDIVE_IMAGE:-kdive:dev}` so the compose smoke can drive
    a pre-built tag (ADR-0359); the guard must read it as its default `kdive:dev` — matching the
    matrix — not as the literal interpolation string. Its `build:` usage is preserved."""
    compose = GOOD_COMPOSE.replace(
        "    image: kdive:dev\n", "    image: ${KDIVE_IMAGE:-kdive:dev}\n"
    )
    images = parse_compose(compose)
    assert "kdive:dev" in images
    assert images["kdive:dev"].built
    assert evaluate(compose, GOOD_MATRIX) == []


def test_image_in_compose_missing_from_matrix() -> None:
    compose = GOOD_COMPOSE.replace("  app:\n", "  cache:\n    image: redis:7\n  app:\n", 1)
    violations = evaluate(compose, GOOD_MATRIX)
    assert _has(violations, "redis:7")


def test_matrix_row_missing_from_compose() -> None:
    matrix = GOOD_MATRIX.replace(
        "<!-- arch-matrix:end -->",
        "| `redis:7` | cache | ✅ | ✅ | ✅ | rely-on-upstream |\n<!-- arch-matrix:end -->",
    )
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, "redis:7")


def test_unknown_handling_token() -> None:
    matrix = GOOD_MATRIX.replace("| build-local |", "| mystery |")
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, "mystery")


def test_rely_on_upstream_requires_published_ppc64le() -> None:
    # Flip postgres (rely-on-upstream) ppc64le ✅ -> ❌.
    matrix = GOOD_MATRIX.replace(
        "| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |",
        "| `postgres:17` | core backend | ✅ | ✅ | ❌ | rely-on-upstream |",
    )
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, "postgres:17")


def test_rely_on_upstream_rejects_malformed_ppc64le_cell() -> None:
    # An empty / prose ppc64le cell on a rely-on-upstream row is fail-closed (not "contains ✅").
    matrix = GOOD_MATRIX.replace(
        "| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |",
        "| `postgres:17` | core backend | ✅ | ✅ | yes | rely-on-upstream |",
    )
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, "postgres:17")


def test_arch_cell_outside_alphabet() -> None:
    matrix = GOOD_MATRIX.replace(
        "| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |",
        "| `postgres:17` | core backend | partial | ✅ | ✅ | rely-on-upstream |",
    )
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, "partial") or _has(violations, "postgres:17")


def test_accept_gap_on_default_profile_image_fails() -> None:
    # Move grafana off the obs profile so it is default-profile, keep its accept-gap row.
    compose = GOOD_COMPOSE.replace(
        '    image: grafana/grafana:13.0.3\n    profiles: ["obs"]\n',
        "    image: grafana/grafana:13.0.3\n",
    )
    violations = evaluate(compose, GOOD_MATRIX)
    assert _has(violations, "grafana/grafana:13.0.3")


def test_empty_profiles_list_is_default_profile() -> None:
    # Docker Compose starts a `profiles: []` service on a bare `up` (len==0 == default), so an
    # accept-gap image gated only by an empty profiles list must be flagged, not passed.
    compose = GOOD_COMPOSE.replace(
        '    image: grafana/grafana:13.0.3\n    profiles: ["obs"]\n',
        "    image: grafana/grafana:13.0.3\n    profiles: []\n",
    )
    assert parse_compose(compose)["grafana/grafana:13.0.3"].default_profile
    assert _has(evaluate(compose, GOOD_MATRIX), "grafana/grafana:13.0.3")


def test_malformed_compose_yaml_propagates() -> None:
    # A syntactically broken compose surfaces a YAMLError (main() catches it into a clean line).
    with pytest.raises(yaml.YAMLError):
        evaluate("services: {oidc: [unterminated\n", GOOD_MATRIX)


def test_mirror_row_requires_issue_reference() -> None:
    matrix = GOOD_MATRIX.replace("oidc #1183", "oidc")
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, "ghcr.io/navikt/mock-oauth2-server:3.0.3")


def test_publish_mirror_requires_published_ppc64le() -> None:
    # Flip the publish-mirror row's ppc64le ✅ -> ❌: a kdive-published mirror must assert ppc64le.
    matrix = GOOD_MATRIX.replace(
        f"| `{_PUB_MIRROR}` | oidc published #1184 | ✅ | ❌ | ✅ | publish-mirror |",
        f"| `{_PUB_MIRROR}` | oidc published #1184 | ✅ | ❌ | ❌ | publish-mirror |",
    )
    violations = evaluate(GOOD_COMPOSE, matrix)
    assert _has(violations, _PUB_MIRROR)


def test_publish_mirror_requires_digest_pin() -> None:
    # Repoint both compose and matrix at a floating tag (set-equality still holds): the
    # publish-mirror obligation rejects it because the reference is not @sha256-pinned.
    floating = "ghcr.io/randomparity/mock-oauth2-server:3.0.3"
    compose = GOOD_COMPOSE.replace(_PUB_MIRROR, floating)
    matrix = GOOD_MATRIX.replace(_PUB_MIRROR, floating)
    violations = evaluate(compose, matrix)
    assert _has(violations, "@sha256")


def test_build_local_requires_a_building_service() -> None:
    # Drop `build: .` from the app service so kdive:dev is pulled, not built.
    compose = GOOD_COMPOSE.replace("    build: .\n    image: kdive:dev\n", "    image: kdive:dev\n")
    violations = evaluate(compose, GOOD_MATRIX)
    assert _has(violations, "kdive:dev")


def test_missing_matrix_block_is_hard_error() -> None:
    with pytest.raises(ValueError):
        evaluate(GOOD_COMPOSE, "no markers here at all\n")


def test_empty_matrix_block_is_hard_error() -> None:
    empty = "<!-- arch-matrix:begin -->\n<!-- arch-matrix:end -->\n"
    with pytest.raises(ValueError):
        evaluate(GOOD_COMPOSE, empty)


def test_header_and_separator_only_block_is_hard_error() -> None:
    header_only = (
        "<!-- arch-matrix:begin -->\n"
        "| Image | Role | amd64 | arm64 | ppc64le | Handling |\n"
        "|---|---|:---:|:---:|:---:|---|\n"
        "<!-- arch-matrix:end -->\n"
    )
    with pytest.raises(ValueError):
        evaluate(GOOD_COMPOSE, header_only)
