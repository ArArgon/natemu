#!/usr/bin/env python3
"""Run the standing suite of docs/restructure/boot-handoff.md §5: every command there, with the same arguments and the same
pass/fail rule (exit status 0; `i8b`: exactly the 7 known QEMU two-NaN departures), in parallel where timing cannot
change a result.

    python3 tools/run_suite.py [--fexc FEXC] [--out DIR] [-j N] [-e N] [--only A,B] [--skip A,B] [--quick] [--list]

The FULL suite (no --quick) is what must pass before a push or merge; `--quick` (about ten minutes) is the development
loop: one small, representative step of every runner family (QUICK below), the same pass/fail rules.
Phases: (1) `pre`, one at a time: the cargo builds and tests, every binary the runners build and sign (the release
host, the `natemu` stub, the resident) and the load-only l1/l2 runners (they run cargo themselves); later steps get
NATEMU_PREBUILT=1, so no runner runs cargo while another runs those binaries (see run_slice1_vm.build); (2) `par`, N
steps at once (default: the performance cores), a step with its own internal parallelism counting as that many, plus up
to E (default: the efficiency cores) steps from E_OK on the efficiency cores: `taskpolicy -c background` clamps the whole
process tree there (about 4x slower per step, so a step starts there only if it would end before the performance cores
drain the queue); large runners are split where they already select a part (k1 and r1b per vCPU count, loop faults per
block size, i5b per group, k2, x3 and m2 per group, corpora per vCPU count) or take `--shard I/N` (faults, loop faults,
probes, k1: the independent cases dealt out round-robin; steps `name@I`), each part with its own `--out` directory and
TMPDIR; a step whose time ends far from its `est` is reported at the end (update `est`; a new step has no estimate and
would otherwise start last); (3) `ser`, one at a
time, alone: the steps whose judgement depends on wall time or on vCPUs running truly concurrently (see SERIAL_WHY);
a failed one is rerun once alone and both results recorded (the rerun's decides).
Machine coordination: phases 1-2 hold a shared flock on NATEMU_RAW_ROOT/.vm.lock and each serial step an exclusive one
(benchmarks take it exclusively with `/usr/bin/lockf -k`), and a serial step also waits until no other VM process runs.
NATEMU_RAM and other environment variables pass through to every step (the 4 MiB suite: NATEMU_RAM=0x400000).
Writes DIR/results.txt (step, phase, result, exit code, seconds, log; one line per step as it ends) and DIR/logs/;
a passing step's output and temporary directories are deleted, a failing step's kept. Exit status 1 on any failure.
--only/--skip take comma-separated step names or fnmatch patterns (`k1-*`, `faults@*`); `release` always runs."""
import argparse, dataclasses, datetime, fcntl, fnmatch, json, os, pathlib, re, shutil, signal, subprocess, sys, threading, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
# The compiler boot-handoff §5's `F=../natemu-raw/...` line names.
FEXC = RAW / re.search(r'^F=\.\./natemu-raw/(\S+)', (ROOT / 'docs/restructure/boot-handoff.md').read_text(), re.M)[1]
I8B_KNOWN = {'i8-forms-001', 'i8-forms-045', 'i8-random-067', 'i8-random-077', 'i8-random-097', 'i8-random-111', 'i8-random-126'}
SERIAL_WHY = {
    'kicks': 'host kicker threads against hot loops (boot-handoff §5: timing-sensitive)',
    'contain': 'wall-clock bounds: each failing run ends within 10 s (watchdog runs: + 5 s), a killed server within 2 s',
    'l4b': 'IRQ0 at most 2 ms late, a 10 ms calibration window, clocks against the TSC (boot-handoff §5: timing-sensitive)',
    'x3-timers': 'LAPIC timer lateness bounded by 2 ms',
    'x3-stress': 'random IPIs and timers at 4 vCPUs (boot-handoff §5: timing-sensitive)',
    'k2-sysfast': 'apic-timer: each delivery 0.5-1.5 periods after the last, the count the periods elapsed +-1',
    'k2-cost': 'reports wall-clock microseconds per iteration (the K2 cost numbers)',
    'm2-litmus-cost': 'the WB store-buffering control must show the non-SC outcome (vCPUs truly concurrent); cost in ns',
    'l4a-stress': 'failed twice in the parallel phase, never alone: a vCPU stopped by the guest watchdog (random 0-255 us '
                  'delays between line changes)',
    'k1-c4': 'each run killed after 300 s: link-full at 4 vCPUs, block size 1, exceeded it in the parallel phase at -j 6 (par k1 steps now take one slot per vCPU and a 450 s kill: oversubscribed P cores were the cause)',
    'boot-smoke': 'Ctrl-C must end the stub within 5 s; boots under a 120 s timeout',
    'vector-split-smp': 'three vCPUs exercise split-lock ownership and vector completion concurrently',
    'virtio-lifecycle-timed': 'held-call schedules released or timed out at 3 s, the 50 ms windowless drain deadline (code-page: none '
                              'may expire), a fixed guest delay against a 300 ms port exit (drain-expiry)',
}


