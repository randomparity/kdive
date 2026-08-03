# Running KDIVE on Kubernetes

The Helm chart under `deploy/helm/kdive` deploys four KDIVE processes
(`server` / `worker` / `reconciler` / `lifecycle-witness`) plus a `migrate` one-shot Job, against
operator-provided Postgres, S3-compatible object storage, and an OIDC issuer.

The chart's value and flag reference is
[`deploy/helm/kdive/README.md`](../../deploy/helm/kdive/README.md). For an end-to-end
bring-up — building and pushing the image, standing up backends, reaching the MCP
endpoint, and verifying — follow [the Kubernetes deploy runbook](runbooks/kubernetes-deploy.md).

## Install

```bash
helm install kdive deploy/helm/kdive \
  --set image.tag=edge \
  --values my-values.yaml
```

The default image tag is the chart `appVersion`, which tracks the next unreleased version
and has no published image until that version is cut. Installing from a source checkout
needs `--set image.tag=edge` to pin the rolling image; a bare `appVersion` default is
correct only when installing a cut release.

## Secrets and values

Backends are external. Supply non-secret connection details through `KDIVE_*` settings and
credentials through the chart's Secret references rather than baking them into the image.
Database access uses five `databaseCredentials.*` refs: migration, server, worker, reconciler,
and lifecycle witness. The migration credential is never present in a runtime Pod.
The lifecycle witness runs as a fourth, dedicated control-plane workload; the reconciler does not
receive its database DSN, broker TLS private key, envelope key, or Kubernetes API token.
Every setting is listed in [the config reference](../guide/reference/config.md); the
chart's README documents which values map to which keys and how the secret is mounted
(non-root containers read the env file at mode 0440 under an `fsGroup`).

The `migrate` Job runs the schema forward before the app workloads start, so the processes
never reach the database ahead of the migration.

## Startup and database reachability

Each process waits up to ten seconds at start for its first database connection before it
begins serving. If it cannot get one it logs an ERROR record reading
`no database connection within 10s of process start` and exits, so the pod restarts — a database outage surfaces as `CrashLoopBackOff` on all three Deployments rather
than as pods that are Running and permanently not-Ready. Check the pod logs for that record
before looking at the KDIVE processes themselves; the usual cause is the database, its
credentials, or a NetworkPolicy, not the chart. The record cannot narrow it further on its
own: the pooling layer reports an unreachable host, a wrong password, and a missing
database identically, so read the `psycopg.pool` warning just above it — that one carries
Postgres's own error.

Because Kubernetes backs a crash loop off to a five-minute ceiling, a pod can lag the
database's own recovery by up to that long. `kubectl rollout restart` on the affected
Deployment clears the backoff once Postgres is answering again.

The ten-second wait is chosen to stay inside the chart's liveness budget
(`initialDelaySeconds: 5`, `periodSeconds: 10` against `/livez`), so a failing process
reports its own error rather than being killed by the probe. If you lower those probe
values, keep the first probe later than the wait or the kubelet will kill a container that
is merely starting slowly. The aux endpoints (`/livez`, `/readyz`, `/metrics`) come up
after the pool opens, so they are unavailable for that window.

### Watching for dropped usage rows

The server records one `tool_invocation` row per tool call on a best-effort basis: if the
write cannot get a database connection within its one-second budget, the row is dropped so
the call itself is never delayed or failed. `/metrics` on the server's aux port counts those
drops as `kdive_mcp_usage_recording_failures`.

A steady zero is the expected reading. A non-zero rate means usage data is incomplete. Read
the `reason` label before anything else: `pool_timeout` is the pool failing to hand out a
connection inside that one-second budget, usually because it had to open a fresh one under
concurrency; `other` is any other failure of the write — a database error or schema drift —
and points at the accompanying WARNING rather than at pool capacity.

There is no setting to tune for the `pool_timeout` case: the pool's minimum size is fixed in
KDIVE's source. What the rate gives you is the evidence for raising it — report it on
[#1535](https://github.com/randomparity/kdive/issues/1535) rather than treating a non-zero
reading as something to configure your way out of.
