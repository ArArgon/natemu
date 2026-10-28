#!/usr/bin/env python3
"""Page faults inside loops in the VM against a signal-based QEMU reference (slice 2, S2d).

    tools/with-env python3 tools/run_slice2_faults.py --fexc FEXC --out DIR [--seed N] [--sizes default 1 2] [--programs P.json ...]

Cases are program x mutation, never written per program. Programs: the control-flow corpus (tools/reference/gen_slice2.py)
and its stride programs, whose memory access crosses a page boundary at iteration k (also past the end of the stack
region, a fault without any mutation). Mutations: for each data page the program's regions cover, `unmap` (clear P) and
`readonly` (clear R/W), and none. The reference is a pinned-QEMU run of the same bytes with the same page made absent
(`munmap`) or read-only (`mprotect`) under a SIGSEGV handler (tools/reference/ref.py "fault mode"): QEMU raises its
precise fault inside the iteration, and the handler stores the context it delivers (GPRs, RIP, EFLAGS, error code, fault
address) and the memory, so the expected precise state comes from QEMU, not from prefix runs. Where the mutated page is
not touched (or not written) QEMU runs to the end, and so must the VM.

The VM must stop with a guest fault at QEMU's RIP, with GPRs, masked RFLAGS and memory equal to QEMU's (the state the
signal delivers) and the FEX frame intact, and the exception record natemu's policy gives (tools/run_slice1_faults.py: the
instruction QEMU faulted on, its registers and memory, the mutated page layout): vector 14, CR2 = the first byte of the
faulting access in the faulting page (one byte for a bt-family memory form; a split access reports the next page's start), error
code U | W (write intent, also the load of a read-modify-write) | P (page present). QEMU's report is cross-checked against
the policy: its fault address is in the same page, its error code claims a write only for a write intent. (QEMU reports a
read-modify-write's load as a read and a never-mapped page as a protection fault, so its error code is not the reference.) Writes DIR/faults.json;
exit status 1 on any failure."""
import argparse, json, os, pathlib, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref, gen_slice2 as gen, run_slice1_vm as vm, run_slice1_faults as F  # noqa: E402

PAGE = 4096
PAGES = [gen.DATA, gen.DATA + PAGE, gen.STACK, gen.STACK + PAGE]


def qemu_path():
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    return raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'


