# syntax=docker/dockerfile:1
#
# The images the GitOps controller and its build Jobs run (published by the
# release workflow as ghcr.io/pynenc/piceli-controller and
# ghcr.io/pynenc/piceli-builder, one tag per version, pinned by digest).
#
# The build context is a directory holding `dist/` with the piceli wheel, the
# same file PyPI receives, so an image never installs anything newer:
#
#   uv build --wheel --out-dir ctx/dist
#   docker buildx build -f images/Dockerfile --target controller \
#       --platform linux/amd64,linux/arm64 -t REGISTRY/piceli-controller:VERSION --push ctx
#   docker buildx build -f images/Dockerfile --target builder \
#       --platform linux/amd64,linux/arm64 -t REGISTRY/piceli-builder:VERSION --push ctx
#
# Pass `--image REGISTRY/piceli-controller@sha256:…` and `--builder-image
# REGISTRY/piceli-builder@sha256:…` to `piceli gitops enable`.

ARG PYTHON_IMAGE=docker.io/library/python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed
ARG RUST_IMAGE=docker.io/library/rust:1.97.0-slim-bookworm@sha256:6d220bf85c74e842a79da63997af8d2e74455c0b8847d8bb3a5888572334991d

FROM ${RUST_IMAGE} AS rust

# --- controller: piceli (with the UI it serves in the cluster) and git -------
FROM ${PYTHON_IMAGE} AS controller
# Links the GHCR package to the repository (its page, README and access).
LABEL org.opencontainers.image.source="https://github.com/pynenc/piceli" \
      org.opencontainers.image.description="Piceli GitOps controller and in-cluster UI" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,source=dist,target=/tmp/dist \
    set -eu; wheel="$(ls /tmp/dist/piceli-*.whl)"; \
    pip install --no-cache-dir "piceli[ui,telemetry] @ file://${wheel}"
# Last step of the Python files: their digests, verified when the controller
# and UI start (PICELI_SELF_CHECK, set by their Deployments). -B: writing
# the manifest compiles nothing new.
RUN python -B -m piceli.integrity write --out /usr/local/share/piceli/files.sha256
USER 65532:65532
WORKDIR /tmp
CMD ["piceli", "--help"]

# --- builder: the controller plus the host-build tools for Rust and Python --
# cargo (rustup, with the linux and wasm32 targets; clippy and rustfmt since
# 0.19.0), cargo-zigbuild, zig, uv, python3, sh, and what crates with C
# builds need (jemalloc-sys: make; aws-lc-sys: cmake; pkg-config, curl, xz),
# lld (`-C link-arg=-fuse-ld=lld` links large Rust binaries faster than GNU
# ld, 0.22.0) and node with npm (development runs of small JS tests,
# 0.18.0). A source that pins its own Rust, node or wasm tools gets those
# versions in the build cache instead (`[build] toolchains`, 0.22.0). The
# toolchain is read-only; cargo
# writes under $HOME, which the build Job points at its work directory.
FROM controller AS builder
LABEL org.opencontainers.image.description="Piceli cluster build Job (Rust and Python host builds)"
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc g++ libc6-dev make cmake pkg-config curl xz-utils lld nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo/bin /usr/local/cargo/bin
ENV RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:$PATH
RUN rustup target add aarch64-unknown-linux-gnu x86_64-unknown-linux-gnu wasm32-unknown-unknown \
    && rustup component add clippy rustfmt \
    && pip install --no-cache-dir ziglang==0.16.0 cargo-zigbuild==0.23.4 uv==0.12.22 \
    && printf '#!/bin/sh\nexec python3 -m ziglang "$@"\n' > /usr/local/bin/zig \
    && chmod 0755 /usr/local/bin/zig \
    && chmod -R a+rX /usr/local/rustup /usr/local/cargo \
    && python -B -m piceli.integrity write --out /usr/local/share/piceli/files.sha256
USER 65532:65532