# Steps that may run on the efficiency cores (`taskpolicy -c background`): single-VM runners that compare final state with
# QEMU and have no wall-clock bound the step itself asserts, no watchdog expected to fire and no vCPUs racing each other (their
# runner timeouts, where any, are far above the 2.4-4x slowdown). Kept on P: pytests (test_vm_lock sleeps, test_check_setup's 50 ms
# deadline) and i5b (QEMU runs killed at 30/60 s). Everything else (multi-vCPU, watchdog, kicker, timing, the 300 s run limits of k1) stays on the
# performance cores. Measured November 10 (docs/implementation.md "Faster suite runs").
E_OK = ('fix*', 'seed*', 'i7*', 'i8*', 'i9*', 'i10*', 'i6*', 'i5c*', 'adx*', 'xsave*', 'rex*', 'br66*', 'sha', 'sha-1', 'flow', 'icebp',
        'faults*', 'loopfaults-*', 'instruction-gaps*', 'cpu-forms', 'frame-*', 'maskmovq*', 'vector-ac')
SHARDS = {'faults': 5, 'i7-faults': 2, 'loopfaults': 3, 'probes': 2, 'k1': {1: 1, 2: 2, 3: 3, 4: 1}}   # parts of about 100 s
PROBES_EST, CORPORA_EST = 900, {2: 160, 3: 135, 4: 200}
# Seconds of the long indivisible steps in the November 10 runs (-j 4 -e 6, machine busy): the k1 link-full case (one run, 220-385 s)
# and the two probe chains; they start first. Everything else comes from the arguments above.
EST = {'k1-c1': 220, 'k1-c2@2': 320, 'k1-c3@3': 385, 'probes@1': 290, 'probes@2': 600, 'system': 175, 'i5b-faults': 120,
       'instruction-gaps': 75, 'instruction-gaps-1': 75, 'cpu-forms': 75}
E_SLOW = 3.0      # a step's time on the efficiency cores over its `est` (measured with the machine busy: 2.4; i8-faults alone: 3.6-4.1)
# --quick: one small member of every runner family (the sharded ones: their first shard).
QUICK = ('build', 'cargo-test', 'cargo-test-fexc', 'runtime', 'l1', 'l2', 'l2-iasl', 'release', 'stack', 'barriers', 'pytests',
         'split-wait-model', 'icebp', 'frame-alias', 'frame-randomization', 'vector-ac', 'cpu-forms', 'maskmovq', 'fix', 'seed', 'i8',
         'i8b', 'i9', 'i10', 'i7-cpl0', 'xsave', 'i6-faults', 'i10-faults', 'i5c', 'i6b-cases', 'flow', 'asm', 'faults@1', 'i7-faults@1',
         'loopfaults-default@1', 'probes@1', 'corpora-c2', 'smp', 'limits', 'litmus', 'events', 'privilege', 'delivery', 'm3',
         'k1-c1', 'k1-c2@1', 'pic-ac-fence-user', 'k2-cost', 'k2-gather', 'nostop-*', 'x3-qemu', 'x3-cases', 'x4', 'm1', 'm2', 'l3', 'l4a', 's4', 'x1', 'r1a', 'r1b-c2',
         'i5b-images', 'instruction-gaps', 'vector-split-smp', 'i8-faults', 'i9-faults', 'i6b', 'system', 'coherence', 'sharedpt', 'fix-1', 'i6-faults-1', 'l4b', 'contain', 'boot-smoke', 'kicks', 'virtio-lifecycle', 'virtio-lifecycle-timed', 'io-coverage', 'net-linux')


