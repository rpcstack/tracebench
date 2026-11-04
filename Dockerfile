# syntax=docker/dockerfile:1.7
#
# One builder compiles every tracebench binary once; one runtime image carries
# them all and docker/entrypoint.sh picks the binary from the first argument.
#
#   docker build --build-arg MODE=masa -t rpcstack-tracebench:masa .
#
# MODE selects the policy preset (Cargo feature of the generic-service crate):
#   baseline  generated stubs use NoopHooks (no policy)
#   masa      sched_slo,abort_slo,signal_slack,est_mean_var,ac_pred
# FEATURES, if set, overrides MODE with an explicit Cargo feature list.
#
# The prepared call-graph directories under trace-analysis/ are copied to
# /app/trace-analysis (and symlinked at /trace-analysis), so graph paths in
# config/*.json such as trace-analysis/graph_branch/S_14677443 resolve as-is.

ARG RUST_IMAGE=rust:1-slim-bookworm
ARG RUNTIME_IMAGE=debian:bookworm-slim

FROM ${RUST_IMAGE} AS builder
ARG MODE=baseline
ARG FEATURES=
ARG BUILD_JOBS=
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# rust-toolchain.toml pins the nightly rpcstack needs; install it in its own layer.
COPY rust-toolchain.toml ./
RUN rustup show active-toolchain || rustup toolchain install
COPY Cargo.toml Cargo.lock ./
COPY crates crates
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/src/target,id=tracebench-target-${MODE} \
    set -eu; \
    features="${FEATURES:-}"; \
    if [ -z "$features" ]; then \
      case "$MODE" in \
        baseline) features=baseline ;; \
        masa) features=masa ;; \
        *) echo "unknown MODE=$MODE (expected baseline or masa)" >&2; exit 1 ;; \
      esac; \
    fi; \
    echo "building tracebench with features: $features"; \
    cargo build --locked --release -p generic-service --bins --features "$features" ${BUILD_JOBS:+-j $BUILD_JOBS}; \
    mkdir -p /out; \
    for f in generic-service tracebench-loadgen tracebench-probe; do \
      cp "target/release/$f" /out/; \
    done; \
    echo "$MODE ${features}" > /out/TRACEBENCH_MODE; \
    ls -l /out

FROM ${RUNTIME_IMAGE} AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/ /usr/local/bin/
RUN mv /usr/local/bin/TRACEBENCH_MODE /etc/tracebench-mode
COPY docker/entrypoint.sh /usr/local/bin/tracebench-entrypoint
COPY trace-analysis /app/trace-analysis
RUN ln -s /app/trace-analysis /trace-analysis
WORKDIR /app
ENV LOG_LEVEL=info \
    RUST_BACKTRACE=1
ENTRYPOINT ["/usr/local/bin/tracebench-entrypoint"]
CMD ["service"]
