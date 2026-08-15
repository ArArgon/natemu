#!/bin/bash
# Fetch the SQLite amalgamation used as the compile workload's source.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$HERE/work"
Z=sqlite-amalgamation-3500400.zip
[ -f "$Z" ] || curl -sS -L -o "$Z" "https://www.sqlite.org/2025/$Z"
shasum -a 256 "$Z"
unzip -o -q "$Z"
echo OK
