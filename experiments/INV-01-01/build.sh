#!/bin/sh
# Build the INV-01-01 capability probe. Apple clang only, no dependency.
# Usage: build.sh [output-dir]  (default: ./build, git-ignored, for local
# dev use only; run.sh passes a mktemp -d directory so evidence runs never
# write under experiments/ — REV-039 C7).
set -eu
cd "$(dirname "$0")"
OUT_DIR="${1:-build}"
mkdir -p "$OUT_DIR"
SDKROOT="$(xcrun --show-sdk-path)"
# -isysroot pins the build to the resolved SDK by construction (REV-039 C9),
# rather than relying on /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
# happening to symlink to the cited MacOSX26.4.sdk.
clang -Wall -Wextra -O1 -isysroot "$SDKROOT" -framework Hypervisor \
    -o "$OUT_DIR/probe" src/probe.c
codesign -s - -f "$OUT_DIR/probe" >/dev/null 2>&1
echo "built: $OUT_DIR/probe (ad-hoc signed, no entitlement); SDK: $SDKROOT"
