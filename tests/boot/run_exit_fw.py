#!/usr/bin/env python3
"""The M2 exit test's firmware and its AP part as a 64-bit program, on the references (docs/restructure/m2-exit-firmware.md).

    tests/boot/run_exit_fw.py --variant firmware|program --backend qemu|kvm [--cpus 1 2 4] --out DIR

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

CPUS = (1, 2, 4)
FW_SIZE = 0x10000                               # at 4 GiB - FW_SIZE
BODY = smp.LOW                                  # the program's body (110000H), its start RIP 1000H further
START = BODY + 0x1000
RSP = 0x3E1800                                  # CPU 0's stack in the smp stub's map (never used: no stack)
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


def judge(variant, n, ans):
    """Differences between an answer and what N CPUs must give (empty: pass)."""
    d = [] if ans.get('exit') == 1 else [f"QEMU exit {ans.get('exit')} (want 1: the debug exit)"]
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--variant', choices=('firmware', 'program'), required=True)
    ap.add_argument('--backend', choices=('qemu', 'kvm'), required=True)
    ap.add_argument('--cpus', type=int, nargs='+', default=list(CPUS))
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    images = build(a.out / 'build')
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