def cases(programs):
    """(name, program with `faults`, [(mode, page)]) for every program and mutation."""
    out = []
    for p in programs:
        pages = [pg for pg in PAGES if any(ref.num(m['addr']) <= pg < ref.num(m['addr']) + len(m['data']) // 2 for m in p['memory'])]
        out.append((p['name'] + '/none', p, []))
        out += [(f"{p['name']}/{mode}-{pg:#x}", p, [(mode, pg)]) for pg in pages for mode in ('unmap', 'readonly')]
    return out


def accesses(text, regs, next_rip):
    """The data accesses of one instruction (tools/run_slice1_faults.py `events`) plus the control transfers it does not model:
    a direct `call` pushes; other direct branches access nothing."""
    m, ops = text.split()[0], F.split_operands(text)
    direct = bool(ops) and not (F.MEM.fullmatch(ops[0]) or ops[0] in ref.R64)
    if m == 'call' and direct: return [('access', (regs['rsp'] - 8) & F.M64, 8, True, True)]
    if direct and (m == 'jmp' or m.startswith('j') or m.startswith('loop')): return []
    return F.events(text, regs, next_rip)


def expected_fault(p, muts, want):
    """(vector, CR2, error code) natemu's policy gives for the instruction QEMU faulted on (its RIP, registers and memory at the
    fault), in the mutated page layout; consistency with what QEMU reported is returned as `problems`."""
    q = want['fault']
    rip = ref.num(want['qemu']['rip'])
    insns = [l for l in p['asm'] if not l.endswith(':')]
    i = p['offsets'].index(rip - ref.num(p['rip']))
    ends = p['offsets'][1:] + [len(p['code']) // 2]
    evs = accesses(insns[i], F.state(want), rip + ends[i] - p['offsets'][i])
    layout = F.layout(p, {mode: [pg] for mode, pg in muts})
    got = F.first_fault(evs, layout, F.reader(want))
    problems = []
    if not got: return None, [f'QEMU faulted at "{insns[i]}" but the policy sees no fault']
    vector, cr2, error = got
    # QEMU's fault address lies in the faulting page; its error code agrees where it is exact: a write only for a write
    # intent (QEMU's read-modify-write loads report a read), not present when the page is unmapped
    if ref.num(q['si_addr']) >> 12 != cr2 >> 12: problems.append(f"QEMU fault address {q['si_addr']} is not in the policy's page of {cr2:#x}")
    if q['err'] & 2 and not error & 2: problems.append(f"QEMU reports a write, policy {error:#x}")
    if not q['err'] & 4: problems.append('QEMU reports a supervisor access')
    return got, problems


def compare_fault(expected, out):
    """Differences between the policy's (vector, CR2, error) and the VM's stop `out`."""
    diffs = [] if out['stop'] == 'guest_fault' else [f"stopped {out['stop']} {out['reason']} at {out['rip']}"]
    vector, cr2, error, rip = (ref.num(x) for x in out['fault'])
    if (vector, cr2, error) != tuple(expected): diffs.append(f'fault {(vector, hex(cr2), hex(error))} vs expected {(expected[0], hex(expected[1]), hex(expected[2]))}')
    return diffs


def reference(jobs, work):
    """QEMU's result for each job: the fault context its SIGSEGV delivers (`fault`: CR2, error code) and the precise state at
    the fault. Phase 1 runs the job in fault mode (single-stepped, the page made absent or read-only): the faulting RIP, the
    executed path and the context. QEMU's signal context is exact for registers and memory but not always for flags a `cmp`
    left lazily computed (a faulting `setcc` store after a `cmp` reports stale AF or CF, even single-stepped), so phase 2 runs
    the straight-line program with the effects of the executed path up to the fault (gen_slice2.unrolled) to its end, where the
    capture is exact; its state is the expected one. A phase-1 context that differs from it in more than flags would be a QEMU
    defect and is returned. Returns (results, [(case, differing parts)])."""
    first, second = [], []
    for i in range(0, len(jobs), 200):      # batches bound the disk the QEMU logs take
        first += ref.run_programs(jobs[i:i + 200], work, qemu_path())[0]
        shutil.rmtree(work / 'jobs')
    exact = [dict(gen.unrolled(j, r['path_ix']), name=j['name'] + '/path', faults={'protect': []}) for j, r in zip(jobs, first) if r.get('fault')]
    for i in range(0, len(exact), 200):
        second += ref.run_programs(exact[i:i + 200], work, qemu_path())[0]
        shutil.rmtree(work / 'jobs')
    second, results, diffs = iter(second), [], []
    for j, r in zip(jobs, first):
        if r.get('fault'):
            t = next(second)
            q1, q2 = r['qemu'], dict(t['qemu'], rip=r['qemu']['rip'])
            changed = (ref.num(q1['rflags']) ^ ref.num(q2['rflags'])) & ref.num(t['rflags_compare_mask'])
            wrong = [k for k in ('gpr', 'memory') if q1[k] != q2[k]] + ['rflags'] * (changed != 0)
            if wrong: diffs.append((j['name'], wrong))
            r = dict(r, qemu=q2, rflags_compare_mask=t['rflags_compare_mask'], undefined_flags=t['undefined_flags'])
        results.append(r)
    return results, diffs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261027)
    ap.add_argument('--count', type=int, default=64)
    ap.add_argument('--sizes', nargs='+', default=['default', '1', '2'])
    ap.add_argument('--programs', type=pathlib.Path, nargs='*', help='program files to use instead of the generated corpus and strides')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    programs = [p for f in a.programs for p in json.loads(f.read_text())['programs']] if a.programs else \
        gen.corpus(a.seed, a.count) + gen.stride_corpus(a.seed)
    plan = cases(programs)
    jobs = [dict(p, name=name, faults={'protect': [{'addr': pg, 'mode': mode} for mode, pg in muts]}) for name, p, muts in plan]
    results, signal_diffs = reference(jobs, a.out / 'ref')
    faulted = sum(1 for r in results if r.get('fault'))
    print(f'{len(plan)} cases ({len(programs)} programs); QEMU faulted in {faulted}, ran to the end in {len(plan) - faulted}', flush=True)
    print(f'QEMU signal context differs from the exact path state in {len(signal_diffs)} cases (flags only): {signal_diffs[:12]}', flush=True)
    assert all(set(d) <= {'rflags'} for _, d in signal_diffs), 'QEMU signal context differs from the exact path state in more than flags'
    host, runtime = vm.build()
    report, failures = [], 0
    for size in a.sizes:
        counts, kinds = {'match': 0, 'unsupported': 0, 'fail': 0}, {'fault': 0, 'end': 0}
        for n, ((name, p, muts), want) in enumerate(zip(plan, results)):
            work = a.out / 'work' / size / f'c{n:04d}'
            work.mkdir(parents=True, exist_ok=True)
            (work / 'program.txt').write_text(vm.description(p) + ''.join(f'{mode} {pg:#x}\n' for mode, pg in muts))
            import subprocess
            run = subprocess.run([host, runtime, 'run-program', a.fexc, work / 'program.txt', work] + ([] if size == 'default' else [size]),
                                 capture_output=True, text=True)
            entry = {'case': name, 'size': size, 'qemu_fault': want.get('fault'), 'qemu_executed': want.get('executed_insns'), 'returncode': run.returncode, 'stderr': run.stderr[-500:]}
            if run.returncode:
                entry['outcome'], diffs = 'fail', ['host error']
            else:
                out = json.loads(run.stdout)
                entry['vm'] = {k: out[k] for k in ('stop', 'reason', 'rip', 'rflags', 'gpr', 'fault', 'stats', 'frame_intact')}
                q = want['qemu']
                limit = vm.declared_limit(out)
                if limit:
                    entry['outcome'], diffs = 'unsupported', [limit]
                else:
                    if want.get('fault'):
                        expected, diffs = expected_fault(p, muts, want)
                        if expected: diffs += compare_fault(expected, out)
                    else:
                        diffs = [] if out['stop'] == 'reached' else [f"QEMU ran to the end; VM stopped {out['stop']} {out['reason']} at {out['rip']}"]
                    diffs += ref.compare(want, [ref.num(v) for v in out['gpr']], ref.num(out['rflags']), ref.num(out['rip']), out['memory'])
                    if not out['frame_intact']: diffs.append('FEX frame changed')
                    entry['outcome'] = 'fail' if diffs else 'match'
                    kinds['fault' if want.get('fault') else 'end'] += entry['outcome'] == 'match'
            entry['diffs'] = diffs
            counts[entry['outcome']] += 1
            report.append(entry)
            if entry['outcome'] != 'match':
                print(f"[{size}] {name}: {entry['outcome']} qemu={want.get('fault')} {'; '.join(diffs)[:300]} {entry['stderr'].strip()[-200:]}", flush=True)
        deep = sum(1 for e in report if e['size'] == size and e['qemu_fault'] and e['qemu_executed'] > 50)
        print(f'[{size}] {counts} matched faults {kinds["fault"]} ({deep} after more than 50 executed instructions), matched runs to the end {kinds["end"]}', flush=True)
        failures += counts['fail'] + counts['unsupported']
    (a.out / 'faults.json').write_text(json.dumps({'results': report}, indent=1) + '\n')
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
