#!/usr/bin/env python3
"""N vCPUs on the general execution path (slice 3, S3a): several programs at once in one VM, one per vCPU.

    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR corpora [--cpus 2 3 4] [--max-block N]
        [--parallel P] [--seed S] [--flow PROGRAMS.json EXPECTED.json] [--no-faults]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR smp [--cpus 2 3 4] [--programs a,b] [--reps R]
        [--parallel P] [--max-block N]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR limits
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR kicks [--cpus 1 2 4] [--kicks M] [--iterations I]
        [--parallel P] [--reps R]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR system [--programs SUBSTR,...]
    tools/with-env python3 tools/run_slice3_vm.py --fexc FEXC --out DIR coherence [--programs SUBSTR,...] [--reps R]
        [--parallel P]

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
shared address space, judged against tools/bench/smp/model_smp.py with smp.compare. Programs using an instruction the
general path does not admit yet are reported `unsupported` when a vCPU stops on it (`unsupported_form`): handshake
and token-ring spin with PAUSE, xchg-lock needs memory XCHG (S3e). Run to the end, they are judged like the others.

`kicks` (S3b, precise stop): (1) a hot linked loop (no exit once linked) on each of N vCPUs (own registers, shared
code page), every vCPU kicked M times (default 10,000) by its own host kicker thread, R runs of it, P at once (P * N
above the core count: oversubscribed); (2) a peer's resident kick: vCPU 1 writes data on vCPU 0's code page while vCPU
0 spins in its hot loop, so vCPU 1's resident posts the page to vCPU 0 and kicks it (`hvc #KICK_HVC`); (3) forged stop
PCs: vCPU V's first stop enters the stop gate with ELR = the word after the stop store, which must halt V (bad_stop)
and cancel the others. Programs are natemu-smp-v1; the reference is a pinned QEMU system-mode run of the same program
(tools/bench/smp, `-accel tcg,thread=multi`), compared with smp.compare. Per vCPU: reached its end, the per-vCPU checks,
stops taken = kicks issued - a kick still pending at its end (none lost or doubled; kicks finding it in the host
included), the resident's stop count = the host's, every stop PC a stop store of a placed block, and (hot loop) all M
kicks issued while it ran. Latency (protect return -> stop exit, per vCPU) is reported. `corpora --kicks M` also kicks
every vCPU M times (mostly while it is in the host or the resident) and adds the same accounting checks.

`system` (S3c1, long-mode system instructions as resident transactions): see cmd_system. Each system program of
tools/bench/smp/system.json runs at block sizes default and 1 against QEMU system mode (smp.SYSTEM_CPU), except the PTE
change without INVLPG (i4v_coherence_model) and the fault cases (tools/system_fault_model.py at QEMU's state just before
the instruction; i4v_modes_model where it covers them; QEMU's own outcome is reported and must agree where it raises an
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

`limits`: 5 vCPUs are refused (0 cannot be written: a description without `cpu` lines is one vCPU; the host's
`prepare` refuses 0 in its unit test); a forged block header on vCPU V of 4 (V = 0..3; the others spin in a linked
loop) halts V with bad_link, cancels and joins the others, and the VM is destroyed (the driver reports only then).

Writes DIR/<mode>.json; exit status 1 on any failure."""
import argparse, collections, concurrent.futures as cf, json, os, pathlib, random, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
sys.path.insert(0, str(ROOT / 'tools' / 'bench' / 'smp'))
import ref, run_slice1_vm as vm, run_slice1_faults as faults, smp, model_smp  # noqa: E402

# Programs needing an instruction not admitted yet (fexc refuses it: `unsupported_form`).
REFUSED = {'handshake': 'PAUSE', 'token-ring': 'PAUSE', 'xchg-lock': 'memory XCHG'}


def driver(host, runtime, fexc, text, work, extra=()):
    """Runs one description; (returncode, stdout JSON or None, stderr)."""
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, *map(str, extra)], capture_output=True, text=True)
    out = json.loads(run.stdout) if run.returncode == 0 else None
    return run.returncode, out, run.stderr[-2000:]


