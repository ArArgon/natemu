#!/usr/bin/env python3
"""Run FEX's own 64-bit ASM suite (unittests/ASM/**/*.asm) through natemu's program driver (slice 2, S2f).

usage: run_asm.py --fexc FEXC --out DIR [--src .../unittests/ASM] [--only SUBSTR] [--limit N] [--jobs N] [--keep]
       run_asm.py --fexc FEXC --out DIR --selftest

Conventions, copied from FEX (CMakeLists.txt in unittests/ASM, Scripts/json_asm_config_parse.py,
Source/Tools/TestHarnessRunner, CommonTools/HarnessHelpers.h) and checked against them:
  * Build: nasm `-i Includes/` on "BITS 64\\n" + the file + "\\nret\\n" (the CMake sed edits; the ret is dead code after the
    test's hlt) -> flat binary, loaded at 0x10000 (Code_start_page); RIP starts there.
  * Initial state: every GPR 0 (RSP too; tests set their own stack), RFLAGS 0x202 (reserved + IF, as FEX's thread init).
  * Memory: FEX maps 0xe0000000 (10 pages) and 0xe7fff000 (2 pages) for every test, plus the JSON `MemoryRegions`
    (base: size), plus the code pages, all RWX; page size 4 KiB. natemu: `region` lines (eager, zero-filled, writable, NX)
    for the data ranges, and a `region` over the code pages so they are writable too (RWX as FEX; stores to translated
    pages invalidate or single-step). `MemoryData` bytes are decoded like parse_hexstring (each blank-
    separated token is a little-endian number) and loaded as `memory` lines.
  * End: the first executed hlt (FEX's SIGSEGV handler accepts a fault only when the byte at RIP is hlt). natemu: fexc cuts
    the block at hlt (system_instruction), the driver stops there with the state banked, RIP = the hlt.
  * Expected: only `RegData`. In FEX's JIT mode `Match`/`Ignore` have no effect (the second state is null), so they are
    ignored here too. A register value list compares the first value (GPR/RIP). FS/GS, XMMn and MMn need state natemu does
    not have (see below). `Mode` 32-bit is out of the slice. `HostFeatures` and `Env` are recorded, not applied (fexc runs
    with its own feature table; Env switches such as x87 reduced precision only matter for x87/SSE code).
Outcome per test:
  pass         ran to the hlt and every GPR/RIP expectation matches (vector expectations, if any, are separate: see below)
  unsupported  with a reason: an instruction outside the slice's allowlist (fexc unsupported_form / system_instruction at a
               RIP before the hlt, with the instruction's mnemonic), 32-bit mode,
               region beyond guest RAM, expectations on vector/x87/segment state natemu does not model (the GPR part had
               matched), ...
  fail         anything else: registers differ, a guest fault, the driver or nasm erred. Each is diagnosed by hand (see the
               report); none is silently ignored.
Known_Failures / Disabled_Tests (the jit lists and the ARMv8.4 class file, the M4's class) are reported as `listed`, separately;
the test is still run."""
import argparse, collections, concurrent.futures as cf, json, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
import run_slice1_vm as vm1  # noqa: E402

CODE = 0x10000
PAGE = 4096
DEFAULT_REGIONS = [(0xe0000000, 10 * PAGE), (0xe8000000 - PAGE, 2 * PAGE)]
# RegData key -> index into the driver's GPR array (rax rcx rdx rbx rsp rbp rsi rdi r8..r15)
GPR = {'RAX': 0, 'RCX': 1, 'RDX': 2, 'RBX': 3, 'RSP': 4, 'RBP': 5, 'RSI': 6, 'RDI': 7, **{f'R{i}': i for i in range(8, 16)}}
# Tests whose expectations are FEX's Linux user-mode environment, not x86 semantics: FEX loads any selector unchecked,
# returns through IRETQ/far JMP without descriptor checks and answers SGDT with a fixed GDTR. natemu executes these
# instructions architecturally (plan S2b) against the driver's start-up descriptor tables (GDTR base 0, limit FFFFH), so the
# selectors fault or SGDT reads other values: counted unsupported with this reason.
OS_SEGMENTS = {'Primary/Primary_8C.asm', 'Primary/Primary_8C_2.asm', 'Primary/Primary_CF.asm', 'PrimaryGroup/5_FF_05.asm',
               'PrimaryGroup/5_FF_05_03_o64.asm', 'PrimaryGroup/5_FF_05_03_o64_imm.asm', 'Secondary/07_XX_00.asm'}


