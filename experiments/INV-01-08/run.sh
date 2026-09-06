#!/bin/sh
# F08-2 external snapshot runner; follow the investigation review workflow.
set -eu
script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/src/supervisor.py" "$@"
