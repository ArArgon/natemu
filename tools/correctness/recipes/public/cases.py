#!/usr/bin/env python3
"""cases.py OUT: the public campaign's rows (docs/restructure/correctness-public-kernel.md), one boot each, written as
  OUT/public-cases.tsv   ID<TAB>COMMAND per row; run-public.sh runs line K+1 for part=K/1 (the guest's /ct copy)
  OUT/public-cases.txt   `PART TEST` per row (testkit/harness/campaign.py's list): PART = K/1, TEST = ID
  OUT/public-boot.txt    `PART [kernel=FILE] WORD...` per row: the kernel the part boots (a file of the image folder;
                         none: the stock Debian kernel) and the words added to its command line (pubcap=, the guest cap)
The rows are tools/correctness/public-prepare.py's users-01 and kernel batches, then the follow-ups that changed a
fixture: mremap_test with overcommit and no ASLR (fixtures-01, layout-01), COW on the GUP_TEST kernel (gup-cow-01) and
the IRQ locking and 90 s RCU/SRCU forward-progress torture (torture-01), then the kernel batches' LKMM corpus: one row
lkmm-NAME per lkmm-tests.txt name, its klitmus7 module litmusNNN.ko (NNN: its line, from 000) loaded, run through
/proc/litmus and unloaded, on the test kernel. A row is listed at every vCPU count: the verdict calls a module that
ran no instance (more threads than CPUs, `NAME: skipped`) unsupported, as the original campaign's ineligible N1 runs."""
import pathlib
import sys

LKMM = [n for n in (pathlib.Path(__file__).parent / 'lkmm-tests.txt').read_text().splitlines() if n and not n.startswith('#')]

KERNEL = 'public-kernel'        # the test kernel build.sh builds (Debian's config + the test modules + GUP_TEST)
CAP = 300                       # guest seconds per command (the campaign's default bound)
rows = []                       # (id, command, kernel)


def add(family, name, command, kernel=None):
    rows.append((f'{family}-{name}', command, kernel))


# --- userspace, on the stock Debian 6.12.107 kernel (public-prepare.py users-01, same order) ---
for family, names in {
    'rseq': ['basic_test', 'basic_percpu_ops_test', 'basic_percpu_ops_mm_cid_test'],
    'futex-functional': ['futex_wait_timeout', 'futex_wait_wouldblock', 'futex_requeue_pi',
                         'futex_requeue_pi_signal_restart', 'futex_requeue_pi_mismatched_ops',
                         'futex_wait_uninitialized_heap', 'futex_wait_private_mapped_file',
                         'futex_wait', 'futex_requeue', 'futex_waitv'],
    'membarrier': ['membarrier_test_single_thread', 'membarrier_test_multi_thread'],
    'mm': ['mremap_dontunmap', 'mremap_test', 'madv_populate', 'cow', 'map_fixed_noreplace'],
    'ptrace': ['get_syscall_info', 'get_set_sud', 'vmaccess', 'peeksiginfo'],
    'qemu': ['cmpxchg', 'adox', 'noexec', 'test-1648', 'test-2175', 'test-2413', 'fma', 'cross-modifying-code'],
}.items():
    for name in names:
        add(family, name, f'cd /opt/public/{family} && ./{name}')
for variant in ('param_test', 'param_test_compare_twice', 'param_test_mm_cid'):
    for kind in 'slbmir':
        add('rseq', variant + '-' + kind, f'cd /opt/public/rseq && ./{variant} -t 8 -r 1000 -T {kind}')
for name, options in [('yield', '-y'), ('signal', '-k')]:
    add('rseq', 'param-' + name, f'cd /opt/public/rseq && ./param_test -t 8 -r 1000 -T i -7 -1 -m 2 {options}')
for name, options in [('broadcast', '-b'), ('broadcast-lock', '-b -l'), ('broadcast-owner', '-b -o'),
                      ('lock', '-l'), ('owner', '-o')]:
    add('futex-functional', 'requeue-pi-' + name, f'cd /opt/public/futex-functional && ./futex_requeue_pi {options}')