def kick_checks(out, kicks=None):
    """Kick accounting of one vCPU's output (`kicks`: every kick must have been issued)."""
    k, problems = out.get('kicks'), []
    if k is None: return ['no kick report']
    if k['stops'] + k['pending'] != k['issued']: problems.append(f"stops {k['stops']} + pending {k['pending']} != issued {k['issued']}")
    # A merge is a failure only when the host kicker is the vCPU's only kick source (`kicks` given): a peer's resident
    # kick may legitimately land while another is pending.
    if k['merged'] and kicks is not None: problems.append(f"{k['merged']} kicks merged")
    if out['stats']['stops'] != k['stops']: problems.append(f"resident stops {out['stats']['stops']} != host {k['stops']}")
    # Judged against the blocks placed at the end, so only without a flush after the driver's initial one (a flush, such
    # as a CR3 load's, reuses code addresses; the resident's stop gate checks every stop PC when it is taken).
    if not k['stops_at_entry'] and out['stats']['flushes'] <= 1: problems.append(f"stop PCs not at stop stores: {k['stop_pcs']}")
    if kicks is not None and k['issued'] != kicks: problems.append(f"{k['issued']} of {kicks} kicks issued before the end")
    return problems


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
                  f"stops {sum(k['stops'] for k in ks)}, pending at end {sum(k['pending'] for k in ks)}", flush=True)
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

    def judge(i, rc, out, err):
        prog = order[i]
        base = prog['name'].split('@')[0]
        e = {'name': prog['name'], 'rep': i}
        if rc:
            outcome = 'unsupported' if base in REFUSED and 'unsupported_form' in err else 'fail'
            return [{**e, 'outcome': outcome, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
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
        if base in REFUSED and any('unsupported_form' in d for d in diffs):
            outcome = 'unsupported'
            diffs = [f'{REFUSED[base]} not admitted'] + diffs
        else: outcome = 'fail' if diffs else 'match'
        stats = [{k: cpu['stats'][k] for k in ('blocks', 'fast', 'requests', 'links', 'retries')} for cpu in cpus]
        return [{**e, 'outcome': outcome, 'diffs': diffs, 'stats': stats}]
    entries = run_groups(a, host, runtime, groups, judge)
    by = collections.defaultdict(collections.Counter)
    for e in entries: by[e['name']][e['outcome']] += 1
    for name, c in by.items(): print(f'{name}: {dict(c)}', flush=True)
    for e in entries:
        if e['outcome'] != 'match': print(f"{e['name']} rep{e['rep']} {e['outcome']}: {'; '.join(e['diffs'])[:400]}")
    (a.out / 'smp.json').write_text(json.dumps({'results': entries}, indent=1) + '\n')
    return sum(c['fail'] for c in by.values())


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
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f'forge{v}', ['--forge-header', '--tamper-vcpu', v])
        stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
        report[f'forge{v}'] = {'returncode': rc, 'stops': stops, 'stderr': err.strip()[-300:]}
        want = [('resident', 'bad_link status=1 detail=9') if w == v else ('cancelled', '') for w in range(4)]
        if rc or stops != want: failures.append(f'forged header on vCPU {v}: {rc} {stops} {err.strip()[-200:]}')
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
    failures, report = [], {'hot': [], 'peer': [], 'forge': []}
    for n in a.cpus:
        prog = hot_program(n, a.iterations)
        want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
        extra = [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', a.kicks]
        groups = [(f"{prog['name']}-{r}", smp_description(prog), extra) for r in range(a.reps)]

        def judge(i, rc, out, err, prog=prog, want=want, n=n):
            e = {'name': prog['name'], 'rep': i}
            if rc: return [{**e, 'diffs': [f'driver exit {rc}: {err.strip()[-300:]}']}]
            return [{**e, 'diffs': judge_smp(prog, want, out, [a.kicks] * n), 'kicks': [c['kicks'] for c in out['cpus']],
                     'stats': [{k: c['stats'][k] for k in ('blocks', 'fast', 'requests', 'stops', 'linker')} for c in out['cpus']]}]
        entries = run_groups(a, host, runtime, groups, judge)
        for e in entries:
            ks = e.get('kicks', [])
            lat = [k['latency_ns'] for k in ks]
            print(f"{e['name']} rep{e['rep']}: {'ok' if not e['diffs'] else 'FAIL ' + '; '.join(e['diffs'])[:400]}; "
                  f"issued {[k['issued'] for k in ks]} in_host {[k['in_host'] for k in ks]} stops {[k['stops'] for k in ks]}; "
                  f"latency p50/p99/p999/max us {[(l['p50'] / 1e3, l['p99'] / 1e3, l['p999'] / 1e3, l['max'] / 1e3) for l in lat]}", flush=True)
            failures += [f"{e['name']} rep{e['rep']}: {d}" for d in e['diffs']]
        report['hot'] += entries
    # A peer's resident kick: no host kicker; vCPU 0 must take at least one stop and drop its blocks from the page.
    prog = peer_program(a.iterations)
    want = qemu_reference(prog, a.out / 'qemu' / prog['name'])
    for r in range(a.reps):
        rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f'peer-{r}')
        diffs = [f'driver exit {rc}: {err.strip()[-300:]}'] if rc else judge_smp(prog, want, out, [None, None])
        if not rc:
            k0, st0 = out['cpus'][0]['kicks'], out['cpus'][0]['stats']
            if k0['issued'] < 1 or k0['stops'] < 1 or st0['requests'] < 3:
                diffs.append(f'vCPU 0 not stopped by the peer, or its loop not translated again: {k0} {st0}')
        report['peer'].append({'rep': r, 'diffs': diffs, 'kicks': out and [c['kicks'] for c in out['cpus']],
                               'stats': out and [c['stats'] for c in out['cpus']]})
        print(f"peer-kick rep{r}: {'ok' if not diffs else 'FAIL ' + '; '.join(diffs)[:400]}; "
              f"vCPU 0 kicks {out and {k: out['cpus'][0]['kicks'][k] for k in ('issued', 'in_host', 'stops', 'pending')}}, "
              f"requests {out and out['cpus'][0]['stats']['requests']}", flush=True)
        failures += [f'peer rep{r}: {d}' for d in diffs]
    # Forged stop PC on each vCPU of 4: it halts with bad_stop, the others are cancelled.
    prog = hot_program(4, a.iterations)
    for v in range(4):
        rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / f'forge{v}',
                              ['--kick-vcpu', v, '--kicks', 1, '--forge-stop', '--tamper-vcpu', v])
        stops = [(c['stop'], c['reason']) for c in out['cpus']] if out else None
        want_stops = [('resident', 'bad_stop status=1 detail=11') if w == v else ('cancelled', '') for w in range(4)]
        ok = rc == 0 and stops == want_stops and out['cpus'][v]['tampered']
        report['forge'].append({'vcpu': v, 'returncode': rc, 'stops': stops, 'native': out and out['cpus'][v]['native']})
        print(f'forged stop on vCPU {v}: {"ok" if ok else "FAIL"} {stops}', flush=True)
        if not ok: failures.append(f'forged stop on vCPU {v}: {rc} {stops} {err.strip()[-200:]}')
    (a.out / 'kicks.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def vm_fault(cpu):
    """natemu's guest exception as smp.parse_dump reports QEMU's: (vector, error, RIP, CR2), or None."""
    if cpu['stop'] != 'guest_fault': return None
    vector, cr2, error, rip = (smp.num(x) for x in cpu['fault'])
    return vector, error, rip, cr2 if vector == 14 else 0


def qemu_fault(f):
    """QEMU's fault record, with one declared correction: pinned QEMU reports a reserved-bit #PF (RSVD) with P = 0; the
    SDM (vol. 3 §4.7: the entry is present) and i4v_coherence_model.walk (`fault(9)`) have P = 1."""
    return f and (f[0], f[1] | 1 if f[0] == 14 and f[1] & 8 else f[1], *f[2:])


def vm_state(prog, out):
    """natemu's run in smp.parse_dump's shape."""
    memory = {smp.num(m['addr']): bytes.fromhex(m['data']) for m in out['cpus'][0]['memory']}
    return {'cpus': [{'gpr': [smp.num(x) for x in c['gpr']], 'rflags': smp.num(c['rflags']), 'fault': vm_fault(c)} for c in out['cpus']],
            'memory': [{'addr': smp.num(r['addr']), 'data': memory[smp.num(r['addr'])]} for r in prog['memory'] if r.get('capture', True)]}


def either_check(prog, out):
    """The PTE change without INVLPG (`sys-pte-no-invlpg`): each CPU's second read of V (r8) and V's final PTE in table A
    must be one of i4v_coherence_model's permitted histories (cached translation, or a fresh walk) after its first read."""
    import programs, i4v_coherence_model as cm
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
             'mov-from-cr2': 'mov rax, cr2', 'mov-to-cr8': 'xor eax, eax\nmov cr8, rax', 'cr3-pwt': 'mov rax, cr3\nbts rax, 3\nmov cr3, rax'}
    return {k: ref.assemble('mov rbx, 1\n' + v).hex() for k, v in cases.items()}


