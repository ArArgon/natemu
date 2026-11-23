#!/usr/bin/env python3
"""The M2 exit test's firmware and its AP part as a 64-bit program, on the references (docs/restructure/m2-exit-firmware.md).

    tests/boot/run_exit_fw.py --variant firmware|program --backend qemu|kvm [--cpus 1 2 4 8] --out DIR
    tests/boot/run_exit_fw.py --variant firmware|program --backend natemu --fexc FEXC [--cpus 1 2 4 8] --out DIR

natemu, the program variant (M2 Part 2): the program driver runs exit_prog.asm's body with CPU 0 at its start and
every other vCPU waiting for a SIPI (`wait-for-sipi`): the description of docs/restructure/m2-exit-firmware.md. Each AP
runs the stub from its SIPI in the boot-time interpreter (real mode, the far JMP to 32-bit code, compatibility mode)
until its far JMP to the 64-bit CS hands it to the translator, which counts it on the stub page. Pass: CPU 0 reaches the
body's end with `expected_registers(N)` (every AP arrived exactly once, from 64-bit code).
natemu, the firmware variant (the firmware boot mode, docs/restructure/firmware-mode.md): the test driver's `boot-linux
--firmware` boots the image at N vCPUs with 64 MiB, as the references do: the BSP from the reset vector through the
boot-time interpreter (real mode, the far JMP to 32-bit code, compatibility mode) to the image's 64-bit code, which the
translator runs from the ROM and which starts the APs. Pass: `judge('firmware', N, answer, want_exit='poweroff')` on
COM1 and the run's stop, and per vCPU at most DECODES_MAX decode exits (the boot-time interpreter's requests to the
host) and SHOOTDOWNS_MAX shootdowns asked (`judge_bounds`: plan section 8, with N APs in and past the 16-bit part).

Both images are assembled here with nasm from tests/boot (exit_fw.asm, exit_prog.asm and the shared exit_ap.inc).
- firmware: the 64 KiB image QEMU maps below 4 GiB with `-bios` (machine pc, 64 MiB, isa-debug-exit at F4H, COM1 into a
  file). Pass at N CPUs: COM1 carries exactly `expected_serial(N)` and QEMU exits 1 (the image's isa-debug-exit write: on
  natemu its PM1a write at B004H ends the run first).
- program: exit_prog.asm's body at 110000H run by tools/bench/smp's stub (Multiboot through SeaBIOS) at CPL 0 in 64-bit
  mode on CPU 0, with ncpu 1 in the stub's config (the stub starts no CPU) and `-smp N` (the program starts the others).
  Pass: CPU 0's final registers are `expected_registers(N)`, no fault, QEMU exits 1 (the stub's dump and debug exit).
Backends: qemu, the pinned QEMU 11.1.2 (NATEMU_QEMU_SYSTEM) under TCG, CPU `max,-pcid,-smap` (natemu's model, as the smp
system programs); kvm, real hardware: the x86 reference machine NATEMU_X86_HOST under KVM, `-cpu host`, with Debian
trixie's QEMU at the campaign images' snapshot (tools/correctness/recipes/common.sh SNAP) installed in the builds' pinned
container image for the run (as tests/io/run_io5.py --backend kvm and the ltp32 reference boots), all N in one session.
Answers (tools/reference/answers.py, NATEMU_ANSWERS): seed runs the backend and records each N's answer (COM1's text or
CPU 0's registers, and QEMU's exit status) under a key of the image's sha256, the backend's pin and QEMU's arguments;
locked never runs it: the lock's answer must be the expected one (an image changed since seeding has none:
ANSWER-MISSING, reseed); live (unset) runs it. Writes DIR/results.json (one result per N) and DIR/exit_fw.json (the
images' sha256, the answers, the reference machine); exit 1 on a failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, hashlib, io, json, os, pathlib, re, shlex, subprocess, sys, tarfile, uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = ROOT / 'tests' / 'boot'
sys.path[:0] = [str(ROOT / 'tools' / 'bench' / 'smp'), str(ROOT / 'tools' / 'reference')]
import smp, answers  # noqa: E402

CPUS = (1, 2, 4, 8)
AP_BASE = 0x8000                                # exit_ap.inc's stub page (SIPI vector 08H)
FW_SIZE = 0x10000                               # at 4 GiB - FW_SIZE
BODY = smp.LOW                                  # the program's body (110000H), its start RIP 1000H further
START = BODY + 0x1000
RSP = 0x3E1800                                  # CPU 0's stack in the smp stub's map (never used: no stack)
FW_RAM = 64 << 20                               # natemu's firmware runs: the references' -m 64
# Per vCPU (plan section 8): decode exits, the BSP's 49 16- and 32-bit instructions and an AP's 30 in runs of up to 16
# (19 and 16 at 1, 2, 4 and 8 CPUs, exit-fw-natemu, M2 Max, November 22); shootdowns asked, one for each peer holding a
# writable shadow of the stub page (its counter stores) when a vCPU marks the page for its 64-bit code's translations
# (plan section 5; at most 3 there at 4 and at 8 CPUs, timing-dependent; one mark asks at most N - 1).
DECODES_MAX = 32
SHOOTDOWNS_MAX = 8
TCG_CPU = smp.SYSTEM_CPU
KVM_CPU = 'host'
KVM_IMAGE = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'   # = testkit/artifacts.py BENCH_BUILDER
QEMU_TIMEOUT = 120                              # seconds per boot (a pass takes about one)
APT_TIMEOUT = 900
REGIONS = {'firmware': [('fw16', 'fw16_end', 16), ('fw32', 'fw32_end', 32), ('reset', 'reset_end', 16),
                        ('ap16', 'ap16_end', 16), ('ap32', 'ap32_end', 32)],
           'program': [('ap16', 'ap16_end', 16), ('ap32', 'ap32_end', 32)]}


def symbols(map_file):
    """nasm's map (`[map symbols FILE]`): {name: address} of the non-local labels."""
    out = {}
    for line in pathlib.Path(map_file).read_text().splitlines():
        m = re.fullmatch(r'\s*([0-9A-F]+)\s+([0-9A-F]+)\s+(\S+)', line)
        if m: out[m[3]] = int(m[2], 16)
    return out


