# 0554 — Bind reference Compose backends to loopback by default

## Status

Accepted (2026-08-06)

## Context

`docker-compose.yml` publishes the dev backends with bare `HOST:CONTAINER` port mappings:

```yaml
ports:
  - "${KDIVE_POSTGRES_PORT:-5432}:5432"
  - "${KDIVE_MINIO_PORT:-9000}:9000"
  - "${KDIVE_MINIO_CONSOLE_PORT:-9001}:9001"
  - "${KDIVE_OIDC_PORT:-8090}:8080"
```

A bare `HOST:CONTAINER` mapping without an explicit bind address binds `0.0.0.0`, so every
network interface on the host reaches Postgres (`kdive`/`kdive`), MinIO
(`minioadmin`/`minioadmin`), and the token-minting mock OIDC issuer with credentials that are
literals in the repository.

[ADR-0552](0552-compose-named-backend-data-volumes.md) raised the stakes: the named data
volumes it introduced make the exposure window unbounded. Before ADR-0552 a plain
`docker compose down` orphaned the anonymous volumes and the next `up` started empty. Now
`kdive-pgdata` and `kdive-minio-data` are named and persist, so a laptop or shared box that
ran the demo once retains every prior investigation's rows and artifacts — including the
`sensitive` (unredacted) artifacts the schema models
(`src/kdive/db/schema/0019_artifacts_quarantine_sensitivity.sql`, ADR-0075) — until someone
runs a destructive teardown. ADR-0552 noted this in its Consequences section and tracked the
fix separately as #1918.

Every in-repo consumer of the published ports connects over `localhost`:
- The live-stack host processes (`KDIVE_DATABASE_URL=postgresql://...@localhost:5432/kdive`)
- The test override fixtures (`KDIVE_TEST_PG_URL`, `KDIVE_TEST_S3_URL`)
- `tests/compose/test_compose_volume_persistence_live.py` already overrides all four ports
  with an explicit `127.0.0.1:<port>` form for its isolated runs

No in-repo consumer connects to these backends from a different host.

## Decision

Add a `127.0.0.1:` bind address to the default value of each backend port mapping:

```yaml
ports:
  - "127.0.0.1:${KDIVE_POSTGRES_PORT:-5432}:5432"
  - "127.0.0.1:${KDIVE_MINIO_PORT:-9000}:9000"
  - "127.0.0.1:${KDIVE_MINIO_CONSOLE_PORT:-9001}:9001"
  - "127.0.0.1:${KDIVE_OIDC_PORT:-8090}:8080"
```

The `KDIVE_*_PORT` variables that override the host-side port already accept a full
`ADDR:PORT` left side, so an operator who genuinely needs remote access can override the
default explicitly, e.g. `KDIVE_POSTGRES_PORT=0.0.0.0:5432`.

The Prometheus and Grafana `obs`-profile services are out of scope: they sit behind an
opt-in profile, do not carry fixed-credential literals in the repository, and do not hold
persistent state. They may be addressed in a follow-on if the same posture is desired.

The `server` port (8000) is also out of scope: it carries the MCP endpoint that agents
connect to, and binding it to loopback would prevent a host-process stack from accepting
connections from an agent on another machine. The `server` carries no fixed credentials;
the mock OIDC issuer that mints its tokens is addressed here.

## Consequences

The reference Compose stack is local-only by default. Fixed-credential Postgres, MinIO, and
the mock OIDC issuer are no longer reachable from outside the host without an explicit
`KDIVE_*_PORT` override.

Remote access to these backends becomes an explicit opt-in through the existing
`KDIVE_*_PORT` variables, which already accepted `ADDR:PORT` syntax before this change.

**Test changes.**
`tests/compose/test_compose_config.py::test_backend_host_port_is_overridable` currently passes
a bare port string (`"17777"`) as the override and asserts that string appears in the
published ports. With loopback-bound defaults the `docker compose config` model renders the
published address as the full `host:port` string (e.g. `"0.0.0.0:17777"`), not `"17777"`,
when the override is bare. The test is updated to pass a full `ADDR:PORT` override and assert
on the port segment only, matching what `_published_ports` already extracts as a string.

`tests/image/compose.smoke.override.yml` drops port publishing with `!reset []` so the CI
smoke test does not conflict with a running stack. With loopback-bound defaults the risk of
conflicting with a stack on another interface is already eliminated. The override file remains
correct and is not changed — a smoke that needs no host ports still benefits from `!reset []`.

`tests/compose/test_compose_volume_persistence_live.py` already overrides every port with
`127.0.0.1:<port>` for isolation. With the default now loopback, those overrides remain
correct and are kept as-is — an isolated run with a unique project still needs unique ports.

**Documentation.** The operator-facing docs note that the default posture is loopback and that
a full `ADDR:PORT` override is the opt-in path to remote access.
