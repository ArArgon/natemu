#!/usr/bin/env python3
"""The computed-exit census under QEMU (docs/restructure/el0-sampler-plan.md, chunk 2): a benchmark boot under the plugin
QEMU (testkit/artifacts.py qemu_plugins) with the census plugin (tests/bench/plugins/indirect.c, census_plugin), and
the plugin's own check. Counts, not timings: nothing here is timed, and other VMs on the machine do not matter.

    python3 tests/bench/run_census.py guest --qemu QEMU --plugin SO --out DIR --name NAME --bench BENCH --kernel BZIMAGE
                                            --initrd CPIO [--vcpus 1] [--cap SECONDS] [--append WORDS]
    python3 tests/bench/run_census.py io5 --qemu QEMU --plugin SO --out DIR --inputs DIR [--vcpus 4] [--ram 8]
                                          [--budget SECONDS]
    python3 tests/bench/run_census.py check --qemu QEMU --plugin SO --out DIR

guest: tools/bench/run.py's QEMU boot (`--emu qemu-multi`: its board and its like-for-like CPU model QCPU, so glibc picks
the string functions it picks under natemu), unchanged, with QEMU started through a wrapper that adds `-plugin`. The
plugin's segments follow the serial console: boot, run1 (NATEMU-BENCH-START to END), between, run2 (START2 to END2),
tail. Writes OUT/NAME.census.json (the plugin's) and the report of it (census_report.py) as OUT/NAME.census.md and
OUT/NAME.census-report.json. Passes when run 1 was verified and its segment is complete.

io5: tests/io/run_io5.py's QEMU boot of the 32-bit toolchain build (`run --backend qemu --i386 --mitigations-off`, as case
io5-solo32-qemu), unchanged, with the same wrapper; the segments follow its step markers (configure, make, i386-setup,
i386-configure, i386-make), and the boot is stopped (SIGTERM, so QEMU exits and the plugin writes its counts) after
i386-make or after --budget seconds (default 2700, io5-solo32-qemu's bound), whichever comes first. run_io5's own
verdict is not used (it is stopped early); passes when the configure segment is complete (its step ok).

check: tests/bench/plugins/census_check.asm assembled with nasm, run under the plugin QEMU, and the plugin's counts for
it compared with what the program does (EXPECTED; addresses from nasm's map file). Exit 0 when every count matches."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
SERIAL_SEGMENTS = (('NATEMU-BENCH-START ', 'run1'), ('NATEMU-BENCH-END ', 'between'),
                   ('NATEMU-BENCH-START2 ', 'run2'), ('NATEMU-BENCH-END2 ', 'tail'))
# io5 (--i386) markers that end one step, and the segment that starts there (the steps run back to back).
IO5_SEGMENTS = (('IO5 begin ', 'setup'), ('IO5 step fetch-python ', 'configure'), ('IO5 step configure ', 'make'),
                ('IO5 step make ', 'i386-setup'), ('IO5 i386-userland ', 'i386-configure'),
                ('IO5 step i386-configure ', 'i386-make'), ('IO5 step i386-make ', 'tail'))


def wrapper(out: pathlib.Path, qemu: str, plugin: str) -> pathlib.Path:
    """A qemu-system-x86_64 that is the plugin QEMU with the census plugin: each start writes OUT/census-PID.json and
    reads segment names from OUT/census.ctl; its pid goes to OUT/census.pid. (A --version call loads no plugin.) Once
    OUT/census.stop exists it starts nothing (run_io5's verify boot after a stopped first boot)."""
    w = out / 'qemu-system-x86_64'
    (out / 'census.ctl').write_text('')
    w.write_text('#!/bin/sh\n'
                 f'[ -e {shlex.quote(str(out / "census.stop"))} ] && {{ echo "census: stopped; no further boots" >&2; exit 1; }}\n'
                 f'echo $$ > {shlex.quote(str(out / "census.pid"))}\n'
                 f'exec {shlex.quote(qemu)} -plugin {shlex.quote(plugin)},out={shlex.quote(str(out))}/census-$$.json,'
                 f'ctl={shlex.quote(str(out / "census.ctl"))} "$@"\n')
    w.chmod(0o755)
    return w


class Follower(threading.Thread):
    """Appends a segment name to OUT/census.ctl at each marker (`marks`: (text, segment), in order) as it appears in
    `path` (a file another process writes)."""
    def __init__(self, path: pathlib.Path, ctl: pathlib.Path, marks, on_segment=None):
        super().__init__(daemon=True)
        self.path, self.ctl, self.marks, self.on_segment = path, ctl, list(marks), on_segment
        self.seen, self.stop = [], threading.Event()

    def run(self):
        while not self.stop.is_set() and self.marks:
            try: text = self.path.read_text(errors='replace')
            except FileNotFoundError: text = ''
            text = text[:text.rfind('\n') + 1]
            while self.marks and self.marks[0][0] in text:
                mark, seg = self.marks.pop(0)
                with open(self.ctl, 'a') as f: f.write(seg + '\n')
                self.seen.append((time.time(), seg))
                print(f'census: segment {seg} ({mark.strip()})', flush=True)
                if self.on_segment: self.on_segment(seg)
            self.stop.wait(0.05)


def census_file(out: pathlib.Path) -> pathlib.Path | None:
    """The plugin's JSON of the boot (the largest, if QEMU started more than once)."""
    found = sorted(out.glob('census-*.json'), key=lambda p: p.stat().st_size)
    return found[-1] if found else None


def report(census: pathlib.Path, out: pathlib.Path, name: str, window: str) -> dict:
    import census_report
    data = json.loads(census.read_text())
    r = census_report.reduce(data, window)
    (out / f'{name}.census-report.json').write_text(json.dumps(r, indent=1) + '\n')
    (out / f'{name}.census.md').write_text(census_report.markdown(r, name))
    print(census_report.markdown(r, name), flush=True)
    return r


def guest(a) -> int:
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    w = wrapper(out, a.qemu, a.plugin)
    follow = Follower(out / f'{a.name}.serial', out / 'census.ctl', SERIAL_SEGMENTS)
    follow.start()
    cmd = [sys.executable, str(ROOT / 'tools/bench/run.py'), '--out', str(out), '--name', a.name, '--emu', 'qemu-multi',
           '--vcpus', str(a.vcpus), '--bench', a.bench, '--kernel', a.kernel, '--initrd', a.initrd, '--qemu', str(w),
           '--cap', str(a.cap), *(['--append', a.append] if a.append else [])]
    print('census:', shlex.join(cmd), flush=True)
    rc = subprocess.run(cmd).returncode
    follow.stop.set(); follow.join()
    f = census_file(out)
    if f is None: print('census: the plugin wrote no counts', flush=True); return 1
    f.rename(out / f'{a.name}.census.json'); f = out / f'{a.name}.census.json'
    jl = out / 'bench.jsonl'
    rec = [json.loads(l) for l in jl.read_text().splitlines() if l.strip()][-1] if jl.exists() else {}
    segs = [s['name'] for s in json.loads(f.read_text())['segments']]
    report(f, out, a.name, 'run1')
    problems = []
    if rc not in (0, 3): problems.append(f'tools/bench/run.py exit {rc}')    # 3: another VM ran meanwhile (counts do not care)
    if not rec.get('verified1'): problems.append('run 1 not verified')
    if 'between' not in segs: problems.append(f'run 1 did not end within the cap (segments {segs})')
    print('census:', 'PASS' if not problems else f'FAIL {problems}', flush=True)
    return 0 if not problems else 1


def io5(a) -> int:
    import run_io5
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    w = wrapper(out, a.qemu, a.plugin)
    t0 = time.time()

    def stop_qemu():
        (out / 'census.stop').touch()
        try: pid = int((out / 'census.pid').read_text())
        except (OSError, ValueError): return
        print(f'census: stopping QEMU (pid {pid}) after {time.time() - t0:.0f} s', flush=True)
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: return
        for _ in range(600):                                  # the plugin writes its counts as QEMU exits
            try: os.kill(pid, 0)
            except ProcessLookupError: return
            time.sleep(0.1)

    follow = Follower(out / 'io5' / 'markers-use.tsv', out / 'census.ctl', IO5_SEGMENTS,
                      on_segment=lambda seg: seg == 'tail' and threading.Thread(target=stop_qemu, daemon=True).start())
    follow.start()
    budget = threading.Timer(a.budget, stop_qemu); budget.daemon = True; budget.start()
    run_io5.QEMU = str(w)                                     # run_io5's QEMU backend starts this binary
    argv = ['run', '--backend', 'qemu', '--inputs', a.inputs, '--out', str(out / 'io5'), '--vcpus', str(a.vcpus),
            '--ram', str(a.ram), '--mitigations-off', '--i386', '--limit', str(a.budget + 120), '--lock', str(out / 'vm.lock')]
    print('census: run_io5.py', shlex.join(argv), flush=True)
    sys.argv = ['run_io5.py', *argv]
    rc = run_io5.main()
    budget.cancel(); follow.stop.set(); follow.join()
    print(f'census: run_io5 exit {rc} (not the verdict: the boot is stopped early)', flush=True)
    f = census_file(out)
    if f is None: print('census: the plugin wrote no counts', flush=True); return 1
    f.rename(out / 'io5.census.json'); f = out / 'io5.census.json'
    segs = [s['name'] for s in json.loads(f.read_text())['segments']]
    for window in ('configure', 'make', 'i386-configure', 'i386-make'):
        nxt = {'configure': 'make', 'make': 'i386-setup', 'i386-configure': 'i386-make', 'i386-make': 'tail'}[window]
        if nxt in segs: report(f, out, f'io5-{window}', window)
    markers = (out / 'io5' / 'markers-use.tsv').read_text() if (out / 'io5' / 'markers-use.tsv').exists() else ''
    problems = []
    if 'make' not in segs: problems.append(f'the configure step did not end within {a.budget} s (segments {segs})')
    elif not re.search(r'IO5 step configure ok', markers): problems.append('the configure step failed')
    print('census:', 'PASS' if not problems else f'FAIL {problems}', flush=True)
    return 0 if not problems else 1


# --- check ---

# (kind, mode, site label, alias added to the site, {target label (+ alias): count}, overflow, flags[, address space])
# in segment "program". Labels are census_check.asm's; KBASE means the label's kernel alias. Flags: the plugin's F_* bits.
# The address space is the label of the page table whose CR3 the site ran under: pml4 for user sites unless given,
# none (0) for kernel ones.
F = dict(mem=1, rex=2, opsize=4, notrack=8, seg=16, rep=32, imm=64)
EXPECTED_SITES = [
    ('jmp', 'user', 'L_kjmp', 0, {('kcode', 'KBASE'): 1}, 0, 0),
    ('call', 'kernel', 'K_call', 'KBASE', {('kfunc', 'KBASE'): 7}, 0, 0),
    ('ret', 'kernel', 'kfunc', 'KBASE', {('K_call_next', 'KBASE'): 7}, 0, 0),
    ('jmp', 'kernel', 'K_back', 'KBASE', {('low_cont', 0): 1}, 0, 0),
    ('call', 'user', 'A_call', 0, {('f1', 0): 1000}, 0, 0),
    ('ret', 'user', 'f1', 0, {('A_next', 0): 1000}, 0, 0),
    ('call', 'user', 'B_call', 0, {('g1', 0): 300, ('g2', 0): 300}, 0, F['mem']),
    ('ret', 'user', 'g1', 0, {('B_next', 0): 300}, 0, 0),
    ('ret', 'user', 'g2', 0, {('B_next', 0): 300}, 0, 0),
    ('jmp', 'user', 'C_jmp', 0, {('t0', 0): 100, ('t1', 0): 100, ('t2', 0): 100, ('t3', 0): 100}, 100, 0),
    ('ret', 'user', 'rec_done', 0, {('rec_after', 0): 110, ('D_next', 0): 10}, 0, 0),
    ('call', 'user', 'E1', 0, {('f3', 0): 5}, 0, F['notrack']),
    ('ret', 'user', 'f3', 0, {('E1_next', 0): 5}, 0, F['rep']),
    ('call', 'user', 'E2', 0, {('f4', 0): 4}, 0, F['mem'] | F['rex']),
    ('ret', 'user', 'f4', 0, {('E2_next', 0): 4}, 0, F['rep']),
    ('call', 'user', 'E3', 0, {('f5', 0): 3}, 0, F['mem'] | F['seg']),
    ('ret', 'user', 'f5', 0, {('E3_next', 0): 3}, 0, F['imm']),
    ('jmp', 'user', 'E4', 0, {('e_after', 0): 1}, 0, F['mem']),
    ('call', 'user', 'G1', 0, {('f6', 0): 2}, 0, 0),
    ('ret', 'user', 'f6', 0, {('G1_next', 0): 2}, 0, 0),
    ('call', 'user', 'G2', 0, {('f6', 'ALIAS'): 3}, 0, 0),
    ('ret', 'user', 'f6', 'ALIAS', {('G2_next', 0): 3}, 0, 0),
    ('call', 'user', 'H_call', 0, {('f7', 'HALIAS'): 1}, 0, 0),
    ('ret', 'user', 'f7', 'HALIAS', {('H_next', 0): 1}, 0, 0),
    ('jmp', 'user', 'I_jmp', 0, {('I_target', 0): 1}, 0, F['mem']),
    ('ret', 'kernel', 'krec_done', 'KBASE', {('krec_done', 'KBASE'): 18, ('pf_after', 'KBASE'): 2}, 0, 0),
    ('call', 'user', 'G3', 0, {('f9', 0): 1}, 0, 0),
    ('ret', 'user', 'f9_ret', 0, {('G3_next', 0): 1}, 0, 0),
    ('call', 'user', 'G4', 0, {('f9', 'ALIAS'): 2}, 0, 0),
    ('ret', 'user', 'f9_ret', 'ALIAS', {('G4_next', 0): 2}, 0, 0),
    ('call', 'user', 'C32_call', 0, {('f32', 0): 3}, 0, 0),
    ('ret', 'user', 'f32', 0, {('C32_next', 0): 3}, 0, 0),
    ('call', 'user', 'X_call', 0, {('f8', 0): 1}, 0, 0, 'pml4'),
    ('ret', 'user', 'f8', 0, {('X_next', 0): 1}, 0, 0, 'pml4'),
    ('call', 'user', 'X_call', 0, {('f8', 0): 1}, 0, 0, 'pml4b'),
    ('ret', 'user', 'f8', 0, {('X_next', 0): 1}, 0, 0, 'pml4b'),
]
# Return model (user): A 1000 + B 600 + E 12 + G 5 + H 1 + G' 3 + K 3 + J 2 pairs hit at every depth; D's 10 recursions of 12 hit 8 and
# find the depth-8 stack empty 4 times, and hit 12 at depths 16 and 32. Kernel: the kernel phase's 7, and in each of
# the two page faults a 10-deep recursion: 8 hits and 2 empty at depth 8, 10 hits at 16 and 32. H's user return comes
# after the fault's kernel calls: with one stack, depth 8 finds it empty; with user and kernel apart (ras_split) it hits.
_K8, _K16, _U = {'hit': 7 + 16, 'miss': 0, 'empty': 4}, {'hit': 7 + 20, 'miss': 0, 'empty': 0}, 1626
EXPECTED_RAS = {'8': {'user': {'hit': _U + 80 - 1, 'miss': 0, 'empty': 41}, 'kernel': _K8},
                '16': {'user': {'hit': _U + 120, 'miss': 0, 'empty': 0}, 'kernel': _K16},
                '32': {'user': {'hit': _U + 120, 'miss': 0, 'empty': 0}, 'kernel': _K16}}
EXPECTED_RAS_SPLIT = {'8': {'user': {'hit': _U + 80, 'miss': 0, 'empty': 40}, 'kernel': _K8},
                      '16': {'user': {'hit': _U + 120, 'miss': 0, 'empty': 0}, 'kernel': _K16},
                      '32': {'user': {'hit': _U + 120, 'miss': 0, 'empty': 0}, 'kernel': _K16}}
EXPECTED_COUNTS = {'direct_calls': {'user': 120, 'kernel': 20},
                   'far': {'call': {'user': 2, 'kernel': 0}, 'jmp': {'user': 1, 'kernel': 0}, 'ret': {'user': 3, 'kernel': 0}},
                   'events': {'interrupts': 4, 'exceptions': 4, 'dropped': 1, 'unresolved': 0, 'wide': 0}}


def symbols(map_text: str) -> dict:
    """NASM map file: {name: value} from its value lines (constants and labels)."""
    out = {}
    for line in map_text.splitlines():
        m = re.match(r'^\s*([0-9A-F]+)\s+(?:([0-9A-F]+)\s+)?([A-Za-z_][\w.]*)\s*$', line)
        if m: out[m.group(3)] = int(m.group(2) or m.group(1), 16)
    return out


def check_problems(data: dict, sym: dict) -> list[str]:
    """What differs between the plugin's segment "program" and EXPECTED_* (empty: all match)."""
    seg = next((s for s in data['segments'] if s['name'] == 'program'), None)
    if seg is None: return [f'no segment "program" (segments {[s["name"] for s in data["segments"]]})']
    addr = lambda label, alias: sym[label] + (sym[alias] if alias else 0)
    lo, hi = sym['mb_header'], sym['image_end']
    inside = lambda pc: lo <= pc < hi or lo <= pc - sym['KBASE'] < hi or any(lo <= pc - sym[a] < hi for a in ('ALIAS', 'HALIAS'))
    got = {(s[0], s[1], s[2], s[3]): s for s in seg['sites'] if inside(s[3])}
    problems = []
    if data.get('cr3') != 'read' or data.get('rip') != 'read': problems.append(f'registers: cr3 {data.get("cr3")}, rip {data.get("rip")}')
    want = set()
    for kind, mode, label, alias, targets, overflow, flags, *space in EXPECTED_SITES:
        pc = addr(label, alias)
        asid = sym[space[0] if space else 'pml4'] & ~0x1fff if mode == 'user' else 0
        want.add((kind, mode, asid, pc))
        s = got.get((kind, mode, asid, pc))
        if s is None:
            others = [hex(k[2]) for k in got if k[:2] == (kind, mode) and k[3] == pc]
            problems.append(f'{kind} at {label}{"+" + alias if alias else ""} ({pc:#x}, {mode}, asid {asid:#x}): no site'
                            + (f' (only in address spaces {others})' if others else '')); continue
        _, _, s_asid, _, _, s_flags, execs, s_over, s_tg = s
        tg = {t: c for t, c in s_tg}
        w = {addr(t, al): c for (t, al), c in targets.items()}
        if (execs, s_over, tg, s_flags) != (sum(w.values()) + overflow, overflow, w, flags):
            problems.append(f'{kind} at {label}: execs {execs} overflow {s_over} flags {s_flags} targets '
                            f'{ {hex(k): v for k, v in tg.items()} }; expected {sum(w.values()) + overflow} {overflow} {flags} '
                            f'{ {hex(k): v for k, v in w.items()} }')
    for k in sorted(set(got) - want, key=lambda k: k[3]):
        problems.append(f'unexpected site {k[0]} {k[1]} at {k[2]:#x}: {got[k]}')
    if seg['ras'] != EXPECTED_RAS: problems.append(f'return model {seg["ras"]}, expected {EXPECTED_RAS}')
    if seg['ras_split'] != EXPECTED_RAS_SPLIT:
        problems.append(f'split return model {seg["ras_split"]}, expected {EXPECTED_RAS_SPLIT}')
    for key, w in EXPECTED_COUNTS.items():
        if seg[key] != w: problems.append(f'{key} {seg[key]}, expected {w}')
    if not seg['insns']['kernel'] or not seg['insns']['user']: problems.append(f'instructions {seg["insns"]}')
    return problems


def check(a) -> int:
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['nasm', '-w-prefix-seg', '-f', 'bin', str(HERE / 'plugins/census_check.asm'), '-o', 'census_check.bin'],
                   cwd=out, check=True)
    sym = symbols((out / 'census_check.map').read_text())
    census = out / 'census_check.json'
    cmd = [a.qemu, '-plugin', f'{a.plugin},out={census},mark={sym["long_entry"]:x}:program',
           '-accel', 'tcg,thread=single', '-cpu', 'max', '-smp', '1', '-m', '64', '-machine', 'pc', '-display', 'none',
           '-nodefaults', '-monitor', 'none', '-serial', 'none', '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04',
           '-no-reboot', '-kernel', str(out / 'census_check.bin')]
    print('census check:', shlex.join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 1: print(f'census check: QEMU exit {r.returncode} (1 is the program\'s exit): {r.stderr[-800:]}'); return 1
    if not census.exists(): print('census check: the plugin wrote no counts'); return 1
    problems = check_problems(json.loads(census.read_text()), sym)
    for p in problems: print('census check:', p)
    (out / 'results.json').write_text(json.dumps({'pass': not problems, 'errors': problems}, indent=1) + '\n')
    print('census check:', 'PASS' if not problems else f'FAIL ({len(problems)} differences)', flush=True)
    return 0 if not problems else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('guest', 'io5', 'check'):
        p = sub.add_parser(name)
        for opt in ('--qemu', '--plugin', '--out'): p.add_argument(opt, required=True)
    g = sub.choices['guest']
    for opt in ('--name', '--bench', '--kernel', '--initrd'): g.add_argument(opt, required=True)
    g.add_argument('--vcpus', type=int, default=1); g.add_argument('--cap', type=float, default=3000); g.add_argument('--append', default='')
    i = sub.choices['io5']
    i.add_argument('--inputs', required=True); i.add_argument('--vcpus', type=int, default=4); i.add_argument('--ram', type=int, default=8)
    i.add_argument('--budget', type=int, default=2700)
    a = ap.parse_args()
    return {'guest': guest, 'io5': io5, 'check': check}[a.cmd](a)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