def build(out):
    """Assemble both images into `out`: {'firmware'|'program': {'path', 'bytes', 'sha256', 'symbols', 'base'}}."""
    out.mkdir(parents=True, exist_ok=True)
    images = {}
    for variant, src, base, defs in (('firmware', 'exit_fw.asm', 2 ** 32 - FW_SIZE, []),
                                     ('program', 'exit_prog.asm', BODY, [f'-DBODY={BODY:#x}'])):
        img, mp = out / f'{src[:-4]}.bin', out / f'{src[:-4]}.map'
        subprocess.run(['nasm', '-f', 'bin', '-I', f'{HERE}/', *defs, f'-DMAPFILE={mp}', '-o', str(img), str(HERE / src)],
                       check=True)
        data = img.read_bytes()
        images[variant] = {'path': img, 'bytes': data, 'sha256': hashlib.sha256(data).hexdigest(), 'symbols': symbols(mp),
                           'base': base}
    fw, prog = images['firmware'], images['program']
    assert len(fw['bytes']) == FW_SIZE, f"firmware is {len(fw['bytes'])} bytes, not {FW_SIZE}"
    assert fw['symbols']['reset'] == 2 ** 32 - 16, 'the reset vector is not at FFFFFFF0H'
    assert prog['symbols']['start'] == START and prog['symbols']['end'] == BODY + len(prog['bytes'])
    for im in images.values():
        s = im['symbols']
        assert s['ap_end'] - s['ap_src'] <= 0x1000 and s['ap_src'] % 16 == 0, 'the AP stub does not fit its page'
    return images


def region(image, start, end):
    """The bytes of [start, end) (labels of the image's map) and their address."""
    s, base = image['symbols'], image['base']
    return image['bytes'][s[start] - base:s[end] - base], s[start]


def program():
    """The natemu-smp-v1 program (tools/bench/smp/README.md) the stub runs: one CPU in its config; QEMU's -smp gives the rest."""
    return {'format': 'natemu-smp-v1', 'name': 'exit-ap', 'ncpu': 1, 'system': True, 'memory': [],
            'bodies': [{'addr': BODY, 'code': None}],
            'cpus': [{'rip': START, 'gpr': [RSP if i == 4 else 0 for i in range(16)], 'rflags': 0, 'cpl': 0}]}


