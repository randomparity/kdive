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
 && install -d -o tester -g tester /work /home/tester/.cache /home/tester/.cache/uv
COPY run-suite.sh /usr/local/bin/run-suite
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/run-suite"]
