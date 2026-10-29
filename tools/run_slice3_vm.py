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
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR contain [--cpus-contain 1 2 3 4] [--watchdog-ms MS] [--compile-deadline-ms MS]
        [--spin-iterations I] [--no-progress] [--reps R]

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
test), and an XCHG chain's outcome must also be one linear order of the exchanges (`check_xchg_chain`). Every vCPU
reaches its end having done all K iterations. `--control drop-fence` runs sb-mfence without its MFENCEs, judged
against sb-mfence's allowed set: it must show the forbidden SB outcome (the harness can see a violation); its run diffs and
trial counts are judged as in a normal run; aligned and unaligned only (`--variants` with a split variant is refused: a split
store completes in the resident with `dsb ish`, so SB cannot relax there).

`kicks` (S3b, E1b, precise stop): (1) a hot linked loop (no exit once linked) on each of N vCPUs (own registers, shared
code page), every vCPU kicked M times (default 10,000) by its own host kicker thread (its GIC SPI doorbell), R runs of
it, P at once (P * N above the core count: oversubscribed); (1b) adversarial: the same at N vCPUs with kicks at random
times (0..`--jitter-us` after each stop) into `adversarial_program` (computed jumps through the dispatcher and L1 every
round, a division `svc` resumed mid-block, self-modifying stores single-stepped, peers rung by SGI), and over all its
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
the instruction; modes_model where it covers them; QEMU's own outcome is reported and must agree where it raises an
exception); refused forms stop `unsupported_system`; a tampered system reply stops `bad_reply`.

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

`contain` (S3h, containment and progress; runs one at a time, each a fresh process). Every failing run must exit as stated,
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
with a shootdown. (3) vCPU k's compile server (k = 1..3 of 4) killed before its 51st request: exit 1, stderr names the
vCPU, the server and its signal; the same server stopped instead (SIGSTOP: alive, never answering): the request deadline
(`--compile-deadline-ms`, 2 s here, driver default 30 s) kills it, exit 1, stderr names the vCPU, the server and
"no reply within". (5) A guest loop that never ends on vCPU v of 4 is stopped by the watchdog (`guest`)
while the others reach their ends and match; a guest that legitimately spins past a tight budget is a `guest` watchdog
stop, and reaches its end under the default budget: the watchdog bounds every span inside the VM without a host exit, so
budget and location (guest code or resident) are what separate the two, not the stop kind. Progress: the coherence
programs with sustained cross-vCPU code writes (`mutual`, `data`, `rewrite`, `stale`) and CPUs ending while a peer
translates their page (`finish`, `finish-late`), as `coherence` (QEMU reference, block sizes default and 1).

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
        run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, *map(str, extra)],
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
                  'gpr ' + ' '.join(f'{smp.num(v):#x}' for v in c['gpr'])]
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
        stats = [{k: cpu['stats'][k] for k in ('blocks', 'requests', 'links', 'retries')} for cpu in cpus]
        return [{**e, 'outcome': outcome, 'diffs': diffs, 'stats': stats}]
    entries = run_groups(a, host, runtime, groups, judge)
    by = collections.defaultdict(collections.Counter)
    for e in entries: by[e['name']][e['outcome']] += 1
    for name, c in by.items(): print(f'{name}: {dict(c)}', flush=True)
    for e in entries:
        if e['outcome'] != 'match': print(f"{e['name']} rep{e['rep']} {e['outcome']}: {'; '.join(e['diffs'])[:400]}")
    (a.out / 'smp.json').write_text(json.dumps({'results': entries}, indent=1) + '\n')
    return sum(c['fail'] for c in by.values())