for name in ['mprotect01', 'mprotect02', 'mprotect03', 'mprotect04', 'mprotect05', 'mmap01', 'mmap02', 'mmap03',
             'mmap04', 'madvise01', 'futex_cmp_requeue01', 'futex_cmp_requeue02', 'futex_wait01', 'futex_wake01']:
    add('ltp', name, f'mkdir -p /tmp/ltp-work && cd /tmp/ltp-work && {name}')
# fixtures-01 / layout-01: the 2 GiB mappings need overcommit, the 40 MiB preamble an address layout without ASLR.
add('mm', 'mremap_test-overcommit-noaslr', 'sysctl -qw vm.overcommit_memory=1 && sysctl -qw kernel.randomize_va_space=0 '
                                           '&& cd /opt/public/mm && ./mremap_test')

# --- kernel tests, on the test kernel (public-prepare.py kernel-*, without lkmm) ---
for name in ('memcpy_kunit', 'usercopy_kunit'):
    add('kunit', name, f'modprobe kunit && modprobe {name} && rmmod {name}', KERNEL)
for name in ('atomic64_test', 'test_bitmap'):
    add('kernel', name, f'modprobe {name} && rmmod {name}', KERNEL)
add('vmalloc', 'normal-cases', 'modprobe test_vmalloc nr_threads=$(nproc) sequential_test_order=1 test_repeat_count=1 '
                               'test_loop_count=100 run_test_mask=1983', KERNEL)
for name in ('spin_lock', 'rw_lock', 'mutex_lock', 'rwsem_lock', 'spin_lock_irq', 'rw_lock_irq'):   # last two: torture-01
    add('locktorture', name, f'modprobe locktorture torture_type={name} onoff_interval=0 stat_interval=10 '
                             f'&& sleep 30 && rmmod locktorture', KERNEL)
for name in ('rcu', 'srcu'):
    add('rcutorture', name, f'modprobe rcutorture torture_type={name} onoff_interval=0 stat_interval=10 '
                            f'&& sleep 30 && rmmod rcutorture', KERNEL)
for name in ('rcu', 'srcu'):                                                                         # torture-01
    add('rcutorture', name + '-fwd', f'modprobe rcutorture torture_type={name} onoff_interval=0 stat_interval=10 '
                                     f'fwd_progress=1 fwd_progress_holdoff=1 && sleep 90 && rmmod rcutorture', KERNEL)
# gup-cow-01: COW with GUP_TEST (debugfs) and eight 2 MiB huge pages.
add('mm', 'cow-gup', 'mount -t debugfs none /sys/kernel/debug && echo 8 > /proc/sys/vm/nr_hugepages '
                     '&& cd /opt/public/mm && ./cow', KERNEL)
# kernel-*'s LKMM corpus (public-prepare.py: insmod, cat /proc/litmus, rmmod), the modules' compiled defaults.
for k, name in enumerate(LKMM):
    m = f'litmus{k:03d}'
    add('lkmm', name, f'insmod /opt/public/lkmm/{m}.ko && cat /proc/litmus && rmmod {m}', KERNEL)

out = pathlib.Path(sys.argv[1])
assert len({r[0] for r in rows}) == len(rows), 'a test id twice'
assert all('\t' not in c and '\n' not in c for _, c, _ in rows)
(out / 'public-cases.tsv').write_text(''.join(f'{i}\t{c}\n' for i, c, _ in rows))
(out / 'public-cases.txt').write_text(''.join(f'{k}/1 {i}\n' for k, (i, _, _) in enumerate(rows)))
(out / 'public-boot.txt').write_text(''.join(f'{k}/1' + (f' kernel={kern}' if kern else '') + f' pubcap={CAP}\n'
                                             for k, (_, _, kern) in enumerate(rows)))
print(f'{len(rows)} rows, {sum(1 for r in rows if r[2])} on {KERNEL}')
