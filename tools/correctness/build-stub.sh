#!/bin/bash
# build-stub.sh WORKTREE: build and sign the natemu boot stub and the release runtime in a clean worktree (boot-handoff §1).
set -euo pipefail
cd "$1"
cargo +1.97.0 build --offline --locked --release -p natemu-cli
codesign --force --sign - --entitlements crates/host/entitlements.plist target/release/natemu
cargo +1.97.0 build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
git rev-parse HEAD; git status --short | wc -l
shasum -a 256 target/release/natemu target/aarch64-unknown-none/release/natemu-runtime