def cmd_litmus(a, host, runtime):
    from models import ordering_model as model
    control = a.control == 'drop-fence'
    families = ['sb-mfence'] if control else a.families or list(programs.FAMILIES)
    # The control refuses the split variants: a split store completes in the resident with `dsb ish`, so SB cannot relax there.
    if control and a.variants and any(v.startswith('split') for v in a.variants):
        sys.exit('--control drop-fence cannot run split variants: a split store completes with dsb ish, so SB cannot relax there')
    variants = a.variants or [v for v in programs.VARIANTS if not (control and v.startswith('split'))]
    cells = [(f, n, v) for f in families for n in a.cpus for v in variants
             if programs.litmus_family(f, n) and not (f == 'xchg' and v.startswith('split'))]
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
        label = f'{f}@{n} {v}' + (' without MFENCE' if control else '')
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
    (a.out / ('litmus-control.json' if control else 'litmus.json')).write_text(json.dumps({'report': report, 'failures': failures}, indent=1) + '\n')
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
    cases = {'cr0-cd': 'mov rax, cr0\nbts rax, 30\nmov cr0, rax', 'cr4-osfxsr': 'mov rax, cr4\nbts rax, 9\nmov cr4, rax',
             'mov-from-cr2': 'mov rax, cr2', 'mov-to-cr8': 'xor eax, eax\nmov cr8, rax', 'cr3-pwt': 'mov rax, cr3\nbts rax, 3\nmov cr3, rax',
             # Valid under the CPU 16/17 profile but not implemented yet (S1/S2): the resident must refuse, never #GP.
             'cr4-pce': 'mov rax, cr4\nbts rax, 8\nmov cr4, rax', 'rdmsr-apic-base': 'mov ecx, 0x1b\nrdmsr',
             'rdmsr-pat': 'mov ecx, 0x277\nrdmsr', 'wrmsr-pat': 'mov ecx, 0x277\nmov eax, 0x00070406\nmov edx, 0x00070406\nwrmsr',
             'rdmsr-tsc-aux': 'mov ecx, 0xC0000103\nrdmsr', 'wrmsr-fs-base': 'mov ecx, 0xC0000100\nxor eax, eax\nxor edx, edx\nwrmsr',
             'rdmsr-misc-enable': 'mov ecx, 0x1a0\nrdmsr', 'rdmsr-mtrrcap': 'mov ecx, 0xfe\nrdmsr'}
    return {k: ref.assemble('mov rbx, 1\n' + v).hex() for k, v in cases.items()}


# Resident behaviour that disagrees with the CPU 16/17 profile (tools/models/cpu_profile.json, via tools/system_fault_model.py)
# and is waiting for chunk S1/S2: the case runs and its mismatch is printed as GAP, not counted as a failure. A listed case that
# starts to pass is a failure, so the entry must be deleted when the resident is fixed.
SYSTEM_GAPS = {
    **{f'sysfault-cr4-{n}@1': 'crates/runtime/src/execution.rs CR4_UNSUPPORTED (0x457f5f) refuses the write as unsupported_system instead of #GP; '
                              'CPUID bit clear in the profile makes the bit reserved (SDM Vol.3 2.5)' for n in ('vme', 'pvi', 'mce', 'umip', 'vmxe', 'smxe', 'pke')},
    **{f'unsupported {n}': 'resident raises #GP for every MSR but EFER (execution.rs:724); the profile has this MSR (CPU 16/17, SDM Vol.4)'
       for n in ('rdmsr-apic-base', 'rdmsr-pat', 'wrmsr-pat', 'rdmsr-tsc-aux', 'wrmsr-fs-base', 'rdmsr-misc-enable', 'rdmsr-mtrrcap')},
}


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
            # The model is given the start-up controls: the prefix must not have written any (MOV to CR 0f 22, WRMSR 0f 30).
            prefix = bytes.fromhex(prog['bodies'][0]['code'])[:at - smp.num(prog['bodies'][0]['addr'])]
            assert b'\x0f\x22' not in prefix and b'\x0f\x30' not in prefix, f'{name}: the prefix changes controls'
            vector = sfm.fault(code, want['cpus'][0]['gpr'], c.get('cpl', 3), 0x80010011, 0x20, 0xd00)
            if vector is None: failures.append(f'{name}: the model expects no fault'); continue
            want['cpus'][0]['fault'] = (*vector, at, 0)
            full, rc = qemu(prog, name, timeout=10)
            entry['qemu'] = 'hang' if rc == 'timeout' else (full and full['cpus'][0]['fault'] and list(full['cpus'][0]['fault'])) or 'no fault'
            # Where QEMU does raise an exception, it is the model's.
            if isinstance(entry['qemu'], list) and tuple(entry['qemu']) != want['cpus'][0]['fault']:
                failures.append(f"{name}: QEMU raises {entry['qemu']}, the model {want['cpus'][0]['fault']}")
        else:
            want, rc = qemu(prog, name)
            if want is None: failures.append(f'{name}: QEMU run failed ({rc})'); continue
            for c in want['cpus']: c['fault'] = qemu_fault(c['fault'])
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
            entry['runs'].append({'max_block': limit[0] if limit else None, 'diffs': diffs,
                                  'systems': out and [c['stats']['systems'] for c in out['cpus']]})
            (gaps if name in SYSTEM_GAPS else failures).extend([f"{name}{' (max-block 1)' if limit else ''}: {d}" for d in diffs])
        bad = any(r['diffs'] for r in entry['runs'])
        if name in SYSTEM_GAPS and not bad: failures.append(f'{name}: listed in SYSTEM_GAPS but passes')
        print(f"{name}: {('GAP' if name in SYSTEM_GAPS else 'FAIL') if bad else 'ok'}"
              + (f"; QEMU full run: {entry['qemu']}" if 'qemu' in entry else '') + (f"; modes model {entry['modes_model']}" if 'modes_model' in entry else '')
              + (f"; history {entry['either']}" if 'either' in entry else ''), flush=True)
        report.append(entry)
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


