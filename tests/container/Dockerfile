# syntax=docker/dockerfile:1
# Test image for `just test-linux` (ADR-0717). It builds at the engine's native architecture,
# so it has no --platform. The bases reuse the root Dockerfile's pins; dependabot keeps both.
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.15.1-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853
COPY --from=uv /uv /uvx /usr/local/bin/
# libvirt-dev and the build tools compile libvirt-python. The slim base lacks tools that CI's
# runner has and tests call: `ss` (iproute2), `ps`/`pgrep` (procps), `ssh-keygen`
# (openssh-client), and the system /usr/bin/python3 that host scripts run (python3). The Docker
# CLI and compose plugin reach the host engine socket.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git gnupg iproute2 libvirt-dev openssh-client \
    pkg-config procps python3 \
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
