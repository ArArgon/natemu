#!/usr/bin/env python3
"""Freeze a selected public-test batch and its initramfs overlay."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import re

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT.parent / 'natemu-raw'
WORK = RAW / 'public-cpu-ram-20261107-01'
mode = sys.argv[1]
out = WORK / mode
out.mkdir(exist_ok=False)
(out / 'runs').mkdir()
(out / 'harness').mkdir()
stage = out / 'overlay'
(stage / 'ct').mkdir(parents=True)
candidate = RAW / 'correctness-forms-20261107-02/candidate-bin'
kernel = RAW / 'kernel-census-20261028-01/dl/linux'
cases = []


def add(family, name, command):
    cases.append(dict(index=len(cases), family=family, id=family + '-' + name,
                      command=command, kernel=str(kernel), image=str(out / 'image.cpio')))


if mode.startswith('kernel-'):
    kernel = WORK / 'images/test-kernel'
    shutil.copytree(WORK / 'payload/usr', stage / 'usr', symlinks=True)
    shutil.copytree(WORK / 'payload/opt/public/lkmm', stage / 'opt/public/lkmm')
    for name in ('memcpy_kunit', 'usercopy_kunit'):
        add('kunit', name, f'modprobe kunit && modprobe {name} && rmmod {name}')
    for name in ('atomic64_test', 'test_bitmap'):
        add('kernel', name, f'modprobe {name} && rmmod {name}')
    add('vmalloc', 'normal-cases', 'modprobe test_vmalloc nr_threads=$(nproc) sequential_test_order=1 test_repeat_count=1 test_loop_count=100 run_test_mask=1983')
    for name in ('spin_lock', 'rw_lock', 'mutex_lock', 'rwsem_lock'):
        add('locktorture', name, f'modprobe locktorture torture_type={name} onoff_interval=0 stat_interval=10 && sleep 30 && rmmod locktorture')
    for name in ('rcu', 'srcu'):
        add('rcutorture', name, f'modprobe rcutorture torture_type={name} onoff_interval=0 stat_interval=10 && sleep 30 && rmmod rcutorture')
    with tarfile.open(WORK / 'sources/lkmm-generated.tar.gz') as archive:
        for i in range(35):
            source = archive.extractfile(f'lkmm/litmus{i:03d}.c').read().decode()
            name = re.search(r'static char \*name = "([^"]+)";', source).group(1)
            threads = int(re.search(r'static const int nthreads = (\d+);', source).group(1))
            add('lkmm', f'{i:03d}', f'insmod /opt/public/lkmm/litmus{i:03d}.ko && cat /proc/litmus && rmmod litmus{i:03d}')
            cases[-1].update(litmus_name=name, threads=threads, eligible_vcpus=[n for n in (1, 4) if n >= threads])
elif mode == 'smoke':
    for name, cmd in [('success', 'true'), ('failure', 'false'), ('skip', 'exit 4'),
                      ('guestcap', 'sleep 20'), ('hostcap', 'sleep 100')]:
        add('harness', name, cmd)
else:
    assert mode == 'users-01'
    shutil.copytree(WORK / 'payload/opt', stage / 'opt')
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
            assert (stage / 'opt/public' / family / name).is_file()
            add(family, name, f'cd /opt/public/{family} && ./{name}')
    for variant in ('param_test', 'param_test_compare_twice', 'param_test_mm_cid'):
        for kind in 'slbmir':
            add('rseq', variant + '-' + kind,
                f'cd /opt/public/rseq && ./{variant} -t 8 -r 1000 -T {kind}')
    for name, options in [('yield', '-y'), ('signal', '-k')]:
        add('rseq', 'param-' + name,
            f'cd /opt/public/rseq && ./param_test -t 8 -r 1000 -T i -7 -1 -m 2 {options}')
    for name, options in [('broadcast', '-b'), ('broadcast-lock', '-b -l'),
                          ('broadcast-owner', '-b -o'), ('lock', '-l'), ('owner', '-o')]:
        add('futex-functional', 'requeue-pi-' + name,
            f'cd /opt/public/futex-functional && ./futex_requeue_pi {options}')
    for name in ['mprotect01', 'mprotect02', 'mprotect03', 'mprotect04', 'mprotect05',
                 'mmap01', 'mmap02', 'mmap03', 'mmap04', 'madvise01',
                 'futex_cmp_requeue01', 'futex_cmp_requeue02', 'futex_wait01', 'futex_wake01']:
        add('ltp', name, f'mkdir -p /tmp/ltp-work && cd /tmp/ltp-work && {name}')

for source, dest in [('public-init.sh', stage / 'init'), ('run-public.sh', stage / 'ct/run-public.sh')]:
    shutil.copy2(ROOT / 'tools/correctness' / source, dest)
    dest.chmod(0o755)
(stage / 'ct/public-cases.tsv').write_text(''.join(c['id'] + '\t' + c['command'] + '\n' for c in cases))
base = WORK / 'images/base.cpio.zst' if mode.startswith('kernel-') else RAW / 'correct-us-20261105-01/ltp.cpio.zst'
subprocess.run([sys.executable, str(ROOT / 'tools/play/mkcpio.py'), str(stage), str(out / 'overlay.cpio')], check=True)
with (out / 'image.cpio').open('wb') as f:
    if mode.startswith('kernel-'):
        f.write(base.read_bytes())
    else:
        subprocess.run(['zstd', '-dc', str(base)], stdout=f, check=True)
    f.write(b'\0' * (-f.tell() % 4))
    f.flush()
    if mode.startswith('kernel-'):
        subprocess.run(['zstd', '-q', '-c', '-1', str(out / 'overlay.cpio')], stdout=f, check=True)
    else:
        f.write((out / 'overlay.cpio').read_bytes())
for name in ['public-campaign.py', 'public-prepare.py', 'public-build.sh', 'public-init.sh', 'run-public.sh']:
    shutil.copy2(ROOT / 'tools/correctness' / name, out / 'harness' / name)
files = [kernel, out / 'image.cpio', *[candidate / n for n in ['natemu', 'natemu-host', 'natemu-runtime', 'fexc']],
         Path('/opt/homebrew/bin/qemu-system-x86_64'),
         *[p for p in (out / 'harness').iterdir() if p.is_file()]]
if mode.startswith('kernel-'):
    files += [WORK / 'build/kernel.config', WORK / 'build/lkmm-oracle.txt', WORK / 'sources/lkmm-generated.tar.gz']
manifest = dict(cases=cases, candidate={n: str(candidate / n) for n in ['natemu', 'natemu-host', 'natemu-runtime', 'fexc']},
    qemu='/opt/homebrew/bin/qemu-system-x86_64', lock=str(RAW / '.vm.lock'),
    qcpu='Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip',
    pins={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    source_commit=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip())
queue = [dict(name=f'{c["index"]:03d}-{engine}-n{n}', index=c['index'], engine=engine,
              vcpus=n, guest_cap=300, host_cap=450)
         for c in cases for n in c.get('eligible_vcpus', (1, 4)) for engine in ('qemu', 'natemu')]
if mode == 'smoke':
    queue = [dict(name=f'{c["index"]:03d}-qemu-n1', index=c['index'], engine='qemu',
                  vcpus=1, guest_cap=1 if c['index'] == 3 else 300,
                  host_cap=8 if c['index'] == 4 else 45) for c in cases]
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
(out / 'queue.json').write_text(json.dumps(queue, indent=2) + '\n')
print(json.dumps(dict(out=str(out), cases=len(cases), boots=len(queue))))
