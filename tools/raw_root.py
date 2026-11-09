"""Where raw results and inputs live: $NATEMU_RAW_ROOT, else <repo>/../natemu-raw (the same default as tools/with-env)."""
import os
import pathlib

RAW_ROOT = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT') or pathlib.Path(__file__).resolve().parents[1].parent / 'natemu-raw')
