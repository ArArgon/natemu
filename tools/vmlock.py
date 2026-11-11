#!/usr/bin/env python3
"""Run a command under the shared VM lock, for anything that starts a VM outside run_suite (which takes it itself).

    python3 tools/vmlock.py shared|exclusive COMMAND [ARG ...]      lock: NATEMU_RAW_ROOT/.vm.lock

The one lock primitive is BSD flock(2) on that file: Python's fcntl.flock, the suite, the I/O runners and `/usr/bin/lockf -k`
(macOS lockf is flock-based and exclusive only) all exclude each other. fcntl.lockf / F_SETLK record locks do NOT: never use them
on this file. Shared: many parallel, correctness-only VM runs. Exclusive: timing-sensitive runs and benchmarks (what
`lockf -k` takes). Waits for the lock."""
import fcntl, os, pathlib, sys

RAW = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', pathlib.Path(__file__).resolve().parent.parent.parent / 'natemu-raw'))


def main():
    assert len(sys.argv) > 2 and sys.argv[1] in ('shared', 'exclusive'), __doc__
    fd = os.open(RAW / '.vm.lock', os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_SH if sys.argv[1] == 'shared' else fcntl.LOCK_EX)
    os.set_inheritable(fd, False)
    os.execvp(sys.argv[2], sys.argv[2:])     # the lock stays with this process image's open file description until exit


if __name__ == '__main__': main()
