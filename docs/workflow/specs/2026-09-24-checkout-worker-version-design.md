# Checkout worker build identity

## Problem

The portable live worker imports KDIVE from a source checkout but starts with `/` as its working directory under a dedicated account. `kdive.version` asks Git in that directory and reports no commit. The live-stack skew probe therefore cannot compare its build with the test checkout. It also probes a lifecycle witness that the portable three-role stack does not deploy and reports its absence as an unexplained unknown. Issue #2737 and ADR-0482 govern these observations.

## Scope

The runtime version resolver owns source commit identity. It locates the repository at the root of the imported `kdive` source tree, verifies that the root itself has Git metadata, and asks Git there for the commit, tag, and dirty state. The Git invocation must accept that the running account differs from the checkout owner without changing global Git configuration. A checkout outside Git remains unknown. The baked `_buildinfo` path still wins over live Git. The worker launcher keeps selecting checkout source through `PYTHONPATH`; it does not export a second commit source.

The live-stack skew probe keeps grading a worker that reports a commit with its existing ancestry rules. When the lifecycle-witness endpoint is absent in the portable three-role stack, its result explains that this role is Kubernetes-only and is not deployed here. That observation warns but never causes a strict-policy skip. A running witness is still graded normally. No Kubernetes deployment or baked artifact generation changes are included.

### Failure model

- Git absent, inaccessible, timed out, or invalid checkout: report an unknown commit, preserving the current fallback.
- Foreign checkout ownership: permit only the imported checkout root for the individual Git command; report the checkout commit.
- Source package under a parent Git tree but not itself a checkout: report unknown rather than attributing the parent's commit.
- Portable witness absent: warn with a non-failure explanation for this role; do not skip or claim a build was observed.

## Success

- A worker importing checkout code reports that checkout's commit when its cwd is `/` and its account does not own the checkout.
- The build endpoint supplies a commit the skew probe can resolve and grade.
- The absent portable witness is described as not deployed; a reachable witness is still graded.
- Baked build identity takes precedence over the checkout.

## Validation

- Focused version tests run a real temporary Git checkout from a foreign cwd and with the current account's ownership treated as unsafe; they assert the checkout SHA and unknown fallback outside a checkout.
- Existing and focused health/skew tests verify the endpoint-to-classifier path, baked precedence, and absent versus reachable witness behavior.
- The worker launcher test verifies checkout source selection remains intact.
- Run `just lint`, `just type`, focused pytest, and the pre-push `just ci` gate. If a provisioned x86 live stack is available, run its skew preflight after restarting the app tier and confirm the worker commit is graded. This behavior does not require native ppc64le proof.
