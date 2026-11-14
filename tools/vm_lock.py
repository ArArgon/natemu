#!/usr/bin/env python3
"""vm_lock.py [--shared] CMD...: run CMD holding the VM lock (natemu-raw/.vm.lock, the file every runner uses).

Exclusive (default) for timed runs: nothing else runs a VM meanwhile. --shared for untimed runs (K1, correctness
suites): they run beside each other and only wait for timed runs, as run_suite.py's parallel steps do. flock and
/usr/bin/lockf locks interoperate on macOS, so this composes with lockf-wrapped and run_io5.py runs."""
import fcntl
import subprocess
import sys

from io_common import VM_LOCK

shared = sys.argv[1:2] == ['--shared']
cmd = sys.argv[2:] if shared else sys.argv[1:]
if not cmd:
    sys.exit(__doc__)
with open(VM_LOCK, 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    sys.exit(subprocess.call(cmd))
