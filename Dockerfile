# syntax=docker/dockerfile:1
# kdive control-plane image (ADR-0088): one multi-stage image for all three
# entrypoints (server/worker/reconciler) plus the migrate one-shot, built to
# drive the remote-libvirt and fault-inject providers over the network.
# local-libvirt stays a venv-on-a-libvirt-host dev/CI provider, not containerized.

# uv binary provider, resolved per target arch (ADR-0359). astral-sh publishes uv
# container images for amd64/arm64 only; ppc64le has no such image, so that arch installs
# the pinned uv wheel from PyPI onto the python base and exposes it at /uv. amd64/arm64
# copy from the pinned upstream image exactly as before, so their `/uv` bytes are unchanged.
# The unselected provider stages are pruned by BuildKit and never resolved, so the amd64/arm64
# builds never touch the ppc64le path and the ppc64le build never resolves the astral image.
ARG TARGETARCH
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv-amd64
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv-arm64
FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS uv-ppc64le
RUN pip install --no-cache-dir uv==0.11.19 && cp "$(command -v uv)" /uv
FROM uv-${TARGETARCH} AS uv

# Builder: resolve the uv environment (deps first for layer caching, then project).
FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS builder
COPY --from=uv /uv /usr/local/bin/uv
# libvirt-python ships no wheels; it compiles against the libvirt headers via
# pkg-config (AGENTS.md). These build-only deps stay in the builder stage and never
# reach the final image, which carries just the runtime shared lib.
#
# On ppc64le several locked deps have no wheel and build from source, so the builder
# needs a wider toolchain — arch-guarded so the amd64 layer's package set (and thus
# image size/contents) is unchanged (ADR-0359). pydantic-core is NOT among them: it
# publishes a cp314 ppc64le wheel, so no Rust toolchain is required. The source-builds
# are all C/C++: grpcio (C++, needs g++ plus system OpenSSL/zlib headers — its vendored
# BoringSSL has no ppc64le target, see the GRPC_* env below); drgn (compiles libdrgn against
# elfutils, needs libelf-dev/libdw-dev + autotools); libvirt-python/pyyaml/markupsafe (C, gcc).
# libkdumpfile-dev gives the source-built drgn kdump-core support: the amd64 wheel vendors
# it, but a from-source drgn links the system lib, and without it ppc64le kdump capture
# silently fails (ADR-0355) — so it is a hard build dep here, not optional.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libc6-dev libvirt-dev pkg-config \
    && if [ "${TARGETARCH}" = "ppc64le" ]; then \
         apt-get install -y --no-install-recommends \
           g++ libelf-dev libdw-dev libkdumpfile-dev libssl-dev zlib1g-dev \
           autoconf automake libtool autoconf-archive make gawk; \
       fi \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# link-mode=copy: the uv cache mount and /opt/venv are on different filesystems, so
# hardlinking falls back to a copy with a warning; ask for the copy explicitly.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Build the from-source grpcio against system OpenSSL/zlib (installed above for ppc64le):
# grpcio's vendored BoringSSL has no ppc64le target (`target.h: #error "Unknown target CPU"`),
# so the vendored TLS build fails on that arch. These knobs are inert on amd64/arm64, where
# grpcio installs from a wheel and never compiles; the builder stage is discarded either way,
# so the shipped image is unaffected.
ENV GRPC_PYTHON_BUILD_SYSTEM_OPENSSL=1 GRPC_PYTHON_BUILD_SYSTEM_ZLIB=1
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group live --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --group live

# Bake version provenance (ADR-0370): a hermetic build has no .git (and this slim stage has no
# git binary), so the commit + release flag come in as build args and stamp _buildinfo.py, which
# rides into the final image on the existing `COPY --from=builder /app/src`. Declared after the
# uv-sync layers so a changing commit never busts that cache. Skipped when no arg is passed (a
# local `docker build`, the ci.yml PR build passes it), leaving today's live-git-less
# `X.Y.Z-dev` reporting rather than a misleading `unknown` commit.
ARG KDIVE_COMMIT=""
ARG KDIVE_RELEASE="false"
RUN if [ -n "$KDIVE_COMMIT" ]; then \
      KDIVE_BUILDINFO_COMMIT="$KDIVE_COMMIT" ./scripts/stamp-buildinfo.sh "$KDIVE_RELEASE"; \
    fi

# Final: slim base + worker toolchain (drives remote-libvirt over the network).
FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30
# All real bookworm packages. drgn is installed from the locked `live`
# dependency group, not apt: bookworm ships only the python3-drgn library,
# whose CLI/version is unproven for the `drgn --version` build check. libelf1,
# libdw1, and zlib1g are drgn's runtime shared libraries.
#
# The second package line is the kernel-build toolchain the worker's Build plane invokes
# at job time (ADR-0146): a Linux `make` hard-requires flex/bison/bc; the warm-tree/server
# build lane also shells out to git (patch_ref + git-clone lane), rsync (warm-tree mirror),
# and xz; and the kernel build compiles scripts/sign-file, scripts/extract-cert, and
# objtool against the libssl-dev/libelf-dev headers. Without these the build lane cannot
# compile a kernel on the shipped image.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc make binutils gdb libvirt-clients openssh-client \
      libelf1 libdw1 zlib1g \
      flex bison bc git rsync xz-utils libssl-dev libelf-dev \
    && if [ "${TARGETARCH}" = "ppc64le" ]; then \
         apt-get install -y --no-install-recommends libkdumpfile10; \
       fi \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
# Put the venv on PATH before verification so the bare `drgn` check resolves.
# PYTHONPATH backs the editable project install at the copied src path.
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src \
    KDIVE_BUILD_WORKSPACE=/var/lib/kdive/build \
    KDIVE_INSTALL_STAGING=/var/lib/kdive/install
# Fail the build (not just the gated smoke test) if any worker tool is missing/broken.
RUN drgn --version && gdb --version && virsh --version && gcc --version && make --version
# Guard the kernel-build toolchain (ADR-0146): the build binaries must answer --version, and
# the -dev packages are verified by the header files the kernel build consumes (no --version).
RUN flex --version && bison --version && bc --version \
    && git --version && rsync --version && xz --version \
    && test -f /usr/include/openssl/ssl.h && test -f /usr/include/libelf.h
# Fixed non-root uid 10001 (k8s runAsNonRoot convention) so compose/Helm can chown the
# mounted writable volumes to a known owner. --no-log-init avoids a sparse lastlog
# allocation for the high uid; not --system (that caps the uid below SYS_UID_MAX).
RUN useradd --create-home --no-log-init --uid 10001 kdive \
    && mkdir -p /var/lib/kdive/build /var/lib/kdive/install \
    && chown -R kdive:kdive /var/lib/kdive
USER kdive
WORKDIR /app
ENTRYPOINT ["python", "-m", "kdive"]
CMD ["server"]