def expected_serial(n):
    ids = ','.join(str(i) for i in range(1, n)) or '-'
    return f'EXITFW long mode\nEXITFW cpus={n} aps={n - 1} ids={ids} status=0\n'


def expected_registers(n):
    """CPU 0's GPRs at the program's end (rax rcx rdx rbx rsp ...) and RFLAGS under smp.FLAG_MASK (`cmp eax, eax`)."""
    g = [0] * 16
    g[0], g[3], g[1], g[2], g[4] = n, n - 1, 0, ((1 << n) - 1) & ~1, RSP
    return g, 0x44


def judge(variant, n, ans, want_exit=1):
    """Differences between an answer and what N CPUs must give (empty: pass). `want_exit`: how the run must end, QEMU's
    exit status 1 (the debug exit) on the references; a natemu case passes its own (the `poweroff` stop for the
    firmware, the end reached for the program)."""
    d = [] if ans.get('exit') == want_exit else [f"exit {ans.get('exit')} (want {want_exit})"]
    if variant == 'firmware':
        if ans.get('serial') != expected_serial(n): d.append(f"COM1 {ans.get('serial')!r} != {expected_serial(n)!r}")
        return d
    g, f = expected_registers(n)
    if ans.get('fault') is not None: d.append(f"CPU 0 fault {ans['fault']}")
    for k, (x, y) in enumerate(zip(ans.get('gpr') or [], g)):
        if x != y: d.append(f'{smp.REGS[k]} {x:#x} != {y:#x}')
    if len(ans.get('gpr') or []) != 16: d.append('no registers')
    if ans.get('rflags') != f: d.append(f"rflags {ans.get('rflags')} != {f:#x} (masked)")
    return d


def qemu_args(variant, n, backend):
    """QEMU's arguments without the executable, IMG and OUT standing for the image and the output file."""
    if variant == 'firmware':
        args = ['-accel', 'tcg,thread=multi', '-cpu', TCG_CPU, '-smp', str(n), '-m', '64', '-machine', 'pc', '-nographic',
                '-nodefaults', '-monitor', 'none', '-serial', 'file:OUT', '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04',
                '-no-reboot', '-bios', 'IMG']
    else:
        args = [str(a) for a in smp.qemu_args(n, 'multi', 'OUT', 'IMG', TCG_CPU)[1:]]
    if backend == 'kvm':
        args[args.index('-accel') + 1], args[args.index('-cpu') + 1] = 'kvm', KVM_CPU
    return args


def snapshot():
    return re.search(r'^SNAP=(\S+)$', (ROOT / 'tools/correctness/recipes/common.sh').read_text(), re.M)[1]


def pin(backend):
    if backend == 'qemu': return answers.pin('system')
    return f'kvm: qemu-system-x86 of Debian trixie at snapshot {snapshot()}'


def answer_of(variant, out_file, rc):
    """The answer of one boot from its output file: COM1's text (CRs dropped) or the stub's parsed dump (CPU 0)."""
    data = out_file.read_bytes() if out_file.exists() else b''
    if variant == 'firmware':
        return {'exit': rc, 'serial': data.decode(errors='replace').replace('\r', '')}
    try:
        c = smp._parse_dump(program(), data)['cpus'][0]
    except ValueError as e:
        return {'exit': rc, 'dump': f'unreadable ({e}; {len(data)} bytes)'}
    return {'exit': rc, 'gpr': c['gpr'], 'rflags': c['rflags'] & smp.FLAG_MASK, 'fault': c['fault']}


def boot_image(variant, images, work):
    """The file QEMU boots: the firmware itself, or the smp stub's Multiboot image with the program's body."""
    if variant == 'firmware': return images['firmware']['bytes']
    prog = program()
    prog['bodies'][0]['code'] = images['program']['bytes'].hex()
    return smp.build_image(prog, smp.build_stub(work / 'stub'))