def header(text):
    parts = text.split('%ifdef CONFIG')
    if len(parts) < 2: return {}
    return {k.upper(): v for k, v in json.loads(parts[1].split('%endif')[0].strip()).items()}


def memory_data(s):
    """Scripts/json_config_parse.py parse_hexstring, verbatim (bug-compatible)."""
    out = []
    for num in s.split(' '):
        if s.startswith('0x'): num = num[2:]
        while num:
            out.append(int(num[-2:], 16)); num = num[:-2]
    return bytes(out)


def lists(src):
    def read(*names):
        r = set()
        for n in names:
            f = src / n
            if f.exists(): r |= {l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith('#')}
        r |= {x.removeprefix('Test_') for x in r}
        return r
    return read('Known_Failures', 'Known_Failures_jit'), read('Disabled_Tests', 'Disabled_Tests_ARMv8.4')


def description(h, binary):
    lines = [f'rip {CODE:#x}', 'rflags 0x202', 'gpr ' + ' '.join(['0'] * 16), f'code {binary.hex()}', 'sse 1']   # I5a: CR4.OSFXSR as Linux
    regions = DEFAULT_REGIONS + [(int(a, 0), int(n, 0)) for a, n in h.get('MEMORYREGIONS', {}).items()]
    regions.append((CODE, -(-len(binary) // PAGE) * PAGE))   # FEX maps the code RWX
    lines += [f'region {a:#x} {n:#x}' for a, n in regions]
    lines += [f'memory {int(a, 0):#x} {memory_data(d).hex()}' for a, d in h.get('MEMORYDATA', {}).items()]
    return '\n'.join(lines) + '\n'


PREFIXES = ('lock', 'rep', 'repe', 'repne', 'repz', 'repnz', 'a32', 'o16', 'o32', 'o64', 'a16', 'xacquire', 'xrelease')


def mnemonic(binary, address):
    r = subprocess.run(['ndisasm', '-b', '64', '-o', hex(CODE), '-e', str(address - CODE), '/dev/stdin'], input=binary, capture_output=True)
    first = r.stdout.decode(errors='replace').splitlines()[:1]
    words = first[0].split()[2:] if first else []
    return ' '.join(words[:2] if words and words[0] in PREFIXES else words[:1]) or '?'


def one(host, runtime, fexc, src, out, path, known, disabled, keep, max_block=None):
    name = str(path.relative_to(src))
    work = out / 'work' / name.replace('/', '__')
    work.mkdir(parents=True, exist_ok=True)
    rec = {'test': name, 'known_failure': name in known, 'disabled': name in disabled}
    try:
        text = path.read_text()
        h = header(text)
        if h.get('MODE', '64BIT').upper() != '64BIT':
            return {**rec, 'outcome': 'unsupported', 'reason': '32-bit code'}
        if name in OS_SEGMENTS:
            return {**rec, 'outcome': 'unsupported', 'reason': "needs FEX's Linux user-mode descriptor tables"}
        rec['env'] = h.get('ENV')
        rec['host_features'] = h.get('HOSTFEATURES')
        (work / 't.asm').write_text('BITS 64\n' + text + '\nret\n')
        n = subprocess.run(['nasm', '-i', str(src / 'Includes') + '/', work / 't.asm', '-o', work / 't.bin'], capture_output=True, text=True)
        if n.returncode:
            return {**rec, 'outcome': 'fail', 'reason': 'harness: nasm failed', 'detail': n.stderr[-500:]}
        binary = (work / 't.bin').read_bytes()
        want = {k.upper(): [int(v, 0) for v in (vals if isinstance(vals, list) else [vals])] for k, vals in h.get('REGDATA', {}).items()}
        # Expectations natemu cannot check: MMn (x87/MMX, I6), XMM16+ and nonzero YMM upper halves (AVX, I8).
        vector = sorted(k for k in want if k not in GPR and k != 'RIP' and not (re.fullmatch(r'XMM\d+', k) and int(k[3:]) < 16 and not any(want[k][2:])))
        (work / 'program.txt').write_text(description(h, binary))
        run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work] + ([str(max_block)] if max_block else []) + vm1.CODE_SHAPE, capture_output=True, text=True, timeout=120)
        if run.returncode:
            err = run.stderr.strip()[-300:]
            if 'guest RAM exhausted' in err: return {**rec, 'outcome': 'unsupported', 'reason': 'region beyond 4 MiB guest RAM'}
            if 'object refused: code layout' in err: return {**rec, 'outcome': 'unsupported', 'reason': 'block over the 32 KiB code-block limit (admission: code layout)'}
            return {**rec, 'outcome': 'fail', 'reason': 'harness: driver error', 'detail': err}
        s = json.loads(run.stdout)
        rip = int(s['rip'], 0)
        at_hlt = s['stop'] == 'no_code' and s['reason'] == 'system_instruction' and CODE <= rip < CODE + len(binary) and binary[rip - CODE] == 0xF4
        if not at_hlt:
            rec['stop'] = f"{s['stop']} {s['reason']} rip={rip:#x}"
            if CODE <= rip < CODE + len(binary): rec['at'] = mnemonic(binary, rip); rec['bytes'] = binary[rip - CODE:rip - CODE + 8].hex()
            if s['stop'] == 'no_code':
                # fexc's end reason; the instruction is the one at RIP
                return {**rec, 'outcome': 'unsupported', 'reason': f"{s['reason']}: {rec.get('at', '?')}"}
            if s['stop'] == 'resident' and s['reason'].startswith('unsupported_atomic'):
                # A memory xchg whose operand crosses 16 bytes (a split lock): stopped before any effect (S3e, boot phase)
                return {**rec, 'outcome': 'unsupported', 'reason': f"split lock: {rec.get('at', '?')}"}
            at = lambda a, n: binary[a - CODE:a - CODE + n].hex() if CODE <= a and a + n <= CODE + len(binary) else ''
            vector = int(s['fault'][0], 0) if s['stop'] == 'guest_fault' else None
            if (vector == 3 and at(rip - 1, 1) == 'cc') or (vector is not None and at(rip - 2, 2) == f'cd{vector:02x}') \
                    or (vector == 6 and (at(rip, 1) == 'ce' or at(rip, 2) in ('0f0b', '0fb9', '0fff'))):
                # X1a: INT3/INT n/INTO/UD0-2 are delivered through the driver's logging IDT; these tests expect Linux's
                # signal for them (before X1a: no code, `system_instruction`).
                return {**rec, 'outcome': 'unsupported', 'reason': f"software interrupt or #UD instruction delivered (vector {int(s['fault'][0], 0)}): {rec.get('at', '?')}"}
            return {**rec, 'outcome': 'fail', 'reason': 'did not reach hlt', 'detail': rec['stop'] + ' fault=' + json.dumps(s['fault'])}
        gpr = [int(v, 0) for v in s['gpr']]
        xmm = [int(v, 0) for v in s['xmm']]
        diffs = []
        for k, vals in want.items():
            if k == 'RIP': got = rip
            elif k in GPR: got = gpr[GPR[k]]
            elif re.fullmatch(r'XMM\d+', k) and int(k[3:]) < 16 and not any(vals[2:]):   # I5a: XMM (low, high); YMM upper must be 0
                n = int(k[3:])
                if xmm[2 * n:2 * n + 2] != vals[:2]:
                    diffs.append(f'{k}: got {xmm[2 * n]:#x} {xmm[2 * n + 1]:#x}, want {vals[0]:#x} {vals[1]:#x}')
                continue
            else: continue
            if got != vals[0]: diffs.append(f'{k}: got {got:#x}, want {vals[0]:#x}')
        if diffs: return {**rec, 'outcome': 'fail', 'reason': 'registers differ', 'detail': '; '.join(diffs), 'stats': s['stats']}
        if 'FS' in want or 'GS' in want:
            return {**rec, 'outcome': 'unsupported', 'reason': 'needs segment base state (GPR part matched)'}
        if vector:
            kinds = sorted({'x87/MMX' if k.startswith('MM') else 'YMM' if k.startswith('XMM') else k for k in vector})
            return {**rec, 'outcome': 'unsupported', 'reason': f"needs vector state: {'+'.join(kinds)} (GPR part matched)"}
        return {**rec, 'outcome': 'pass', 'expectations': len(want), 'stats': s['stats']}
    except Exception as e:
        return {**rec, 'outcome': 'fail', 'reason': 'harness: exception', 'detail': repr(e)}
    finally:
        if not keep: shutil.rmtree(work, ignore_errors=True)


def category(reason):
    """Coarse family of an unsupported reason, for the summary."""
    if not reason.startswith(('unsupported_form', 'system_instruction')): return reason.split(' (')[0]
    kind, text = reason.split(': ', 1)
    if kind == 'system_instruction': return 'system instruction (cpuid, segment/cr moves, pushf/popf, rdtsc, ...)'
    w = text.split()
    pre = w[0] if w[0] in PREFIXES else None
    base = w[1] if pre and len(w) > 1 else w[0]
    if base.startswith('v'): return 'AVX (VEX-encoded)'
    if base.startswith('f') and base != 'femms': return 'x87'
    if re.match(r'^(movap[sd]|movup[sd]|movdq[au]|movntdqa|movss|movsd|movq|movd|p[a-z0-9]+|cvt.*|round.*|aes.*|sha.*|lddqu|[ls]dmxcsr|emms|femms|pf.*|pi2f.*)$', base):
        return 'MMX/SSE/3DNow/AES/SHA vector'
    if pre == 'lock' or base in ('xchg', 'cmpxchg', 'xadd', 'cmpxchg8b', 'cmpxchg16b'): return 'atomics: lock prefix, xchg/cmpxchg/xadd on memory'
    if base in ('cld', 'std', 'stc', 'clc', 'lahf', 'sahf'): return 'flag moves: cld std stc clc lahf sahf'
    if base in ('shld', 'shrd'): return 'shld/shrd'
    if base in ('popcnt', 'lzcnt', 'tzcnt', 'crc32', 'movbe', 'adox', 'blsr', 'blsi', 'blsmsk', 'bextr', 'bzhi', 'pdep', 'pext', 'mulx', 'rorx', 'sarx', 'shlx', 'shrx', 'andn', 'rdrand', 'rdseed', 'rdpid', 'clwb'): return 'BMI/ABM/other ISA extensions'
    return 'other: ' + text


SELFTEST = {   # synthetic tests: (source, expected outcome, reason prefix)
    'pass.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0x1","RSP":"0xE0000020","RIP":"0x1000a"}}\n%endif\nmov rsp, 0xe0000020\nmov eax, 1\nhlt\n', 'pass', ''),
    'wrong.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0x2"}}\n%endif\nmov eax, 1\nhlt\n', 'fail', 'registers differ'),
    'memory.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0x0807060504030201","RBX":"0x1234"},"MemoryRegions":{"0x100000000":"4096"},"MemoryData":{"0x100000000":"0102030405060708"}}\n%endif\n'
                   'mov rcx, 0x100000000\nmov rax, [rcx]\nbswap rax\nmov word [rcx+8], 0x1234\nmovzx ebx, word [rcx+8]\nhlt\n', 'pass', ''),
    'vector.asm': ('%ifdef CONFIG\n{"RegData":{"MM0":["0","0"],"RAX":"0"}}\n%endif\nxor eax,eax\nhlt\n', 'unsupported', 'needs vector state'),
    'vector_insn.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0"}}\n%endif\nxor eax,eax\npaddd mm0, mm1\nhlt\n', 'unsupported', 'unsupported_form'),
    'xmm.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x1","0x0"],"XMM1":["0x0","0x0","0x0","0x0"]}}\n%endif\nmov eax, 1\nmovd xmm0, eax\nhlt\n', 'pass', ''),
    'xmm_wrong.asm': ('%ifdef CONFIG\n{"RegData":{"XMM0":["0x2","0x0"]}}\n%endif\nmov eax, 1\nmovd xmm0, eax\nhlt\n', 'fail', 'registers differ'),
    'bits32.asm': ('%ifdef CONFIG\n{"Mode":"32BIT","RegData":{"RAX":"0"}}\n%endif\nhlt\n', 'unsupported', '32-bit'),
    'fault.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0"}}\n%endif\nmov rax, [0x70000000]\nhlt\n', 'fail', 'did not reach hlt'),
    'huge.asm': ('%ifdef CONFIG\n{"RegData":{"RAX":"0"},"MemoryRegions":{"0x500000000":"0x100000000"}}\n%endif\nhlt\n', 'unsupported', 'region beyond'),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--src', type=pathlib.Path, default=ROOT / 'third_party/FEX/unittests/ASM')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--jobs', type=int, default=6)
    ap.add_argument('--max-block', type=int, help='block size limit in guest instructions (upstream jit_1 = 1, jit_500 = 500)')
    ap.add_argument('--no-build', action='store_true', help='use the binaries already in target/')
    ap.add_argument('--keep', action='store_true', help='keep per-test work dirs')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.selftest:
        a.src = pathlib.Path(tempfile.mkdtemp(dir=a.out))
        (a.src / 'Includes').mkdir()
        for n, (t, _, _) in SELFTEST.items(): (a.src / n).write_text(t)
    if a.no_build: host, runtime = ROOT / 'target/release/natemu-host', ROOT / 'target/aarch64-unknown-none/release/natemu-runtime'
    else: host, runtime = vm1.build()
    known, disabled = lists(a.src)
    tests = sorted(p for p in a.src.rglob('*.asm') if 'Includes' not in p.parts)
    if a.only: tests = [t for t in tests if a.only in str(t.relative_to(a.src))]
    if a.limit: tests = tests[:a.limit]
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda p: one(host, runtime, a.fexc, a.src, a.out, p, known, disabled, a.keep, a.max_block), tests))
    (a.out / 'results.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in results))
    if a.selftest:
        bad = [(r['test'], r['outcome'], r.get('reason'), SELFTEST[r['test']][1:]) for r in results
               if (r['outcome'], (r.get('reason') or '')[:len(SELFTEST[r['test']][2])]) != SELFTEST[r['test']][1:]]
        print('selftest', 'FAILED' if bad else 'ok', bad or [(r['test'], r['outcome'], r.get('reason')) for r in results])
        sys.exit(1 if bad else 0)
    c = collections.Counter(r['outcome'] for r in results)
    print(f'{len(results)} tests: ' + ', '.join(f'{k} {v}' for k, v in sorted(c.items())))
    by = collections.Counter((r['outcome'], category(r['reason']) if r['outcome'] == 'unsupported' else r.get('reason', '')) for r in results if r['outcome'] != 'pass')
    for (o, why), n in by.most_common(): print(f'  {o:12} {n:5}  {why}')
    for flag in ('known_failure', 'disabled'):
        sel = [r for r in results if r[flag]]
        print(f'{flag}: ' + ', '.join(f"{r['test']}={r['outcome']}" for r in sel))
    for r in results:
        if r['outcome'] == 'fail': print('FAIL', r['test'], r.get('reason'), r.get('detail', ''))


if __name__ == '__main__': main()
