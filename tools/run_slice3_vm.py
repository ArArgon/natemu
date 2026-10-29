#!/usr/bin/env python3
"""N vCPUs on the general execution path (slice 3, S3a): several programs at once in one VM, one per vCPU.

    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR corpora [--cpus 2 3 4] [--max-block N]
        [--parallel P] [--seed S] [--flow PROGRAMS.json EXPECTED.json] [--no-faults]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR smp [--cpus 2 3 4] [--programs a,b] [--reps R]
        [--parallel P] [--max-block N] [--qemu]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR litmus [--cpus 2 3 4] [--families sb,mp]
        [--variants aligned,unaligned,split] [--k K] [--runs R] [--parallel P] [--control drop-fence]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR limits
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR kicks [--cpus 1 2 4] [--kicks M] [--iterations I]
        [--parallel P] [--reps R]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR system [--programs SUBSTR,...]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR coherence [--programs SUBSTR,...] [--reps R]
        [--parallel P]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR sharedpt [--programs SUBSTR,...] [--reps R]
        [--cpus 2 4] [--parallel P]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR privilege
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR contain [--cpus-contain 1 2 3 4] [--watchdog-ms MS] [--compile-deadline-ms MS]
        [--spin-iterations I] [--no-progress] [--reps R] [--kill-reps R]

`corpora`: the single-vCPU corpora — the slice-1 fixtures, the control-flow corpus (default: the S2d run's
programs and QEMU results under NATEMU_RAW_ROOT/s2d/corpus) and the slice-1 fault cases (QEMU prefix references, as
tools/run_slice1_faults.py, its paging cells included except those with a 2 MiB page: guest RAM holds one 2 MiB
frame per run) — are shuffled (seed) and grouped N at a time; each group runs as one multi-vCPU
description (driver `cpu` sections), every program in its own address space (own tables, disjoint guest-physical
pages) and the same linear addresses, all vCPUs concurrently. Each vCPU's final state is judged by its corpus's own
evaluator against its own single-program reference. Also per vCPU: EnTSO read back, the FEX frame intact, and, when
the cache neither flushed (beyond the driver's initial one) nor invalidated, host requests = distinct block RIPs.
`--parallel P` runs P groups (processes, each a VM) at once: with P * N above the core count the vCPU threads are
oversubscribed and migrate between cores.

`smp`: the deterministic programs of tools/bench/smp (natemu-smp-v1: the bytes QEMU system mode ran) at N vCPUs over one
shared address space, judged against tools/bench/smp/model_smp.py with smp.compare, and with `--qemu` also against a
pinned QEMU system-mode run of the same program (`-accel tcg,thread=multi`). Every vCPU must reach its end.

`litmus` (S3e, ordering on the general path): generated litmus programs (tools/bench/smp/programs.py `litmus_family`:
sb, sb-mfence, sb-xchg, mp, lb, wrc, iriw, 2+2W and XCHG chains, each generalised to N participants) at N vCPUs with
their locations aligned, unaligned inside a 16-byte block, or (`split`, `split-hi`) each crossing a page (every store to it
takes the split-store service; exchanged locations stay aligned; the changing byte is on the first page for `split`, on the
second for `split-hi`, so both halves of a split store are observed), R runs of K iterations per cell. Every iteration's outcome
must be in tools/models/ordering_model.py's allowed set for that program (the model enumerates it; nothing is written per
test), and an XCHG chain's outcome must also be one linear order of the exchanges (`check_xchg_chain`). I3 adds the
LOCKed read-modify-write families (`RMW_FAMILIES`): sb with one LOCKed RMW of each Arm atomic word FEX emits between the
store and the load (xadd, and, xor, or, cmpxchg, cmpxchg8b, cmpxchg16b, neg, and failing cmpxchg/cmpxchg8b), the RMW chains
`xadd` (a counter) and `cas` (one winner), and `cas-read`/`cas8b-read` (a failing LOCK CMPXCHG as SB's observing read),
judged by the model's `rmw` operation; RMW locations never split (a LOCKed access crossing 16 bytes stops). Every vCPU
reaches its end having done all K iterations. `--control drop-fence` runs sb-mfence without its MFENCEs, judged
against sb-mfence's allowed set: it must show the forbidden SB outcome (the harness can see a violation); `--control
drop-lock` (I3) likewise runs sb-xadd and the xadd counter chain (or `--families`) with their LOCK prefixes removed (plain
read-modify-writes); its run diffs and
trial counts are judged as in a normal run; aligned and unaligned only (`--variants` with a split variant is refused: a split
store completes in the resident with `dsb ish`, so SB cannot relax there).

`kicks` (S3b, E1b, precise stop): (1) a hot linked loop (no exit once linked) on each of N vCPUs (own registers, shared
code page), every vCPU kicked M times (default 10,000) by its own host kicker thread (its GIC SPI doorbell), R runs of
it, P at once (P * N above the core count: oversubscribed); (1b) adversarial: the same at N vCPUs with kicks at random
times (0..`--jitter-us` after each stop) into `adversarial_program` (computed jumps through the dispatcher and L1 every
round, a division `svc` resumed mid-block, self-modifying stores single-stepped, peers rung by SGI), (1c, I2) the same
kicks into `rep_program` (long REP STOSQ/MOVSQ both directions, REPE CMPSB, REPNE SCASB: stops between elements at the
REP's own exit, resumed there), and over all its
runs stops must have been taken at every place (block entry, dispatcher, L1, armed exit, resident entry, in the step
block, re-armed after a mid-block resume; at a resident entry only with N > 1: peers' SGIs); (2) a peer's resident kick: vCPU 1 writes data on vCPU 0's
code page while vCPU 0 spins in its hot loop, so vCPU 1's resident posts the page to vCPU 0 and rings its SGI; (3)
forged stops: the stop word over vCPU V's first block entry (a stop nobody armed) and an armed-stop record whose word
does not hold the stop word must each halt V (bad_stop) and cancel the others. Programs are natemu-smp-v1; the reference
is a pinned QEMU system-mode run of the same program (tools/bench/smp, `-accel tcg,thread=multi`), compared with
smp.compare. Per vCPU: reached its end, the per-vCPU checks and the doorbell accounting (`kick_checks`: host kicks
acknowledged + still pending = issued, none lost or doubled, kicks finding it in the host included; stops + doorbells
merged into an armed stop = doorbells; stops = the sum by place; nothing armed at the end; (hot loop) all M kicks issued
while it ran, none merged). The resident validates every stop's place (a block's first word, the dispatcher or L1, a
word it armed, or a resident entry at a block boundary; anything else halts bad_stop); the final state is compared with
QEMU. Latency (ring -> the resident's stop, per vCPU, one clock) and the mean acknowledge cost are reported. `corpora
--kicks M` also kicks every vCPU M times (mostly while it is in the host or the resident) and adds the same checks.

`system` (S3c1, long-mode system instructions as resident transactions): see cmd_system. Each system program of
tools/bench/smp/system.json runs at block sizes default and 1 against QEMU system mode (smp.SYSTEM_CPU), except the PTE
change without INVLPG (coherence_model) and the fault cases (tools/system_fault_model.py at QEMU's state just before
the instruction and the controls the case declares; modes_model where it covers them; QEMU's own outcome is reported
and must agree where it raises an exception, but for QEMU_DIFFERS); refused forms stop `unsupported_system`; a tampered system reply stops `bad_reply`. The presented CPU (S1,
tools/models/cpu_profile.py as the judge, QEMU's CPU being another): CPUID leaf by leaf, MSR reset values, MISC_ENABLE,
the UCODE_REV latch, TSC/RDTSC/RDTSCP and CR4.PSE/TSD at 1, 2 and 4 vCPUs (`profile_program`), and CPUID/RDTSC at CPL3,
RDTSC/RDTSCP #GP at CPL3 with CR4.TSD (SDM Vol.2 RDTSC). S2a: CR0/CR4 bits, CR2, CR8, DR0..DR7 read-backs and the
kernel head's EFER/CR0 sequence against QEMU; CR8/DR/RDPMC fault cases; a breakpoint-enabling DR7 refused; CR0.AM with
RFLAGS.AC at CPL3 refused (`unsupported_mode`, no #AC).

`coherence` (S3d, cross-vCPU code coherence): the programs of tools/bench/smp/coherence.json (`programs.coherence_all`:
code written at run time by one CPU and run by others — fresh code on a new page and on a page used for data before, a
function patched in rounds and run after a CR3 load, every CPU patching in turn, data stored on a peer's running code
page, a page rewritten (with its own bytes) on every fetch, CPUs translating pages the others write, CPUs ending while a
peer translates a page they held writable; at 3 and 4 vCPUs a function called in a loop until a patch shows while peers
store data on its page) at 2 and 4 vCPUs, each run R times at block sizes default and 1 and compared
with pinned QEMU system mode (`-accel tcg,thread=multi`; QEMU keeps translated code coherent with stores), plus the
per-vCPU and kick accounting checks. Every vCPU must reach its end (a `bad_link` or any other halt fails). The runs'
steps, stale links, invalidations, stops and shootdowns are reported.

`sharedpt` (S3f, shared guest page-table coherence): see cmd_sharedpt and tools/bench/smp/sharedpt.py: A/D races on one PTE
from N vCPUs while one clears A/D and INVLPGs, remap with per-vCPU INVLPG, 2 MiB <-> 4 KiB changes, aliases across vCPUs,
contended A/D walks (data, fetch, both) that must never halt; at 2 and 4 vCPUs (default), block sizes default and 1.

`limits`: 5 vCPUs are refused (0 cannot be written: a description without `cpu` lines is one vCPU; the host's
`prepare` refuses 0 in its unit test); a forged literal exit (its RIP move altered after admission) on vCPU V of 4 (V = 0..3; the others spin in a linked
loop) halts V with bad_reply (the resident decodes each link point's move at the reply), cancels and joins the others, and the VM is destroyed (the driver reports only then).

`contain` (S3h, containment and progress; runs one at a time, each a fresh process; admission's code-shape debug check
off, as by default: containment must hold with object validation alone, E3). Every failing run must exit as stated,
end within its bound (10 s; watchdog runs: the watchdog + 5 s) and print the driver's teardown line with every vCPU joined
and destroyed, the VM destroyed (status 0) and no child process left (no compile server alive or unreaped); each is
followed at once by a fresh process running fixture programs on as many vCPUs, which must match QEMU. (1, 4) Every
run-time tamper of the driver (forged literal exit, link-point offset, next-page reply given twice, system decode, linked
word and link record before a flush, a computed exit naming the block entry, a link point's target changed alone, the
stop word at a block entry, an armed record at a word without the stop word, access sites off the instruction grid or of
the wrong kind; and, generated code running at EL0, system instructions and accesses to the private window forged over a
block's entry: EL0_FORGED) on each vCPU v at N = 1..4, the others in linked loops: v halts with
its defence's stop (bad_link, bad_reply, bad_stop, unknown_abort, site_mismatch, trapped, private_access) with its FEX frame intact and its banked
state at a block boundary the run reached (the system, site and site-kind tampers: the unchanged start state, rip
0x100000 and every GPR 0), the others are cancelled, exit 0. A run that outlives its bound by 5 s is killed and fails.
(2) A peer that never answers a
shootdown (`--ignore-kicks` on vCPU k = 1..3 of 4: its doorbells are disabled and its own watchdog is off, so it never
stops or drains, while a host kicker rings it every millisecond, unheard: its kicks issued, its doorbell pending, no IRQ
taken): vCPU 0, waiting in the resident for k's shadow reset, is stopped by its
watchdog, reported `resident` with k's `SHADOWS` request still pending; k is cancelled; an untampered control matches QEMU
with a shootdown. (3) vCPU k's compile server (k = 1..3 of 4) killed before its 51st request: exit 1 within 2 s, stderr
names the vCPU, the server and its signal (`--kill-reps R`: each case R times, a stress check); the same server stopped
instead (SIGSTOP: alive, never answering): the request deadline
(`--compile-deadline-ms`, 2 s here, driver default 30 s) kills it, exit 1, stderr names the vCPU, the server and
"no reply within". (5) A guest loop that never ends on vCPU v of 4 is stopped by the watchdog (`guest`)
while the others reach their ends and match; a guest that legitimately spins past a tight budget is a `guest` watchdog
stop, and reaches its end under the default budget: the watchdog bounds every span inside the VM without a host exit, so
budget and location (guest code or resident) are what separate the two, not the stop kind. Progress: the coherence
programs with sustained cross-vCPU code writes (`mutual`, `data`, `rewrite`, `stale`) and CPUs ending while a peer
translates their page (`finish`, `finish-late`), as `coherence` (QEMU reference, block sizes default and 1).

`privilege` (U1, x86 privilege in shadows: one shadow space per CPL class): see cmd_privilege.

Writes DIR/<mode>.json; exit status 1 on any failure."""
import argparse, collections, concurrent.futures as cf, json, os, pathlib, random, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
sys.path.insert(0, str(ROOT / 'tools' / 'bench' / 'smp'))
import ref, run_slice1_vm as vm, run_slice1_faults as faults, smp, model_smp, programs  # noqa: E402


def driver(host, runtime, fexc, text, work, extra=(), timeout=None):
    """Runs one description; (returncode, stdout JSON or None, stderr). A run still going after `timeout` seconds is
    killed: returncode 'timeout'."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    try:
        run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, *CODE_SHAPE, *map(str, extra)],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode(errors='replace') if isinstance(e.stderr, bytes) else e.stderr or ''
        return 'timeout', None, f'killed after {timeout} s; stderr: {err[-1800:]}'
    out = json.loads(run.stdout) if run.returncode == 0 else None
    return run.returncode, out, run.stderr[-2000:]


def kick_checks(out, kicks=None):
    """Doorbell accounting of one vCPU's output (`kicks`: every kick must have been issued)."""
    k, problems = out.get('kicks'), []
    if k is None: return ['no kick report']
    spi = k['irqs'] - k['irq_sgis']
    # Every host kick is acknowledged once or still pending at the end (none lost or doubled).
    if spi + k['pending'] != k['issued']: problems.append(f"host doorbells {spi} + pending {k['pending']} != issued {k['issued']}")
    # Every doorbell stopped the vCPU once, or merged into the stop it had armed.
    if k['stops'] + k['merged'] != k['irqs']: problems.append(f"stops {k['stops']} + merged {k['merged']} != doorbells {k['irqs']}")
    places = k['stops_entry'] + k['stops_dispatch'] + k['stops_l1'] + k['stops_exit'] + k['stops_resident']
    if places != k['stops']: problems.append(f"stops by place {places} != stops {k['stops']}")
    if k['armed_at_end']: problems.append(f"{k['armed_at_end']} words still armed at the end")
    if out['stats']['stops'] != k['stops']: problems.append(f"resident stops {out['stats']['stops']} != {k['stops']}")
    # A merge is a failure only when the host kicker is the vCPU's only kick source (`kicks` given): it waits for each
    # stop before the next kick, while a peer's resident kick may legitimately land on an armed stop.
    if kicks is not None and k['merged']: problems.append(f"{k['merged']} doorbells merged")
    if kicks is not None and k['issued'] != kicks: problems.append(f"{k['issued']} of {kicks} kicks issued before the end")
    return problems


def places(ks):
    """Stops by place, summed over kick reports."""
    return {p: sum(k[f'stops_{p}'] for k in ks) for p in ('entry', 'dispatch', 'l1', 'exit', 'resident')}


def per_vcpu_checks(out):
    """Checks every vCPU's output must pass whatever it ran."""
    problems = []
    if not out.get('tso'): problems.append('EnTSO not read back')
    if not out.get('frame_intact'): problems.append('FEX frame changed')
    st = out['stats']
    if st['flushes'] == 1 and st['invalidations'] == 0 and st['steps'] == 0 and st['host_requests'] != st['distinct']:
        problems.append(f"host requests {st['host_requests']} != distinct blocks {st['distinct']}")
    return problems


def corpus_items(a):
    """[(name, single description, evaluate(out) -> (outcome, diffs))]."""
    items = []
    fixtures = json.loads((ROOT / 'fixtures/slice1/programs.json').read_text())['programs']
    expected = {r['name']: r for r in json.loads((ROOT / 'fixtures/slice1/expected.json').read_text())['results']}
    for p in fixtures:
        items.append(('fixture/' + p['name'], vm.description(p), lambda out, p=p: vm.evaluate(p, expected[p['name']], out)))
    if a.flow:
        programs = json.loads(a.flow[0].read_text())['programs']
        results = {r['name']: r for r in json.loads(a.flow[1].read_text())['results']}
        for p in programs:
            items.append(('flow/' + p['name'], vm.description(p), lambda out, p=p: vm.evaluate(p, results[p['name']], out)))
    if not a.no_faults:
        for case in faults.expectations(fixtures, faults.probes(), a.out / 'ref'):
            if case[2].get('large'): continue       # guest RAM holds one 2 MiB frame per run, not one per vCPU
            def judge(out, case=case):
                e = faults.evaluate(case, out)
                return e['outcome'], e['diffs']
            items.append((f'fault/{case[0]}', faults.case_description(case), judge))
    return items


def run_groups(a, host, runtime, groups, judge_group):
    """Runs every (label, text, extra) group (work directory: label), `a.parallel` at a time; judge_group(index, rc,
    out, err) -> entries."""
    entries = []
    with cf.ThreadPoolExecutor(a.parallel) as pool:
        futures = [pool.submit(driver, host, runtime, a.fexc, text, a.out / 'work' / label, extra)
                   for label, text, extra in groups]
        for i, f in enumerate(futures):
            rc, out, err = f.result()
            entries += judge_group(i, rc, out, err)
    return entries