def run_tcg(variant, n, img, work):
    work.mkdir(parents=True, exist_ok=True)
    p, out = work / 'image.bin', work / f'out-{n}'
    p.write_bytes(img)
    out.unlink(missing_ok=True)
    args = [a.replace('IMG', str(p)).replace('OUT', str(out)) for a in qemu_args(variant, n, 'qemu')]
    try:
        r = subprocess.run([smp.QEMU, *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=QEMU_TIMEOUT)
        rc = r.returncode
        if r.stderr.strip(): print(f'QEMU N={n}: {r.stderr.decode(errors="replace")[-400:]}', flush=True)
    except subprocess.TimeoutExpired:
        rc = 'timeout'
    return answer_of(variant, out, rc)


def run_kvm(variant, cpus, img, work):
    """All of `cpus` in one session on NATEMU_X86_HOST: ({n: answer}, the reference: machine, kernel, QEMU)."""
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: raise SystemExit('--backend kvm runs on the x86 reference machine: set NATEMU_X86_HOST=user@host')
    work.mkdir(parents=True, exist_ok=True)
    name = f'natemu-exitfw-{uuid.uuid4().hex[:12]}'
    runs = '; '.join(f"timeout -s KILL {QEMU_TIMEOUT} qemu-system-x86_64 "
                     + ' '.join(shlex.quote(a.replace('IMG', '/b/image.bin').replace('OUT', f'/b/out/out-{n}'))
                                for a in qemu_args(variant, n, 'kvm')) + f' < /dev/null; echo $? > /b/out/rc-{n}'
                     for n in cpus)
    apt = f'http://snapshot.debian.org/archive/debian/{snapshot()}/'
    guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] {apt} trixie main' > /etc/apt/sources.list; "
             f'timeout {APT_TIMEOUT} apt-get -o Acquire::Retries=5 update -qq >&2 && '
             f'timeout {APT_TIMEOUT} apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends qemu-system-x86 >&2 && '
             'echo "ref-qemu: $(qemu-system-x86_64 --version | head -1)" >&2 && mkdir -p /b/out && '
             f'{runs}; chmod -R a+rwX /b/out')
    script = (f'set -e; d=$(mktemp -d); '
              f"trap 'docker kill {name} >/dev/null 2>&1 || true; docker run --rm -v \"$d:/b\" {KVM_IMAGE} rm -rf /b/out "
              f">/dev/null 2>&1 || true; rm -rf \"$d\"' EXIT HUP TERM INT; tar xf - -C \"$d\"; "
              "echo \"ref-cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')\" >&2; "
              'echo "ref-kernel: $(uname -r)" >&2; '
              f'docker run --rm -i --name {name} --device /dev/kvm -v "$d:/b" {KVM_IMAGE} sh -c {shlex.quote(guest)} '
              '< /dev/null >&2 || true; tar cf - -C "$d" out')
    pack = io.BytesIO()
    with tarfile.open(fileobj=pack, mode='w') as t:
        info = tarfile.TarInfo('image.bin'); info.size = len(img)
        t.addfile(info, io.BytesIO(img))
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, script], input=pack.getvalue(), capture_output=True,
                       timeout=2 * APT_TIMEOUT + len(cpus) * QEMU_TIMEOUT + 300)
    err = r.stderr.decode(errors='replace')
    (work / 'kvm.log').write_text(err)
    ref = {k: v.strip() for k, v in re.findall(r'^ref-(\w+): (.*)$', err, re.M)}
    if r.returncode != 0 or not r.stdout:
        raise SystemExit(f'the KVM session on {host} failed (exit {r.returncode}): {err[-600:]}')
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        t.extractall(work, filter='data')
    got = {}
    for n in cpus:
        rcf = work / 'out' / f'rc-{n}'
        rc = int(rcf.read_text()) if rcf.exists() else 'none'
        got[n] = answer_of(variant, work / 'out' / f'out-{n}', 'timeout' if rc == 137 else rc)
    return got, ref                             # the machine's CPU and kernel, the QEMU (not its address)


def natemu_description(image, n):
    """The program variant in natemu's program driver (the contract's description): CPU 0 runs the program, the others
    wait for a SIPI."""
    lines = ['identity', f'body {BODY:#x} {image.hex()}', f'region {AP_BASE:#x} 0x1000', 'cpu', f'rip {START:#x}', 'cpl 0',
             'nxe 0', 'if 0', 'gpr ' + ' '.join(hex(RSP) if i == 4 else '0' for i in range(16))]
    return '\n'.join(lines + ['cpu', 'wait-for-sipi'] * (n - 1)) + '\n'