@dataclasses.dataclass
class Step:
    name: str
    argv: list
    phase: str = 'par'    # pre | par | ser
    est: int = 5          # seconds in the parallel phase (November 5, -j 4): it starts the longest first
    w: int = 1            # processes the step runs at once (its own --parallel/--jobs)
    judge: object = None  # judge(out dir) -> (ok, note) when the step exits 1
    core: str = 'P'       # P, or E: started on the efficiency cores (only E_OK steps, only by the scheduler)


def i8b_judge(out):
    try: res = json.loads((out / 'results.json').read_text())['results']
    except (OSError, ValueError, KeyError) as e: return False, f'no results.json ({e})'
    bad = {r['name'] for r in res if r['outcome'] == 'fail'}
    return bad == I8B_KNOWN, 'the 7 known QEMU departures' if bad == I8B_KNOWN else f'failures {sorted(bad)}, known {sorted(I8B_KNOWN)}'


def steps(F, O):
    S, cargo = [], ['cargo', '+1.97.0']
    def add(name, argv, phase=None, est=5, w=1, judge=None):
        S.append(Step(name, [str(x) for x in argv], phase or ('ser' if name in SERIAL_WHY else 'par'), est, w, judge))
    def vm(name, script, *args, **kw): add(name, ['python3', f'tools/{script}', *args, '--fexc', F, '--out', O / 'out' / name], **kw)
    def s3(name, *args, **kw): vm(name, 'run_slice3_vm.py', *args, **kw)
    def shards(n, name, script, *args, est, **kw):    # n steps `name@I`, each one `--shard I/N` of the runner (tools/shard.py)
        for i in range(1, n + 1): vm(f'{name}@{i}', script, *args, '--shard', f'{i}/{n}', est=-(-est // n), **kw)
    add('build', cargo + ['build', '--offline', '--locked'], 'pre')
    add('cargo-test', cargo + ['test', '--offline', '--locked'], 'pre')
    add('cargo-test-fexc', ['env', f'FEXC={F}'] + cargo + ['test', '--offline', '--locked'], 'pre')
    add('runtime', cargo + ['build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], 'pre')
    for l in ('l1', 'l2', 'l2-iasl'): add(l, ['python3', f"tools/run_{l.replace('-', '_')}.py", '--out', O / 'out' / l], 'pre')   # their cargo builds re-link the host unsigned: before `release`
    # What the runners' own build() calls make and sign, last: NATEMU_PREBUILT=1 makes later build() calls use them.
    add('release', ['python3', '-c', "import sys; sys.path[:0] = ['tools']; import run_slice1_vm as v, run_boot_smoke as b; v.build(); b.build()"], 'pre')
    add('stack', ['python3', 'tools/check_resident_stack.py'])
    add('barriers', ['python3', 'tools/check_resident_barriers.py'])   # PCID plan P1: the page walk's speculation barriers
    add('pytests', ['env', f'FEXC={F}', 'python3', '-B', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_*.py'], est=10)
    add('split-wait-model', ['python3', '-B', '-m', 'unittest', 'discover', '-s', 'tools/models', '-p', 'test_split_wait_model.py'])
    vm('icebp', 'run_icebp.py')
    vm('instruction-gaps', 'run_instruction_gaps.py', '--max-block', '0', est=180)
    vm('instruction-gaps-1', 'run_instruction_gaps.py', '--max-block', '1', est=180)
    vm('frame-alias', 'run_frame_alias.py', '--guest-state-alias', '0xffff407abcde0000')
    vm('frame-alias-1', 'run_frame_alias.py', '--max-block', '1', '--guest-state-alias', '0xffff407abcde0000')
    vm('frame-randomization', 'run_frame_randomization.py')
    vm('frame-randomization-1', 'run_frame_randomization.py', '--max-block', '1')
    vm('vector-ac', 'run_vector_ac.py')
    vm('cpu-forms', 'run_cpu_forms.py', est=120)
    vm('vector-split-smp', 'run_vector_split_smp.py')
    vm('maskmovq', 'run_maskmovq.py', '--no-build')
    vm('maskmovq-1', 'run_maskmovq.py', '--no-build', '--max-block', '1')
    corpora = {'seed': 's1d1-seed4242', 'i8': 'i8-corpus-20261030-01', 'i8b': 'i8-corpus-20261030-02', 'i9': 'i9-corpus-20261030-01',
               'i9-big': 'i9-big-20261030-01', 'sha': 'sha-corpus-20261103-01', 'adx': 'adx-corpus-20261104-01',
               'xsave': 'xsave-corpus-20261104-01', 'i10': 'i10-corpus-20261030-01', 'rex': 'rex-corpus-20261102-02',
               'br66': 'br66-corpus-20261102-01', 'i7': 'i7-corpus-20261103-01'}
    pair = lambda c: (RAW / corpora[c] / 'programs.json', RAW / corpora[c] / 'expected.json')
    for sfx, blk in (('', []), ('-1', ['--max-block', '1'])):
        vm('fix' + sfx, 'run_slice1_vm.py', *blk)
        for c in corpora: vm(c + sfx, 'run_slice1_vm.py', *blk, *pair(c), judge=i8b_judge if c == 'i8b' else None)
        vm('i7-cpl0' + sfx, 'run_slice1_vm.py', '--cpl', '0', *blk, *pair('i7'))
        shards(SHARDS['faults'], 'faults' + sfx, 'run_slice1_faults.py', *blk, est=650)
        shards(SHARDS['i7-faults'], 'i7-faults' + sfx, 'run_slice1_faults.py', '--no-probes', *blk, pair('i7')[0], est=380)
        for r, est in (('run_i6_faults.py', 10), ('run_i8_faults.py', 55), ('run_i9_faults.py', 38), ('run_i10_faults.py', 20), ('run_i5c.py', 8)):
            vm(r[4:-3].replace('_', '-') + sfx, r, *blk, est=est)
        vm('i6b' + sfx, 'run_i6b.py', 'corpus', *blk, RAW / 'i6b-corpus-20261030-01/programs.json', RAW / 'i6b-corpus-20261030-01/expected.json')
        vm('i6b-cases' + sfx, 'run_i6b.py', 'cases', *blk)
    vm('flow', 'run_slice2_vm.py', '--programs', RAW / 's2d/corpus/programs.json', '--expected', RAW / 's2d/corpus/expected.json')
    for size in ('default', '1', '2'): shards(SHARDS['loopfaults'], f'loopfaults-{size}', 'run_slice2_faults.py', '--sizes', size, est=150)
    shards(SHARDS['probes'], 'probes', 'run_slice2_probes.py', est=PROBES_EST)
    vm('asm', 'fexc/upstream_tests/run_asm.py', '--jobs', '4', est=15, w=4)
    vm('asm-1', 'fexc/upstream_tests/run_asm.py', '--jobs', '4', '--max-block', '1', est=15, w=4)
    for n in (2, 3, 4): s3(f'corpora-c{n}', 'corpora', '--cpus', n, '--parallel', 2, est=CORPORA_EST[n], w=2)    # each part redoes the QEMU references
    s3('smp', 'smp', '--parallel', '2', w=2)
    s3('limits', 'limits')
    s3('kicks', 'kicks', '--kicks', '1000', '--cpus', '1', '2', '4')
    s3('system', 'system', est=155)
    s3('coherence', 'coherence', '--reps', '1', '--parallel', '2', est=40, w=2)
    s3('sharedpt', 'sharedpt', '--reps', '1', '--parallel', '2', w=2)
    s3('litmus', 'litmus', '--k', '5000', '--runs', '1', '--parallel', '2', est=10, w=2)
    s3('contain', 'contain')
    s3('privilege', 'privilege')
    s3('delivery', 'delivery')
    s3('events', 'events', '--kicks', '2000', est=10)
    vm('m3', 'run_m3.py', est=18, w=4)
    for g in ('cost', 'rdrand', 'xmc', 'coherence', 'rollback', 'nopruns', 'tsc', 'swapgs', 'sysfast', 'stifirst', 'stmxcsr', 'repbulk', 'cr2', 'verw', 'gather'):
        vm(f'k2-{g}', 'run_k2.py', '--only', g)
    # No-stop change (correctness-nostop.md): masked moves, split locks, refused forms as #UD, the opcode-map sweep.
    for g in ('maskmov', 'splitlock', 'refused', 'sweep'):
        vm(f'nostop-{g}', 'run_nostop.py', '--only', g)
    for g in ('qemu', 'ipi', 'timers', 'stress', 'halt', 'cases'): vm(f'x3-{g}', 'run_x3.py', '--only', g)
    vm('x4', 'run_x4.py')
    for n, est in ((1, 80), (2, 215), (3, 275), (4, 155)):
        if n == 4 or SHARDS['k1'][n] == 1: vm(f'k1-c{n}', 'run_k1.py', '--cpus', n, *(() if n == 4 else ('--timeout', 450)), est=est, w=n)     # c4 serial: its runs time out at 300 s under load
        else: shards(SHARDS['k1'][n], f'k1-c{n}', 'run_k1.py', '--cpus', n, '--timeout', 450, est=est, w=n)
    # PIC study stage 2: stops at each entry word, svc-word links, a forged slot. One step per mode (each runs the K1 programs at
    # 1 and 2 vCPUs, about 1780 s in all in the November 10 serial-chain run, 137-step suite 13: its longest step); the three
    # no-K1 modes together.
    for mode in ('and', 'str', 'body', 'svc', 'forge', 'forge-noncanonical', 'forge-half'): vm(f'pic-{mode}', 'run_pic.py', '--cpus', 1, 2, '--modes', mode, est=225, w=2)
    vm('pic-ac-fence-user', 'run_pic.py', '--modes', 'ac,fence,user', est=60)
    vm('m1', 'run_m1.py')
    vm('l3', 'run_l3.py')
    vm('l4a', 'run_l4a.py', '--only', 'qemu,xapic,halt,lint1')
    vm('l4a-stress', 'run_l4a.py', '--only', 'stress')
    vm('l4b', 'run_l4b.py', est=50)
    for g in ('images', 'faults'): vm(f'i5b-{g}', 'run_i5b.py', '--group', g, est=85 if g == 'faults' else 10)
    vm('s4', 'run_s4.py')
    vm('m2', 'run_m2.py', '--only', 'qemu,types,devices,firmware,faults')
    vm('m2-litmus-cost', 'run_m2.py', '--only', 'litmus,cost')
    vm('x1', 'run_x1.py', est=8)
    vm('r1a', 'run_r1.py', 'r1a')
    vm('boot-smoke', 'run_boot_smoke.py', est=13)
    # Block device: the deterministic lifecycle and backend-failure cases through guest MMIO (the publish-* cases need an
    # io-test-hooks build), and stock Linux on partitioned disks, guest reboot, full guest and host disks.
    target = pathlib.Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target'))
    import run_virtio_lifecycle as lc
    for name, timed in (('virtio-lifecycle', False), ('virtio-lifecycle-timed', True)):
        add(name, ['python3', 'tools/run_virtio_lifecycle.py', 'run', '--host', target / 'release/natemu-host', '--no-lock',
                   '--runtime', target / 'aarch64-unknown-none/release/natemu-runtime', '--fexc', F, '--out', O / 'out' / name,
                   '--cases', *(c for c in lc.CASES if not c.startswith('publish-') and (c in lc.TIMED) == timed)], est=5)
    vm('io-coverage', 'run_io_coverage.py', est=90)
    # Network: stock Linux on user-mode NAT (DHCP, HTTP to the host, iperf3, a reset under traffic, cancel); no internet.
    add('net-linux', ['python3', 'tools/run_net_linux.py', 'run', '--offline', '--fexc', F, '--out', O / 'out' / 'net-linux'], est=35)
    for n in (2, 3, 4): vm(f'r1b-c{n}', 'run_r1.py', 'r1b', '--cpus', n, est=25)
    # Fixed-address GP probes must name an unmapped page; DR watchpoints must name the actual frame.
    distant = {'faults', 'faults-1', 'events', 'privilege', 'k2-swapgs', 'k2-cr2', 'k2-stifirst', 'k2-verw'}
    for step in S: step.est = EST.get(step.name, step.est)
    for step in S:
        alias = '0xffff407abcde0000' if step.name.split('@')[0] in distant else '0xffff400000001000' if step.name == 's4' else None
        if alias is not None:
            step.argv = ['env', f'NATEMU_TEST_GUEST_STATE_ALIAS={alias}', *step.argv]
    return S


class Suite:
    def __init__(self, a):
        self.a, self.O, self.lock, self.procs, self.failed = a, a.out, threading.Lock(), set(), []
        self.finished, self.times = set(), {}
        for d in ('logs', 'out', 'tmp'): (a.out / d).mkdir(parents=True, exist_ok=True)
        self.results = open(a.out / 'results.txt', 'a', buffering=1)
        self.vmlock = os.open(RAW / '.vm.lock', os.O_RDWR | os.O_CREAT, 0o644)

    def flock(self, mode):
        try: fcntl.flock(self.vmlock, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"waiting for the {'shared' if mode == fcntl.LOCK_SH else 'exclusive'} VM lock", flush=True)
            fcntl.flock(self.vmlock, mode)

    def run(self, s, tag=''):
        log, tmp = self.O / 'logs' / f'{s.name}{tag}.log', self.O / 'tmp' / s.name
        tmp.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, NATEMU_RAW_ROOT=str(RAW), TMPDIR=f'{tmp}/', **({} if s.phase == 'pre' else {'NATEMU_PREBUILT': '1'}))
        t = time.monotonic()
        with open(log, 'w') as f:
            argv = ['taskpolicy', '-c', 'background', *s.argv] if s.core == 'E' else s.argv
            p = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            with self.lock: self.procs.add(p)
            rc = p.wait()
            with self.lock: self.procs.discard(p)
        ok, note = rc == 0, ''
        if rc == 1 and s.judge: ok, note = s.judge(self.O / 'out' / s.name)
        return ok, rc, time.monotonic() - t, log, note

    def record(self, s, r, rerun=None):
        ok, rc, dt, log, note = rerun or r
        line = f"{s.name:<18} {s.phase + ('/E' if s.core == 'E' else ''):<4} {'PASS' if ok else 'FAIL'} rc={rc} {dt:.0f}s {log.relative_to(self.O)}"
        self.times[s.name] = dt
        if rerun: line = f"{s.name:<18} {s.phase:<4} {'PASS' if ok else 'FAIL'} first: rc={r[1]} {r[2]:.0f}s {r[3].relative_to(self.O)}; rerun: rc={rc} {dt:.0f}s {log.relative_to(self.O)}"
        line += f' ({note})' if note else ''
        with self.lock:
            self.results.write(line + '\n'); self.finished.add(s.name)
            if not ok: self.failed.append(s.name)
            print(line, flush=True)
        if ok:
            for d in ('out', 'tmp'): shutil.rmtree(self.O / d / s.name, ignore_errors=True)
        return ok

    def parallel(self, todo, j, e=0):
        free, efree, cv, threads = j, e, threading.Condition(), []
        todo = sorted(todo, key=lambda s: -s.est)    # longest first; a step that does not fit waits, the next may start
        def go(s, w):
            nonlocal free, efree
            try: self.record(s, self.run(s))
            finally:
                with cv:
                    if s.core == 'E': efree += 1
                    else: free += w
                    cv.notify_all()
        def pick():
            nonlocal free, efree
            s = next((s for s in todo if min(s.w, j) <= free), None)        # the performance cores: the longest that fits
            if s is not None:
                free -= min(s.w, j); return s, min(s.w, j)
            # An efficiency core takes the longest E_OK step that would end before the performance cores drain the queue.
            horizon = sum(t.est * min(t.w, j) for t in todo) / max(j, 1)
            s = next((s for s in todo if efree and s.w == 1 and s.est * E_SLOW <= horizon and any(fnmatch.fnmatchcase(s.name, q) for q in E_OK)), None)
            if s is not None: efree -= 1; s.core = 'E'
            return s, 1
        with cv:
            while todo:
                s, w = pick()
                if s is None: cv.wait(); continue
                todo.remove(s)
                threads.append(threading.Thread(target=go, args=(s, w), daemon=True)); threads[-1].start()
        for t in threads: t.join()

    def serial(self, s):
        self.flock(fcntl.LOCK_EX)
        try:
            idle(); r = self.run(s)
            if not r[0]:        # the suite's rule: one rerun alone, recorded beside the first result
                first = self.O / 'out' / s.name
                if first.exists(): first.rename(first.with_name(s.name + '.first'))
                idle(); return self.record(s, r, self.run(s, '.rerun'))
            return self.record(s, r)
        finally: fcntl.flock(self.vmlock, fcntl.LOCK_UN)

    def kill(self):
        with self.lock:
            for p in self.procs:
                try: os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError: pass


def idle():
    """Waits until no natemu or QEMU VM process runs (the suite's rule for timing-sensitive steps)."""
    checks = (['pgrep', '-f', 'natemu' + '-host'], ['pgrep', '-fx', '.*/natemu .*'], ['pgrep', '-f', 'qemu' + '-system'])
    while any(subprocess.run(c, stdout=subprocess.DEVNULL).returncode == 0 for c in checks): time.sleep(5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    eff = subprocess.run(['sysctl', '-n', 'hw.perflevel1.physicalcpu'], capture_output=True, text=True).stdout.strip()
    ap.add_argument('--fexc', type=pathlib.Path, default=FEXC)
    ap.add_argument('--out', type=pathlib.Path, default=RAW / f"suite-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    perf = subprocess.run(['sysctl', '-n', 'hw.perflevel0.physicalcpu'], capture_output=True, text=True).stdout.strip()
    ap.add_argument('-j', type=int, default=int(perf) if perf.isdigit() else os.cpu_count())
    ap.add_argument('--only', type=lambda s: s.split(','))
    ap.add_argument('-e', type=int, default=int(eff) if eff.isdigit() else 0, help='steps at once on the efficiency cores (E_OK steps; 0: none)')
    ap.add_argument('--skip', type=lambda s: s.split(','), default=[])
    ap.add_argument('--quick', action='store_true', help='one small step of every runner family (about ten minutes); the full suite is still required before a push')
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    a.fexc, a.out = a.fexc.resolve(), a.out.resolve()
    match = lambda n, pats: any(fnmatch.fnmatchcase(n, p) for p in pats)
    todo = [s for s in steps(a.fexc, a.out) if s.name == 'release' or ((a.only is None or match(s.name, a.only)) and not match(s.name, a.skip)
                                                                       and (not a.quick or match(s.name, QUICK)))]
    if a.list:
        for s in todo: print(f"{s.name:<18} {s.phase:<4} {'x' + str(s.w) if s.w > 1 else '  '} {SERIAL_WHY.get(s.name, '')}")
        return
    if not a.fexc.is_file(): sys.exit(f'no compiler at {a.fexc}')
    suite, t0 = Suite(a), time.monotonic()
    suite.results.write(f'# {datetime.datetime.now():%Y-%m-%d %H:%M:%S} fexc {a.fexc} -j {a.j} -e {a.e}' + (' --quick' if a.quick else '') + '\n')
    phase_s = {}
    try:
        suite.flock(fcntl.LOCK_SH)
        t = time.monotonic()
        for s in [s for s in todo if s.phase == 'pre']:
            if not suite.record(s, suite.run(s)) and s.name in ('build', 'runtime', 'release'): sys.exit(f'{s.name} failed: see {a.out}/logs')
        phase_s['pre'] = time.monotonic() - t; t = time.monotonic()
        suite.parallel([s for s in todo if s.phase == 'par'], a.j, a.e)
        phase_s['par'] = time.monotonic() - t
        fcntl.flock(suite.vmlock, fcntl.LOCK_UN); t = time.monotonic()
        for s in [s for s in todo if s.phase == 'ser']: suite.serial(s)
        phase_s['ser'] = time.monotonic() - t
    except BaseException:
        suite.kill(); raise
    missing = sorted({s.name for s in todo} - suite.finished)
    if missing:
        suite.failed.extend(n for n in missing if n not in suite.failed)
        print('Missing results: ' + ', '.join(missing), flush=True)
    summary = f"# wall {time.monotonic() - t0:.0f}s ({', '.join(f'{k} {v:.0f}s' for k, v in phase_s.items())}); " \
              f"{len(todo) - len(suite.failed)}/{len(todo)} passed" + (f"; FAILED: {' '.join(suite.failed)}" if suite.failed else '')
    by = {s.name: s for s in todo}      # a step far over its estimate (an E step: over est * E_SLOW) would start too late: report it
    drift = [f'{n} {t:.0f}s (est {by[n].est})' for n, t in sorted(suite.times.items(), key=lambda x: -x[1])
             if t > 60 and t > 2 * by[n].est * (E_SLOW if by[n].core == 'E' else 1) and n != 'k1-c4'][:12]
    if drift: summary += '\n# est far off (update the steps): ' + ', '.join(drift)
    suite.results.write(summary + '\n'); print(summary)
    sys.exit(1 if suite.failed else 0)


if __name__ == '__main__': main()