def cmd_corpora(a, host, runtime):
    items = corpus_items(a)
    print(f'{len(items)} programs', flush=True)
    results, failed = [], 0
    for n in a.cpus:
        order = items[:]
        random.Random(a.seed + n).shuffle(order)
        order += order[:(-len(order)) % n]          # the last group is filled from the start
        groups = [order[i:i + n] for i in range(0, len(order), n)]
        limit = [a.max_block] if a.max_block else []
        if a.kicks: limit += [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', a.kicks]
        texts = [(f'n{n}g{i}', ''.join('cpu\n' + text for _, text, _ in g), limit) for i, g in enumerate(groups)]

        def judge(i, rc, out, err, groups=groups, n=n):
            entries = []
            for v, (name, _, evaluate) in enumerate(groups[i]):
                e = {'n': n, 'group': i, 'vcpu': v, 'name': name}
                if rc or len(out['cpus']) != n:
                    e.update(outcome='fail', diffs=[f'driver exit {rc}: {err.strip()[-300:]}'])
                else:
                    cpu = out['cpus'][v]
                    outcome, diffs = evaluate(cpu)
                    diffs = list(diffs) + per_vcpu_checks(cpu) + (kick_checks(cpu) if a.kicks else [])
                    if outcome == 'match' and diffs: outcome = 'fail'
                    e.update(outcome=outcome, diffs=diffs, stop=cpu['stop'], stats=cpu['stats'], kicks=cpu.get('kicks'))
                entries.append(e)
            return entries
        entries = run_groups(a, host, runtime, texts, judge)
        counts = collections.Counter(e['outcome'] for e in entries)
        for e in entries:
            if e['outcome'] == 'fail': print(f"N={n} g{e['group']} vcpu{e['vcpu']} {e['name']}: {'; '.join(e['diffs'])[:300]}", flush=True)
        print(f'N={n}: {len(groups)} runs, {dict(counts)}', flush=True)
        if a.kicks:
            ks = [e['kicks'] for e in entries if e.get('kicks')]
            print(f"N={n} kicks: issued {sum(k['issued'] for k in ks)}, in host {sum(k['in_host'] for k in ks)}, "
                  f"stops {sum(k['stops'] for k in ks)} {places(ks)}, pending at end {sum(k['pending'] for k in ks)}", flush=True)
        failed += counts['fail']
        results += entries
    (a.out / 'corpora.json').write_text(json.dumps({'results': results}, indent=1, default=str) + '\n')
    return failed


def smp_description(prog):
    """natemu-smp-v1 -> driver description: the bodies and memory form the shared space, one `cpu` section per CPU. A
    `system` program's space is identity-mapped (its bodies build page tables from physical addresses)."""
    lines = ['identity'] if prog.get('system') else []
    lines += [f"body {smp.num(b['addr']):#x} {b['code']}" for b in prog['bodies']]
    lines += [f"memory {smp.num(r['addr']):#x} {smp.region_bytes(r).hex()}" for r in prog['memory']]
    for c in prog['cpus']:
        lines += ['cpu', f"rip {smp.num(c['rip']):#x}", f"rflags {smp.num(c['rflags']):#x}", f"cpl {c.get('cpl', 3)}",
                  'gpr ' + ' '.join(f'{smp.num(v):#x}' for v in c['gpr'])] + (['sse 1'] if prog.get('sse') else [])
    return '\n'.join(lines) + '\n'


def body_end(prog, rip):
    return next(smp.num(b['addr']) + len(b['code']) // 2 for b in prog['bodies']
                if smp.num(b['addr']) <= rip < smp.num(b['addr']) + len(b['code']) // 2)


def cmd_smp(a, host, runtime):
    programs = [p for p in json.loads((ROOT / 'tools/bench/smp/programs.json').read_text())
                if p['ncpu'] in a.cpus and (not a.programs or p['name'].split('@')[0] in a.programs)]
    groups = [(f"{p['name']}-{r}", smp_description(p), [a.max_block] if a.max_block else []) for p in programs for r in range(a.reps)]
    order = [p for p in programs for _ in range(a.reps)]
    qemu = {p['name']: qemu_reference(p, a.out / 'qemu' / p['name']) for p in programs} if a.qemu else {}

    def judge(i, rc, out, err):
        prog = order[i]
        e = {'name': prog['name'], 'rep': i}
        if rc:
            return [{**e, 'outcome': 'fail', 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
        cpus = out['cpus']
        diffs = []
        for v, (c, cpu) in enumerate(zip(prog['cpus'], cpus)):
            end = body_end(prog, smp.num(c['rip']))
            if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end:
                diffs.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
            diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu)]
        captured = [r for r in prog['memory'] if r.get('capture', True)]
        memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpus[0]['memory']}
        got = {'cpus': [{'gpr': [smp.num(x) for x in cpu['gpr']], 'rflags': smp.num(cpu['rflags'])} for cpu in cpus],
               'memory': [{'addr': smp.num(r['addr']), 'data': memory[smp.num(r['addr'])]} for r in captured]}
        diffs += smp.compare(prog, got, model_smp.expected(prog))
        if prog['name'] in qemu: diffs += ['qemu: ' + d for d in smp.compare(prog, got, qemu[prog['name']])]
        outcome = 'fail' if diffs else 'match'
        stats = [{k: cpu['stats'][k] for k in ('blocks', 'requests', 'links', 'retries', 'linked', 'link_points')} for cpu in cpus]
        return [{**e, 'outcome': outcome, 'diffs': diffs, 'stats': stats}]
    entries = run_groups(a, host, runtime, groups, judge)
    by = collections.defaultdict(collections.Counter)
    for e in entries: by[e['name']][e['outcome']] += 1
    for name, c in by.items(): print(f'{name}: {dict(c)}', flush=True)
    for e in entries:
        if e['outcome'] != 'match': print(f"{e['name']} rep{e['rep']} {e['outcome']}: {'; '.join(e['diffs'])[:400]}")
    # I3: LOCK NEG's lost-race exit to its own RIP must have run: under the race some CPU linked an exit that no CPU of the
    # race-free loop ever takes (the same code; its other exits are taken in both).
    lost = 0
    for n in a.cpus:
        race = [s['linked'] for e in entries if e['name'] == f'neg-race@{n}' for s in e['stats']]
        free = [s['linked'] for e in entries if e['name'] == f'neg-private@{n}' for s in e['stats']]
        if race and free:
            print(f"neg-race@{n}: linked exits per CPU {sorted(set(race))}, race-free {sorted(set(free))}", flush=True)
            if max(race) <= max(free): lost += 1; print(f'neg-race@{n}: no CPU took the lost-race self-exit')
    (a.out / 'smp.json').write_text(json.dumps({'results': entries}, indent=1) + '\n')
    return sum(c['fail'] for c in by.values()) + lost


def cmd_litmus(a, host, runtime):
    from models import ordering_model as model
    control = a.control in ('drop-fence', 'drop-lock')
    # drop-lock (I3): the LOCKed RMW families without their LOCK prefix (plain read-modify-writes: no barrier, not atomic).
    families = (a.families or ['sb-xadd', 'xadd']) if a.control == 'drop-lock' else ['sb-mfence'] if control else a.families or list(programs.FAMILIES)
    # The control refuses the split variants: a split store completes in the resident with `dsb ish`, so SB cannot relax there.
    if control and a.variants and any(v.startswith('split') for v in a.variants):
        sys.exit(f'--control {a.control} cannot run split variants: a split store completes with dsb ish, so SB cannot relax there')
    variants = a.variants or [v for v in programs.VARIANTS if not (control and v.startswith('split'))]
    cells = [(f, n, v) for f in families for n in a.cpus for v in variants
             if programs.litmus_family(f, n) and not (f in ('xchg', 'xadd', 'cas', 'cas-read', 'cas8b-read') and v.startswith('split'))]   # (no plain location to split)
    failures, report = [], []
    for f, n, v in cells:
        test = programs.litmus_family(f, n)
        prog = programs.family_program(test, v, a.k, fences=not control)
        allowed = model.enumerate_outcomes(test).outcomes
        groups = [(f"{prog['name']}-{r}", smp_description(prog), []) for r in range(a.runs)]

        def judge(i, rc, out, err, prog=prog, test=test):
            if rc: return [{'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
            diffs = [f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']}" for v, c in enumerate(out['cpus'])
                     if c['stop'] != 'reached' or smp.num(c['rip']) != body_end(prog, smp.num(prog['cpus'][v]['rip']))]
            diffs += [f'cpu{v} ' + d for v, c in enumerate(out['cpus']) for d in per_vcpu_checks(c)]
            got = {'memory': [{'addr': smp.num(m['addr']), 'data': bytes.fromhex(m['data'])} for m in out['cpus'][0]['memory']]}
            try: outcomes = programs.litmus_outcomes(prog, test, got, a.k, prog['params']['stride'], programs.FAMILY_RES)
            except AssertionError as e: return [{'diffs': diffs + [str(e)]}]
            return [{'diffs': diffs, 'hist': collections.Counter(outcomes)}]
        entries = run_groups(a, host, runtime, groups, judge)
        hist = collections.Counter()
        for e in entries: hist.update(e.get('hist', {}))
        forbidden = {o: c for o, c in hist.items() if o not in allowed}
        unchained = []
        if f == 'xchg':       # each outcome: returned tokens and the final word form one exchange order
            for o in hist:
                try: model.check_xchg_chain({i: i + 1 for i in range(n)}, dict(enumerate(o[:n])), o[n])
                except ValueError as e: unchained.append((o, str(e)))
        label = f'{f}@{n} {v}' + ((' without LOCK' if a.control == 'drop-lock' else ' without MFENCE') if control else '')
        trials = sum(hist.values())
        print(f"{label}: {trials} trials, {len(hist)} distinct outcomes ({len(allowed)} allowed), forbidden {sum(forbidden.values())} "
              f"{dict(sorted(forbidden.items()))}; histogram {dict(sorted(hist.items()))}", flush=True)
        bad = [e['diffs'] for e in entries if e['diffs']]
        # Run diffs (a vCPU not reaching its end, a driver error) and the trial count are judged in control mode too: a
        # control that lost trials or vCPUs would show its outcome for the wrong reason.
        if bad: failures.append(f'{label}: {bad[0][:3]}')
        if trials != a.k * a.runs: failures.append(f'{label}: {trials} of {a.k * a.runs} trials')
        if control:
            if not forbidden: failures.append(f'{label}: control shows no forbidden outcome')
        else:
            if forbidden: failures.append(f'{label}: forbidden {forbidden}')
            if unchained: failures.append(f'{label}: not linearizable {unchained[:3]}')
        report.append({'family': f, 'n': n, 'variant': v, 'control': control, 'trials': trials,
                       'histogram': {str(o): c for o, c in sorted(hist.items())}, 'allowed': len(allowed),
                       'forbidden': {str(o): c for o, c in forbidden.items()}, 'run_diffs': bad})
    for x in failures: print(x)
    (a.out / (f'litmus-{a.control}.json' if control else 'litmus.json')).write_text(json.dumps({'report': report, 'failures': failures}, indent=1) + '\n')
    return len(failures)


def cmd_limits(a, host, runtime):
    failures, report = [], {}
    loop = 'body 0x100000 ebfe\n'                  # jmp $: a linked loop with no exit
    five = loop + ''.join('cpu\nrip 0x100000\n' for _ in range(5))
    rc, out, err = driver(host, runtime, a.fexc, five, a.out / 'work' / 'five')
    report['five'] = {'returncode': rc, 'stderr': err.strip()}
    if rc == 0 or '5 vCPUs' not in err: failures.append(f'5 vCPUs not refused: {rc} {err.strip()[-200:]}')
    straight = vm.description(json.loads((ROOT / 'fixtures/slice1/programs.json').read_text())['programs'][0])
    for v in range(4):
        text = ''.join('cpu\n' + (straight if w == v else 'rip 0x100000\nbody 0x100000 ebfe\n') for w in range(4))
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f'forge{v}', ['--forge-exit', '--tamper-vcpu', v])
        stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
        report[f'forge{v}'] = {'returncode': rc, 'stops': stops, 'stderr': err.strip()[-300:]}
        want = [('resident', 'bad_reply status=1 detail=2') if w == v else ('cancelled', '') for w in range(4)]
        if rc or stops != want: failures.append(f'forged exit on vCPU {v}: {rc} {stops} {err.strip()[-200:]}')
    for f in failures: print(f)
    print(json.dumps(report, indent=1))
    (a.out / 'limits.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1) + '\n')
    return len(failures)


HOT = ref.assemble('1: add rax, rcx\nxor rdx, rax\nrol rdx, 7\ndec rcx\njnz 1b').hex()


def stack(v):
    """vCPU v's RSP: the reference's capture sequence uses the stack (as tools/bench/smp's programs)."""
    return f'{0x3e1800 + 0x2000 * v:#x}'


def hot_program(n, iterations):
    """N vCPUs, each in the hot loop on its own registers (shared code page, no memory)."""
    cpus = [{'rip': '0x110000', 'rflags': 0,
             'gpr': [f'{0x9e3779b97f4a7c15 * (v + 1) & (2**64 - 1):#x}', f'{iterations + 7 * v:#x}', '0x0', '0x0', stack(v)] + ['0x0'] * 11}
            for v in range(n)]
    return {'name': f'hot@{n}', 'ncpu': n, 'bodies': [{'addr': '0x110000', 'code': HOT}], 'cpus': cpus,
            'memory': [{'addr': '0x120000', 'size': 64, 'fill': 'zero'}], 'params': {}, 'format': 'natemu-smp-v1'}


REP_BUF, REP_STRIDE, REP_QWORDS = 0x130000, 0x20000, 0x1000     # per vCPU: two 32 KiB halves and a sentinel page above
REP_LOOP = ref.assemble(
    '1: mov rdi, rbx\nmov ecx, 0x1000\nmov rax, r8\nrep stosq\n'              # fill the low half with r8
    'mov rsi, rbx\nlea rdi, [rbx + 0x8000]\nmov ecx, 0x1000\nrep movsq\n'     # copy it up
    'lea rsi, [rbx + 0x7ff8]\nlea rdi, [rbx + 0xfff8]\nmov ecx, 0x1000\nstd\nrep movsq\ncld\n'   # and down again
    'lea rsi, [rbx + 1]\nmov rdi, rbx\nmov ecx, 0xfff\nrepe cmpsb\nadd r9, rcx\n'   # equal bytes: REPE runs to the end
    'lea rdi, [rbx + 0x8000]\nmov ecx, 0x8000\nmov al, 0x5a\nrepne scasb\nadd r10, rcx\n'
    'mov rdx, 0x0101010101010101\nadd r8, rdx\ndec r12\njnz 1b').hex()


def rep_program(n, iterations):
    """I2: N vCPUs, each `iterations` rounds of 32 KiB REP STOSQ, REP MOVSQ up and (DF = 1) down, REPE CMPSB and REPNE
    SCASB over its own buffer (rbx); nearly all its time is inside REP strings, so kicks stop it between elements (the
    REP's own exit) and it must resume there: final registers, buffers and the sentinel page above them equal QEMU's."""
    cpus = [{'rip': '0x110000', 'rflags': 0,
             'gpr': [f'{x:#x}' for x in (0, 0, 0, REP_BUF + REP_STRIDE * v, int(stack(v), 16), 0, 0, 0, 0x1111111111111111 * (v + 1), 0, 0, 0,
                                          iterations + v)] + ['0x0'] * 3} for v in range(n)]
    return {'name': f'rep@{n}', 'ncpu': n, 'bodies': [{'addr': '0x110000', 'code': REP_LOOP}], 'cpus': cpus, 'params': {},
            'format': 'natemu-smp-v1', 'memory': [{'addr': f'{REP_BUF + REP_STRIDE * v:#x}', 'size': 0x11000, 'fill': 'lcg', 'seed': v + 1}
                                                   for v in range(n)]}


def peer_program(iterations):
    """vCPU 0: set the flag at 0x120000, then the hot loop (its first block, flag store to jnz, is placed before the
    store runs, so the code page is marked before the flag is seen). vCPU 1 (own code page): wait for the flag, then
    spin 2 * 10^6 iterations (vCPU 0 is then in its hot loop) and store rcx to 0x110800 (data on vCPU 0's code page) 64
    times: the first store posts the page to vCPU 0 and kicks it, which drops its running loop block and translates it
    again."""
    cpu0 = ref.assemble('mov byte ptr [0x120000], 1').hex() + HOT
    cpu1 = ref.assemble('1: cmp byte ptr [0x120000], 0\nje 1b\nmov ecx, 2000000\n3: dec ecx\njnz 3b\n'
                        'mov ecx, 64\n2: mov [0x110800], rcx\ndec rcx\njnz 2b').hex()
    gpr = lambda *v: [f'{x:#x}' for x in v] + ['0x0'] * (16 - len(v))
    return {'name': 'peer-kick@2', 'ncpu': 2, 'params': {}, 'format': 'natemu-smp-v1',
            'bodies': [{'addr': '0x110000', 'code': cpu0}, {'addr': '0x111000', 'code': cpu1}],
            'cpus': [{'rip': '0x110000', 'rflags': 0, 'gpr': gpr(0x1234567, iterations, 0, 0, 0x3e1800)},
                     {'rip': '0x111000', 'rflags': 0, 'gpr': gpr(0, 0, 0, 0, 0x3e3800)}],
            'memory': [{'addr': '0x120000', 'size': 64, 'fill': 'zero'}, {'addr': '0x110800', 'size': 8, 'fill': 'zero'}]}


def adversarial_program(n, iterations):
    """N vCPUs on one shared code page, own registers. Each round: a computed jump into a function and a computed jump
    back (the dispatcher and L1 every time), a 128/64 division (`svc`, resumed mid-block), and every 1,024th round a
    store of the same byte over the next instruction (self-modifying code: the store is single-stepped, the page's blocks
    are invalidated, and the peers holding them are posted and rung). The rounds' results stay in r8-r10."""
    code = ref.assemble(
        '1: lea r13, [rip + 3f]\nlea rax, [rip + 5f]\njmp rax\n'
        '3: mov rdx, 1\nmov rax, rcx\ndiv rbx\nadd r8, rax\nadd r9, rdx\n'
        'test ecx, 0x3ff\njnz 4f\nmov byte ptr [rip + 2f], 0x90\n2: nop\n'
        '4: dec rcx\njnz 1b\njmp 6f\n'
        '5: add r10, rcx\nrol r10, 3\njmp r13\n6:').hex()
    gpr = lambda v: [f'{x:#x}' for x in (0, iterations + 13 * v, 0, 3, int(stack(v), 16), 0, 0, 0, 7 * v, 0, v)] + ['0x0'] * 5
    return {'name': f'adversarial@{n}', 'ncpu': n, 'params': {}, 'format': 'natemu-smp-v1',
            'bodies': [{'addr': '0x110000', 'code': code}], 'cpus': [{'rip': '0x110000', 'rflags': 0, 'gpr': gpr(v)} for v in range(n)],
            # Data on the code page makes it writable (the self-modifying store).
            'memory': [{'addr': '0x110800', 'size': 8, 'fill': 'zero'}]}


def qemu_reference(prog, work):
    """The program's final state under pinned QEMU system mode (smp.parse_dump form)."""
    rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, smp.build_stub(work)), work, 'multi', timeout=600)
    if rc != 1: raise RuntimeError(f'QEMU {prog["name"]}: {rc} {err}')
    return smp.parse_dump(prog, dump)


def judge_smp(prog, want, out, kicks):
    """Differences of a natemu run of `prog` from QEMU's `want`; `kicks[v]`: kicks vCPU v must have been issued (None:
    no count required)."""
    diffs = []
    cpus = out['cpus']
    for v, (c, cpu) in enumerate(zip(prog['cpus'], cpus)):
        end = body_end(prog, smp.num(c['rip']))
        if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end:
            diffs.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
        diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu) + kick_checks(cpu, kicks[v])]
    memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpus[0]['memory']}
    got = {'cpus': [{'gpr': [smp.num(x) for x in cpu['gpr']], 'rflags': smp.num(cpu['rflags'])} for cpu in cpus],
           'memory': [{'addr': smp.num(r['addr']), 'data': memory[smp.num(r['addr'])]} for r in prog['memory'] if r.get('capture', True)]}
    return diffs + smp.compare(prog, got, want)


