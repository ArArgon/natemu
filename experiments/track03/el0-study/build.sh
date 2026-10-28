#!/bin/sh
# usage: build.sh OUTDIR   -> OUTDIR/probe (signed with the hypervisor entitlement)
set -e; here=$(cd "$(dirname "$0")" && pwd); mkdir -p "$1"
xcrun clang -arch arm64 -isysroot "$(xcrun --show-sdk-path)" -std=c11 -Wall -Wextra -Werror -O2 -pthread "$here/probe.c" -framework Hypervisor -o "$1/probe"
codesign --force --sign - --entitlements "$here/entitlements.plist" "$1/probe"
