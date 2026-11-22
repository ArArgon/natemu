#!/usr/bin/env python3
"""OVMF (edk2-stable202608, OvmfPkgX64, DEBUG, no SMM, no Secure Boot) from its reset vector to SEC's first
instruction in 64-bit mode, on the references (docs/restructure/real-code-suites.md).

    tests/boot/run_ovmf.py --backend qemu|kvm [--entries reset main32] --out DIR
    tests/boot/run_ovmf.py --backend natemu --fexc FEXC [--entries reset main32] --out DIR

The image comes from the framework's build (NATEMU_INPUT_OVMF, tests/boot/built_inputs.py: OVMF.fd, SECFV.Fv.map and
provenance.json). SEC's entry is the SecMain line of SECFV.Fv.map (the address the reset vector itself finds in the
boot firmware volume); the boot firmware volume is the firmware-volume header the reset vector's search finds (4 KiB
steps down from 4 GiB). QEMU maps the image as the BIOS ROM below 4 GiB (machine pc, 256 MiB, 1 CPU; the debug port
402H, isa-debugcon, into a file) and starts paused at reset; tools/reference/gdbrsp.py sets a breakpoint at SEC's
entry, continues, reads the CPU there, then lets SEC run on until the debug port holds its first line
(`SecCoreStartupWithStack(...)`). Entries:
- reset: the power-on state: the 16-bit reset vector, the climb through 32-bit flat mode to 64-bit mode.
- main32: the same image entered at FFFFFFF0H with CR0.PE already set (gdbrsp.py --main32: CS a flat 32-bit code
  segment, the data segments flat, the power-on CR0 with PE), which edk2's reset vector takes to Main32: no 16-bit code.
Backends: qemu, the pinned QEMU 11.1.2 (NATEMU_QEMU_SYSTEM) under TCG, CPU `max,-pcid,-smap`; kvm, the x86 reference
machine NATEMU_X86_HOST under KVM, -cpu host,-pku (tests/boot/kvm_ref.py; python3 for gdbrsp.py there too).
The answer (`answer`): the stop, the registers at SEC's entry (EFLAGS without AF, undefined after the AND before the
jump, and RF), every access the CPU made to a device (any I/O port, assigned or not, and any MMIO page) from reset to
that stop, from QEMU's memory_region_ops_read/write trace events (the same on TCG and KVM; RAM and the ROM never appear
there), and the debug port's lines up to SEC's first. Its key: the entry, the backend's pin, QEMU's arguments and
the image's upstream (provenance.json), never the built image's hash. Judged by `judge` against what the reset vector's
source gives (expected_registers). Answers (tools/reference/answers.py): seed runs the backend and records; locked
never runs it: the lock's answer must pass the judge for the image as built now (its SEC entry, its boot firmware
volume); live runs it. Writes DIR/results.json (one result per entry), DIR/ovmf.json (the image's addresses, the
answers, the reference machine); exit 1 on a failure.
natemu (the firmware boot mode, docs/restructure/firmware-mode.md): the test driver's `boot-linux --firmware` boots
OVMF.fd with 256 MiB (`main32`: `--firmware-start flat32`, the state the rig gives QEMU) until the boot processor is about
to run SEC's entry (`--stop-at`); the answer is natemu's: the stop (`breakpoint` there), its registers, its record of
port and unassigned-page accesses before the stop (`devices`), no debug port lines (natemu stops at SEC's entry, before
SEC's first line; the registers name the boot firmware volume in RBP). Pass: `judge` without its debug-port line, and the
registers equal to the locked KVM answer's (`key(entry, 'kvm', provenance)`; QEMU's and KVM's agree) when the lock holds
its value. A run that ends elsewhere names its stop, RIP, CS and mode in the differences.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, datetime, json, os, pathlib, re, shlex, shutil, struct, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools' / 'bench' / 'smp'), str(ROOT / 'tools' / 'reference')]
import smp, answers, gdbrsp, kvm_ref  # noqa: E402

ENTRIES = ('reset', 'main32')
MEM_MB = 256
TCG_CPU = smp.SYSTEM_CPU
QEMU_TIMEOUT = 120
INPUT = 'NATEMU_INPUT_OVMF'
FOUR_GIB = 1 << 32
FFS_GUIDS = (bytes.fromhex('78e58c8c3d8a1c4f9935896185c32dd3'),        # EFI_FIRMWARE_FILE_SYSTEM2_GUID
             bytes.fromhex('7ac07354cb3dca4d961e6fbd9a34e789'))        # EFI_FIRMWARE_FILE_SYSTEM3_GUID
FLAGS_MASK = 0x3F7FD7 & ~0x10010            # EFLAGS' defined bits less AF and RF
NATEMU_TIMEOUT = 300
FIRST_LINE = 'SecCoreStartupWithStack('
PAGE_TABLES = 0x800000                       # PcdOvmfSecPageTablesBase (OvmfPkg/Include/Fdf/MemFd.fdf.inc)
# QEMU's log trace backend with -msg timestamp=on (system/trace-events): "<time> memory_region_ops_read cpu N mr 0x...
# addr 0x... value 0x... size N name 'REGION'", the time as "PID@SECONDS.MICROS:" in QEMU 10 (Debian's, the KVM
# reference) and as "YYYY-MM-DDTHH:MM:SS.MICROSZ " (UTC) in QEMU 11.
TRACE = re.compile(r"^(?:\d+@(\d+\.\d+):|(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z )memory_region_ops_(read|write) cpu (-?\d+) "
                   r"mr \S+ addr 0x([0-9a-f]+) value 0x[0-9a-f]+ size (\d+) name '([^']*)'", re.M)


def sec_entry(map_text: str) -> int:
    m = re.search(r'^SecMain \(.*?EntryPoint=0x([0-9a-fA-F]+)', map_text, re.M)
    if not m: raise SystemExit('SECFV.Fv.map has no SecMain entry point')
    return int(m[1], 16)


def boot_fv(img: bytes) -> int:
    """The boot firmware volume's base: UefiCpuPkg's Flat32SearchForBfvBase over the image mapped below 4 GiB."""
    base = FOUR_GIB - len(img)
    for a in range(FOUR_GIB - 0x1000, max(base, 0xFF000000) - 1, -0x1000):
        h = img[a - base:a - base + 0x30]
        if h[0x10:0x20] in FFS_GUIDS and a + struct.unpack_from('<Q', h, 0x20)[0] == FOUR_GIB: return a
    raise SystemExit('no boot firmware volume ends at 4 GiB')


def reset_vector(img: bytes) -> dict:
    """The reset vector's two targets from its 16 bytes at FFFFFFF0H (OvmfPkg/ResetVector/Ia16/ResetVectorVtf0.nasm.inc:
    mov eax, cr0; test al, 1; jz .Real; (32-bit) jmp Main32; .Real: (16-bit) jmp EarlyBspInitReal16)."""
    v = img[-16:]
    if v[:7] != bytes.fromhex('0f20c0a8017405') or v[7] != 0xE9 or v[12] != 0xE9:
        raise SystemExit(f'unexpected reset vector {v.hex()}')
    main32 = (0xFFFFFFF0 + 12 + struct.unpack_from('<i', v, 8)[0]) & 0xFFFFFFFF
    real16 = 0xFFFF0000 + ((0xFFF0 + 15 + struct.unpack_from('<h', v, 13)[0]) & 0xFFFF)
    return {'reset': 0xFFFFFFF0, 'main32': main32, 'early_bsp_init_real16': real16}


def expected_registers(entry: str, sec: int, bfv: int) -> dict:
    """The CPU at SEC's first instruction as the reset vector's source leaves it (OvmfPkg/ResetVector, at edk2-stable202608,
    off TDX and SEV): CS the 64-bit selector 38H, the data selectors 18H; CR3 the page tables at 800000H, CR4 PAE (and
    the 16-bit path's MCE, OSFXSR, OSXMMEXCPT), CR0 PG with the 16-bit path's 23H (and ET, which reads 1) or the PE-set
    CR0 kept, EFER LME and LMA; RSI SEC's entry (jmp rsi), RBP the boot firmware volume, RAX = RSP = 0 (SevCpuidExit's
    ESP), RCX 1 (the SEV-ES status bit index), RDI `BP` from the 16-bit path (EarlyBspInitReal16), the rest 0."""
    r = {k: 0 for k in gdbrsp.REGS}
    r.update(rip=sec, rsi=sec, rbp=bfv, rcx=1, cs=0x38, ss=0x18, ds=0x18, es=0x18, fs=0x18, gs=0x18, cr3=PAGE_TABLES, efer=0x500)
    if entry == 'reset': r.update(rdi=0x5042, cr0=0x80000033, cr4=0x660)
    else: r.update(cr0=0xE0000011, cr4=0x20)
    del r['eflags']
    return r


def devices(trace: str, until: float) -> list | None:
    """The CPU's device accesses (vCPU 0) before `until` (time.time()), from the trace: one {op, region, addr, size,
    count} per distinct access, in the order of its first occurrence (values left out). None when the trace shows no
    access by the CPU after `until` either: SEC reads and writes the debug port, so such a trace saw nothing (a
    format this parser does not read, or no trace backend) and proves nothing."""
    seen, after = {}, 0
    for m in TRACE.finditer(trace):
        epoch, iso, op, cpu, addr, size, name = m.groups()
        t = float(epoch) if epoch else datetime.datetime.fromisoformat(iso + '+00:00').timestamp()
        if int(cpu) != 0: continue
        if t >= until: after += 1; continue
        k = (op, name, int(addr, 16), int(size))
        seen[k] = seen.get(k, 0) + 1
    if not after: return None
    return [{'op': op, 'region': name, 'addr': hex(addr), 'size': size, 'count': n} for (op, name, addr, size), n in seen.items()]


def answer(got: dict, debugcon: bytes, trace: str | None) -> dict:
    """One run's answer: the stop ('breakpoint', or the stub's reply), the registers (EFLAGS masked), the device
    accesses before the stop (None without a trace) and the debug port's lines up to and including SEC's first (CRs
    dropped)."""
    stop = 'breakpoint' if re.match(r'[TS]05', got.get('stop', '')) else got.get('stop', 'none')
    regs = dict(got.get('regs') or {})
    if 'eflags' in regs: regs['eflags'] &= FLAGS_MASK
    lines = []
    for l in debugcon.decode(errors='replace').replace('\r', '').split('\n'):
        lines.append(l)
        if l.startswith(FIRST_LINE): break
    else:
        lines.append('(no SEC line)')
    dev = devices(trace, got['resumed_at']) if trace is not None and 'resumed_at' in got else None
    return {'stop': stop, 'regs': regs, 'devices': dev, 'debugcon': [l for l in lines if l]}


def judge(entry: str, ans: dict, sec: int, bfv: int, debugcon: bool = True) -> list:
    """Differences between an answer and the pass (empty: pass): the stop at SEC's entry, the registers there
    (expected_registers; IF clear), no device access before it (the boot-time interpreter's first version stops at any
    port I/O or device page in 16- and 32-bit code), SEC's first debug line naming the boot firmware volume (not with
    `debugcon` false: natemu's runs stop at SEC's entry, before SEC writes it)."""
    d = [] if ans.get('stop') == 'breakpoint' else [f"stop {ans.get('stop')!r}: the CPU did not reach SEC's entry {sec:#x}"]
    regs = ans.get('regs') or {}
    for k, v in expected_registers(entry, sec, bfv).items():
        if regs.get(k) != v: d.append(f'{k} {regs.get(k, "none") if not isinstance(regs.get(k), int) else hex(regs[k])} != {v:#x}')
    if regs.get('eflags', 0) & 0x200: d.append('IF set at SEC entry')
    if ans.get('devices') is None: d.append("no device trace (or one that shows not even SEC's debug port): it proves nothing")
    elif ans['devices']: d.append(f"device accesses before SEC's entry: {ans['devices']}")
    last = (ans.get('debugcon') or ['(none)'])[-1]
    want = f'{FIRST_LINE}0x{bfv:X}, '
    if debugcon and not last.upper().startswith(want.upper()): d.append(f'debug port: {last!r} (want {want}...)')
    return d


def natemu_answer(out: dict, sec: int) -> dict:
    """natemu's answer from the test driver's report `out`, in `answer`'s form: the stop (`breakpoint` when the run ended
    at `--stop-at`, RIP SEC's entry), the boot processor's registers (EFLAGS masked as there), the accesses natemu counted
    to ports and unassigned pages before the stop (each with its reads and writes), no debug port lines."""
    st = out['state']
    g = [int(x, 16) for x in st['gpr']]
    regs = dict(zip(('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi'), g))
    regs.update({f'r{k}': g[k] for k in range(8, 16)})
    regs['rip'], regs['eflags'] = int(st['rip'], 16), int(st['rflags'], 16) & FLAGS_MASK
    regs.update(zip(('es', 'cs', 'ss', 'ds', 'fs', 'gs'), (int(x, 16) for x in st['seg'])))
    regs.update({k: int(st[k], 16) for k in ('fs_base', 'gs_base', 'cr0', 'cr3', 'cr4', 'efer')})
    stop = 'breakpoint' if out['stop'] == 'reached' and regs['rip'] == sec else f"{out['stop']} {out['reason']}".strip()
    dev = [{'op': 'port', 'addr': x['port'], 'reads': x['reads'], 'writes': x['writes']} for x in out.get('ports', [])]
    dev += [{'op': 'page', 'addr': x['page'], 'reads': x['reads'], 'writes': x['writes']} for x in out.get('unassigned_pages', [])]
    return {'stop': stop, 'regs': {k: regs[k] for k in gdbrsp.REGS}, 'devices': dev, 'debugcon': [],
            'natemu': {'stop': out['stop'], 'reason': out['reason'], 'native': out['vcpus'][0]['native']}}


def run_natemu(entry: str, img: pathlib.Path, sec: int, work: pathlib.Path, fexc: str) -> dict:
    """One entry in natemu's firmware boot mode, to SEC's entry (see the module doc)."""
    import run_slice1_vm as vm                  # tests/lib
    host, runtime = vm.build()
    work.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([host, runtime, 'boot-linux', fexc, img, '--firmware', *(['--firmware-start', 'flat32'] if entry == 'main32' else []),
                            '--ram', hex(MEM_MB << 20), '--stop-at', hex(sec), '--watchdog-ms', '60000', *vm.CODE_SHAPE],
                           capture_output=True, text=True, timeout=NATEMU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'stop': 'timeout'}
    (work / f'{entry}.stderr').write_text(r.stderr)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'stop': f'driver exit {r.returncode}: {r.stderr.strip()[-400:]}'}
    (work / f'{entry}.json').write_text(json.dumps(out, indent=1) + '\n')
    return natemu_answer(out, sec)


def qemu_args(backend: str) -> list:
    """QEMU's arguments without the executable; IMG, DBG, TRACE and SOCK stand for the image, the debug port's file,
    the trace's file and the gdbstub's socket."""
    accel, cpu = ('tcg,thread=multi', TCG_CPU) if backend == 'qemu' else ('kvm', kvm_ref.CPU)
    return ['-accel', accel, '-cpu', cpu, '-smp', '1', '-m', str(MEM_MB), '-machine', 'pc', '-display', 'none', '-nodefaults',
            '-monitor', 'none', '-serial', 'none', '-debugcon', 'file:DBG', '-global', 'isa-debugcon.iobase=0x402',
            '-no-reboot', '-bios', 'IMG', '-msg', 'timestamp=on', '-trace', 'enable=memory_region_ops_*,file=TRACE',
            '-S', '-gdb', 'unix:SOCK,server=on,wait=off']


def pin(backend):
    return answers.pin('system') if backend == 'qemu' else kvm_ref.pin()


def key(entry, backend, prov):
    return answers.make_key('boot-ovmf', entry, pin(backend), qemu_args(backend), prov['upstream'])


def inputs() -> tuple:
    d = os.environ.get(INPUT)
    if not d: raise SystemExit(f'{INPUT} is not set: the framework builds OVMF (tests/boot/built_inputs.py)')
    d = pathlib.Path(d)
    return d, json.loads((d / 'provenance.json').read_text())


def _read(p: pathlib.Path):
    return p.read_text(errors='replace') if p.exists() else None


def run_tcg(entry, img: pathlib.Path, sec: int, work: pathlib.Path) -> dict:
    qemu = os.environ.get('NATEMU_QEMU_SYSTEM')
    if not qemu: raise SystemExit('NATEMU_QEMU_SYSTEM is not set: the framework gives the pinned QEMU build')
    work.mkdir(parents=True, exist_ok=True)
    dbg, trace = work / f'{entry}.debugcon', work / f'{entry}.trace'
    for f in (dbg, trace): f.unlink(missing_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='natemu-gdb-'))        # a short path: a socket's is at most 104 bytes
    sock = tmp / 'gdb.sock'
    args = [a.replace('IMG', str(img)).replace('DBG', str(dbg)).replace('TRACE', str(trace)).replace('SOCK', str(sock))
            for a in qemu_args('qemu')]
    p = subprocess.Popen([qemu, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        got = gdbrsp.session(f'unix:{sock}', sec, entry == 'main32', QEMU_TIMEOUT, dbg, FIRST_LINE, 60)
    except (OSError, EOFError, RuntimeError) as e:
        got = {'stop': f'gdbstub: {type(e).__name__}: {e}'}
    finally:
        try: p.wait(timeout=30)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    err = p.stderr.read().decode(errors='replace').strip()
    if err: print(f'QEMU {entry}: {err[-400:]}', flush=True)
    (work / f'{entry}.json').write_text(json.dumps(got, indent=1) + '\n')
    return answer(got, dbg.read_bytes() if dbg.exists() else b'', _read(trace))


def run_kvm(entries, img: pathlib.Path, sec: int, work: pathlib.Path) -> tuple:
    """Every entry in one session on NATEMU_X86_HOST: ({entry: answer}, the machine's description)."""
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: raise SystemExit('--backend kvm runs on the x86 reference machine: set NATEMU_X86_HOST=user@host')
    files = {'OVMF.fd': img.read_bytes(), 'gdbrsp.py': (ROOT / 'tools/reference/gdbrsp.py').read_bytes()}
    runs = []
    for e in entries:
        q = shlex.join(a.replace('IMG', '/b/OVMF.fd').replace('DBG', f'/b/out/{e}.debugcon').replace('TRACE', f'/b/out/{e}.trace')
                       .replace('SOCK', f'/tmp/gdb-{e}.sock') for a in qemu_args('kvm'))
        g = shlex.join(['python3', '/b/gdbrsp.py', '--connect', f'unix:/tmp/gdb-{e}.sock', '--break', hex(sec),
                        *(['--main32'] if e == 'main32' else []), '--timeout', str(QEMU_TIMEOUT), '--debugcon',
                        f'/b/out/{e}.debugcon', '--until', FIRST_LINE, '--wait', '60', '--out', f'/b/out/{e}.json'])
        runs.append(f'(timeout -s KILL {3 * QEMU_TIMEOUT} qemu-system-x86_64 {q} < /dev/null 2> /b/out/{e}.qemu & q=$!; '
                    f'timeout -s KILL {3 * QEMU_TIMEOUT} {g} > /b/out/{e}.gdb 2>&1; wait $q)')
    ref = kvm_ref.session(host, files, '; '.join(runs), work, timeout=len(entries) * 3 * QEMU_TIMEOUT,
                          packages=('qemu-system-x86', 'python3'))
    got = {}
    for e in entries:
        o = work / 'out'
        j, dbg = o / f'{e}.json', o / f'{e}.debugcon'
        raw = json.loads(j.read_text()) if j.exists() else {'stop': 'no answer: ' + (_read(o / f'{e}.gdb') or '')[-300:]}
        got[e] = answer(raw, dbg.read_bytes() if dbg.exists() else b'', _read(o / f'{e}.trace'))
    return got, ref


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--backend', choices=('qemu', 'kvm', 'natemu'), required=True)
    ap.add_argument('--entries', nargs='+', choices=ENTRIES, default=list(ENTRIES))
    ap.add_argument('--fexc', help='natemu: the compiler')
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    if a.backend == 'natemu' and not a.fexc: raise SystemExit('--backend natemu needs --fexc')
    a.out.mkdir(parents=True, exist_ok=True)
    d, prov = inputs()
    img = (d / 'OVMF.fd').read_bytes()
    sec, bfv = sec_entry((d / 'SECFV.Fv.map').read_text(errors='replace')), boot_fv(img)
    image = {'size': len(img), 'base': FOUR_GIB - len(img), 'sec_entry': sec, 'boot_fv': bfv, **reset_vector(img)}
    print('image: ' + ', '.join(f'{k} {v:#x}' for k, v in image.items()), flush=True)
    if a.backend == 'natemu':
        return main_natemu(a, d, prov, img, sec, bfv)
    keys = {e: key(e, a.backend, prov) for e in a.entries}
    mode, got, ref = answers.mode(), {}, {}
    if mode == 'locked':
        for e, k in keys.items():
            try:
                x = answers.lookup(k)
                got[e] = json.loads(x.value) if x.value is not None else {'locked': 'hash only'}
            except answers.Missing:
                got[e] = {'missing': k}
    elif a.backend == 'qemu':
        got = {e: run_tcg(e, d / 'OVMF.fd', sec, a.out / 'tcg') for e in a.entries}
    else:
        got, ref = run_kvm(a.entries, d / 'OVMF.fd', sec, a.out / 'kvm')
    results = []
    for e in a.entries:
        if mode == 'seed':
            repro = {'supplier': 'qemu-system' if a.backend == 'qemu' else 'kvm', 'pin': pin(a.backend), 'test': f'ovmf {e}',
                     'args': qemu_args(a.backend), 'upstream': prov['upstream'], 'image_sha256': prov['built']['OVMF.fd'],
                     'image': {k: hex(v) for k, v in image.items()}, **({'reference': ref} if ref else {})}
            answers.record(keys[e], answers.canon(got[e]), repro)
        diffs = [f'no answer locked for this input ({keys[e]}): seed it'] if 'missing' in got[e] else judge(e, got[e], sec, bfv)
        results.append({'name': f'{e}-{a.backend}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
        print(f"{e} {a.backend}: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
        regs = got[e].get('regs') or {}
        if regs: print('  ' + ' '.join(f'{k}={v:#x}' for k, v in regs.items()), flush=True)
        if 'devices' in got[e]: print(f"  device accesses before SEC's entry: {got[e]['devices']}", flush=True)
        for line in got[e].get('debugcon') or []: print(f'  debug port: {line}', flush=True)
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    (a.out / 'ovmf.json').write_text(json.dumps({
        'mode': mode, 'backend': a.backend, 'provenance': prov, 'image': {k: hex(v) for k, v in image.items()},
        'reference': ref, 'answers': got}, indent=1, default=str) + '\n')
    return 1 if any(r['diffs'] for r in results) else 0


def main_natemu(a, d: pathlib.Path, prov: dict, img: bytes, sec: int, bfv: int) -> int:
    """`--backend natemu`: each entry in natemu's firmware boot mode, judged as the module doc says."""
    results, got = [], {}
    for e in a.entries:
        ans = got[e] = run_natemu(e, d / 'OVMF.fd', sec, a.out / 'natemu', a.fexc)
        diffs = judge(e, ans, sec, bfv, debugcon=False)
        try:
            ref = answers.lookup(key(e, 'kvm', prov))
            want = json.loads(ref.value)['regs'] if ref is not None and ref.value is not None else None
        except (answers.Missing, answers.Unconfigured):
            want = None
        if want is None: print(f'{e}: the lock holds no value of the KVM answer: the registers judged alone', flush=True)
        diffs += [f"{k} {ans['regs'].get(k)} != KVM's {v:#x}" for k, v in (want or {}).items() if ans['regs'].get(k) != v]
        if diffs and ans.get('regs'):
            r = ans['regs']
            diffs.append(f"natemu stopped: {(ans.get('natemu') or {}).get('reason', '')[:120]} at CS {r['cs']:#x}:{r['rip']:#x}, "
                         f"CR0 {r['cr0']:#x}, EFER {r['efer']:#x}")
        results.append({'name': f'{e}-natemu', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
        print(f"{e} natemu: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
        regs = ans.get('regs') or {}
        if regs: print('  ' + ' '.join(f'{k}={v:#x}' for k, v in regs.items()), flush=True)
        print(f"  stop {(ans.get('natemu') or {}).get('reason') or ans.get('stop')}"[:300], flush=True)
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    (a.out / 'ovmf.json').write_text(json.dumps({'backend': 'natemu', 'provenance': prov, 'answers': got}, indent=1,
                                                default=str) + '\n')
    return 1 if any(r['diffs'] for r in results) else 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
