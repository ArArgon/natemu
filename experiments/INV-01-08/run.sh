#!/bin/sh
# F08-2 reviewed supervisor; see README for explicit external authorization.
# No authorization or native evidence execution is supplied by this wrapper.
set -eu
script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/src/supervisor.py" "$@"