# ---- contain (S3h): containment and progress --------------------------------------------------------------------------
LOOP = 'rip 0x100000\nbody 0x100000 ebfe\n'          # jmp $: a linked loop with no exit, in its own space
TEARDOWN = 'teardown: vcpus {n} joined {n} destroyed {n} vm_destroy 0 children none'
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
            # EL0 isolation by hardware (E2): words admission never admits, forged over the first block's entry.
            *[(f'el0-{name}', ['--forge-entry', words], fixture, stop) for name, words, stop in EL0_FORGED]]


# Forged block entries (name, A64 words, expected halt): system instructions trap at EL0 (ESR classes 0x00, 0x01,
# 0x18); loads, stores and branches into the private window (x28 is the EL1-only frame page; `adr` names the
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
              ('load-frame', 'f940039f', 'private_access'), ('store-frame', 'f900039c', 'private_access'),
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

    # 3: vCPU k's compile server dies mid-run (killed before its 51st request): the run fails naming the cause.
    for k in (1, 2, 3):
        label = f'compiler-v{k}'
        text = ''.join('cpu\n' + (chain_section() if w == k else LOOP) for w in range(4))
        rc, out, err, secs = run(label, text, ['1', '--kill-compiler', 50, '--tamper-vcpu', k], 10)
        diffs = [] if rc == 1 and out is None else [f'driver exit {rc}, want 1']
        if f'owner {k}: fexc --serve (pid' not in err or 'signal: 9' not in err: diffs.append(f'cause not named: {err.strip()[-300:]}')
        diffs += teardown(4, err) + ([f'took {secs:.1f} s'] if secs > 10 else [])
        diffs += fresh(4, label)
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
    ap.add_argument('mode', choices=['corpora', 'smp', 'litmus', 'limits', 'kicks', 'system', 'coherence', 'sharedpt', 'contain'])
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
    ap.add_argument('--control', choices=['drop-fence'])
    ap.add_argument('--programs', type=lambda s: s.split(','))
    ap.add_argument('--reps', type=int, default=1)
    ap.add_argument('--kicks', type=int, help='kicks per vCPU (kicks: default 10000; corpora: off unless given)')
    ap.add_argument('--iterations', type=int, default=400_000_000, help='hot-loop iterations per vCPU (kicks)')
    ap.add_argument('--adversarial-iterations', type=int, default=2_000_000, help='adversarial rounds per vCPU (kicks)')
    ap.add_argument('--jitter-us', type=int, default=40, help='adversarial kicks: random extra delay 0..J us after each stop')
    ap.add_argument('--cpus-contain', type=int, nargs='+', default=[1, 2, 3, 4], help='contain: N for the tamper matrix')
    ap.add_argument('--watchdog-ms', type=int, default=1000, help='contain: the watchdog of the runs that must hit it')
    ap.add_argument('--compile-deadline-ms', type=int, default=2000, help='contain: the compile deadline of the stalled-server runs')
    ap.add_argument('--spin-iterations', type=int, default=1_500_000_000, help='contain: the legitimate spin delay loop')
    ap.add_argument('--no-progress', action='store_true', help='contain: skip the coherence progress programs')
    a = ap.parse_args()
    if a.mode == 'sharedpt' and '--cpus' not in sys.argv: a.cpus = [2, 4]
    a.out.mkdir(parents=True, exist_ok=True)
    host, runtime = vm.build()
    if a.mode == 'kicks' and a.kicks is None: a.kicks = 10_000
    failed = {'corpora': cmd_corpora, 'smp': cmd_smp, 'litmus': cmd_litmus, 'limits': cmd_limits, 'kicks': cmd_kicks, 'system': cmd_system, 'coherence': cmd_coherence, 'sharedpt': cmd_sharedpt, 'contain': cmd_contain}[a.mode](a, host, runtime)
    print('failures:', failed)
    sys.exit(1 if failed else 0)


if __name__ == '__main__': main()
