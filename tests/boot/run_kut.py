#!/usr/bin/env python3
"""kvm-unit-tests a2891d95's smptest and dummy on the references (docs/restructure/real-code-suites.md).

    tests/boot/run_kut.py --backend qemu|kvm [--tests smptest dummy] [--cpus 1 2 3 4] --out DIR
    tests/boot/run_kut.py --backend natemu --fexc FEXC [--tests smptest dummy] [--cpus 1 2 3 4] --out DIR

The images come from the framework's build (NATEMU_INPUT_KVM_UNIT_TESTS, tests/boot/built_inputs.py: x86/<test>.flat
and provenance.json). Each runs as kvm-unit-tests runs it (x86/run), here as natemu will: Multiboot v1 through QEMU's
-kernel (machine pc, 128 MiB), its environment as the one module (-initrd, the text `NR_CPUS=<N>`: natemu's board has
no fw_cfg, from which the tests otherwise read the CPU count), COM1 into a file, isa-debug-exit at F4H (the test's exit
writes its status there: QEMU exits with (status << 1) | 1, so 1 is a pass), N CPUs (-smp N).
Backends: qemu, the pinned QEMU 11.1.2 (NATEMU_QEMU_SYSTEM) under TCG, CPU `max,-pcid,-smap` (natemu's model, as the smp
system programs); kvm, the x86 reference machine NATEMU_X86_HOST under KVM, -cpu host,-pku (tests/boot/kvm_ref.py), every
(test, N) in one session.
The answer of one boot (`answer`): QEMU's exit status and COM1's lines, CRs dropped, sorted (the CPUs print
concurrently, so their order varies from run to run). Its key: the test, N, the module's text, the backend's pin and
QEMU's arguments, and the image's upstream (provenance.json: the source's pin, the recipe's sha256, the builder image),
never the built image's hash. Judged by `judge`: the test's own verdict and its CPUs.
Also, per image as built (`code`): its 16- and 32-bit code the boot runs (CODE: the Multiboot entry, the 16-bit SIPI
trampoline, prepare_64 with enter_long_mode, ap_start32), found by its symbols and disassembled by ndisasm, uses only
the boot-time interpreter's first-version instructions (tests/boot/first_version.py; the 5-level switch is not run).
Answers (tools/reference/answers.py, NATEMU_ANSWERS): seed runs the backend and records each answer; locked never runs
it: the lock's answer must pass the judge; live (unset) runs it.
natemu (real-code-suites.md, "The natemu cases"): natemu's program driver boots the image (`boot-linux`: its Multiboot
loader, the module, 128 MiB, N vCPUs whose APs wait for INIT and SIPI, `--debug-exit`, COM1 into a file); the answer
is natemu's debug-exit status and COM1's lines, judged by `judge` and equal in its lines to the locked KVM answer. Writes DIR/results.json (one result per test and N, and
per image's code) and DIR/kut.json (the images' provenance and code, the answers, the reference machine); exit 1 on a
failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, os, pathlib, re, shlex, struct, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools' / 'bench' / 'smp'), str(ROOT / 'tools' / 'reference')]
import smp, answers, kvm_ref  # noqa: E402
from first_version import disassemble, refused  # noqa: E402

TESTS = ('smptest', 'dummy')
CPUS = (1, 2, 3, 4)
MEM_MB = 128
TCG_CPU = smp.SYSTEM_CPU
QEMU_TIMEOUT = 120                      # seconds per boot (a pass takes a second or two)
INPUT = 'NATEMU_INPUT_KVM_UNIT_TESTS'
# The 16- and 32-bit code a boot runs (x86/cstart64.S, x86/trampolines.S): (symbol, bits, where it runs, its last
# instruction). The trampoline runs copied to physical 0 (lib/x86/smp.c bringup_aps: SIPI vector 0).
CODE = (('start', 32, 'P32: the Multiboot entry, to its far jump to 64-bit code (C32 after prepare_64)', r'jmp (word )?0x\w+:(word |dword )?0x\w+'),
        ('sipi_entry', 16, 'R, P16: the SIPI trampoline, to its far jump', r'jmp (word )?0x\w+:(word |dword )?0x\w+'),
        ('prepare_64', 32, 'P32, then C32 after CR0.PG: the GDT, segments, PAE, CR3, EFER.LME, PG; near RET', r'ret'),
        ('ap_start32', 32, 'P32: an AP after the trampoline, to its far RET into 64-bit code (C32 after prepare_64)', r'retf'))


def elf_symbols(data: bytes) -> dict:
    """{name: address} of an ELF32 image's symbol table."""
    if data[:5] != b'\x7fELF\x01': raise SystemExit('not an ELF32 image')
    shoff, = struct.unpack_from('<I', data, 0x20)
    shentsize, shnum = struct.unpack_from('<HH', data, 0x2E)
    secs = [struct.unpack_from('<IIIIIIIIII', data, shoff + i * shentsize) for i in range(shnum)]
    out = {}
    for name, typ, flags, addr, off, size, link, info, align, entsize in secs:
        if typ != 2: continue                   # SHT_SYMTAB
        stroff = secs[link][4]
        for k in range(size // 16):
            st_name, value = struct.unpack_from('<II', data, off + k * 16)
            end = data.index(b'\0', stroff + st_name)
            if st_name: out[data[stroff + st_name:end].decode()] = value
    return out


def elf_bytes(data: bytes, addr: int, n: int) -> bytes:
    shoff, = struct.unpack_from('<I', data, 0x20)
    shentsize, shnum = struct.unpack_from('<HH', data, 0x2E)
    for i in range(shnum):
        _, typ, _, a, off, size, *_ = struct.unpack_from('<IIIIIIIIII', data, shoff + i * shentsize)
        if typ == 1 and a <= addr < a + size: return data[off + addr - a:off + min(addr - a + n, size)]   # SHT_PROGBITS
    raise SystemExit(f'{addr:#x} is in no section of the image')


def code(image: bytes, tmp: pathlib.Path) -> tuple:
    """({symbol: [(address, text)]}, [problems]): each CODE region disassembled from its symbol to its last
    instruction, and every instruction outside the first version."""
    syms, regions, bad = elf_symbols(image), {}, []
    for sym, bits, _, last in CODE:
        rows = disassemble(elf_bytes(image, syms[sym], 256), syms[sym], bits, tmp)
        end = next((i for i, (_, _, t) in enumerate(rows) if re.fullmatch(last, re.sub(r'^(o32|o16) ', '', t))), None)
        if end is None: bad.append(f'{sym}: no {last!r} in 256 bytes'); continue
        regions[sym] = [(a, t) for a, _, t in rows[:end + 1]]
        bad += [f'{sym} {a:#x} {t}: {why}' for a, t in regions[sym] if (why := refused(t, bits))]
    return regions, bad


def environment(n: int) -> str:
    """The Multiboot module: the test's environment (lib/x86/setup.c setup_libcflat; fwcfg.c reads NR_CPUS from it)."""
    return f'NR_CPUS={n}\n'


def qemu_args(n: int, backend: str) -> list:
    """QEMU's arguments without the executable; IMG, ENV and OUT stand for the image, the module and COM1's file."""
    accel, cpu = ('tcg,thread=multi', TCG_CPU) if backend == 'qemu' else ('kvm', kvm_ref.CPU)
    return ['-accel', accel, '-cpu', cpu, '-smp', str(n), '-m', str(MEM_MB), '-machine', 'pc', '-display', 'none', '-nodefaults',
            '-monitor', 'none', '-serial', 'file:OUT', '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04', '-no-reboot',
            '-kernel', 'IMG', '-initrd', 'ENV']


def answer(rc, com1: bytes) -> dict:
    """One boot's answer: QEMU's exit status and COM1's lines (CRs dropped, empty lines kept out), sorted."""
    lines = [l for l in com1.decode(errors='replace').replace('\r', '').split('\n') if l.strip()]
    return {'exit': rc, 'lines': sorted(lines)}


def judge(test: str, n: int, ans: dict, want_exit=1) -> list:
    """Differences between an answer and the pass at N CPUs (empty: pass). The test's own verdict: kvm-unit-tests'
    exit status 0, which QEMU's isa-debug-exit returns as 1 (`want_exit`: a natemu case passes its own debug-exit
    status); smptest's PASS line and summary, dummy's greeting; every other CPU brought up once (lib/x86/smp.c:
    `smp: waiting for N-1 APs`, then `setup: CPU <APIC ID> online` from each AP, IDs 1 to N-1), each CPU's
    `enabling apic` (lib/x86/apic.c), and no failure line. COM1 holds the test's output alone: no firmware text
    (QEMU's SeaBIOS writes its banner there with -nographic; natemu loads the image itself)."""
    d = [] if ans.get('exit') == want_exit else [f"exit {ans.get('exit')} (want {want_exit}: the test's status 0)"]
    lines = ans.get('lines') or []
    if fw := [l for l in lines if 'SeaBIOS' in l or 'Booting from' in l]: d.append(f'firmware text on COM1: {fw}')
    if (c := lines.count('enabling apic')) != n: d.append(f"'enabling apic' printed {c} times (want {n}, once per CPU)")
    want = [f'smp: waiting for {n - 1} APs'] + [f'setup: CPU {k} online' for k in range(1, n)]
    want += {'smptest': [f'found {n} cpus', 'PASS: IPI to each CPU', 'SUMMARY: 1 tests'] + [f'ipi called, cpu {k}' for k in range(n)],
             'dummy': ['Dummy Hello World!']}[test]
    for w in want:
        c = lines.count(w)
        if c != 1: d.append(f'{w!r} printed {c} times (want once)')
    online = [l for l in lines if l.startswith('setup: CPU ')]
    if len(online) != n - 1: d.append(f'{len(online)} CPUs came online, want {n - 1}: {online}')
    if bad := [l for l in lines if re.match(r'(FAIL|XFAIL|SKIP|but wrong cpu|Unhandled exception|PANIC|ERROR)', l)]:
        d.append(f'failure lines: {bad}')
    return d


def inputs() -> tuple:
    d = os.environ.get(INPUT)
    if not d: raise SystemExit(f'{INPUT} is not set: the framework builds kvm-unit-tests (tests/boot/built_inputs.py)')
    d = pathlib.Path(d)
    return d, json.loads((d / 'provenance.json').read_text())


def key(test, n, backend, prov):
    return answers.make_key('boot-kut', test, n, environment(n), pin(backend), qemu_args(n, backend), prov['upstream'])


def pin(backend):
    return answers.pin('system') if backend == 'qemu' else kvm_ref.pin()


def run_tcg(test, n, img: pathlib.Path, work: pathlib.Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    env, out = work / f'env-{n}', work / f'{test}-{n}.com1'
    env.write_text(environment(n)); out.unlink(missing_ok=True)
    args = [a.replace('IMG', str(img)).replace('ENV', str(env)).replace('OUT', str(out)) for a in qemu_args(n, 'qemu')]
    qemu = os.environ.get('NATEMU_QEMU_SYSTEM')
    if not qemu: raise SystemExit('NATEMU_QEMU_SYSTEM is not set: the framework gives the pinned QEMU build')
    try:
        r = subprocess.run([qemu, *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=QEMU_TIMEOUT)
        rc = r.returncode
        if r.stderr.strip(): print(f'QEMU {test} N={n}: {r.stderr.decode(errors="replace")[-400:]}', flush=True)
    except subprocess.TimeoutExpired:
        rc = 'timeout'
    return answer(rc, out.read_bytes() if out.exists() else b'')


def run_natemu(test, n, img: pathlib.Path, work: pathlib.Path, fexc) -> dict:
    """One boot under natemu: the answer (`exit` the debug exit's status, QEMU's form: 1 for the test's status 0)."""
    import run_slice1_vm as vm                  # tests/lib
    host, runtime = vm.build()
    work.mkdir(parents=True, exist_ok=True)
    env, out = work / f'env-{n}', work / f'{test}-{n}.com1'
    env.write_text(environment(n)); out.unlink(missing_ok=True)
    cmd = [host, runtime, 'boot-linux', fexc, img, '--initrd', env, '--ram', hex(MEM_MB << 20), '--vcpus', str(n),
           '--debug-exit', '--serial', out]
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=QEMU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'exit': 'timeout', 'lines': []}
    (work / f'{test}-{n}.json').write_text(r.stdout)
    (work / f'{test}-{n}.stderr').write_text(r.stderr)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'exit': f'driver exit {r.returncode}: {r.stderr.strip()[-400:]}', 'lines': []}
    status = (res.get('debug_exit') or [None, f"{res.get('stop')}: {res.get('reason', '')}"])[1]
    return answer(status, out.read_bytes() if out.exists() else b'')


def run_kvm(runs: list, d: pathlib.Path, work: pathlib.Path) -> tuple:
    """Every (test, N) of `runs` in one session on NATEMU_X86_HOST: ({(test, n): answer}, the machine's description)."""
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: raise SystemExit('--backend kvm runs on the x86 reference machine: set NATEMU_X86_HOST=user@host')
    files = {f'{t}.flat': (d / f'{t}.flat').read_bytes() for t in sorted({t for t, _ in runs})}
    files |= {f'env-{n}': environment(n).encode() for n in sorted({n for _, n in runs})}
    script = '; '.join(f'timeout -s KILL {QEMU_TIMEOUT} qemu-system-x86_64 '
                       + shlex.join(a.replace('IMG', f'/b/{t}.flat').replace('ENV', f'/b/env-{n}').replace('OUT', f'/b/out/{t}-{n}.com1')
                                    for a in qemu_args(n, 'kvm')) + f' < /dev/null; echo $? > /b/out/{t}-{n}.rc'
                       for t, n in runs)
    ref = kvm_ref.session(host, files, script, work, timeout=len(runs) * QEMU_TIMEOUT)
    got = {}
    for t, n in runs:
        rcf, com1 = work / 'out' / f'{t}-{n}.rc', work / 'out' / f'{t}-{n}.com1'
        rc = int(rcf.read_text()) if rcf.exists() else 'none'
        got[(t, n)] = answer('timeout' if rc == 137 else rc, com1.read_bytes() if com1.exists() else b'')
    return got, ref


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--backend', choices=('qemu', 'kvm', 'natemu'), required=True)
    ap.add_argument('--tests', nargs='+', choices=TESTS, default=list(TESTS))
    ap.add_argument('--cpus', type=int, nargs='+', default=list(CPUS))
    ap.add_argument('--fexc', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    d, prov = inputs()
    runs = [(t, n) for t in a.tests for n in a.cpus]
    if a.backend == 'natemu':
        if not a.fexc: raise SystemExit('--backend natemu needs --fexc')
        results, got = [], {}
        for t, n in runs:
            got[(t, n)] = run_natemu(t, n, d / f'{t}.flat', a.out / 'natemu', a.fexc)
            diffs = judge(t, n, got[(t, n)])
            try:                                    # the lines of the locked reference answer (KVM's; QEMU's agree)
                e = answers.lookup(key(t, n, 'kvm', prov))
                want = json.loads(e.value)['lines'] if e.value is not None else None
            except answers.Missing:
                want = None
            if want is None: diffs.append('no KVM answer locked with its lines')
            elif got[(t, n)].get('lines') != want:
                diffs.append(f"COM1 differs from KVM's: natemu {got[(t, n)].get('lines')}, KVM {want}")
            results.append({'name': f'{t}-natemu N={n}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
            print(f"{t} natemu N={n}: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
        (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
        (a.out / 'kut.json').write_text(json.dumps({'backend': 'natemu', 'provenance': prov,
                                                    'answers': {f'{t} N={n}': got[(t, n)] for t, n in runs}}, indent=1) + '\n')
        return 1 if any(r['diffs'] for r in results) else 0
    keys = {r: key(*r, a.backend, prov) for r in runs}
    mode, got, ref = answers.mode(), {}, {}
    if mode == 'locked':
        for r, k in keys.items():
            try:
                e = answers.lookup(k)
                got[r] = json.loads(e.value) if e.value is not None else {'locked': 'hash only'}
            except answers.Missing:
                got[r] = {'missing': k}
    elif a.backend == 'qemu':
        got = {(t, n): run_tcg(t, n, d / f'{t}.flat', a.out / 'tcg') for t, n in runs}
    else:
        got, ref = run_kvm(runs, d, a.out / 'kvm')
    results = []
    for (t, n) in runs:
        k = keys[(t, n)]
        if mode == 'seed':
            repro = {'supplier': 'qemu-system' if a.backend == 'qemu' else 'kvm', 'pin': pin(a.backend), 'test': f'kvm-unit-tests {t}',
                     'cpus': n, 'environment': environment(n), 'args': qemu_args(n, a.backend), 'upstream': prov['upstream'],
                     'image_sha256': prov['built'][f'{t}.flat'], **({'reference': ref} if ref else {})}
            answers.record(k, answers.canon(got[(t, n)]), repro)
        diffs = [f'no answer locked for this input ({k}): seed it'] if 'missing' in got[(t, n)] else judge(t, n, got[(t, n)])
        results.append({'name': f'{t}-{a.backend} N={n}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
        print(f"{t} {a.backend} N={n}: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
        for line in got[(t, n)].get('lines') or []: print(f'  COM1: {line}', flush=True)
    regions = {}
    with tempfile.TemporaryDirectory(prefix='natemu-kut-') as tmp:
        for t in a.tests:
            regions[t], bad = code((d / f'{t}.flat').read_bytes(), pathlib.Path(tmp))
            results.append({'name': f'{t} 16- and 32-bit code', 'outcome': 'fail' if bad else 'match', 'diffs': bad})
            print(f"{t} 16- and 32-bit code: {'first-version instructions only' if not bad else 'FAIL ' + '; '.join(bad)}", flush=True)
            for sym, rows in regions[t].items():
                print(f'  {sym}: ' + '; '.join(txt for _, txt in rows), flush=True)
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    (a.out / 'kut.json').write_text(json.dumps({
        'mode': mode, 'backend': a.backend, 'provenance': prov, 'reference': ref,
        'code': {t: {s: [f'{x:#x} {txt}' for x, txt in rows] for s, rows in r.items()} for t, r in regions.items()},
        'answers': {f'{t} N={n}': got[(t, n)] for t, n in runs}}, indent=1, default=str) + '\n')
    return 1 if any(r['diffs'] for r in results) else 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
