#!/bin/bash
# Build the guest_rates TCG plugins against Homebrew QEMU 11.0.3's plugin header.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HERE/bin"
for src in plugin_regs plugin_rates; do
    [ -f "$HERE/$src.c" ] || continue
    cc -O2 -shared -fPIC -o "$HERE/bin/$src.so" "$HERE/$src.c" \
       -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) \
       -undefined dynamic_lookup
    echo "built bin/$src.so"
done
