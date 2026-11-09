"""Helpers shared by the run_io_*, run_fio_*, run_virtio_* and run_serial_stress runners."""
import hashlib
import json
import os
import pathlib
import signal
import subprocess

from raw_root import RAW_ROOT

ROOT = pathlib.Path(__file__).resolve().parent.parent
VM_LOCK = RAW_ROOT / '.vm.lock'   # default --lock: one VM-running agent at a time (flock on this file)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def write_json(path, value):
    pathlib.Path(path).write_text(json.dumps(value, indent=2) + '\n')


def stop_group(process):
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired: pass
    # A compiler descendant can survive the main process; terminate the whole session.
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait()