def cmd_kicks(a, host, runtime):
    failures, report = [], {'hot': [], 'adversarial': [], 'peer': [], 'forge': []}
    for n in a.cpus:
        prog = hot_program(n, a.iterations)
        want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
        extra = [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', a.kicks]
        groups = [(f"{prog['name']}-{r}", smp_description(prog), extra) for r in range(a.reps)]

        def judge(i, rc, out, err, prog=prog, want=want, n=n):
            e = {'name': prog['name'], 'rep': i}
            if rc: return [{**e, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
            return [{**e, 'diffs': judge_smp(prog, want, out, [a.kicks] * n), 'kicks': [c['kicks'] for c in out['cpus']],
                     'stats': [{k: c['stats'][k] for k in ('blocks', 'requests', 'stops', 'linker')} for c in out['cpus']]}]
        entries = run_groups(a, host, runtime, groups, judge)
        for e in entries:
            ks = e.get('kicks', [])
            lat = [k['latency_ns'] for k in ks]
            print(f"{e['name']} rep{e['rep']}: {'ok' if not e['diffs'] else 'FAIL ' + '; '.join(e['diffs'])[:400]}; "
                  f"issued {[k['issued'] for k in ks]} in_host {[k['in_host'] for k in ks]} stops {[k['stops'] for k in ks]} {places(ks)}; "
                  f"ack ns {[k['ack_ns_mean'] for k in ks]}; ring ns {[k['ring_ns_mean'] for k in ks]}; "
                  f"latency p50/p99/p999/max us {[(l['p50'] / 1e3, l['p99'] / 1e3, l['p999'] / 1e3, l['max'] / 1e3) for l in lat]}", flush=True)
            failures += [f"{e['name']} rep{e['rep']}: {d}" for d in e['diffs']]
        report['hot'] += entries
    # I2: kicks into long REP strings (rep_program): every stop lands between elements and the run must still match QEMU.
    report['rep'] = []
    for n in a.cpus:
        prog = rep_program(n, a.rep_iterations)
        want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
        extra = [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', a.kicks]
        groups = [(f"{prog['name']}-{r}", smp_description(prog), extra) for r in range(a.reps)]

        def judge(i, rc, out, err, prog=prog, want=want, n=n):
            e = {'name': prog['name'], 'rep': i}
            if rc: return [{**e, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
            return [{**e, 'diffs': judge_smp(prog, want, out, [a.kicks] * n), 'kicks': [c['kicks'] for c in out['cpus']],
                     'stats': [{k: c['stats'][k] for k in ('blocks', 'requests', 'stops', 'linker')} for c in out['cpus']]}]
        for e in run_groups(a, host, runtime, groups, judge):
            ks = e.get('kicks', [])
            print(f"{e['name']} rep{e['rep']}: {'ok' if not e['diffs'] else 'FAIL ' + '; '.join(e['diffs'])[:400]}; "
                  f"issued {[k['issued'] for k in ks]} stops {[k['stops'] for k in ks]} {places(ks)}", flush=True)
            failures += [f"{e['name']} rep{e['rep']}: {d}" for d in e['diffs']]
            report['rep'].append(e)
    # Adversarial: kicks at random times (0..J us after each stop) into a program that keeps every vCPU moving through
    # blocks, the dispatcher, L1, mid-block resident entries and the step block, with peers' SGIs from its code writes.
    # Every run matches QEMU with the doorbell accounting; over all runs every place must have taken stops.
    seen = collections.Counter()
    for n in a.cpus:
        prog = adversarial_program(n, a.adversarial_iterations)
        want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
        extra = [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', a.kicks, '--kick-jitter-us', a.jitter_us]
        groups = [(f"{prog['name']}-{r}", smp_description(prog), extra) for r in range(a.reps)]

        def judge(i, rc, out, err, prog=prog, want=want, n=n):
            e = {'name': prog['name'], 'rep': i}
            if rc: return [{**e, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
            # Peers' SGIs may merge into a stop a host kick armed: no merge count is required.
            diffs = judge_smp(prog, want, out, [None] * n)
            diffs += [f'cpu{v} {c["kicks"]["issued"]} of {a.kicks} kicks issued' for v, c in enumerate(out['cpus']) if c['kicks']['issued'] != a.kicks]
            return [{**e, 'diffs': diffs, 'kicks': [c['kicks'] for c in out['cpus']], 'steps': [c['stats']['steps'] for c in out['cpus']]}]
        for e in run_groups(a, host, runtime, groups, judge):
            ks = e.get('kicks', [])
            seen.update(places(ks))
            seen.update({k: sum(x[k] for x in ks) for k in ('irqs_step', 'rearms', 'irq_sgis', 'merged')})
            print(f"{e['name']} rep{e['rep']}: {'ok' if not e['diffs'] else 'FAIL ' + '; '.join(e['diffs'])[:400]}; "
                  f"stops {[k['stops'] for k in ks]} {places(ks)}; in step {[k['irqs_step'] for k in ks]}, "
                  f"re-armed {[k['rearms'] for k in ks]}, SGIs {[k['irq_sgis'] for k in ks]}, merged {[k['merged'] for k in ks]}, "
                  f"steps {e.get('steps')}; ack ns {[k['ack_ns_mean'] for k in ks]}", flush=True)
            failures += [f"{e['name']} rep{e['rep']}: {d}" for d in e['diffs']]
            report['adversarial'].append(e)
    # A resident-entry stop needs a doorbell pending when the vCPU enters the resident: peers' SGIs (N > 1).
    wanted = ('entry', 'dispatch', 'l1', 'exit', 'irqs_step', 'rearms') + (('resident',) if max(a.cpus) > 1 else ())
    missing = [p for p in wanted if not seen[p]]
    print(f'adversarial places: {dict(seen)}', flush=True)
    if missing: failures.append(f'adversarial: no stop at {missing}')
    # A peer's resident kick: no host kicker; vCPU 0 must take at least one stop and drop its blocks from the page.
    prog = peer_program(a.iterations)
    want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
    for r in range(a.reps):
        rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f'peer-{r}')
        diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else judge_smp(prog, want, out, [None, None])
        if not rc:
            k0, st0 = out['cpus'][0]['kicks'], out['cpus'][0]['stats']
            if k0['irq_sgis'] < 1 or k0['stops'] < 1 or st0['requests'] < 3:
                diffs.append(f'vCPU 0 not stopped by the peer, or its loop not translated again: {k0} {st0}')
        report['peer'].append({'rep': r, 'diffs': diffs, 'kicks': out and [c['kicks'] for c in out['cpus']],
                               'stats': out and [c['stats'] for c in out['cpus']]})
        print(f"peer-kick rep{r}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:400]}; "
              f"vCPU 0 kicks {out and {k: out['cpus'][0]['kicks'][k] for k in ('irq_sgis', 'stops', 'merged', 'sgi_ns_mean')}}, "
              f"vCPU 1 SGIs sent {out and out['cpus'][1]['kicks']['sgis_sent']}, "
              f"requests {out and out['cpus'][0]['stats']['requests']}", flush=True)
        failures += [f'peer rep{r}: {d}' for d in diffs]
    # Forged stops on each vCPU of 4 (the stop word at a block entry; an armed record at a word without it): it halts
    # with bad_stop, the others are cancelled.
    prog = hot_program(4, a.iterations)
    for kind, extra in (('stop', ['--forge-stop']), ('armed', ['--tamper', 'armed'])):
        for v in range(4):
            rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f'forge-{kind}{v}',
                                  ['--kick-vcpu', v, '--kicks', 1, *extra, '--tamper-vcpu', v])
            stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
            want_stops = [('resident', 'bad_stop status=1 detail=11') if w == v else ('cancelled', '') for w in range(4)]
            ok = rc == 0 and stops == want_stops and out['cpus'][v]['tampered']
            report['forge'].append({'kind': kind, 'vcpu': v, 'returncode': rc, 'stops': stops, 'native': out and out['cpus'][v]['native']})
            print(f'forged {kind} on vCPU {v}: {"ok" if ok else "FAIL"} {stops}', flush=True)
            if not ok: failures.append(f'forged {kind} on vCPU {v}: {rc} {stops} {err.strip()[-200:]}')
    (a.out / 'kicks.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def vm_fault(cpu):
    """natemu's guest exception as smp.parse_dump reports QEMU's: (vector, error, RIP, CR2), or None."""
    if cpu['stop'] != 'guest_fault': return None
    vector, cr2, error, rip = (smp.num(x) for x in cpu['fault'])
    return vector, error, rip, cr2 if vector == 14 else 0


def qemu_fault(f):
    """QEMU's fault record, with one declared correction: pinned QEMU reports a reserved-bit #PF (RSVD) with P = 0; the
    SDM (vol. 3 §4.7: the entry is present) and coherence_model.walk (`fault(9)`) have P = 1."""
    return f and (f[0], f[1] | 1 if f[0] == 14 and f[1] & 8 else f[1], *f[2:])


def vm_state(prog, out):
    """natemu's run in smp.parse_dump's shape."""
    memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in out['cpus'][0]['memory']}
    return {'cpus': [{'gpr': [smp.num(x) for x in c['gpr']], 'rflags': smp.num(c['rflags']), 'fault': vm_fault(c)} for c in out['cpus']],
            'memory': [{'addr': smp.num(r['addr']), 'data': memory[smp.num(r['addr'])]} for r in prog['memory'] if r.get('capture', True)]}


def either_check(prog, out):
    """The PTE change without INVLPG (`sys-pte-no-invlpg`): each CPU's second read of V (r8) and V's final PTE in table A
    must be one of coherence_model's permitted histories (cached translation, or a fresh walk) after its first read."""
    import programs
    from models import coherence_model as cm
    problems = []
    memory = {smp.num(r['addr']): smp.region_bytes(r) for r in prog['memory']}
    for i, cpu in enumerate(out['cpus']):
        ta, d = programs.SYS_T(i, 0), programs.SYS_D(i)
        tables = {}
        for a, data in memory.items():
            if ta <= a < ta + 0x5000: tables.update({a + k: int.from_bytes(data[k:k + 8], 'little') for k in range(0, len(data), 8)})
        ctx = cm.Context('four', ta, cpl=0, wp=True, nxe=True)
        first = cm.walk(tables, ctx, d + programs.V, 'read')
        cache = cm.Cache(); cache.remember(ctx, first)
        leaf = programs.sys_leaf(i, 0, d + programs.V)
        changed = dict(first.tables); changed[leaf] = (d + programs.F2) | 7
        permitted = cm.access_outcomes(changed, ctx, d + programs.V, 'read', cache)
        got = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in cpu['memory']}
        frames = {memory[d + f][:8]: d + f for f in (programs.F1, programs.F2)}
        frame = frames.get(smp.num(cpu['gpr'][8]).to_bytes(8, 'little'))
        start = max(a for a in got if a <= leaf)
        pte = int.from_bytes(got[start][leaf - start:leaf - start + 8], 'little')
        observed = [r for r in permitted if r.route and r.route.gpa & ~4095 == frame and r.tables[leaf] == pte]
        try:
            cm.check_observation(observed[0] if observed else None, permitted)
            cpu['either'] = observed[0].policy
        except ValueError as e:
            problems.append(f'cpu{i}: read {cpu["gpr"][8]} (frame {frame and hex(frame)}), PTE {pte:#x}: {e}')
    return problems


def unsupported_cases():
    """Instructions the resident refuses (`unsupported_system`), each alone at CPL0 after a flag-setting prefix."""
    cases = {'cr3-pwt': 'mov rax, cr3\nbts rax, 3\nmov cr3, rax',
             # A DR7 that enables a breakpoint (L0, G3): breakpoints are S4's.
             'dr7-l0': 'mov eax, 0x401\nmov dr7, rax', 'dr7-g3': 'mov eax, 0x480\nmov dr7, rax',
             # IA32_APIC_BASE moved (a valid value): the LAPIC's page and mode, no LAPIC yet.
             'wrmsr-apic-base-move': 'mov ecx, 0x1b\nmov eax, 0xfed00800\nxor edx, edx\nwrmsr'}
    return {k: ref.assemble('mov rbx, 1\n' + v).hex() for k, v in cases.items()}


# Resident behaviour that disagrees with the CPU 16/17 profile (tools/models/cpu_profile.json, via tools/system_fault_model.py):
# the case runs and its mismatch is printed as GAP, not counted as a failure. A listed case that starts to pass is a failure,
# so the entry must be deleted when the resident is fixed. Empty since S1.
SYSTEM_GAPS = {}
# Fault cases where pinned QEMU raises another exception than the SDM and the profile (system_fault_model) give: the
# model judges; QEMU's answer is recorded, not compared.
QEMU_DIFFERS = {'sysfault-rdpmc@1': 'QEMU raises #UD for RDPMC at CPL 0 (unimplemented); with no counters (CPUID.0AH = 0) every ECX is an invalid counter: #GP(0)',
                # S2b (segment_model): QEMU's choices where the SDM (or the model's recorded choice) differs.
                'seg-ss-null-rpl1@1': 'QEMU loads a null SS with RPL != CPL at CPL 0; SDM MOV 64-bit mode exceptions: #GP(0)',
                'seg-ltr-null@1': 'QEMU loads a null TR; SDM LTR: #GP(0) for a null selector',
                'seg-call-far-noncanonical@1': 'QEMU commits the call and faults at the fetch of the non-canonical RIP; SDM CALL: #GP(0) at the CALL',
                'seg-lret-noncanonical@1': 'QEMU commits the return and faults at the fetch of the non-canonical RIP; SDM RET: #GP(0) at the RET',
                'seg-iret-null-ss-rpl@1': 'QEMU accepts the null SS with RPL != the new CPL and does not reach the end (timeout); SDM IRET 64-bit mode exceptions: #GP(0) (segment_model B6)',
                # S3 (syscall_model): the profile's CPU (Intel, CPU 16) where pinned QEMU's -cpu max or its helpers differ.
                'sc-sysret-noncanonical@1': "QEMU commits SYSRET and faults at the fetch of the non-canonical RIP (AMD's behaviour); Intel SDM SYSRET: #GP(0) at CPL 0 before the switch (syscall_model C4)",
                'sc-wrfsbase-noncanonical@1': 'QEMU loads a non-canonical FS base; SDM WRFSBASE: #GP(0)',
                'sc-wrgsbase-noncanonical-cpl3@1': "QEMU loads a non-canonical GS base and its stub's fault handler, which uses GS, does not finish; SDM WRGSBASE: #GP(0)",
                'sc-rdseed@1': 'QEMU -cpu max implements RDSEED; the profile does not advertise it (CPUID.7.0:EBX.RDSEED = 0): #UD',
                'sc-rdpid-cpl3@1': 'QEMU -cpu max implements RDPID; the profile does not advertise it (CPUID.7.0:ECX.RDPID = 0): #UD',
                'sc-clflushopt@1': 'QEMU -cpu max implements CLFLUSHOPT; the profile does not advertise it: #UD',
                'sc-wbinvd-cpl3@1': 'QEMU executes WBINVD at CPL 3; SDM WBINVD: #GP(0) if CPL > 0',
                'sc-invd-cpl3@1': 'QEMU executes INVD at CPL 3; SDM INVD: #GP(0) if CPL > 0',
                'sc-clflush-noncanonical@1': 'QEMU makes no access for CLFLUSH; SDM CLFLUSH (faults of a byte load): #GP(0) for a non-canonical address',
                'sc-clflush-noncanonical-rbp@1': 'QEMU makes no access for CLFLUSH; SDM CLFLUSH: #SS(0) for a non-canonical address through RBP',
                'sc-clflush-supervisor-cpl3@1': 'QEMU makes no access for CLFLUSH; SDM CLFLUSH: #PF(5) for a CPL 3 byte load of a supervisor page'}


# ---- S1: the presented CPU, judged by tools/models/cpu_profile.py (QEMU presents its own CPU) --------------------------------
TSC_WRITE = 1 << 48


def profile_program(n):
    """natemu-smp-v1 program (identity, CPL0) whose CPU i stores, at RES(i): its MSRs after reset (but EFER, set by the
    driver, and TSC), the CPUID matrix of gen_cpu_profile_rs.leaves, CPUID with MISC_ENABLE bit 22 set, MISC_ENABLE and
    UCODE_REV after writes, CPUID.1 reloading the UCODE_REV latch, IA32_APIC_BASE after a same-value write and after
    toggling its BSP flag (bit 8, R/W), TSC/RDTSC/RDTSCP and TSC_AUX, and CR4 after setting PSE and TSD. Cross-vCPU TSC
    order: CPU 0 reads its TSC and then sets a flag; every other CPU waits for the flag and then reads its own, which must
    not be smaller. Returns (program, judge): judge([bytes of RES(i)]) lists the differences from the model."""
    from models import cpu_profile as cp, gen_cpu_profile_rs as gen
    P = cp.load()
    readable = [m for m in sorted(P._msr) if P.msr(m)['access'] in ('rw', 'ro') and m not in (0x10, 0xc0000080)]
    matrix = gen.leaves(P)
    limited = [0, 1, 2, 3, 4, 0x15, 0x16, 0x40000000, 0x80000000, 0x80000008, 0x80000009]
    res = lambda i: programs.SYS_D(i) + 0x8000
    bodies, cpus, memory, layouts, flag = [], [], [], [], [None]
    for i in range(n):
        out, lay, at = [], [], [0]
        def slot(kind, info, size):
            lay.append((kind, info, at[0])); at[0] += size; return res(i) + at[0] - size
        def rd(index, kind, info=None):
            o = slot(kind, index if info is None else info, 8)
            out.append(f'mov ecx, {index:#x}\nrdmsr\nmov [{o:#x}], eax\nmov [{o + 4:#x}], edx')
        def wr(index, value):
            out.append(f'mov ecx, {index:#x}\nmov eax, {value & 0xffffffff:#x}\nmov edx, {value >> 32:#x}\nwrmsr')
        def cpuid(leaf, sub, kind):
            o = slot(kind, (leaf, sub), 16)
            out.append(f'mov eax, {leaf:#x}\nmov ecx, {sub:#x}\ncpuid\n' + '\n'.join(f'mov [{o + 4 * k:#x}], {r}' for k, r in enumerate(('eax', 'ebx', 'ecx', 'edx'))))
        for m in readable: rd(m, 'reset')
        for leaf, sub in matrix: cpuid(leaf, sub, 'cpuid')
        wr(0x1a0, 0x401801)
        for leaf in limited: cpuid(leaf, 0, 'cpuid-limited')
        rd(0x1a0, 'misc', 0x401801)
        for v in (0x1801, 0x1, 0x80, 0x800, 0x401881, 0x1801):
            wr(0x1a0, v); rd(0x1a0, 'misc', v)
        wr(0x8b, 0x1234 << 32); rd(0x8b, 'value', 0x1234 << 32)
        out.append('mov eax, 1\nxor ecx, ecx\ncpuid'); rd(0x8b, 'value', 0)
        apic = 0xfee00900 if i == 0 else 0xfee00800
        out.append('mov ecx, 0x1b\nrdmsr\nwrmsr'); rd(0x1b, 'value', apic)
        wr(0x1b, apic ^ 0x100); rd(0x1b, 'value', apic ^ 0x100); wr(0x1b, apic)
        # Cross-vCPU order (before any TSC write): CPU 0's RDTSC, then its flag; the others' RDTSC after seeing the flag.
        x = slot('order', 0, 8)
        if i == 0:
            flag[0] = slot('flag', 0, 8)
            out.append(f'rdtsc\nmov [{x:#x}], eax\nmov [{x + 4:#x}], edx\nmov dword [{flag[0]:#x}], 1')
        else:
            out.append(f'.wait:\npause\ncmp dword [{flag[0]:#x}], 1\njne .wait\nrdtsc\nmov [{x:#x}], eax\nmov [{x + 4:#x}], edx')
        # TSC: RDTSC <= RDMSR TSC <= RDTSCP (ECX = TSC_AUX = 0, then 0x55 + i); after a TSC write W: W <= RDMSR <= RDTSC.
        t = [slot('tsc', k, 8) for k in range(6)]
        aux = [slot('aux', k, 4) for k in range(2)]
        out.append(f'rdtsc\nmov [{t[0]:#x}], eax\nmov [{t[0] + 4:#x}], edx')
        out.append(f'mov ecx, 0x10\nrdmsr\nmov [{t[1]:#x}], eax\nmov [{t[1] + 4:#x}], edx')
        out.append(f'rdtscp\nmov [{t[2]:#x}], eax\nmov [{t[2] + 4:#x}], edx\nmov [{aux[0]:#x}], ecx')
        wr(0xc0000103, 0x55 + i)
        out.append(f'rdtscp\nmov [{t[3]:#x}], eax\nmov [{t[3] + 4:#x}], edx\nmov [{aux[1]:#x}], ecx')
        wr(0x10, TSC_WRITE)
        out.append(f'mov ecx, 0x10\nrdmsr\nmov [{t[4]:#x}], eax\nmov [{t[4] + 4:#x}], edx')
        out.append(f'rdtsc\nmov [{t[5]:#x}], eax\nmov [{t[5] + 4:#x}], edx')
        c4 = [slot('cr4', k, 8) for k in range(2)]
        out.append(f'mov rax, cr4\nmov [{c4[0]:#x}], rax\nmov rbx, rax\nor rax, 0x14\nmov cr4, rax\nrdtsc\nmov rax, cr4\nmov [{c4[1]:#x}], rax\nmov cr4, rbx')
        out.append('cmp r11, r11')
        layouts.append(lay)
        code = programs.BODY0 + 0x2000 * i
        bodies.append(programs.body(code, '\n'.join(out)))
        assert len(bodies[-1]['code']) // 2 < 0x2000
        cpus.append(dict(programs.cpu(i, code), cpl=0))
        memory.append(programs.zero(res(i), at[0]))
    prog = {'format': 'natemu-smp-v1', 'name': f'profile@{n}', 'ncpu': n, 'system': True, 'bodies': bodies, 'cpus': cpus, 'memory': memory}
    reset = {**{m: cp._i(P.msr(m)['reset']) for m in readable}, **P.mtrr_state()}

    def judge(datas):
        order = [int.from_bytes(d[o:o + 8], 'little') for d, lay in zip(datas, layouts) for kind, _, o in lay if kind == 'order']
        diffs = [f'cpu{i} TSC {t:#x} read after CPU 0 read {order[0]:#x}' for i, t in enumerate(order) if t < order[0]]
        if order[0] > 60 * cp.load().d['identity']['tsc_hz']: diffs.append(f'CPU 0 TSC {order[0]:#x}: not near 0 after VM creation')
        return diffs + [d for i, data in enumerate(datas) for d in judge_one(i, data)]

    def judge_one(i, data):
        diffs, u64 = [], lambda o: int.from_bytes(data[o:o + 8], 'little')
        tsc, aux, cr4 = {}, {}, {}
        for kind, info, o in layouts[i]:
            if kind == 'reset':
                want = reset[info] if not (info == 0x1b and i == 0) else cp._i(P.msr(0x1b)['reset_bsp'])
                got = u64(o)
            elif kind in ('cpuid', 'cpuid-limited'):
                want = P.cpuid(*info, n=n, vcpu=i, misc_enable=0x401801 if kind == 'cpuid-limited' else 0x1801)
                got = tuple(int.from_bytes(data[o + 4 * k:o + 4 * k + 4], 'little') for k in range(4))
            elif kind == 'misc':
                want, got = P.misc_enable_after_write(info), u64(o)
            elif kind == 'value':
                want, got = info, u64(o)
            elif kind in ('order', 'flag'):
                continue
            else:
                {'tsc': tsc, 'aux': aux, 'cr4': cr4}[kind][info] = u64(o) if kind != 'aux' else int.from_bytes(data[o:o + 4], 'little')
                continue
            if got != want: diffs.append(f'cpu{i} {kind} {info if not isinstance(info, int) else hex(info)}: {got if isinstance(got, tuple) else hex(got)} != {want if isinstance(want, tuple) else hex(want)}')
        if not tsc[0] <= tsc[1] <= tsc[2] <= tsc[3]: diffs.append(f'cpu{i} TSC not monotonic: {[hex(tsc[k]) for k in range(4)]}')
        if not TSC_WRITE <= tsc[4] <= tsc[5] < TSC_WRITE + 100 * cp.load().d['identity']['tsc_hz']:
            diffs.append(f'cpu{i} TSC after writing {TSC_WRITE:#x}: {tsc[4]:#x}, {tsc[5]:#x}')
        if (aux[0], aux[1]) != (0, 0x55 + i): diffs.append(f'cpu{i} RDTSCP ECX {aux[0]:#x}, {aux[1]:#x} (want 0, {0x55 + i:#x})')
        if cr4[1] != cr4[0] | 0x14: diffs.append(f'cpu{i} CR4 with PSE and TSD {cr4[1]:#x} (from {cr4[0]:#x})')
        return diffs
    return prog, judge


DLV_EVENTS = [3, 0x21, 3, 6, 14, 14, 13, 0]      # programs.DLV_BODY's events, in order


def delivery_log_problems(prog, state):
    """X1a: each CPU's log of `dlv-handlers` (programs.DLV_BODY) in `state` (smp.parse_dump's shape) against
    delivery_model: the vectors in order, and each frame (RIP, CS, RFLAGS under the logged mask, RSP, SS), error code and
    frame address as the model delivers that event at the logged RIP (less the INT's length), RSP and RFLAGS."""
    import delivery_check
    memory = {r['addr']: bytes(r['data']) for r in state['memory']}
    problems = []
    for i, at in enumerate(prog['params']['delivery']):
        log = next(data[at - a:] for a, data in memory.items() if a <= at < a + len(data))
        entries = [[int.from_bytes(log[k + 8 * j:k + 8 * j + 8], 'little') for j in range(9)] for k in range(0, 0x50 * len(DLV_EVENTS), 0x50)]
        if [e[0] for e in entries] != DLV_EVENTS:
            problems.append(f'cpu{i} vectors {[hex(e[0]) for e in entries]} != {[hex(v) for v in DLV_EVENTS]}'); continue
        for k, (vector, error, rip, cs, rflags, rsp, ss, base, cr2) in enumerate(entries):
            length = {3: 1, 0x21: 2}.get(vector)
            frame, slot = delivery_check.model_frame(vector, error, cr2, rip - (length or 0), rsp, rflags, length)
            frame[2] &= 0x10cd5
            if [rip, cs, rflags, rsp, ss, error if vector in (13, 14) else 0] != frame or base != slot:
                problems.append(f'cpu{i} event {k} (vector {vector:#x}): logged {[hex(x) for x in (rip, cs, rflags, rsp, ss, error, base)]}, '
                                f'model {[hex(x) for x in frame + [slot]]}')
    return problems


# ---- X1a: faults during delivery, #DF and shutdown, gate checks (delivery_model only: QEMU resets on a triple fault) --------
DLV_CODE, DLV_IDT, DLV_PTR, DLV_BAD_STACK = 0x40_0000, 0x60_0000, 0x60_1000, 0x7f_0000
DLV_GDT, DLV_TSS = 0x60_2000, 0x60_3000          # a case's own GDT and TSS (`own`), loaded at CPL 0 before its IDT


def delivery_cases():
    """[name, cpl, code text, {vector: (type, dpl, present, ist[, CS selector])}, rsp, own]: a CPL 0 or 3 program in the
    driver's space that loads its own IDT (at CPL 0), whose gates lead to the driver's logging handlers (the last delivery
    ends the run and is reported), then raises one event; with `own` = (TSS limit, IST1, extra GDT entries {selector:
    descriptor}) it first loads its own GDT (0x08 code64 DPL 0, 0x10 data and 0x28 code32 as the driver's, the TSS at 0x30,
    the extras from 0x40)
    and TR. Gates not listed are zero (#GP for their type)."""
    import models.delivery_model as dm
    ok, ist1 = (0xE, 0, True, 0), (0xE, 0, True, 1)
    return [
        # #UD (benign) whose frame push faults: the #PF is delivered serially; its push faults: #DF on IST 1.
        ('ud-pf-df', 0, 'ud2', {6: ok, 14: ok, 8: ist1}, DLV_BAD_STACK, None),
        # The same with #DF on the bad stack too: a fault while delivering #DF, shutdown.
        ('ud-pf-df-shutdown', 0, 'ud2', {6: ok, 14: ok, 8: ok}, DLV_BAD_STACK, None),
        # #GP (contributory) whose push faults (#PF, page after contributory: serial), then #PF's push: #DF.
        ('gp-pf-df', 0, 'mov rax, 0x8000000000000000\nmov rbx, [rax]', {13: ok, 14: ok, 8: ist1}, DLV_BAD_STACK, None),
        # #GP's gate not present: #NP(13 * 8 + 2 + EXT), contributory after contributory: #DF.
        ('gp-np-df', 0, 'mov rax, 0x8000000000000000\nmov rbx, [rax]', {13: (0xE, 0, False, 0), 8: ist1}, 0x50_0800, None),
        # Gate checks of a software interrupt (EXT = 0): a task gate, a gate not present, a DPL 0 gate from CPL 3, a trap
        # gate (IF kept), a 16-bit interrupt gate (#GP(0), A11), INT3 through a DPL 3 gate from CPL 3.
        ('int-task-gate', 0, 'int 0x40', {0x40: (0x5, 0, True, 0), 13: ist1}, 0x50_0800, None),
        ('int-not-present', 0, 'int 0x41', {0x41: (0xE, 0, False, 0), 11: ist1}, 0x50_0800, None),
        ('int-dpl-cpl3', 3, 'int 0x42', {0x42: (0xE, 0, True, 0), 13: ist1}, 0x50_0800, None),
        ('int-trap-gate', 0, 'int 0x43', {0x43: (0xF, 0, True, 0)}, 0x50_0808, None),
        ('int-16bit-gate', 0, 'int 0x40', {0x40: (0x6, 0, True, 0), 13: ist1}, 0x50_0800, None),
        ('int3-dpl3-cpl3', 3, 'int3', {3: (0xE, 3, True, 1)}, 0x50_0800, None),
        ('into-ud', 0, '.byte 0xce', {6: ist1}, 0x50_0800, None),
        # rdmsr_safe's probe (`K kernel/cpu/intel.c:528`): MSR_PLATFORM_INFO is not in the profile, #GP(0) (QEMU reads 0).
        ('rdmsr-safe-platform-info', 0, 'mov ecx, 0xce\nrdmsr', {13: ist1}, 0x50_0800, None),
        # CPL 3 -> 0 through a gate without IST: the stack is the TSS's RSP0.
        ('ud-cpl3-rsp0', 3, 'ud2', {6: ok}, 0x50_0800, None),
        # The target CS: a 32-bit code segment (#GP(vector * 8 + 2 + EXT), A11), not present (#NP(sel + EXT)).
        ('ud-cs-32bit', 0, 'ud2', {6: (0xE, 0, True, 0, 0x28), 13: ist1}, 0x50_0800, None),
        ('ud-cs-not-present', 0, 'ud2', {6: (0xE, 0, True, 0, 0x40), 11: (0xE, 0, True, 0)}, 0x50_0800,
         (0x67, DLV_BAD_STACK, {0x40: dm.segment(0, present=False)})),
        # The stack from the TSS: IST1 beyond the TSS limit (#TS(TR + EXT)), a non-canonical IST1 (#SS(EXT)).
        ('ud-ist-ts', 0, 'ud2', {6: ist1, 10: ok}, 0x50_0800, (0x23, 0x50_0800, {})),
        ('ud-ist-noncanonical', 0, 'ud2', {6: ist1, 12: ok}, 0x50_0800, (0x67, 0x8000_0000_0000_0000, {})),
        # A frame straddling two pages, the lower (second written) not present: #PF there (CR2 judged), on IST 1.
        ('ud-frame-straddle', 0, 'ud2', {6: ok, 14: ist1}, 0x50_0020, None),
    ]


def delivery_own(own):
    """A case's own GDT image and TSS image (`delivery_cases`)."""
    import models.delivery_model as dm
    limit, ist1, extra = own
    entries = {0x08: dm.segment(0), 0x10: dm.segment(0, code=False), 0x28: dm.segment(0, l=0, d=1), **extra}
    gdt = bytearray(0x48)                         # the TSS's 16 bytes at 0x30, extras from 0x40
    for sel, d in entries.items():
        gdt[sel:sel + 8] = d.to_bytes(8, 'little')
    tss = DLV_TSS
    gdt[0x30:0x40] = (limit | (tss & 0xff_ffff) << 16 | 0x89 << 40 | (tss >> 24 & 0xff) << 56).to_bytes(8, 'little') + (tss >> 32).to_bytes(8, 'little')
    return bytes(gdt), dm.tss(rsp=(0x50_0800, 0, 0), ist=(ist1,) + (0,) * 6)


def delivery_description(cpl, text, gates, rsp, own):
    import delivery_check as dc
    idt = bytearray(16 * 0x44)
    for v, g in gates.items():
        gtype, dpl, present, ist = g[:4]
        idt[16 * v:16 * v + 16] = dm_gate(dc.LOG_BASE + 0x2000 + 16 * v, gtype, dpl, present, ist, g[4] if len(g) > 4 else 0x08)
    ptr = (len(idt) - 1).to_bytes(2, 'little') + DLV_IDT.to_bytes(8, 'little')
    setup = f'lgdt [{DLV_PTR + 0x10:#x}]\nmov ax, 0x30\nltr ax\n' if own else ''
    head = ref.assemble(setup + f'lidt [{DLV_PTR:#x}]') + (b'\x90' if cpl == 3 else b'')   # CPL 3: a switch (a nop) after it
    code = head + ref.assemble(text)
    gpr = ['0'] * 16
    gpr[4] = f'{rsp:#x}'
    lines = [f'rip {DLV_CODE:#x}', 'cpl 0', 'gpr ' + ' '.join(gpr), f'code {code.hex()}', f'memory {DLV_IDT:#x} {bytes(idt).hex()}',
             f'memory {DLV_PTR:#x} {(ptr + bytes(6) + (0x47).to_bytes(2, "little") + DLV_GDT.to_bytes(8, "little")).hex()}',
             'region 0x500000 0x1000']
    if own:
        gdt, tss = delivery_own(own)
        lines += [f'memory {DLV_GDT:#x} {gdt.hex()}', f'memory {DLV_TSS:#x} {tss.hex()}']
    if cpl == 3:
        lines.append(f'switch {DLV_CODE + len(head) - 1:#x} 3')
    return '\n'.join(lines) + '\n', DLV_CODE + len(code) - len(ref.assemble(text.split('\n')[-1])), idt


def dm_gate(offset, gtype, dpl, present, ist, selector=0x08):
    from models import delivery_model as dm
    return dm.gate(offset, selector, dpl=dpl, gtype=gtype, ist=ist, present=present)


def cmd_delivery(a, host, runtime):
    """X1a: nested faults, #DF and shutdown, gate, CS and TSS-stack checks, natemu against delivery_model (events, the
    delivered frame on the handler's stack, its RSP and CPL, CR2 (the model's, or any it permits), or the shutdown record),
    at block sizes default and 1."""
    import delivery_check as dc
    from models import delivery_model as dm
    failures, report = [], []
    for name, cpl, text, gates, rsp, own in delivery_cases():
        desc, at, idt = delivery_description(cpl, text, gates, rsp, own)
        s = dc._state(dc.LOG_BASE, 0, cpl, at, rsp, 0x202)
        s['idtr'] = (DLV_IDT, len(idt) - 1)
        s['memory'][DLV_IDT >> 12] = dm.page(); dm.place(s['memory'], DLV_IDT, bytes(idt))
        s['memory'][0x500] = dm.page(user=True)
        if own:
            gdt, tss = delivery_own(own)
            for addr, data in ((DLV_GDT, gdt), (DLV_TSS, tss)):
                s['memory'][addr >> 12] = dm.page(); dm.place(s['memory'], addr, data)
            s['gdtr'], s['tr'] = (DLV_GDT, 0x47), (0x30, DLV_TSS, own[0])
        insn = ref.assemble(text.split('\n')[-1])
        event = {'int3': {'kind': 'int3', 'length': 1}, '.byte 0xce': {'kind': 'into', 'length': 1}, 'ud2': {'kind': 'exception', 'vector': 6}}.get(
            text, {'kind': 'int', 'vector': insn[1], 'length': 2} if insn[0] == 0xcd else {'kind': 'exception', 'vector': 13, 'error': 0})
        want = dm.deliver(s, event)
        cr2s = want['undefined']['state'].get('cr2', {want['state']['cr2']})
        for limit in ([], [1]):
            rc, out, err = driver(host, runtime, a.fexc, desc, a.out / 'work' / f"{name}{'-1' if limit else ''}", limit)
            diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else []
            if not rc and want['outcome'] == 'shutdown':
                v, e = want['events'][-1]
                f = [int(x, 16) for x in out['fault']]
                if out['stop'] != 'shutdown' or [f[0], f[2], f[3]] != [v, e, at] or f[1] not in cr2s:
                    diffs.append(f"want shutdown {want['events']} CR2 in {sorted(map(hex, cr2s))}, got {out['stop']} {out['fault']}")
            elif not rc:
                n, (v, e) = want['state'], want['events'][-1]
                frame = [dm.qword(n['memory'], n['rsp'] + 8 * k) for k in range(6 if e is not None else 5)]
                frame = frame[1:] + frame[:1] if e is not None else frame + [0]
                d = out.get('delivered') or {}
                got = [int(x, 16) for x in d.get('frame', [])]
                if out['stop'] != 'guest_fault' or d.get('vector') != v or got != frame or int(d['handler_rsp'], 16) != n['rsp'] \
                        or d['cpl'] != n['cpl'] or int(d['cr2'], 16) not in cr2s:
                    diffs.append(f"want {want['events']} frame {[hex(x) for x in frame]} at {n['rsp']:#x} CR2 in {sorted(map(hex, cr2s))}, got {out['stop']} {d}")
            failures += [f"{name}{' (max-block 1)' if limit else ''}: {x}" for x in diffs]
        report.append({'name': name, 'model': want['outcome'], 'events': want['events']})
        print(f"{name}: {'FAIL' if any(f.startswith(name + ':') or f.startswith(name + ' (') for f in failures) else 'ok'} {want['outcome']} {want['events']}", flush=True)
    for f in failures: print(f)
    (a.out / 'delivery.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def cpl3_cases():
    """(name, description, check(out) -> problems): CPUID and RDTSC/RDTSCP run at CPL3; with CR4.TSD both are #GP(0) at
    CPL3 (SDM Vol.2 RDTSC, RDTSCP); CR0.AM with RFLAGS.AC at CPL3 gives #AC(0) on a misaligned load (X2). One vCPU, the driver's user space; the CPL
    drops through a `switch` nop."""
    from models import cpu_profile as cp
    want = cp.load().cpuid(1)
    cpuid = 'mov eax, 1\nmov ecx, 7\ncpuid\nrdtsc\nmov r8, rax\nrdtscp\nmov r9, rcx\ncmp r11, r11'

    def ran(out):
        g = [smp.num(x) for x in out['gpr']]
        return [] if out['stop'] == 'reached' and g[3] == want[1] and g[9] == 0 else [f"{out['stop']} {out['reason']} rbx {g[3]:#x} r9 {g[9]:#x}"]
    cases = [('cpl3-cpuid-rdtsc', f"cpl 3\nrip 0x110000\ncode {ref.assemble(cpuid).hex()}\n", ran)]
    for name, insn in (('rdtsc', 'rdtsc'), ('rdtscp', 'rdtscp')):
        head = ref.assemble('mov rax, cr4\nbts rax, 2\nmov cr4, rax')
        code = head + b'\x90' + ref.assemble(insn)
        at = 0x110000 + len(head) + 1

        def gp(out, at=at):
            f = out.get('fault')
            return [] if out['stop'] == 'guest_fault' and [smp.num(x) for x in f] == [13, 0, 0, at] else [f"{out['stop']} {out['reason']} {f}"]
        cases.append((f'tsd-cpl3-{name}', f"cpl 0\nrip 0x110000\nswitch {0x110000 + len(head):#x} 3\ncode {code.hex()}\n", gp))
    # Alignment checking (CR0.AM, RFLAGS.AC, CPL 3): a misaligned access is #AC(0) (X2; before it, entering the state was
    # refused); AM and AC at CPL 0, or AM without AC at CPL 3, run.
    head = ref.assemble('mov rax, cr0\nbts rax, 18\nmov cr0, rax\nmov rbx, 1')
    tail = ref.assemble('mov rcx, [rip+1]\ncmp r11, r11')                # a misaligned load (no #AC without AC)
    code, at = head + b'\x90' + tail, 0x110000 + len(head)

    # X2 builds #AC: the misaligned load at CPL 3 is #AC(0) (the state before it: RBX = 1).
    def refused(out):
        f = out.get('fault')
        return [] if out['stop'] == 'guest_fault' and [smp.num(x) for x in f] == [17, 0, 0, at + 1] and smp.num(out['gpr'][3]) == 1 \
            else [f"{out['stop']} {out['reason']} at {out['rip']} {f}"]

    def runs(out):
        return [] if out['stop'] == 'reached' else [f"{out['stop']} {out['reason']} at {out['rip']}"]
    for name, rflags, check in (('am-ac-cpl3', 0x40000, refused), ('am-cpl3', 0, runs)):
        cases.append((name, f"cpl 0\nrflags {rflags:#x}\nrip 0x110000\nswitch {at:#x} 3\ncode {code.hex()}\n", check))
    return cases


# ---- X2: events (interrupt eligibility, shadows, POPF, IRETQ, HLT, NMI) ---------------------------------------------------
QEMU_EVENTS_RF = ('QEMU pushes RF = 1 in the frame of every external interrupt and NMI; the SDM (and delivery_model) push '
                  'the current RF (1 only between REP iterations): QEMU frames are judged without RF')


def event_logs(prog, cpus_memory):
    """Per CPU, its log entries (from each CPU's captured memory {addr: bytes})."""
    import events
    return [events.log(m[events.LOG(i)]) for i, m in enumerate(cpus_memory)]


def x2_cases():
    """natemu-only X2 cases (name, description, check(out) -> problems), judged by the SDM rules the delivery and system
    models state: one vCPU on the driver's logging IDT (a delivered exception ends the run with its record)."""
    def fault(vector, at):
        def check(out):
            f = out.get('fault')
            return [] if out['stop'] == 'guest_fault' and [smp.num(x) for x in f] == [vector, 0, 0, at] else [f"{out['stop']} {out['reason']} {f}"]
        return check

    def reached(**want):
        def check(out):
            g = {r: smp.num(out['gpr'][smp.REGS.index(r)]) for r in want}
            ok = out['stop'] == 'reached' and g == want
            return [] if ok else [f"{out['stop']} {out['reason']} at {out['rip']} {({k: hex(v) for k, v in g.items()})}"]
        return check

    def halted(at):
        def check(out):
            ok = out['stop'] == 'resident' and out['reason'].startswith('halted') and smp.num(out['rip']) == at
            return [] if ok else [f"{out['stop']} {out['reason']} at {out['rip']} (want halted at {at:#x})"]
        return check

    def refused(reason):
        def check(out):
            return [] if out['stop'] == 'resident' and out['reason'].startswith(reason) else [f"{out['stop']} {out['reason']} at {out['rip']}"]
        return check
    cases, base = [], 0x110000
    stack = 'region 0x200000 0x1000\ngpr ' + ' '.join(['0'] * 4 + ['0x201000'] + ['0'] * 11) + '\n'
    # CLI/STI at CPL 3 with IOPL < 3: #GP(0) (no VME/PVI); with IOPL = 3 they run; HLT at CPL 3: #GP(0).
    for insn in ('cli', 'sti', 'hlt'):
        for iopl in (0, 3):
            code = ref.assemble(f'nop\n{insn}\nmov rbx, 1\ncmp r11, r11')
            check = reached(rbx=1) if iopl == 3 and insn != 'hlt' else fault(13, base + 1)
            cases.append((f'x2-{insn}-cpl3-iopl{iopl}', f'cpl 3\nrflags {iopl << 12:#x}\nrip {base:#x}\ncode {code.hex()}\n', check))
    # POPF at CPL 3, IOPL 0: IF and IOPL unchanged, the other flags written (AC, ID, DF, arithmetic); at IOPL 3, IF written.
    popf = 'push {v:#x}\npopfq\npushfq\npop rbx\ncmp r11, r11'
    for iopl, v, want in ((0, 0x243cd5 & ~0x200, 0x240cd7 | 0x200), (0, 0x3000, 0x202), (3, 0x3000, 0x3002)):
        code = ref.assemble(popf.format(v=v))
        cases.append((f'x2-popf-cpl3-iopl{iopl}-{v:#x}', f'cpl 3\nrflags {iopl << 12:#x}\nrip {base:#x}\n{stack}code {code.hex()}\n', reached(rbx=want)))
    # POPF at CPL 0 writes IOPL and IF; RF, VM, VIF, VIP stay; a 16-bit POPF writes only bits 15:0.
    for v, size, want in ((0x1f3eff, 8, 0x43ed7), (0x1f3eff, 2, 0x3ed7)):
        # push imm32 (sign-extended) + popfq, or push imm16 (66 68 iw) + popfw (66 9d)
        pop = b'\x68' + v.to_bytes(4, 'little') + b'\x9d' if size == 8 else b'\x66\x68' + (v & 0xffff).to_bytes(2, 'little') + b'\x66\x9d'
        code = pop + ref.assemble('pushfq\npop rbx\ncli\ncmp r11, r11')
        cases.append((f'x2-popf{size * 8}-cpl0', f'cpl 0\nrip {base:#x}\n{stack}code {code.hex()}\n', reached(rbx=want)))
    # POPF setting TF: single-step is S4's (refused before any effect). LOCK CLI: #UD.
    popf_tf = ref.assemble('push 0x102\npopfq')
    cases.append(('x2-popf-tf', f'cpl 0\nrip {base:#x}\n{stack}code {popf_tf.hex()}\n', refused('unsupported_system')))
    lock_cli = bytes([0x90, 0xf0, 0xfa]) + ref.assemble('cmp r11, r11')
    cases.append(('x2-lock-cli', f'cpl 0\nrip {base:#x}\ncode {lock_cli.hex()}\n', fault(6, base + 1)))
    # HLT with no event that can wake it: IF = 0 and an interrupt pending (masked, model A10), or nothing pending. The
    # driver ends the run (`halted`) with RIP after the HLT.
    for name, text in (('masked', 'cli\nnop\nhlt\nnop'), ('idle', 'cli\nhlt\nnop')):
        code = ref.assemble(text + '\ncmp r11, r11')
        at = base + len(ref.assemble(text.rsplit('\n', 1)[0]))
        post = f'post {base + 1:#x} 0x40\n' if name == 'masked' else ''
        cases.append((f'x2-hlt-{name}', f'cpl 0\nrip {base:#x}\n{post}code {code.hex()}\n', halted(at)))
    cases += ac_cases(fault, reached, refused)
    return cases


def ac_cases(fault, reached, refused):
    """#AC (X2): CR0.AM set at CPL 0, RFLAGS.AC from the start, then a `switch` to CPL 3 and one access. Judged by the SDM
    (Vol.3 7.15 #AC, Table 7-7: a fault, error code 0, nothing written; only CPL 3 data accesses; the canonical check
    first). QEMU does not implement alignment checking, so it is no reference here. Where the SDM gives no order between #AC
    and #PF (a misaligned access to a page not present), natemu gives #AC (the access is checked before any walk)."""
    base, cases = 0x110000, []
    head = ref.assemble('mov rax, cr0\nbts rax, 18\nmov cr0, rax\nxor eax, eax')
    at = base + len(head) + 1
    regs = lambda rsp: 'gpr ' + ' '.join(['0', '0', '0', '0x200000', f'{rsp:#x}'] + ['0'] * 11)
    region = 'region 0x200000 0x2000\n'

    def case(name, body, check, rflags=0x40000, rsp=0x201800, am=True, extra=''):
        h = head if am else ref.assemble('xor eax, eax')
        code = h + b'\x90' + body + ref.assemble('cmp r11, r11')
        cases.append((f'x2-ac-{name}', f'cpl 0\nrflags {rflags:#x}\nrip {base:#x}\nswitch {base + len(h):#x} 3\n{region}{regs(rsp)}\n{extra}code {code.hex()}\n',
                      check))
    asm = ref.assemble
    for name, text in (('load8', 'mov rcx, [rbx+1]'), ('load4', 'mov ecx, [rbx+2]'), ('load2', 'mov cx, [rbx+3]'),
                       ('store8', 'mov [rbx+4], rbx'), ('store2', 'mov [rbx+1], bx'), ('rmw4', 'add [rbx+1], ecx'),
                       ('lock-xadd8', 'lock xadd [rbx+0xc], rcx'), ('xchg4', 'xchg [rbx+0xe], ecx'),
                       ('movsq', 'lea rsi, [rbx+0x100]\nlea rdi, [rbx+0x203]\nmov ecx, 4\nrep movsq'),
                       ('cross-page-store8', 'mov [rbx+0xffd], rbx'), ('cross-page-load4', 'mov ecx, [rbx+0x1ffe]'),
                       ('movsd8', 'movq xmm0, [rbx+4]'), ('movups4', 'movups xmm0, [rbx+4]')):
        insn = asm(text)
        last = len(asm(text.rsplit('\n', 1)[0])) if '\n' in text else 0
        case(name, insn, fault(17, at + last), extra='sse 1\n' if 'xmm' in text else '')
    # The canonical check first (#GP(0)); a page not present (natemu: #AC first); a push at a misaligned RSP; POPF (the
    # resident's explicit CPL 3 access) at a misaligned RSP.
    case('noncanonical', asm('mov rcx, 0x8000000000000001\nmov rdx, [rcx]'), fault(13, at + 10))
    case('not-present', asm('mov rcx, [rbx+0x3001]'), fault(17, at))
    case('push', asm('push rbx'), fault(17, at), rsp=0x201801)
    case('popf', asm('popfq'), fault(17, at), rsp=0x201804)
    # No #AC: aligned, bytes, AC cleared by POPF at CPL 3 (AC is writable there), AM clear, CPL 0 (no switch effect needed:
    # the access before the switch), an 8-byte aligned 16-byte MOVUPS (the SDM permits no #AC there: refused, not built).
    case('aligned', asm('mov rcx, [rbx+8]\nmov [rbx+16], ecx\nmov dx, [rbx+2]\nmov al, [rbx+1]\nmov ebx, 1'), reached(rbx=1))
    case('popf-clears-ac', asm('push 0x202\npopfq\nmov rcx, [rbx+1]\nmov ebx, 1'), reached(rbx=1))
    case('am-clear', asm('mov rcx, [rbx+1]\nmov ebx, 1'), reached(rbx=1), am=False)
    case('movups8', asm('movups xmm0, [rbx+8]'), refused('unsupported_mode'), extra='sse 1\n')
    # BT/BTS/BTR/BTC on memory (fexc cuts them under alignment checking: FEX touches one byte): #AC(0) when the operand is
    # misaligned at its size (with a register bit offset, the unit it selects has EA's alignment), else they run (results
    # checked), with LOCK too; AM clear: no #AC.
    for name, text in (('bt4-odd', 'bt dword ptr [rbx+1], ecx'), ('bt2-odd', 'bt word ptr [rbx+1], cx'),
                       ('bts8-imm-4', 'bts qword ptr [rbx+4], 5'), ('btc4-reg-far', 'mov ecx, 0x1000\nbtc dword ptr [rbx+2], ecx'),
                       ('lock-btr4-odd', 'lock btr dword ptr [rbx+3], ecx')):
        last = len(asm(text.rsplit('\n', 1)[0])) if '\n' in text else 0
        case(name, asm(text), fault(17, at + last))
    bts = 'mov dword ptr [rbx+8], 0\nmov ecx, 37\nbts dword ptr [rbx+4], ecx\nlock btc word ptr [rbx+8], 6\nbt qword ptr [rbx+8], 7\nsetnc dl\nmov eax, [rbx+8]'
    case('bt-aligned', asm(bts + '\nmov ebx, 1'), reached(rax=0x60, rdx=1, rbx=1))
    case('bt-odd-am-clear', asm('bt dword ptr [rbx+1], ecx\nmov ebx, 1'), reached(rbx=1), am=False)
    # A null DS override (3e) on the BT form: decoded as without it (64-bit mode), misaligned #AC, aligned runs.
    case('bt-ds-odd', bytes.fromhex('3e0fa34b01'), fault(17, at))
    case('bt-ds-aligned', bytes.fromhex('3e0fa34b04') + asm('mov ebx, 1'), reached(rbx=1))
    # SYSRET (S3) returning to CPL 3 with AC from R11 and CR0.AM: it completes, and the next misaligned access is #AC(0).
    sysret, labels = asm('mov rax, cr0\nbts rax, 18\nmov cr0, rax\nmov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\n'
                         'mov ecx, 0xc0000081\nxor eax, eax\nmov edx, 0x00100000\nwrmsr\nlea rcx, [rip + user]\n'
                         'mov r11d, 0x40202\nsysretq\nuser:\nmov rdx, [rbx+1]\ncmp r11, r11', labels=True)
    cases.append(('x2-ac-after-sysret', f'cpl 0\nrip {base:#x}\n{region}{regs(0x201800)}\ncode {sysret.hex()}\n',
                  fault(17, base + labels['user'])))
    cpl0 = head + asm('mov rcx, [rbx+1]\nmov ebx, 1\ncmp r11, r11')
    cases.append(('x2-ac-cpl0', f'cpl 0\nrflags 0x40000\nrip {base:#x}\n{region}{regs(0x201800)}\ncode {cpl0.hex()}\n', reached(rbx=1)))
    return cases


def cmd_events(a, host, runtime):
    """X2: the event programs (tools/bench/smp/events.py) on QEMU and natemu at 1, 2 and 4 vCPUs and block sizes default
    and 1, every logged delivery judged by delivery_model; natemu-only cases (faults, refusals, HLT without a wake);
    stress: events posted at random times into the stress loop and the HLT loop at 1-4 vCPUs."""
    import events
    stub = smp.build_stub(a.out / 'qemu-build')
    failures, report = [], []
    names = [k for k in events.PROGRAMS if not a.programs or any(p in k for p in a.programs)]
    for name in names:
        for n in (1, 2, 4):
            prog = events.make(name, n)
            label = f'{name}@{n}'
            rc, err, dump, _ = smp.run_image(prog['qemu'], smp.build_image(prog['qemu'], stub), a.out / 'qemu' / label, 'multi', timeout=60)
            want = smp.parse_dump(prog['qemu'], dump) if rc == 1 else None
            entry = {'name': label, 'runs': []}
            if want is None:
                failures.append(f'{label}: QEMU run failed ({rc} {err})')
            else:
                qlogs = [[(e[0], e[1], e[2], e[3] & ~events.RF, *e[4:]) for e in events.log(m['data'])] for m in want['memory'][:n]]
                entry['qemu'] = events.judge(prog, qlogs)
                failures += [f'{label}: QEMU vs delivery_model: {d}' for d in entry['qemu']]
            for limit in ([], [1]):
                text = events.description(prog['natemu'], prog['hooks'])
                rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f"{label}{'-1' if limit else ''}", limit)
                if rc:
                    diffs = [f'driver exit {rc}: {err.strip()[-300:]}']
                else:
                    diffs = []
                    for v, c in enumerate(out['cpus']):
                        end = body_end(prog['natemu'], smp.num(prog['natemu']['cpus'][v]['rip']))
                        if c['stop'] != 'reached' or smp.num(c['rip']) != end:
                            diffs.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
                        diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(c)]
                    mems = [{smp.num(m['addr']): bytes.fromhex(m['data']) for m in c['memory']} for c in out['cpus']]
                    diffs += events.judge(prog, event_logs(prog, [mems[0]] * n))
                    if want is not None:
                        got = {'cpus': [{'gpr': [smp.num(x) for x in c['gpr']], 'rflags': smp.num(c['rflags']), 'fault': None} for c in out['cpus']], 'memory': []}
                        diffs += smp.compare(prog['qemu'], got, {'cpus': want['cpus'], 'memory': []})
                entry['runs'].append({'max_block': limit[0] if limit else None, 'diffs': diffs})
                failures += [f"{label}{' (max-block 1)' if limit else ''}: {d}" for d in diffs]
            bad = any(r['diffs'] for r in entry['runs']) or entry.get('qemu')
            print(f"{label}: {'FAIL' if bad else 'ok'}", *[d for r in entry['runs'] for d in r['diffs']][:6], sep='\n  ', flush=True)
            report.append(entry)
    for name, text, check in ([] if a.programs else x2_cases()):
        for limit in ([], [1]):
            rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f"{name}{'-1' if limit else ''}", limit, timeout=60)
            problems = check(out) if rc == 0 else [f'driver exit {rc}: {err.strip()[-300:]}']
            print(f"{name}{' (max-block 1)' if limit else ''}: {'ok' if not problems else 'FAIL'} {problems}", flush=True)
            report.append({'name': name, 'max_block': limit, 'problems': problems})
            failures += [f'{name}: {p}' for p in problems]
    if not a.programs or 'stress' in a.programs:
        failures += events_stress(a, host, runtime, stub, report)
    print(QEMU_EVENTS_RF)
    for f in failures: print(f)
    (a.out / 'events.json').write_text(json.dumps({'report': report, 'failures': failures, 'qemu_differs': QEMU_EVENTS_RF}, indent=1, default=str) + '\n')
    return len(failures)


def events_stress(a, host, runtime, stub, report):
    """Random-time events (the driver's kickers post 0x40, 0x41, 0x50 or an NMI before each doorbell) into the stress loop
    (REP strings, shadows) and the HLT loop, at 1, 2 and 4 vCPUs and block sizes default and 1: every logged delivery
    judged (events.stress_check: all of them, and no event a shadow held taken late); the stress loop's registers and data equal QEMU's run without events; the HLT loop ends
    (all its HLTs woken) or is ended halted by the driver after the kicks (RIP after a HLT)."""
    import events
    failures = []
    kicks = a.kicks or 2000
    for name, iterations in (('stress', 400), ('hlt', kicks // 2)):
        for n in (1, 2, 4):
            prog = events.stress(name, n, iterations)
            label = f'ev-{name}@{n}'
            want = None
            if name == 'stress':
                rc, err, dump, _ = smp.run_image(prog['qemu'], smp.build_image(prog['qemu'], stub), a.out / 'qemu' / label, 'multi', timeout=120)
                want = smp.parse_dump(prog['qemu'], dump) if rc == 1 else None
                if want is None: failures.append(f'{label}: QEMU run failed ({rc} {err})')
            for limit in ([], [1]):
                extra = limit + [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', kicks, '--kick-jitter-us', 30,
                                                                                     '--kick-events', '0x40,0x41,0x50,nmi']
                text = events.description(prog['natemu'], prog['hooks'])
                rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f"{label}{'-1' if limit else ''}", extra, timeout=600)
                diffs, delivered, coverage = [], [], collections.Counter()
                if rc:
                    diffs.append(f'driver exit {rc}: {err.strip()[-300:]}')
                else:
                    labels = prog['labels']
                    for v, c in enumerate(out['cpus']):
                        end = body_end(prog['natemu'], smp.num(prog['natemu']['cpus'][v]['rip']))
                        stopped_ok = c['stop'] == 'reached' and smp.num(c['rip']) == end
                        if name == 'hlt' and c['stop'] == 'resident' and c['reason'].startswith('halted'):
                            stopped_ok = smp.num(c['rip']) == labels[v]['after_hlt']
                        if not stopped_ok: diffs.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']}")
                        diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(c) + kick_checks(c)]
                        mem = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in out['cpus'][0]['memory']}
                        entries = events.log(mem[events.STRESS_LOG_AT(v)])
                        count = int.from_bytes(mem[events.EV(v) + 0x1000][0x20:0x28], 'little')
                        delivered.append(count)
                        for k in ('interrupts', 'nmis', 'halts', 'shadow_steps'): coverage[k] += c['stats'][k]
                        if c['stats']['interrupts'] + c['stats']['nmis'] != count:
                            diffs.append(f"cpu{v}: the resident delivered {c['stats']['interrupts']} + {c['stats']['nmis']}, the handlers logged {count}")
                        diffs += events.stress_check(prog, v, entries, count, coverage, c['stats']['shadow_steps'])
                        if count > kicks: diffs.append(f'cpu{v}: {count} deliveries for {kicks} events posted')
                    if want is not None:
                        got = vm_state(prog['natemu'], out)
                        keep = lambda st: {'cpus': st['cpus'], 'memory': [m for m in st['memory'] if any(m['addr'] == events.DATA(i) for i in range(n))]}
                        for c in got['cpus']: c['fault'] = None
                        diffs += smp.compare(prog['qemu'], keep(got), keep(want))
                print(f"{label}{' (max-block 1)' if limit else ''}: {'ok' if not diffs else 'FAIL'} delivered {delivered}, "
                      f"judged: {dict(coverage)}",
                      *diffs[:8], sep='\n  ', flush=True)
                report.append({'name': label, 'max_block': limit, 'delivered': delivered, 'coverage': coverage, 'diffs': diffs})
                failures += [f'{label}: {d}' for d in diffs]
    return failures


def segment_runs(a, host, runtime, qemu, report, failures, progs=None):
    """S2b (tools/bench/smp/segments.py): descriptor tables, segment loads and far transfers at block sizes default and 1.
    Positive programs (1, 2 and 4 vCPUs) and fault cases against QEMU system mode (registers, captured memory, fault);
    every fault case's outcome (fault (vector, error, RIP, CR2) or completion) must be one segment_model permits, for
    natemu and for QEMU (unless listed in QEMU_DIFFERS); registers and memory are compared where both took the same one.
    Refused cases (compatibility-mode CS, call gate, TF from IRETQ) stop `unsupported_mode`/`unsupported_system` at the
    instruction; QEMU is not run for them. The LTR race (natemu only) runs 10 times per block size: exactly one vCPU
    loads TR, every other takes #GP(0x70) at its LTR."""
    import segments
    for prog in progs if progs is not None else segments.programs():
        name, model = prog['name'], prog['params'].get('model')
        if a.programs and not any(n in name for n in a.programs):
            continue
        entry = {'name': name, 'runs': []}
        if 'race' in prog['params']:
            for limit in ([], [1]):
                for rep in range(10):
                    rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{name}{'-1' if limit else ''}", limit)
                    stops = out and [(c['stop'], vm_fault(c)) for c in out['cpus']]
                    won = out and sum(s == 'reached' for s, _ in stops)
                    lost = out and sum(f == (13, 0x70, at, 0) for (_, f), at in zip(stops, prog['params']['race']))
                    ok = rc == 0 and won == 1 and won + lost == prog['ncpu']
                    entry['runs'].append({'max_block': limit[0] if limit else None, 'ok': ok, 'stops': stops})
                    if not ok: failures.append(f"{name}{' (max-block 1)' if limit else ''} rep {rep}: {rc} {stops} {err.strip()[-200:]}")
            print(f"{name}: {'ok' if all(r['ok'] for r in entry['runs']) else 'FAIL'} ({len(entry['runs'])} runs, one LTR each)", flush=True)
            report.append(entry)
            continue
        if model and model['outcome'] == 'refused':
            want_stop = prog['params'].get('stop') or ('unsupported_mode' if model['reason'].startswith('compatibility') else 'unsupported_system')
            for limit in ([], [1]):
                rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{name}{'-1' if limit else ''}", limit)
                cpu = out and out['cpus'][0]
                ok = rc == 0 and cpu['stop'] == 'resident' and cpu['reason'].startswith(want_stop) and smp.num(cpu['rip']) == model['at']
                entry['runs'].append({'max_block': limit[0] if limit else None, 'ok': ok})
                if not ok: failures.append(f"{name}{' (max-block 1)' if limit else ''}: want {want_stop} at {model['at']:#x}, got {rc} {cpu and (cpu['stop'], cpu['reason'], cpu['rip'])} {err.strip()[-200:]}")
            print(f"{name}: {'ok' if all(r['ok'] for r in entry['runs']) else 'FAIL'} (refused: {model['reason']})", flush=True)
            report.append(entry)
            continue
        want, rc = qemu(prog, name, timeout=30 if name in QEMU_DIFFERS else 120)
        if want is None and not (model and name in QEMU_DIFFERS): failures.append(f'{name}: QEMU run failed ({rc})'); continue
        # A listed case QEMU does not finish (it takes another path) is judged by segment_model alone.
        want = want or {'cpus': [{'fault': f'QEMU: {rc}'}], 'memory': []}
        for c in want['cpus']: c['fault'] = qemu_fault(c['fault']) if not isinstance(c['fault'], str) else c['fault']
        # Bytes where QEMU leaves what the SDM changes (an accessed bit set by a CS load): QEMU must differ there, and the
        # SDM's value is compared instead.
        for addr, value in prog['params'].get('sdm_bytes', {}).items():
            region = next(r for r in want['memory'] if r['addr'] <= smp.num(addr) < r['addr'] + len(r['data']))
            at = smp.num(addr) - region['addr']
            if region['data'][at] == value: failures.append(f'{name}: QEMU writes the SDM value at {addr}: drop sdm_bytes')
            region['data'] = region['data'][:at] + bytes([value]) + region['data'][at + 1:]
        permitted = None
        if model:
            # segment_model's permitted outcomes as faults in smp's shape (None: the instruction completes).
            fault = lambda f: f and (f[0], f[1], model['at'], f[2] if f[0] == 14 else 0)
            permitted = {fault(f) for _, f in model['permitted']}
            entry['qemu'] = qf = want['cpus'][0]['fault']
            if qf not in permitted:
                if name in QEMU_DIFFERS: entry['qemu_differs'] = QEMU_DIFFERS[name]
                else: failures.append(f"{name}: QEMU {qf}, segment_model permits {sorted(permitted, key=str)}")
            elif name in QEMU_DIFFERS: failures.append(f'{name}: listed in QEMU_DIFFERS but QEMU is permitted')
        for limit in ([], [1]):
            rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{name}{'-1' if limit else ''}", limit)
            if rc:
                diffs = [f'driver exit {rc}: {err.strip()[-300:]}']
            else:
                diffs = []
                for v_, (c, cpu) in enumerate(zip(prog['cpus'], out['cpus'])):
                    end = body_end(prog, smp.num(c['rip']))
                    done = want['cpus'][v_]['fault'] is None if permitted is None else vm_fault(cpu) is None
                    if done and (cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end):
                        diffs.append(f"cpu{v_} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
                    diffs += [f'cpu{v_} ' + d for d in per_vcpu_checks(cpu)]
                got = vm_state(prog, out)
                g = got['cpus'][0]['fault']
                if permitted is not None and g not in permitted:
                    diffs.append(f"fault {g} not permitted by segment_model {sorted(permitted, key=str)}")
                if permitted is None or g == want['cpus'][0]['fault']:
                    diffs += smp.compare(prog, got, want)
                # S3: result bytes the model gives (syscalls.py's LAR/LSL/VERR/VERW tables): one of the permitted images.
                if prog['params'].get('model_memory'):
                    captured = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in out['cpus'][0]['memory']}
                    for addr, images in prog['params']['model_memory'].items():
                        at = smp.num(addr)
                        start = max(x for x in captured if x <= at)
                        image = captured[start][at - start:at - start + 16].hex()
                        if image not in images:
                            diffs.append(f'{addr}: {image} not permitted by syscall_model {images}')
            entry['runs'].append({'max_block': limit[0] if limit else None, 'diffs': diffs})
            failures.extend(f"{name}{' (max-block 1)' if limit else ''}: {d}" for d in diffs)
        bad = any(r['diffs'] for r in entry['runs'])
        print(f"{name}: {'FAIL' if bad else 'ok'}" + (f"; model {model['fault']}" + (f" (permits {len(permitted)})" if len(permitted) > 1 else '') if model else '')
              + (f"; QEMU differs: {entry['qemu']}" if 'qemu_differs' in entry else ''), flush=True)
        report.append(entry)


def cmd_system(a, host, runtime):
    """S3c1: system programs (tools/bench/smp/system.json) against QEMU system mode; fault cases against
    tools/system_fault_model.py at the state QEMU reaches just before the instruction (and modes_model where it covers
    them); the PTE change without INVLPG against coherence_model; refusals of unimplemented forms."""
    import system_fault_model as sfm
    from models import modes_model as modes
    programs = [p for p in json.loads((ROOT / 'tools/bench/smp/system.json').read_text()) if not a.programs or any(n in p['name'] for n in a.programs)]
    stub = smp.build_stub(a.out / 'qemu-build')
    report, failures, gaps = [], [], []

    def qemu(prog, label, timeout=120):
        rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / label, 'multi', timeout=timeout)
        return (smp.parse_dump(prog, dump) if rc == 1 else None), rc
    for prog in programs:
        name, fault_at = prog['name'], prog['params'].get('fault_at')
        entry = {'name': name, 'runs': []}
        if fault_at:
            # QEMU's state just before the instruction under test: the body cut there.
            pre = json.loads(json.dumps(prog)); at = smp.num(fault_at); b = pre['bodies'][0]
            b['code'] = b['code'][:2 * (at - smp.num(b['addr']))]
            want, rc = qemu(pre, name + '-prefix')
            if want is None: failures.append(f'{name}: QEMU prefix run failed ({rc})'); continue
            code = bytes.fromhex(prog['bodies'][0]['code'])[at - smp.num(prog['bodies'][0]['addr']):]
            c = prog['cpus'][0]
            # The model is given the start-up controls (CR0, CR4, EFER, DR7) and those the case declares (params.controls):
            # the prefix must not have written others (MOV to CR 0f 22, WRMSR 0f 30, MOV to DR 0f 23).
            declared = prog['params'].get('controls', {})
            prefix = bytes.fromhex(prog['bodies'][0]['code'])[:at - smp.num(prog['bodies'][0]['addr'])]
            assert (b'\x0f\x22' not in prefix or 'cr4' in declared) and b'\x0f\x30' not in prefix \
                and (b'\x0f\x23' not in prefix or 'dr7' in declared), f'{name}: the prefix changes controls'
            ctl = {'cr0': 0x80010011, 'cr4': 0x20, 'efer': 0xd00, 'dr7': 0x400, **declared}
            vector = sfm.fault(code, want['cpus'][0]['gpr'], c.get('cpl', 3), ctl['cr0'], ctl['cr4'], ctl['efer'], ctl['dr7'])
            if vector is None: failures.append(f'{name}: the model expects no fault'); continue
            want['cpus'][0]['fault'] = (*vector, at, 0)
            full, rc = qemu(prog, name, timeout=10)
            entry['qemu'] = 'hang' if rc == 'timeout' else (full and full['cpus'][0]['fault'] and list(full['cpus'][0]['fault'])) or 'no fault'
            # Where QEMU does raise an exception, it is the model's.
            if isinstance(entry['qemu'], list) and tuple(entry['qemu']) != want['cpus'][0]['fault']:
                if name in QEMU_DIFFERS: entry['qemu_differs'] = QEMU_DIFFERS[name]
                else: failures.append(f"{name}: QEMU raises {entry['qemu']}, the model {want['cpus'][0]['fault']}")
            elif name in QEMU_DIFFERS: failures.append(f'{name}: listed in QEMU_DIFFERS but QEMU agrees ({entry["qemu"]})')
        else:
            want, rc = qemu(prog, name)
            if want is None: failures.append(f'{name}: QEMU run failed ({rc})'); continue
            for c in want['cpus']: c['fault'] = qemu_fault(c['fault'])
            # X1a: QEMU's frames are a reference only where they agree with delivery_model (the plan's fault references).
            if prog['params'].get('delivery'):
                failures += [f'{name}: QEMU vs delivery_model: {d}' for d in delivery_log_problems(prog, want)]
        # (profile, EDX): profile 22's first WRMSR writes a legal EAX, so with EDX = 1 only EDX is reserved.
        model_profile, edx = {'sysfault-efer-lme-clear@1': (25, 0), 'sysfault-cr4-pae-clear@1': (27, 0),
                              'sysfault-cr0-pg-clear@1': (28, 0), 'sysfault-efer-reserved-1@1': (29, 0),
                              'sysfault-efer-reserved-edx@1': (22, 1)}.get(name, (None, 0))
        if model_profile:
            last = modes.model(model_profile, 0, modes.data(model_profile, [0] * 0x400), edx)['records'][-1]
            entry['modes_model'] = (model_profile, last['vector'])
            if last['vector'] != want['cpus'][0]['fault'][0]:
                failures.append(f"{name}: modes_model profile {model_profile} gives vector {last['vector']}")
        for limit in ([], [1]):
            rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{name}{'-1' if limit else ''}", limit)
            if rc:
                diffs = [f'driver exit {rc}: {err.strip()[-300:]}']
            else:
                diffs = []
                for v, (c, cpu) in enumerate(zip(prog['cpus'], out['cpus'])):
                    end = body_end(prog, smp.num(c['rip']))
                    if want['cpus'][v]['fault'] is None and (cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end):
                        diffs.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
                    diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu)]
                if prog['params'].get('either'):
                    diffs += either_check(prog, out)
                    entry['either'] = [c.get('either') for c in out['cpus']]
                else:
                    diffs += smp.compare(prog, vm_state(prog, out), want)
                if prog['params'].get('delivery'):
                    diffs += delivery_log_problems(prog, vm_state(prog, out))
            entry['runs'].append({'max_block': limit[0] if limit else None, 'diffs': diffs,
                                  'systems': out and [c['stats']['systems'] for c in out['cpus']]})
            (gaps if name in SYSTEM_GAPS else failures).extend([f"{name}{' (max-block 1)' if limit else ''}: {d}" for d in diffs])
        bad = any(r['diffs'] for r in entry['runs'])
        if name in SYSTEM_GAPS and not bad: failures.append(f'{name}: listed in SYSTEM_GAPS but passes')
        print(f"{name}: {('GAP' if name in SYSTEM_GAPS else 'FAIL') if bad else 'ok'}"
              + (f"; QEMU full run: {entry['qemu']}" if 'qemu' in entry else '') + (f"; modes model {entry['modes_model']}" if 'modes_model' in entry else '')
              + (f"; history {entry['either']}" if 'either' in entry else ''), flush=True)
        report.append(entry)
    segment_runs(a, host, runtime, qemu, report, failures)
    import syscalls
    segment_runs(a, host, runtime, qemu, report, failures, syscalls.programs())
    # Refusals: the vCPU stops `unsupported_system` at the instruction, after the prefix.
    for name, code in ([] if a.programs else unsupported_cases().items()):
        text = f'cpl 0\nrip 0x110000\ncode {code}\n'
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f'unsupported-{name}')
        ok = rc == 0 and out['stop'] == 'resident' and out['reason'].startswith('unsupported_system') and smp.num(out['gpr'][3]) == 1
        print(f'unsupported {name}: {"ok" if ok else ("GAP" if f"unsupported {name}" in SYSTEM_GAPS else "FAIL")} {out and (out["stop"], out["reason"], out["rip"])}', flush=True)
        report.append({'name': f'unsupported-{name}', 'ok': ok})
        if f'unsupported {name}' in SYSTEM_GAPS:
            (gaps if not ok else failures).append(f'unsupported {name}: ' + (f'{rc} {out and out["stop"]} {out and out["reason"]}' if not ok else 'listed in SYSTEM_GAPS but passes'))
        elif not ok: failures.append(f'unsupported {name}: {rc} {out and out["reason"]} {err.strip()[-200:]}')
    # S1: the presented CPU against cpu_profile.py.
    for n in ([] if a.programs else (1, 2, 4)):
        prog, judge = profile_program(n)
        rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f'profile{n}')
        if rc:
            diffs = [f'driver exit {rc}: {err.strip()[-300:]}']
        else:
            memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in out['cpus'][0]['memory']}
            diffs = [f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']}" for v, c in enumerate(out['cpus'])
                     if c['stop'] != 'reached' or smp.num(c['rip']) != body_end(prog, smp.num(prog['cpus'][v]['rip']))]
            diffs += judge([memory[smp.num(prog['memory'][v]['addr'])] for v in range(n)])
        print(f"profile@{n}: {'ok' if not diffs else 'FAIL'}", *diffs[:10], sep='\n  ', flush=True)
        report.append({'name': f'profile@{n}', 'diffs': diffs})
        failures += [f'profile@{n}: {d}' for d in diffs]
    for name, text, check in ([] if a.programs else cpl3_cases()):
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / name)
        problems = check(out) if rc == 0 else [f'driver exit {rc}: {err.strip()[-300:]}']
        print(f"{name}: {'ok' if not problems else 'FAIL'} {problems}", flush=True)
        report.append({'name': name, 'problems': problems})
        failures += [f'{name}: {p}' for p in problems]
    # A malformed system reply (driver `--tamper system`: length 16) is refused before anything of it runs.
    if not a.programs:
        prog = next(p for p in programs if p['name'] == 'sys-cr3-switch@1')
        rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / 'tamper-system', ['--tamper', 'system'])
        stops = out and [(c['stop'], c['reason'], c['tampered'], c['stats']['systems']) for c in out['cpus']]
        ok = rc == 0 and stops == [('resident', 'bad_reply status=1 detail=2', True, 0)]
        print(f'tampered system reply: {"ok" if ok else "FAIL"} {stops}', flush=True)
        report.append({'name': 'tamper-system', 'ok': ok, 'stops': stops})
        if not ok: failures.append(f'tampered system reply: {rc} {stops} {err.strip()[-200:]}')
    for g in gaps: print('GAP', g, '--', SYSTEM_GAPS[g.split(':')[0].replace(' (max-block 1)', '')])
    for f in failures: print(f)
    (a.out / 'system.json').write_text(json.dumps({'report': report, 'failures': failures, 'gaps': gaps}, indent=1, default=str) + '\n')
    return len(failures)


