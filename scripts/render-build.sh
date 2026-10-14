#!/usr/bin/env bash
# Render's build command (set in the dashboard as `bash scripts/render-build.sh`),
# kept here so changing how the server builds is a reviewed commit rather
# than a dashboard edit.
#
# Builds the server (rust/). Cargo's home and its build directory live
# under ~/.cache, which Render's build cache keeps between deploys, so a
# deploy rebuilds only what changed. Its own toolchain, installed there
# once, rather than whatever the image has. The binary is copied into the
# checkout, which is what the running service gets.
set -euo pipefail

export CARGO_HOME="$HOME/.cache/ctt-cargo"
export RUSTUP_HOME="$HOME/.cache/ctt-rustup"
export CARGO_TARGET_DIR="$HOME/.cache/ctt-target"
export PATH="$CARGO_HOME/bin:$PATH"
if [ ! -x "$CARGO_HOME/bin/cargo" ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path
fi
cargo --version
cargo build --release --locked --manifest-path rust/Cargo.toml --bin time-tracker-server
mkdir -p bin
cp "$CARGO_TARGET_DIR/release/time-tracker-server" bin/time-tracker-server