def run_natemu(n, images, work, fexc):
    """One run of the program variant under natemu: the answer `judge` reads (`exit` 'reached' when CPU 0 reached the
    program's end), with each AP's end (`aps`) for the report."""
    import run_slice1_vm as vm                  # tests/lib
    host, runtime = vm.build()
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(natemu_description(images['program']['bytes'], n))
    try:
        r = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, *vm.frame_alias_args(),
                            *vm.CODE_SHAPE], capture_output=True, text=True, timeout=QEMU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'exit': 'timeout'}
    if r.returncode:
        return {'exit': f'driver exit {r.returncode}: {r.stderr.strip()[-400:]}'}
    out = json.loads(r.stdout)
    cpus = out['cpus'] if 'cpus' in out else [out]
    (work / f'out-{n}.json').write_text(json.dumps(out, indent=1) + '\n')
    bsp = cpus[0]
    aps = [{'stop': c['stop'], 'reason': c.get('reason', ''), **{k: (c.get('startup') or {}).get(k) for k in ('cs', 'cs_base', 'rip')}}
           for c in cpus[1:]]
    return {'exit': bsp['stop'], 'gpr': [smp.num(x) for x in bsp['gpr']], 'rflags': smp.num(bsp['rflags']) & smp.FLAG_MASK,
            'fault': None if bsp['stop'] == 'reached' else f"{bsp['stop']} {bsp.get('reason')}", 'aps': aps}


def run_natemu_firmware(n, images, work, fexc):
    """One run of the firmware in natemu's firmware boot mode (`natemu-host RUNTIME boot-linux FEXC IMAGE --firmware`): the
    answer `judge` reads (`exit` natemu's stop, `poweroff` for the image's PM1a write; `serial` COM1's text) with each
    vCPU's stop, where it waited, decode exits and shootdowns, and the BSP's final state and stop detail."""
    import run_slice1_vm as vm                  # tests/lib
    host, runtime = vm.build()
    work.mkdir(parents=True, exist_ok=True)
    image, serial = work / 'exit_fw.bin', work / f'com1-{n}.txt'
    image.write_bytes(images['firmware']['bytes'])
    serial.unlink(missing_ok=True)
    try:
        r = subprocess.run([host, runtime, 'boot-linux', fexc, image, '--firmware', '--vcpus', str(n), '--ram', hex(FW_RAM),
                            '--serial', serial, '--watchdog-ms', '30000', *vm.CODE_SHAPE],
                           capture_output=True, text=True, timeout=QEMU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'exit': 'timeout'}
    (work / f'stderr-{n}.txt').write_text(r.stderr)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'exit': f'driver exit {r.returncode}: {r.stderr.strip()[-400:]}'}
    (work / f'out-{n}.json').write_text(json.dumps(out, indent=1) + '\n')
    text = serial.read_bytes().decode(errors='replace').replace('\r', '') if serial.exists() else ''
    vcpus = [{'stop': v['stop'], 'waiting': v.get('waiting'), 'decodes': v.get('decodes', 0), 'shootdowns': v['shootdowns'],
              'native': [int(x, 16) for x in v['native']]} for v in out['vcpus']]
    return {'exit': out['stop'], 'reason': out['reason'], 'serial': text, 'state': out['state'], 'vcpus': vcpus}