def cmd_coherence(a, host, runtime):
    failures, report = coherence_runs(a, host, runtime, a.programs)
    for f in failures: print(f)
    (a.out / 'coherence.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def coherence_runs(a, host, runtime, names):
    """The coherence.json programs whose names contain one of `names` (all if None), as cmd_coherence: (failures, report)."""
    programs = [p for p in json.loads((ROOT / 'tools/bench/smp/coherence.json').read_text()) if not names or any(n in p['name'] for n in names)]
    failures, report = [], []
    stub = smp.build_stub(a.out / 'qemu-build')
    for prog in programs:
        # Pinned QEMU (MTTCG) now and then never finishes a program whose code another CPU rewrites while it runs (seen with
        # `stale`): a run that times out is repeated; its final state, when it finishes, is deterministic by construction.
        for attempt in range(5):
            rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / prog['name'], 'multi', timeout=30)
            if rc != 'timeout': break
            print(f"{prog['name']}: QEMU run {attempt + 1} timed out", flush=True)
        if rc != 1: raise RuntimeError(f"QEMU {prog['name']}: {rc} {err}")
        want = smp.parse_dump(prog, dump)
        n = prog['ncpu']
        for limit in ([], [1]):
            groups = [(f"{prog['name']}{'-1' if limit else ''}-{r}", smp_description(prog), limit) for r in range(a.reps)]

            def judge(i, rc, out, err, prog=prog, want=want, n=n):
                if rc: return [{'rep': i, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
                stats = {k: sum(c['stats'][k] for c in out['cpus']) for k in ('steps', 'stale_links', 'invalidations', 'stops', 'shootdowns', 'requests')}
                stats['stops_by_place'] = places([c['kicks'] for c in out['cpus']])
                return [{'rep': i, 'diffs': judge_smp(prog, want, out, [None] * n), 'stats': stats}]
            entries = run_groups(a, host, runtime, groups, judge)
            totals = collections.Counter()
            for e in entries: totals.update(e.get('stats', {}))
            bad = [e for e in entries if e['diffs']]
            label = f"{prog['name']}{' (max-block 1)' if limit else ''}"
            print(f"{label}: {len(entries) - len(bad)}/{len(entries)} match; {dict(totals)}", flush=True)
            failures += [f"{label} rep{e['rep']}: {'; '.join(e['diffs'])[:400]}" for e in bad]
            report.append({'name': prog['name'], 'max_block': limit[0] if limit else None, 'runs': len(entries), 'match': len(entries) - len(bad),
                           'totals': dict(totals), 'entries': entries})
    return failures, report


def cmd_sharedpt(a, host, runtime):
    """S3f: tools/bench/smp/sharedpt.py programs (several CPUs on one set of page tables). Each program runs R times at block
    sizes default and 1. Every CPU must reach its end (no halt, no watchdog); the final memory (captured regions: the PTE
    bytes, the data pages, the flags) equals pinned QEMU system mode's (registers hold schedule-dependent counters and are
    not compared); the CPUs' logged observations and the PTE bytes are judged by sharedpt.judge against
    tools/models/coherence_model.py. Reports how often each history was seen."""
    import sharedpt
    failures, report = [], []
    stub = smp.build_stub(a.out / 'qemu-build')
    for prog in sharedpt.generate(a.programs, a.cpus):
        name = prog['name']
        for attempt in range(5):
            rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / name, 'multi', timeout=120)
            if rc != 'timeout': break
            print(f'{name}: QEMU run {attempt + 1} timed out', flush=True)
        if rc != 1: raise RuntimeError(f'QEMU {name}: {rc} {err}')
        want = smp.parse_dump(prog, dump)
        for limit in ([], [1]):
            groups = [(f"{name}{'-1' if limit else ''}-{r}", smp_description(prog), limit) for r in range(a.reps)]

            def judge(i, rc, out, err, prog=prog, want=want):
                if rc: return [{'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
                diffs = []
                for v, cpu in enumerate(out['cpus']):
                    end = body_end(prog, smp.num(prog['cpus'][v]['rip']))
                    if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != end:
                        diffs.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']} (end {end:#x})")
                    diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu)]
                got = vm_state(prog, out)
                got['cpus'] = want['cpus']
                diffs += ['qemu: ' + d for d in smp.compare(prog, got, want)]
                try:
                    found, hist = sharedpt.judge(prog, out)
                except (KeyError, ValueError) as e:
                    found, hist = [f'judge: {e!r}'], collections.Counter()
                stats = {k: sum(c['stats'][k] for c in out['cpus']) for k in ('steps', 'invalidations', 'retries', 'requests', 'systems')}
                return [{'diffs': diffs + ['model: ' + d for d in found], 'hist': hist, 'stats': stats}]
            entries = run_groups(a, host, runtime, groups, judge)
            hist, totals = collections.Counter(), collections.Counter()
            for e in entries: hist.update(e.get('hist', {})); totals.update(e.get('stats', {}))
            bad = [e for e in entries if e['diffs']]
            label = f"{name}{' (max-block 1)' if limit else ''}"
            print(f'{label}: {len(entries) - len(bad)}/{len(entries)} runs ok; {dict(totals)}', flush=True)
            for k, c in sorted(hist.items()): print(f'    {c:8d}  {k}', flush=True)
            failures += [f"{label} rep{i}: {'; '.join(e['diffs'])[:600]}" for i, e in enumerate(entries) if e['diffs']]
            report.append({'name': name, 'max_block': limit[0] if limit else None, 'runs': len(entries), 'ok': len(entries) - len(bad),
                           'stats': dict(totals), 'histories': dict(sorted(hist.items())), 'diffs': [e['diffs'] for e in bad][:3]})
    for f in failures: print(f)
    (a.out / 'sharedpt.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


# ---- privilege (U1): one shadow space per x86 privilege class --------------------------------------------------------
# CPU i: code at PRIV_CODE(i), two pages (the first supervisor, the second user), its own tables A/B (programs.sys_tables,
# identity, loaded by the program's first instruction at CPL0) and data pages at programs.SYS_D(i). The CPL changes only at
# the driver's `switch` items (no x86 instruction changes it yet): the first at the supervisor page's last byte, so the
# CPL3 part runs from the user page.
PRIV_CODE = lambda i: 0x110000 + 0x2000 * i
PRIV_PAGES = {'S': (0x0000, False, True), 'SR': (0x1000, False, False), 'UW': (0x2000, True, True), 'UR': (0x3000, True, False)}
PRIV_V, PRIV_F1, PRIV_F2 = 0x4000, 0x5000, 0x6000
PTE_FLAGS = 0x61                                       # P | A | D; | RW (2) | US (4) per page


def priv_word(i, page, k):
    """The initial qword k of CPU i's data page at offset `page`."""
    return 0x1111_0000_0000 * (page // 0x1000 + 1) + 0x100 * i + k


def priv_cpu(i, n, text, wp=1):
    """CPU i's code (`text`: labels sw3/sw0... are switch points, `swN_<cpl>` naming the CPL) and memory."""
    d, user = programs.SYS_D(i), programs.LEAF
    over = {}
    for j in range(n):
        over[PRIV_CODE(j)] = PRIV_CODE(j) | PTE_FLAGS | 2           # supervisor code page
        over[PRIV_CODE(j) + 0x1000] = PRIV_CODE(j) + 0x1000 | user  # user code page
    for name, (o, u, w) in PRIV_PAGES.items():
        over[d + o] = d + o | PTE_FLAGS | 2 * w | 4 * u
    over_b = dict(over)
    over[d + PRIV_V], over_b[d + PRIV_V] = d + PRIV_F1 | user, d + PRIV_F2 | user
    tables = programs.sys_tables(i, 0, n, over) + programs.sys_tables(i, 1, n, over_b)
    memory = [(smp.num(t['addr']), bytes.fromhex(t['data'])) for t in tables]
    memory += [(d + o, b''.join(priv_word(i, o, k).to_bytes(8, 'little') for k in range(4)))
               for o in [o for o, _, _ in PRIV_PAGES.values()] + [PRIV_F1, PRIV_F2]]
    fields = {f'{k}': d + o for k, (o, _, _) in PRIV_PAGES.items()} | dict(V=d + PRIV_V, ta=programs.SYS_T(i, 0), tb=programs.SYS_T(i, 1),
                                                                         la=programs.sys_leaf(i, 0, d + PRIV_V), F2=d + PRIV_F2)
    code, labels = ref.assemble(text.format(**fields), labels=True)
    switches = [(PRIV_CODE(i) + off, int(name.rsplit('_', 1)[1])) for name, off in labels.items() if name.startswith('sw')]
    # Control writes (MOV to CR): each resets the shadows once.
    controls = len(re.findall(r'\bmov cr\d', text))
    return {'addr': PRIV_CODE(i), 'code': code, 'controls': controls, 'labels': {k: PRIV_CODE(i) + v for k, v in labels.items()},
            'memory': memory, 'switches': switches, 'wp': wp, 'fields': fields}


def priv_description(cpus):
    lines = ['identity'] + [f"body {c['addr']:#x} {c['code'].hex()}" for c in cpus]
    lines += [f'memory {a:#x} {b.hex()}' for c in cpus for a, b in c['memory']]
    for c in cpus:
        lines += ['cpu', f"rip {c['addr']:#x}", 'cpl 0', f"wp {c['wp']}"] + [f'switch {r:#x} {p}' for r, p in c['switches']]
    return '\n'.join(lines) + '\n'


PRIV_HEAD = 'mov rax, {ta}\nmov cr3, rax\n'
PRIV_TO_USER = 'jmp sw_3\n.org 0xfff, 0xcc\nsw_3: nop\n'   # the switch is the supervisor page's last byte


def priv_access(reg, page, write, offset):
    return f'mov qword ptr [{{{page}}}+{offset}], 0x5a' if write else f'mov {reg}, qword ptr [{{{page}}}+{offset}]'


def priv_pf(cpl, user, rw, write, wp):
    """#PF error code of a data access to a present page (SDM vol. 3 §4.6: U/S, R/W, CR0.WP; SMAP is not advertised),
    or None: CPL3 needs U/S = 1 and, to write, R/W = 1; CPL0 writes need R/W = 1 only with WP = 1."""
    if cpl == 3 and (not user or (write and not rw)): return 1 | 2 * write | 4
    if cpl == 0 and write and not rw and wp: return 1 | 2
    return None


def priv_matrix():
    """(name, case) for each prime/probe pair: the page is accessed at one CPL (allowed there, so shadowed in that CPL's
    space), the CPL changes, and the same page is accessed at the other CPL; the probe's outcome must be a fresh check at
    the new CPL (never the shadow's)."""
    cases = []
    for page, (_, user, rw) in PRIV_PAGES.items():
        for p in (0, 3):
            for a in (0, 1):
                for b in (0, 1):
                    for wp in (0, 1):
                        if priv_pf(p, user, rw, a, wp) is not None: continue
                        prime, probe = priv_access('rbx', page, a, 8), 'probe: ' + priv_access('r8', page, b, 16 if b else 24)
                        text = PRIV_HEAD + (prime + '\n' + PRIV_TO_USER + probe if p == 0 else
                                            PRIV_TO_USER + prime + '\nsw_0: nop\n' + probe) + '\nmov r9, 1\n'
                        cases.append((f"{page}-{'rw'[a]}{p}-{'rw'[b]}{3 - p}-wp{wp}",
                                      dict(text=text, wp=wp, error=priv_pf(3 - p, user, rw, b, wp), page=page, prime=a, probe=b)))
    return cases


def priv_expect(c, case):
    """Expected (fault or None, {register: value}) of a matrix case on CPU c."""
    o = PRIV_PAGES[case['page']][0]
    if case['error'] is not None:
        return (14, case['error'], c['labels']['probe'], c['fields'][case['page']] + 16 if case['probe'] else c['fields'][case['page']] + 24), {}
    regs = {9: 1}
    if not case['prime']: regs[3] = priv_word(c['i'], o, 1)
    if not case['probe']: regs[8] = priv_word(c['i'], o, 3)
    return None, regs


def priv_programs(i):
    """Named programs beyond the matrix: {name: (text, wp, expect(c) -> (fault, regs))}."""
    f1, f2 = (lambda c: priv_word(c['i'], PRIV_F1, 0)), (lambda c: priv_word(c['i'], PRIV_F2, 0))
    remap = lambda change: (PRIV_HEAD + 'mov rbx, qword ptr [{V}]\n' + PRIV_TO_USER + 'mov r8, qword ptr [{V}]\nsw1_0: nop\n'
                            + change + 'mov r9, qword ptr [{V}]\nsw2_3: nop\nmov r10, qword ptr [{V}]\n')
    both = lambda c: (None, {3: f1(c), 8: f1(c), 9: f2(c), 10: f2(c)})
    smep = (PRIV_HEAD + 'mov rax, cr4\nbts rax, 20\nmov cr4, rax\nmov rbx, qword ptr [{UW}]\n' + PRIV_TO_USER
            + 'mov r8, qword ptr [{UW}]\nsw_0: nop\nafter: mov r9, 1\n')
    return {
        # INVLPG at CPL0 drops V's shadow from both spaces: each CPL reads the new frame.
        'invlpg': (remap('mov qword ptr [{la}], {F2}+0x67\nmov rax, {V}\ninvlpg [rax]\n'), 1, both),
        # A CR3 load (table B maps V to F2) drops both spaces.
        'cr3': (remap('mov rax, {tb}\nmov cr3, rax\n'), 1, both),
        # SMEP: a CPL0 read of a user page is allowed; back at CPL0 on the user code page, the fetch faults (#PF 0x11).
        'smep': (smep, 1, lambda c: ((14, 0x11, c['labels']['after'], c['labels']['after']), {3: priv_word(c['i'], 0x2000, 0)})),
    }


def priv_loop(iterations):
    """Accesses at CPL0 (supervisor page, user page) and CPL3 (user pages) in a loop of 2 switches per round."""
    text = (PRIV_HEAD + f'mov rcx, {iterations}\nxor r12, r12\nxor r13, r13\nloop0:\nmov rax, qword ptr [{{S}}]\nadd r12, rax\n'
            'mov qword ptr [{S}+8], rcx\nmov rax, qword ptr [{UW}]\nadd r12, rax\n' + PRIV_TO_USER
            + 'mov rax, qword ptr [{UW}]\nadd r13, rax\nmov qword ptr [{UW}+8], rcx\nmov rax, qword ptr [{UR}]\nadd r13, rax\n'
            'sw_0: nop\ndec rcx\njnz loop0\n')
    def expect(c):
        w = lambda o: priv_word(c['i'], o, 0)
        m = (1 << 64) - 1
        return None, {1: 0, 12: iterations * (w(0) + w(0x2000)) & m, 13: iterations * (w(0x2000) + w(0x3000)) & m}
    return text, expect


def cmd_privilege(a, host, runtime):
    """U1: x86 privilege in the shadows. (1) The fault matrix: every page kind (supervisor/user, writable/read-only) accessed
    at one CPL and then the other (WP 0 and 1), each case alone (1 vCPU) and four at once (4 vCPUs, one case each); the
    probe's #PF or success is the SDM rule at the new CPL (`priv_pf`), never the first access's shadow. (2) INVLPG and a CR3
    load drop both spaces (each CPL reads the new frame); SMEP still faults a CPL0 fetch from the user page after a CPL3
    phase. (3) A loop of 2 CPL changes per round: the same shadow resets (the CR3 load and the final halt) and retries at
    10 and 1,000 rounds: no flush per change. Block sizes default and 1; every run also passes the per-vCPU checks."""
    failures, report = [], []
    named = priv_programs(0)
    loops = {f'loop{n}': (*priv_loop(n), n) for n in (10, 1000)}

    def run(label, cpus, expects, limit, extra_checks=None):
        rc, out, err = driver(host, runtime, a.fexc, priv_description(cpus), a.out / 'work' / f"{label}{'-1' if limit else ''}", limit)
        diffs = [] if rc == 0 else [f'driver exit {rc}: {err.strip()[-300:]}']
        for v, (c, (fault, regs)) in enumerate(zip(cpus, expects)):
            if rc: break
            cpu = out['cpus'][v] if 'cpus' in out else out
            got = vm_fault(cpu)
            if fault is not None and got != fault: diffs.append(f'cpu{v} fault {got} != {fault} ({cpu["stop"]} {cpu["reason"]})')
            if fault is None and (cpu['stop'] != 'reached' or smp.num(cpu['rip']) != c['addr'] + len(c['code'])):
                diffs.append(f"cpu{v} stopped {cpu['stop']} {cpu['reason']} at {cpu['rip']}")
            for r, want in regs.items():
                if smp.num(cpu['gpr'][r]) != want: diffs.append(f"cpu{v} gpr{r} {cpu['gpr'][r]} != {want:#x}")
            # Every switch reached is one system transaction; resets: the controls written (CR3, CR4) and the final halt.
            controls = c['controls']
            if cpu['stats']['shadow_resets'] != controls + 1:
                diffs.append(f"cpu{v} shadow resets {cpu['stats']['shadow_resets']} != {controls + 1}")
            diffs += [f'cpu{v} ' + d for d in per_vcpu_checks(cpu)]
            if extra_checks: diffs += extra_checks(v, cpu)
        stats = out and [{k: (c['stats'] if 'cpus' in out else out['stats'])[k] for k in ('systems', 'retries', 'shadow_resets', 'flushes')}
                         for c in (out['cpus'] if 'cpus' in out else [out])]
        report.append({'name': label, 'max_block': limit[0] if limit else None, 'diffs': diffs, 'stats': stats})
        failures.extend(f"{label}{' (max-block 1)' if limit else ''}: {d}" for d in diffs)
        return out
    matrix = priv_matrix()
    for limit in ([], [1]):
        bad = len(failures)
        for name, case in matrix:
            c = dict(priv_cpu(0, 1, case['text'], case['wp']), i=0)
            run(f'matrix-{name}', [c], [priv_expect(c, case)], limit)
        for g in range(0, len(matrix), 4):
            group = matrix[g:g + 4]
            cpus = [dict(priv_cpu(i, len(group), case['text'], case['wp']), i=i) for i, (_, case) in enumerate(group)]
            run(f'matrix4-{g // 4}', cpus, [priv_expect(c, case) for c, (_, case) in zip(cpus, group)], limit)
        print(f"matrix ({len(matrix)} cases, 1 and 4 vCPUs){' max-block 1' if limit else ''}: {'ok' if len(failures) == bad else 'FAIL'}", flush=True)
        for name, (text, wp, expect) in named.items():
            for n in (1, 4):
                bad = len(failures)
                cpus = [dict(priv_cpu(i, n, text, wp), i=i) for i in range(n)]
                run(f'{name}{n}', cpus, [expect(c) for c in cpus], limit)
                print(f"{name} at {n} vCPU(s){' max-block 1' if limit else ''}: {'ok' if len(failures) == bad else 'FAIL'}", flush=True)
        seen = {}
        for name, (text, expect, rounds) in loops.items():
            for n in (1, 4):
                bad = len(failures)
                cpus = [dict(priv_cpu(i, n, text), i=i) for i in range(n)]
                # 2 switches per round plus the CR3 load.
                check = lambda v, cpu, rounds=rounds: [] if cpu['stats']['systems'] == 2 * rounds + 1 else [f"systems {cpu['stats']['systems']} != {2 * rounds + 1}"]
                out = run(f'{name}-{n}', cpus, [expect(c) for c in cpus], limit, check)
                seen[name, n] = out and [c['stats']['retries'] for c in (out['cpus'] if 'cpus' in out else [out])]
                print(f"{name} at {n} vCPU(s){' max-block 1' if limit else ''}: {'ok' if len(failures) == bad else 'FAIL'} retries {seen[name, n]}", flush=True)
        # Shadow work does not grow with the number of CPL changes.
        for n in (1, 4):
            if seen.get(('loop10', n)) != seen.get(('loop1000', n)):
                failures.append(f"retries at 10 and 1000 rounds differ ({n} vCPUs{', max-block 1' if limit else ''}): {seen.get(('loop10', n))} {seen.get(('loop1000', n))}")
    for f in failures: print(f)
    (a.out / 'privilege.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


# ---- contain (S3h): containment and progress --------------------------------------------------------------------------
# The driver's code-shape option (admission's debug check), on for every mode but `contain` (main clears it there).
CODE_SHAPE = vm.CODE_SHAPE
LOOP = 'rip 0x100000\nbody 0x100000 ebfe\n'          # jmp $: a linked loop with no exit, in its own space
TEARDOWN = 'teardown: vcpus {n} joined {n} destroyed {n} vm_destroy 0 children none'
KILL_BOUND = 2                                        # seconds a compile-server-kill run may take (normally ~0.05)
SLACK = 5                                             # seconds a contain run may outlive its bound before it is killed


def chain_section(count=600, trips=3):
    """`count` distinct ADDs in a counted loop: at block size 1 with a small code cache it fills the cache (flushes, with
    links) and makes many compile requests."""
    code = ref.assemble('\n'.join([f'mov ecx, {trips}', '1:'] + [f'add rax, {0x10000 + 977 * i:#x}' for i in range(count)] + ['dec ecx', 'jnz 1b']))
    return f'rip 0x100000\ncode {code.hex()}\n'


def tamper_kinds(fixture):
    """Every run-time tamper of the driver: (name, driver options for the tampered vCPU, its section, its expected halt).
    `--tamper-after` is not here: it corrupts after every vCPU stopped (the host self-check, run_slice2_probes.py)."""
    flush = ['1', '--code-cache', 32768]
    system = 'rip 0x100000\ncpl 0\ncode ' + ref.assemble('mov rax, cr0\nnop').hex() + '\n'
    load = 'rip 0x100000\ncode ' + ref.assemble('mov rax, qword ptr [0x200000]\nnop').hex() + '\nmemory 0x200000 0102030405060708\n'
    return [('forge-exit', ['--forge-exit'], fixture, 'bad_reply'),
            ('offset', ['--tamper', 'offset'], fixture, 'bad_reply'),
            ('next-page', ['--tamper', 'next-page'], fixture, 'bad_reply'),
            ('system', ['--tamper', 'system'], system, 'bad_reply'),
            ('site', ['--tamper', 'site'], load, 'unknown_abort'),
            ('site-kind', ['--tamper', 'site-kind'], load, 'site_mismatch'),
            *[(k, flush + ['--tamper', k], chain_section(), 'bad_link') for k in ('word', 'record')],
            ('dispatch', ['--tamper', 'dispatch'], fixture, 'bad_reply'),
            ('target', ['--tamper', 'target'], fixture, 'bad_reply'),
            # Not LOOP: its entry word is its exit's link point, which the reply check refuses (bad_reply) once forged.
            ('forge-stop', ['--kicks', 1, '--forge-stop'], fixture, 'bad_stop'),
            ('armed', ['--kicks', 1, '--tamper', 'armed'], LOOP, 'bad_stop'),
            # EL0 isolation by hardware (E2, E3): words forged over the first block's entry after admission. With the
            # code-shape check off (contain's setting) admission's object validation alone would still refuse most of
            # them (x28 operands, branches, svc); the system words that name no x25/x28 it would admit as unknown words,
            # which the hardware traps. Either way the defence measured here is the hardware's.
            *[(f'el0-{name}', ['--forge-entry', words], fixture, stop) for name, words, stop in EL0_FORGED]]


# Forged block entries (name, A64 words, expected halt): system instructions trap at EL0 (ESR classes 0x00, 0x01,
# 0x18); loads, stores and branches into the private window (`sub x0, x28, #0x1000` names the EL1-only frame alias; a
# branch to x28 is a branch into the window, the guest-state page being EL0 data, never executable; `adr` names the
# execute-only code area) abort there.
EL0_FORGED = [('msr-daifset', 'd50342df', 'trapped'), ('msr-ttbr0', 'd5182000', 'trapped'),
              ('mrs-sctlr', 'd5381000', 'trapped'), ('mrs-ctr', 'd53b0020', 'trapped'), ('tlbi', 'd508871f', 'trapped'),
              ('dc-civac', 'd50b7e20', 'trapped'), ('ic-ivau', 'd50b7520', 'trapped'), ('eret', 'd69f03e0', 'trapped'),
              ('wfi', 'd503207f', 'trapped'), ('hvc', 'd4000002', 'trapped'),
              # The debug channel (MDSCR_EL1.TDCC), counters and timers (CNTKCTL_EL1 = 0), DC ZVA (SCTLR_EL1.DZE).
              ('mrs-mdccsr', 'd5330100', 'trapped'), ('msr-dbgdtrtx', 'd5130500', 'trapped'), ('mrs-dbgdtrrx', 'd5330500', 'trapped'),
              ('mrs-cntvct', 'd53be040', 'trapped'), ('msr-cntv-ctl', 'd51be320', 'trapped'), ('dc-zva', 'd50b743c', 'trapped'), ('mrs-pmccntr', 'd53b9d00', 'trapped'),
              # A literal exit to a RIP none of the block's link points targets: the linker's own check.
              ('link-forged', 'd2824680,d4000021', 'bad_link'),
              # x28 is the guest-state page's EL0 alias since I1 (G1: EL0 may read and write it); its EL1-only alias, one
              # page below in the window (abi FRAME_VA), stays unreachable.
              ('load-frame', 'd1400780,f940001f', 'private_access'), ('store-frame', 'd1400780,f900001c', 'private_access'),
              ('branch-frame', 'd61f0380', 'private_access'), ('read-code', '10000000,f9400000', 'private_access')]


# Tampers whose section is a single instruction at 0x100000 with all GPRs 0: the vCPU halted before its first
# instruction ran.
AT_START = ('site', 'site-kind', 'system')


def halted_state(name, section, cpu):
    """The tampered vCPU's banked state (contracts 56, 58: a defence halts before any effect, nothing fabricated): the
    FEX frame intact; for AT_START tampers the start state (rip 0x100000, every GPR 0), for the others a block boundary
    the run reached: the entry of a block it translated, or the section's end (a forged exit is refused at the reply of
    the forged block, banked at its entry)."""
    problems = [] if cpu['frame_intact'] else ['FEX frame changed']
    rip = smp.num(cpu['rip'])
    if name == 'el0-link-forged':  # the linker banks the exit's state, the forged RIP (0x1234) included, and halts
        return problems + ([] if rip == 0x1234 else [f"banked rip {cpu['rip']}, want the forged 0x1234"])
    end = 0x100000 + len(re.search(r'(?:code|body 0x100000) ([0-9a-f]+)', section).group(1)) // 2
    if name in AT_START:
        if rip != 0x100000 or any(smp.num(g) for g in cpu['gpr']):
            problems.append(f"banked rip {cpu['rip']} gpr {cpu['gpr']}, want rip 0x100000 and every GPR 0")
    elif rip not in {smp.num(r) for r in cpu['block_rips']} | {end}:
        problems.append(f"banked rip {cpu['rip']} is no block boundary of the run ({len(cpu['block_rips'])} blocks, end {end:#x})")
    return problems


def unacked_program(n, k):
    """vCPU k stores to a data word on page 12 (a writable shadow of it), raises GEN and waits for DONE in a linked loop;
    vCPU 0 waits for GEN, delays (vCPU k is then in its linked loop), calls the function on page 12 (its miss marks the
    page and must wait for vCPU k's shadow reset) and raises DONE; the others end at once."""
    P, done = programs.CC_PAGE(12), programs.CC_DONE
    t0 = programs.cc_wait(programs.CC_GEN, 1, 'g') + '    mov ecx, 5000000\n.x: dec ecx\n    jnz .x\n'
    t0 += programs.cc_call(P + programs.CC_FN, 'b') + f'    mov qword [0x{done:x}], 1\n'
    texts = [t0] + [f'    mov [0x{P + 0x800:x}], rbx\n    mov qword [0x{programs.CC_GEN:x}], 1\n' + programs.cc_wait(done, 1, 'd')
                    if i == k else '' for i in range(1, n)]
    return programs.cc_program(f'unacked{k}', n, texts, [(12, bytes(programs.CC_FN - 0x10) + programs.cc_function(1))],
                               regs=lambda i: {'rbx': 0x3333 * (i + 1)})


def spin_program(iterations):
    """vCPU 0 waits in a linked loop for a flag vCPU 1 raises after `iterations` rounds of a delay loop (no exits in
    either): a guest that legitimately spins for as long as the delay takes."""
    t0 = programs.cc_wait(programs.CC_GEN, 1, 'w')
    t1 = f'    mov rcx, {iterations}\n.x: dec rcx\n    jnz .x\n    mov qword [0x{programs.CC_GEN:x}], 1\n'
    return programs.cc_program('spin', 2, [t0, t1], [])


def cmd_contain(a, host, runtime):
    """See the module docstring (`contain`)."""
    import time
    fixtures = json.loads((ROOT / 'fixtures/slice1/programs.json').read_text())['programs']
    expected = {r['name']: r for r in json.loads((ROOT / 'fixtures/slice1/expected.json').read_text())['results']}
    fixture = vm.description(fixtures[0])
    report, failures, fresh_runs = {k: [] for k in ('tamper', 'unacked', 'compiler', 'stall', 'spin', 'legit', 'fresh')}, [], [0]
    wd = a.watchdog_ms

    def run(label, text, extra, bound):
        """One process, killed if it outlives its bound by SLACK seconds (a hang is then a diff, not a stuck harness)."""
        t = time.monotonic()
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / label, extra, timeout=bound + SLACK)
        return rc, out, err, time.monotonic() - t

    def teardown(n, err):
        return [] if TEARDOWN.format(n=n) in err else [f'no clean teardown line: {err.strip()[-300:]}']

    def fresh(n, after):
        """A fresh process right after a failure: n fixture programs (rotated) on n vCPUs, each must match QEMU."""
        i = fresh_runs[0]; fresh_runs[0] += 1
        ps = [fixtures[(i * 4 + v) % len(fixtures)] for v in range(n)]
        rc, out, err, secs = run(f'fresh-{after}', ''.join('cpu\n' + vm.description(p) for p in ps), [], 10)
        diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else []
        for v, p in enumerate(ps if not rc else []):
            outcome, d = vm.evaluate(p, expected[p['name']], out['cpus'][v])
            if outcome != 'match' or per_vcpu_checks(out['cpus'][v]): diffs.append(f"cpu{v} {p['name']}: {outcome} {d} {per_vcpu_checks(out['cpus'][v])}")
        diffs += teardown(n, err)
        report['fresh'].append({'after': after, 'n': n, 'programs': [p['name'] for p in ps], 'diffs': diffs, 'seconds': round(secs, 2)})
        return [f'fresh after {after}: {d}' for d in diffs]

    def judge(n, rc, out, err, secs, want, bound):
        """want[v] = (stop, reason prefix); the run must exit 0, tear down cleanly and end within `bound` seconds."""
        diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else []
        stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
        if out and not all(s == w[0] and r.startswith(w[1]) for (s, r), w in zip(stops, want)): diffs.append(f'stops {stops}, want {want}')
        diffs += teardown(n, err)
        if secs > bound: diffs.append(f'took {secs:.1f} s (bound {bound} s)')
        return stops, diffs

    # 1 and 4: every tamper on every vCPU at N = 1..4 (the others in linked loops); the tampered vCPU halts, the others are
    # cancelled, all joined, the VM destroyed; then a fresh process.
    for name, extra, section, reason in tamper_kinds(fixture):
        for n in a.cpus_contain:
            for v in range(n):
                text = ''.join('cpu\n' + (section if w == v else LOOP) for w in range(n))
                kick = ['--kick-vcpu', v] if name in ('forge-stop', 'armed') else []
                label = f'{name}-n{n}-v{v}'
                rc, out, err, secs = run(label, text, extra + kick + ['--tamper-vcpu', v], 10)
                want = [('resident', reason) if w == v else ('cancelled', '') for w in range(n)]
                stops, diffs = judge(n, rc, out, err, secs, want, 10)
                if out and name != 'forge-exit' and not out['cpus'][v]['tampered']: diffs.append('not tampered')
                if out: diffs += halted_state(name, section, out['cpus'][v])
                diffs += fresh(n, label)
                report['tamper'].append({'label': label, 'stops': stops, 'seconds': round(secs, 2), 'diffs': diffs})
                print(f"{label}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]} ({secs:.2f} s)", flush=True)
                failures += [f'{label}: {d}' for d in diffs]

    # 2: a peer that never answers a shootdown: its doorbells are disabled, so it never stops or drains, and its own
    # watchdog is off (a live peer); a host kicker rings it every millisecond, unheard. Control: the same untampered.
    for k in (1, 2, 3):
        prog = unacked_program(4, k)
        kicks = ['--kick-vcpu', k, '--kicks', 100_000, '--kick-every-us', 1000, '--watchdog-ms', wd]
        if k == 1:
            want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
            rc, out, err, secs = run('unacked-control', smp_description(prog), kicks, wd / 1000 + 5)
            diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else judge_smp(prog, want, out, [None] * 4)
            if out and out['cpus'][0]['stats']['shootdowns'] < 1: diffs.append('vCPU 0 shot nothing down')
            diffs += teardown(4, err)
            report['unacked'].append({'label': 'control', 'diffs': diffs, 'shootdowns': out and out['cpus'][0]['stats']['shootdowns']})
            print(f"unacked control: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]}", flush=True)
            failures += [f'unacked control: {d}' for d in diffs]
        label = f'unacked-v{k}'
        rc, out, err, secs = run(label, smp_description(prog), kicks + ['--ignore-kicks', '--tamper-vcpu', k], wd / 1000 + 5)
        want = [('watchdog', 'resident pc=') if w == 0 else ('cancelled', '') if w == k else ('reached', '') for w in range(4)]
        stops, diffs = judge(4, rc, out, err, secs, want, wd / 1000 + 5)
        if stops and f'shadows_asked=[{k}]' not in stops[0][1]: diffs.append(f'vCPU {k} not named as the peer asked')
        # Its doorbell was rung, stayed pending (disabled) and never interrupted it.
        kk = out and out['cpus'][k]['kicks']
        if kk and not (kk['issued'] >= 1 and kk['pending'] and kk['irqs'] == 0): diffs.append(f'vCPU {k} kicks {kk}')
        diffs += fresh(4, label)
        report['unacked'].append({'label': label, 'stops': stops, 'seconds': round(secs, 2), 'diffs': diffs})
        print(f"{label}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]} {stops and stops[0]} ({secs:.2f} s)", flush=True)
        failures += [f'{label}: {d}' for d in diffs]

    # 3: vCPU k's compile server dies mid-run (killed before its 51st request): the run fails at once (KILL_BOUND, far
    # below the peers' watchdogs) naming the cause; `--kill-reps R` repeats each case (a fresh process after the first only).
    for k, rep in ((k, rep) for k in (1, 2, 3) for rep in range(a.kill_reps)):
        label = f'compiler-v{k}' + (f'-r{rep}' if rep else '')
        text = ''.join('cpu\n' + (chain_section() if w == k else LOOP) for w in range(4))
        rc, out, err, secs = run(label, text, ['1', '--kill-compiler', 50, '--tamper-vcpu', k], 10)
        diffs = [] if rc == 1 and out is None else [f'driver exit {rc}, want 1']
        if f'owner {k}: fexc --serve (pid' not in err or 'signal: 9' not in err: diffs.append(f'cause not named: {err.strip()[-300:]}')
        diffs += teardown(4, err) + ([f'took {secs:.1f} s (bound {KILL_BOUND} s)'] if secs > KILL_BOUND else [])
        diffs += fresh(4, label) if not rep else []
        if rep and not diffs: continue
        report['compiler'].append({'label': label, 'stderr': err.strip()[-600:], 'seconds': round(secs, 2), 'diffs': diffs})
        print(f"{label}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]} ({secs:.2f} s)", flush=True)
        failures += [f'{label}: {d}' for d in diffs]

    # 3: vCPU k's compile server stops answering (SIGSTOP before its 51st request): the request's deadline kills it and the
    # run fails the same way, naming the vCPU, the server and the deadline.
    cd = a.compile_deadline_ms
    for k in (1, 2, 3):
        label = f'stall-v{k}'
        text = ''.join('cpu\n' + (chain_section() if w == k else LOOP) for w in range(4))
        rc, out, err, secs = run(label, text, ['1', '--stall-compiler', 50, '--tamper-vcpu', k, '--compile-deadline-ms', cd], cd / 1000 + 5)
        diffs = [] if rc == 1 and out is None else [f'driver exit {rc}, want 1']
        if f'owner {k}: fexc --serve (pid' not in err or f'no reply within {cd / 1000:g}s' not in err or 'signal: 9' not in err:
            diffs.append(f'cause not named: {err.strip()[-300:]}')
        diffs += teardown(4, err) + ([f'took {secs:.1f} s'] if secs > cd / 1000 + 5 else [])
        diffs += fresh(4, label)
        report['stall'].append({'label': label, 'stderr': err.strip()[-600:], 'seconds': round(secs, 2), 'diffs': diffs})
        print(f"{label}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]} ({secs:.2f} s)", flush=True)
        failures += [f'{label}: {d}' for d in diffs]

    # 5: the watchdog contains a guest loop that never ends (vCPU v; the others run fixtures to their ends).
    for v in range(4):
        label = f'spin-v{v}'
        ps = [fixtures[(v + w) % len(fixtures)] for w in range(4)]
        text = ''.join('cpu\n' + (LOOP if w == v else vm.description(ps[w])) for w in range(4))
        rc, out, err, secs = run(label, text, ['--watchdog-ms', wd], wd / 1000 + 5)
        want = [('watchdog', 'guest pc=') if w == v else ('reached', '') for w in range(4)]
        stops, diffs = judge(4, rc, out, err, secs, want, wd / 1000 + 5)
        for w in range(4 if out else 0):
            if w != v and vm.evaluate(ps[w], expected[ps[w]['name']], out['cpus'][w])[0] != 'match': diffs.append(f'cpu{w} does not match')
        diffs += fresh(4, label)
        report['spin'].append({'label': label, 'stops': stops, 'seconds': round(secs, 2), 'diffs': diffs})
        print(f"{label}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]} ({secs:.2f} s)", flush=True)
        failures += [f'{label}: {d}' for d in diffs]

    # A guest that legitimately spins (and computes) longer than a tight budget: the watchdog reports a guest stop; with
    # the default budget the same program reaches its end on both vCPUs.
    prog = spin_program(a.spin_iterations)
    for budget, ok_stop in ((300, 'watchdog'), (10_000, 'reached')):
        rc, out, err, secs = run(f'legit-{budget}', smp_description(prog), ['--watchdog-ms', budget], budget / 1000 + 5)
        stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
        diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else []
        if ok_stop == 'reached' and stops != [('reached', '')] * 2: diffs.append(f'stops {stops}')
        if ok_stop == 'watchdog' and not (stops and any(s == 'watchdog' and r.startswith('guest pc=') for s, r in stops)
                                          and all(s in ('watchdog', 'cancelled') for s, _ in stops)):
            diffs.append(f'stops {stops}')
        diffs += teardown(2, err)
        report['legit'].append({'budget_ms': budget, 'stops': stops, 'seconds': round(secs, 2), 'diffs': diffs})
        print(f"legit spin, watchdog {budget} ms: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:300]} {stops} ({secs:.2f} s)", flush=True)
        failures += [f'legit spin {budget} ms: {d}' for d in diffs]

    # 5: progress under sustained cross-vCPU code writes and while a peer misses on a finishing vCPU's page (coherence).
    if not a.no_progress:
        bad, report['progress'] = coherence_runs(a, host, runtime, ['mutual', 'data', 'rewrite', 'stale', 'finish'])
        failures += bad
    for f in failures: print(f)
    print(f"fresh-process controls: {len(report['fresh'])}, failing {sum(bool(r['diffs']) for r in report['fresh'])}")
    (a.out / 'contain.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['corpora', 'smp', 'litmus', 'limits', 'kicks', 'system', 'coherence', 'sharedpt', 'contain', 'privilege', 'delivery', 'events'])
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--cpus', type=int, nargs='+', default=[2, 3, 4])
    ap.add_argument('--max-block', type=int)
    ap.add_argument('--parallel', type=int, default=1)
    ap.add_argument('--seed', type=int, default=20261027)
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    flow = [raw / 's2d/corpus/programs.json', raw / 's2d/corpus/expected.json']
    ap.add_argument('--flow', type=pathlib.Path, nargs=2, default=flow if all(f.exists() for f in flow) else None)
    ap.add_argument('--no-faults', action='store_true')
    ap.add_argument('--qemu', action='store_true', help='smp: also compare with a pinned QEMU system-mode run')
    ap.add_argument('--families', type=lambda s: s.split(','))
    ap.add_argument('--variants', type=lambda s: s.split(','))
    ap.add_argument('--k', type=int, default=25_000, help='litmus: iterations per run')
    ap.add_argument('--runs', type=int, default=4, help='litmus: runs per cell')
    ap.add_argument('--control', choices=['drop-fence', 'drop-lock'])
    ap.add_argument('--programs', type=lambda s: s.split(','))
    ap.add_argument('--reps', type=int, default=1)
    ap.add_argument('--kicks', type=int, help='kicks per vCPU (kicks: default 10000; corpora: off unless given)')
    ap.add_argument('--iterations', type=int, default=400_000_000, help='hot-loop iterations per vCPU (kicks)')
    ap.add_argument('--rep-iterations', type=int, default=3000, help='REP-string rounds per vCPU (kicks, I2)')
    ap.add_argument('--adversarial-iterations', type=int, default=2_000_000, help='adversarial rounds per vCPU (kicks)')
    ap.add_argument('--jitter-us', type=int, default=40, help='adversarial kicks: random extra delay 0..J us after each stop')
    ap.add_argument('--cpus-contain', type=int, nargs='+', default=[1, 2, 3, 4], help='contain: N for the tamper matrix')
    ap.add_argument('--watchdog-ms', type=int, default=1000, help='contain: the watchdog of the runs that must hit it')
    ap.add_argument('--compile-deadline-ms', type=int, default=2000, help='contain: the compile deadline of the stalled-server runs')
    ap.add_argument('--spin-iterations', type=int, default=1_500_000_000, help='contain: the legitimate spin delay loop')
    ap.add_argument('--no-progress', action='store_true', help='contain: skip the coherence progress programs')
    ap.add_argument('--kill-reps', type=int, default=1, help='contain: runs of each compile-server-kill case (stress)')
    a = ap.parse_args()
    if a.mode == 'sharedpt' and '--cpus' not in sys.argv: a.cpus = [2, 4]
    a.out.mkdir(parents=True, exist_ok=True)
    host, runtime = vm.build()
    if a.mode == 'contain':
        global CODE_SHAPE
        CODE_SHAPE = []   # containment holds with object validation alone (the host's default)
    if a.mode == 'kicks' and a.kicks is None: a.kicks = 10_000
    failed = {'corpora': cmd_corpora, 'smp': cmd_smp, 'litmus': cmd_litmus, 'limits': cmd_limits, 'kicks': cmd_kicks, 'system': cmd_system, 'coherence': cmd_coherence, 'sharedpt': cmd_sharedpt, 'contain': cmd_contain, 'privilege': cmd_privilege, 'delivery': cmd_delivery, 'events': cmd_events}[a.mode](a, host, runtime)
    print('failures:', failed)
    sys.exit(1 if failed else 0)


if __name__ == '__main__': main()
