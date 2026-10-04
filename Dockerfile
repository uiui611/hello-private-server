FROM rust:1.98.0-bookworm AS builder
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY static ./static
RUN cargo build --locked --release

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/codex-hello-server /usr/local/bin/codex-hello-server
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/codex-hello-server"]