def cmd_system(a, host, runtime):
    """S3c1: system programs (tools/bench/smp/system.json) against QEMU system mode; fault cases against
    tools/system_fault_model.py at the state QEMU reaches just before the instruction (and i4v_modes_model where it covers
    them); the PTE change without INVLPG against i4v_coherence_model; refusals of unimplemented forms."""
    import system_fault_model as sfm, i4v_modes_model as modes
    programs = [p for p in json.loads((ROOT / 'tools/bench/smp/system.json').read_text()) if not a.programs or any(n in p['name'] for n in a.programs)]
    stub = smp.build_stub(a.out / 'qemu-build')
    report, failures = [], []

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
        model_profile = {'sysfault-efer-lme-clear@1': 25, 'sysfault-cr4-pae-clear@1': 27, 'sysfault-cr0-pg-clear@1': 28,
                         'sysfault-efer-reserved-1@1': 29, 'sysfault-efer-reserved-edx@1': 29}.get(name)
        if model_profile:
            last = modes.model(model_profile, 0, modes.data(model_profile, [0] * 0x400))['records'][-1]
            entry['modes_model'] = (model_profile, last['vector'])
            if last['vector'] != want['cpus'][0]['fault'][0]:
                failures.append(f"{name}: i4v_modes_model profile {model_profile} gives vector {last['vector']}")
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
            failures += [f"{name}{' (max-block 1)' if limit else ''}: {d}" for d in diffs]
        print(f"{name}: {'ok' if not any(r['diffs'] for r in entry['runs']) else 'FAIL'}"
              + (f"; QEMU full run: {entry['qemu']}" if 'qemu' in entry else '') + (f"; modes model {entry['modes_model']}" if 'modes_model' in entry else '')
              + (f"; history {entry['either']}" if 'either' in entry else ''), flush=True)
        report.append(entry)
    # Refusals: the vCPU stops `unsupported_system` at the instruction, after the prefix.
    for name, code in ([] if a.programs else unsupported_cases().items()):
        text = f'cpl 0\nrip 0x110000\ncode {code}\n'
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'work' / f'unsupported-{name}')
        ok = rc == 0 and out['stop'] == 'resident' and out['reason'].startswith('unsupported_system') and smp.num(out['gpr'][3]) == 1
        print(f'unsupported {name}: {"ok" if ok else "FAIL"} {out and (out["stop"], out["reason"], out["rip"])}', flush=True)
        report.append({'name': f'unsupported-{name}', 'ok': ok})
        if not ok: failures.append(f'unsupported {name}: {rc} {out and out["reason"]} {err.strip()[-200:]}')
    # A malformed system reply (driver `--tamper system`: length 16) is refused before anything of it runs.
    if not a.programs:
        prog = next(p for p in programs if p['name'] == 'sys-cr3-switch@1')
        rc, out, err = driver(host, runtime, a.fexc, smp_description(prog), a.out / 'work' / 'tamper-system', ['--tamper', 'system'])
        stops = out and [(c['stop'], c['reason'], c['tampered'], c['stats']['systems']) for c in out['cpus']]
        ok = rc == 0 and stops == [('resident', 'bad_reply status=1 detail=2', True, 0)]
        print(f'tampered system reply: {"ok" if ok else "FAIL"} {stops}', flush=True)
        report.append({'name': 'tamper-system', 'ok': ok, 'stops': stops})
        if not ok: failures.append(f'tampered system reply: {rc} {stops} {err.strip()[-200:]}')
    for f in failures: print(f)
    (a.out / 'system.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def cmd_coherence(a, host, runtime):
    programs = [p for p in json.loads((ROOT / 'tools/bench/smp/coherence.json').read_text()) if not a.programs or any(n in p['name'] for n in a.programs)]
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
                stats['stops_at_step'] = sum(c['kicks']['stops_at_step'] for c in out['cpus'])
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
    for f in failures: print(f)
    (a.out / 'coherence.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    return len(failures)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['corpora', 'smp', 'limits', 'kicks', 'system', 'coherence'])
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
    ap.add_argument('--programs', type=lambda s: s.split(','))
    ap.add_argument('--reps', type=int, default=1)
    ap.add_argument('--kicks', type=int, help='kicks per vCPU (kicks: default 10000; corpora: off unless given)')
    ap.add_argument('--iterations', type=int, default=400_000_000, help='hot-loop iterations per vCPU (kicks)')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    host, runtime = vm.build()
    if a.mode == 'kicks' and a.kicks is None: a.kicks = 10_000
    failed = {'corpora': cmd_corpora, 'smp': cmd_smp, 'limits': cmd_limits, 'kicks': cmd_kicks, 'system': cmd_system, 'coherence': cmd_coherence}[a.mode](a, host, runtime)
    print('failures:', failed)
    sys.exit(1 if failed else 0)


if __name__ == '__main__': main()
