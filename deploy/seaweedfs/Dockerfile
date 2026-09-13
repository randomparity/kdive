# ADR-0647: the source archive and multi-architecture bases are immutable inputs.
FROM --platform=$BUILDPLATFORM golang@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628 AS build

ARG TARGETOS
ARG TARGETARCH
ARG SEAWEEDFS_COMMIT=d997fba1575583a89cf0cc50dc0150642286c86d
ARG SEAWEEDFS_TARBALL_SHA256=2e042bfe3e0be78c97af2cd3f5502bdf0b204bfc110b68f719e1b5557ea5a1e0

WORKDIR /src
RUN wget -O seaweedfs.tar.gz "https://codeload.github.com/seaweedfs/seaweedfs/tar.gz/${SEAWEEDFS_COMMIT}" \
    && echo "${SEAWEEDFS_TARBALL_SHA256}  seaweedfs.tar.gz" | sha256sum -c - \
    && tar -xzf seaweedfs.tar.gz --strip-components=1 \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
       go build -trimpath -buildvcs=false -o /out/weed ./weed

FROM alpine@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40
RUN addgroup -S seaweed && adduser -S -G seaweed seaweed \
    && mkdir /data && chown seaweed:seaweed /data
COPY --from=build /out/weed /usr/local/bin/weed
USER seaweed
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8333
ENTRYPOINT ["weed"]
CMD ["mini", "-dir=/data"]