def judge_bounds(ans):
    """Differences from the bounds on decode exits and shootdowns per vCPU (plan section 8)."""
    return [f"CPU {i}: {k} {v[k]} > {m}" for i, v in enumerate(ans.get('vcpus', []))
            for k, m in (('decodes', DECODES_MAX), ('shootdowns', SHOOTDOWNS_MAX)) if v[k] > m]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--variant', choices=('firmware', 'program'), required=True)
    ap.add_argument('--backend', choices=('qemu', 'kvm', 'natemu'), required=True)
    ap.add_argument('--cpus', type=int, nargs='+', default=list(CPUS))
    ap.add_argument('--fexc', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    images = build(a.out / 'build')
    if a.backend == 'natemu' and a.variant == 'firmware':
        if not a.fexc: raise SystemExit('--backend natemu needs --fexc')
        results = []
        for n in a.cpus:
            ans = run_natemu_firmware(n, images, a.out / 'natemu', a.fexc)
            diffs = judge('firmware', n, ans, want_exit='poweroff') + judge_bounds(ans)
            if diffs and ans.get('exit') == 'resident':
                st = ans['state']
                diffs.append(f"BSP stop {ans['reason'][:120]} at CS {st['seg'][1]}:{st['rip']}, CR0 {st['cr0']}, EFER {st['efer']}")
            results.append({'name': f'firmware-natemu N={n}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
            print(f"firmware natemu N={n}: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
            for line in ans.get('serial', '').splitlines(): print(f'  COM1: {line}', flush=True)
            print(f"  stop {ans.get('exit')} {ans.get('reason', '')}"[:300], flush=True)
            for i, v in enumerate(ans.get('vcpus', [])):
                print(f"  CPU {i}: {v['stop'][:80]}, waiting {v['waiting']}, decodes {v['decodes']}, shootdowns {v['shootdowns']}",
                      flush=True)
        (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
        return 1 if any(r['diffs'] for r in results) else 0
    if a.backend == 'natemu':
        if not a.fexc:
            raise SystemExit('--backend natemu runs the program variant, with --fexc')
        results = []
        for n in a.cpus:
            ans = run_natemu(n, images, a.out / 'natemu', a.fexc)
            diffs = judge('program', n, ans, want_exit='reached')
            if diffs and ans.get('aps'):
                diffs.append(f"APs: {ans['aps']}")
            results.append({'name': f'program-natemu N={n}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
            print(f"program natemu N={n}: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
        (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
        return 1 if any(r['diffs'] for r in results) else 0
    img = boot_image(a.variant, images, a.out)
    sha = hashlib.sha256(img).hexdigest()
    keys = {}
    for n in a.cpus:
        args = qemu_args(a.variant, n, a.backend)
        repro = {'supplier': 'qemu-system' if a.backend == 'qemu' else 'kvm', 'pin': pin(a.backend), 'image_sha256': sha,
                 'args': args, 'test': f'exit-{a.variant}'}
        keys[n] = (answers.make_key('exit-fw', a.variant, repro['pin'], args, sha), repro)
    mode, got, ref = answers.mode(), {}, {}
    if mode == 'locked':
        for n, (k, _) in keys.items():
            try:
                e = answers.lookup(k)
                got[n] = json.loads(e.value) if e.value is not None else {'locked': 'hash only'}
            except answers.Missing:
                got[n] = {'missing': k}
    elif a.backend == 'qemu':
        got = {n: run_tcg(a.variant, n, img, a.out / 'tcg') for n in a.cpus}
    else:
        got, ref = run_kvm(a.variant, a.cpus, img, a.out / 'kvm')
    results = []
    for n in a.cpus:
        k, repro = keys[n]
        if mode == 'seed' and 'missing' not in got[n]:
            answers.record(k, answers.canon(got[n]), {**repro, **({'reference': ref} if ref else {})})
        diffs = [f'no answer locked for this image ({k}): seed it'] if 'missing' in got[n] else judge(a.variant, n, got[n])
        results.append({'name': f'{a.variant}-{a.backend} N={n}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
        print(f"{a.variant} {a.backend} N={n}: {'pass' if not diffs else 'FAIL ' + '; '.join(diffs)}", flush=True)
        if a.variant == 'firmware' and 'serial' in got[n]:
            for line in got[n]['serial'].splitlines(): print(f'  COM1: {line}', flush=True)
        elif 'gpr' in got[n]:
            g = got[n]['gpr']
            print(f'  CPU 0: rax (CPUs) {g[0]}, rbx (APs arrived) {g[3]}, rcx (status) {g[1]}, rdx (IDs) {g[2]:#x}', flush=True)
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    (a.out / 'exit_fw.json').write_text(json.dumps({
        'mode': mode, 'variant': a.variant, 'backend': a.backend, 'image_sha256': sha,
        'images': {v: {'sha256': im['sha256'], 'bytes': len(im['bytes'])} for v, im in images.items()},
        'reference': ref, 'answers': {str(n): got[n] for n in a.cpus}}, indent=1, default=str) + '\n')
    return 1 if any(r['diffs'] for r in results) else 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
