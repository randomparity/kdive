# Implementation Plan: Bind Compose backends to loopback by default (#1918)

**Branch:** `feat/loopback-compose-backends-1918`
**BASE_BRANCH:** `main`
**Guardrails:** `just ci`
**ADR:** `docs/adr/0554-compose-backends-bind-loopback-by-default.md`
**Status:** Accepted (2026-08-06)

## Context checkpoint

- branch: `feat/loopback-compose-backends-1918`
- BASE_BRANCH: `main`
- guardrail: `just ci`
- open findings: none

## Tasks

### 1. Write ADR-0554 ✅

`docs/adr/0554-compose-backends-bind-loopback-by-default.md` — already done.

### 2. Update `docker-compose.yml` — add `127.0.0.1:` bind prefix to four backend ports

Files touched: `docker-compose.yml`

Change each of the four backend `ports:` entries:
```yaml
# before
- "${KDIVE_POSTGRES_PORT:-5432}:5432"
- "${KDIVE_MINIO_PORT:-9000}:9000"
- "${KDIVE_MINIO_CONSOLE_PORT:-9001}:9001"
- "${KDIVE_OIDC_PORT:-8090}:8080"

# after
- "127.0.0.1:${KDIVE_POSTGRES_PORT:-5432}:5432"
- "127.0.0.1:${KDIVE_MINIO_PORT:-9000}:9000"
- "127.0.0.1:${KDIVE_MINIO_CONSOLE_PORT:-9001}:9001"
- "127.0.0.1:${KDIVE_OIDC_PORT:-8090}:8080"
```

Also update the header comment that shows the recommended env var values, removing the
implication that only `localhost:PORT` is needed (it already is localhost — the comment
stays accurate).

### 3. Update `test_compose_config.py::test_backend_host_port_is_overridable`

File: `tests/compose/test_compose_config.py`

The test currently passes `override = "17777"` (a bare port) and asserts `override in published`.
With loopback-bound defaults, `docker compose config` renders the override as `0.0.0.0:17777`
when the override is a bare port — the `0.0.0.0` comes from Docker interpreting the bare
`17777:CONTAINER` mapping the same way (bind `0.0.0.0`). The assertion `"17777" in published`
still passes because `_published_ports` returns `{str(p.get("published")) for p in ...}` and
`p.get("published")` is `"17777"` (the published/host port segment, not the host binding IP).

Check the actual `docker compose config` output format to confirm. If `published` stays as the
numeric/string port regardless of bind address, the test requires **no change**. This needs a
quick verification step before deciding.

Verification: run `docker compose config --format json` with an override and inspect
`.services.postgres.ports[0]` to see what key holds the bind address vs the port number.

### 4. Update `docs/operating/docker-compose.md`

Add a note in the relevant section that backend ports bind `127.0.0.1` by default and that
a full `ADDR:PORT` override (e.g. `KDIVE_POSTGRES_PORT=0.0.0.0:5432`) is the opt-in path
for remote access.

### 5. Update `deploy/compose/README.md`

Same note as above — the reference note that backend ports are `localhost`-only by default.

### 6. Commit all changes and run `just ci`

Commit message format:
```
fix(compose): bind backend ports to 127.0.0.1 by default (#1918)

ADR-0554. A bare HOST:CONTAINER port mapping binds 0.0.0.0, so fixed-credential
Postgres, MinIO, and the mock OIDC issuer were reachable from every interface.
Prefixing each mapping with 127.0.0.1: confines them to loopback. Remote access
remains available through the existing KDIVE_*_PORT override variables, which
accept a full ADDR:PORT left side.
```
