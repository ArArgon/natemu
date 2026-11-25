#!/usr/bin/env python3
"""glmark2-*: glmark2 on Mesa's llvmpipe (OpenGL rendered by the CPU), off-screen under Xvfb, booted under natemu, QEMU
or, for the real-x86 reference, KVM on the x86 machine (docs/benchmarks.md, "OpenGL on the CPU").

    tests/bench/run_glmark2.py run --emu natemu|qemu|kvm --kernel BZIMAGE --inputs GLMARK2_INPUTS --out DIR
        [--caps native|sse4.1] [--boots 5] [--vcpus 4] [--ram 4] [--cap 600]
        [--fexc FEXC --natemu NATEMU --runtime RUNTIME [--natemu-set KEY=VALUE ...]]   (natemu)
        [--qemu QEMU_SYSTEM]                                                               (qemu)

The guest is the Debian 6.12.107 kernel the other guest benchmarks boot and an initramfs: the input glmark2-inputs
(tools/bench/glmark2: glmark2 2023.01, Mesa 25.0.7's llvmpipe, Xvfb, from a fixed snapshot.debian.org date) with a
second, uncompressed cpio appended here: this script's guest init (GUEST), /dev/console and the folders it mounts on.
The init mounts /proc, /sys, /dev and tmpfs, starts Xvfb on :1, prints `glxinfo -B` (llvmpipe's renderer string names
its vector width: 256 bits with AVX2, 128 with SSE4.1), then runs `glmark2 --off-screen -s 800x600` over SCENES, each
for DURATION seconds, and powers off. In the first boot it runs `glmark2 --validate` over the same scenes first: no
scene may report a failed validation (llvmpipe's picture checked against glmark2's reference).

--caps sse4.1 adds `glcaps=sse4.1` to the kernel command line, from which the init sets GALLIUM_OVERRIDE_CPU_CAPS: llvmpipe
then generates SSE4.1 code (128-bit vectors, no AVX2 gathers) whatever the CPU offers; the renderer string must say so.

natemu: 4 vCPUs, 4 GiB, its translation cache off (`--compile-cache off`, as the other guest benchmarks): every boot
translates from nothing, and glmark2's scenes include their first frames. QEMU: the pinned qemu-system-x86_64, TCG
thread=multi, the board and CPU model of the other guest benchmarks (tools/bench/run.py QMACHINE, QCPU). kvm: the x86
machine NATEMU_X86_HOST (ssh, Docker, /dev/kvm; as tests/io/run_io5.py --backend kvm) boots the same kernel and initramfs
under KVM with Debian's QEMU (at the recipe's snapshot) in the builds' pinned container image, `-cpu` QCPU: llvmpipe sees
the same CPU features there.

The numbers are glmark2's own: per scene its FPS and frame time (the guest's clock; FPS is rounded to an integer, the
frame time in ms has three decimals), and its score (the mean FPS over the scenes). Each of BOOTS boots is one line of
OUT/bench.jsonl; OUT/result.json holds the median per scene and of the score over the boots, and each one's spread
(min, max). natemu's system-instruction counts per boot (kind 7a: AVX2 gathers the resident ran) are recorded beside them.
Exit 0 when every boot ran every scene; 3 when another VM ran during a boot (contended: not comparable); 1 otherwise."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import os
import re
import shlex
import signal
import statistics
import stat
import subprocess
import time
from pathlib import Path

from io_common import clone, digest, stop_group, write_json
from run_io5 import others
from bench.run import QCPU, QMACHINE, system_counts       # QCPU = tools/bench/boot.py's: natemu's features, as TCG has them
from play.mkcpio import entry

# (glmark2 scene, its options): the experiment's ten, one of each kind of work (geometry, texturing, lighting, fragment
# shaders, 2D convolution, blending, a blurred desktop, the vertex-heavy jellyfish, refraction, shader branches).
SCENES = [('build', 'use-vbo=true'), ('texture', 'texture-filter=linear'), ('shading', 'shading=phong'),
          ('bump', 'bump-render=normals'), ('effect2d', 'kernel=0,1,0;1,-4,1;0,1,0;'), ('pulsar', ''),
          ('desktop', 'effect=blur'), ('jellyfish', ''), ('refract', ''), ('conditionals', '')]
DURATION = 5                     # seconds per scene
SIZE = '800x600'
CAPS = {'native': None, 'sse4.1': 'sse4.1'}
WIDTH = {'native': '256 bits', 'sse4.1': '128 bits'}      # llvmpipe's vector width in its renderer string
INIT = 'glmark2-init'
CMDLINE = f'console=ttyS0 mitigations=off nokaslr rdinit=/{INIT}'
KVM_IMAGE = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'   # = testkit/artifacts.py BENCH_BUILDER
SNAPSHOT = '20261120T000000Z'    # = tools/bench/glmark2/container.sh SNAP: Debian's QEMU for the kvm reference
KVM_DIR = 'natemu-glmark2-reference'

GUEST = r'''#!/bin/sh
# glmark2-init: glmark2 off-screen on llvmpipe under Xvfb, then power off. Markers "GLMARK2..." on the console.
B=/bin/busybox
$B mount -t proc proc /proc; $B mount -t sysfs sys /sys; $B mount -t devtmpfs dev /dev
$B mount -t tmpfs tmp /tmp; $B mkdir -p /dev/shm && $B mount -t tmpfs shm /dev/shm
export HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin GALLIUM_DRIVER=llvmpipe
caps=$($B sed -n 's/.*glcaps=\([^ ]*\).*/\1/p' /proc/cmdline)
[ -n "$caps" ] && export GALLIUM_OVERRIDE_CPU_CAPS=$caps
echo "GLMARK2-BEGIN cpus=$(nproc) caps=${caps:-native}"
Xvfb :1 -screen 0 1024x768x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
i=0; while [ ! -S /tmp/.X11-unix/X1 ] && [ $i -lt 1200 ]; do sleep 0.1; i=$((i + 1)); done
export DISPLAY=:1
glxinfo -B 2>&1 | $B grep -E 'renderer string|version string' | $B sed 's/^/GLMARK2-INFO /'
if $B grep -q glvalidate=1 /proc/cmdline; then
  { glmark2 --off-screen -s @SIZE@ --validate @BENCH@; echo "rc=$?"; } 2>&1 | $B sed 's/^/GLMARK2-VALIDATE /'
fi
{ glmark2 --off-screen -s @SIZE@ @BENCH@; echo "rc=$?"; } 2>&1 | $B sed 's/^/GLMARK2 /'
echo "GLMARK2-END uptime=$(cut -d' ' -f1 /proc/uptime)"
$B sync; $B poweroff -f
'''


def bench_args() -> str:
    return ' '.join('-b ' + shlex.quote(':'.join(x for x in (name, opts, f'duration={DURATION}') if x)) for name, opts in SCENES)


def guest_script() -> str:
    return GUEST.replace('@SIZE@', SIZE).replace('@BENCH@', bench_args())


def tail_cpio() -> bytes:
    """The uncompressed cpio appended to the userland: the init, /dev/console and the folders the init mounts on."""
    import io
    out = io.BytesIO()
    items = [('dev', stat.S_IFDIR | 0o755, b'', (0, 0)), ('dev/console', stat.S_IFCHR | 0o600, b'', (5, 1)),
             ('proc', stat.S_IFDIR | 0o555, b'', (0, 0)), ('sys', stat.S_IFDIR | 0o555, b'', (0, 0)),
             ('tmp', stat.S_IFDIR | 0o1777, b'', (0, 0)),
             ('etc/hosts', stat.S_IFREG | 0o644, b'127.0.0.1 localhost\n', (0, 0)),
             (INIT, stat.S_IFREG | 0o755, guest_script().encode(), (0, 0))]
    for ino, (name, mode, data, rdev) in enumerate(items, 1): entry(out, ino, name, mode, data, rdev)
    entry(out, 0, 'TRAILER!!!', 0)
    return out.getvalue()


def make_initrd(inputs: Path, dest: Path) -> dict:
    """OUT/initrd.img: the userland (an APFS clone of the input), zero-padded to 4 bytes (the kernel reads an appended
    archive only from a 4-byte boundary), then tail_cpio()."""
    src = inputs / 'userland.cpio.zst'
    recorded = json.loads((inputs / 'provenance.json').read_text())
    sha = digest(src)
    if sha != recorded['userland_sha256']: raise SystemExit(f'{src}: sha256 {sha}, its entry recorded {recorded["userland_sha256"]}')
    dest.unlink(missing_ok=True)
    clone(src, dest)
    with open(dest, 'ab') as f:
        f.write(b'\0' * (-dest.stat().st_size % 4)); f.write(tail_cpio())
    return {'userland_sha256': sha, 'builder': recorded.get('builder')}


LINE = re.compile(r'^GLMARK2 \[(\w+)\] (\S*): FPS: (\d+) FrameTime: ([0-9.]+) ms', re.M)


def parse(serial: str) -> dict:
    """One boot's results from its console: per scene FPS and frame time, the score, the renderer, validation."""
    scenes = {m.group(1): {'options': m.group(2), 'fps': int(m.group(3)), 'frame_ms': float(m.group(4))} for m in LINE.finditer(serial)}
    score = re.search(r'^GLMARK2 .*glmark2 Score: (\d+)', serial, re.M)
    renderer = re.search(r'^GLMARK2-INFO .*OpenGL renderer string: (.*)$', serial, re.M)
    validation = {m.group(1): m.group(2) for m in re.finditer(r'^GLMARK2-VALIDATE \[(\w+)\] .*Validation: (\w+)', serial, re.M)}
    rc = re.findall(r'^GLMARK2(?:-VALIDATE)? rc=(\d+)', serial, re.M)
    begin = re.search(r'^GLMARK2-BEGIN cpus=(\d+) caps=(\S+)', serial, re.M)
    return {'scenes': scenes, 'score': int(score.group(1)) if score else None,
            'renderer': renderer.group(1).strip() if renderer else None, 'validation': validation,
            'rc': [int(x) for x in rc], 'cpus': int(begin.group(1)) if begin else None, 'caps': begin.group(2) if begin else None,
            'ended': '\nGLMARK2-END ' in '\n' + serial}


def problems(r: dict, caps: str, validated: bool, vcpus: int) -> list[str]:
    """Why a boot's results do not count (empty: they do)."""
    out = []
    if not r['ended']: out.append('no GLMARK2-END line')
    missing = [n for n, _ in SCENES if n not in r['scenes']]
    if missing: out.append(f'scenes missing: {", ".join(missing)}')
    if r['score'] is None: out.append('no score')
    if r['cpus'] != vcpus: out.append(f'guest saw {r["cpus"]} CPUs, not {vcpus}')
    if r['caps'] != caps: out.append(f'guest caps {r["caps"]}, not {caps}')
    if not r['renderer'] or 'llvmpipe' not in r['renderer'] or WIDTH[caps] not in r['renderer']:
        out.append(f'renderer {r["renderer"]!r}: not llvmpipe at {WIDTH[caps]}')
    if any(r['rc']): out.append(f'glmark2 exit status {r["rc"]}')
    if validated:
        if not r['validation']: out.append('validation ran no scene')
        bad = sorted(n for n, v in r['validation'].items() if v not in ('Success', 'Unknown'))
        if bad: out.append(f'validation failed: {", ".join(f"{n} {r["validation"][n]}" for n in bad)}')
    return out


def summary(boots: list[dict]) -> dict:
    """Median and spread per scene (FPS, frame time) and of the score over the boots."""
    def stats(xs): return {'median': statistics.median(xs), 'min': min(xs), 'max': max(xs), 'all': xs}
    return {'scenes': {n: {'fps': stats([b['scenes'][n]['fps'] for b in boots]),
                           'frame_ms': stats([b['scenes'][n]['frame_ms'] for b in boots])} for n, _ in SCENES},
            'score': stats([b['score'] for b in boots])}


def boot_local(args, i: int, initrd: Path, cmdline: str) -> dict:
    """One boot under natemu or QEMU on this Mac, its console in OUT/boot-I.serial."""
    serial, stderr = args.out / f'boot-{i}.serial', args.out / f'boot-{i}.stderr'
    serial.unlink(missing_ok=True)
    if args.emu == 'natemu':
        cmd = [args.natemu, '--runtime', args.runtime, '--fexc', args.fexc, '--kernel', args.kernel, '--initrd', initrd,
               '--cmdline', cmdline, '--vcpus', str(args.vcpus), '--ram', f'{args.ram}G', '--serial', serial,
               '--compile-cache', 'off', *(w for kv in args.natemu_set for w in ('--set', kv))]
    else:
        cmd = [args.qemu, '-nodefaults', '-machine', f'{QMACHINE},hpet=off,i8042=off,vmport=off', '-accel', 'tcg,thread=multi',
               '-cpu', QCPU, '-m', str(args.ram * 1024), '-smp', str(args.vcpus), '-kernel', args.kernel, '-initrd', initrd,
               '-append', cmdline, '-display', 'none', '-monitor', 'none', '-serial', f'file:{serial}', '-no-reboot']
    env = dict(os.environ, NATEMU_FP_TRAP_COUNTS='1')      # natemu prints its system-instruction counts at exit
    before = others()
    t0 = time.monotonic()
    p = subprocess.Popen([str(c) for c in cmd], env=env, stdin=subprocess.DEVNULL, stdout=open(args.out / f'boot-{i}.stdout', 'wb'),
                         stderr=open(stderr, 'wb'), start_new_session=True)
    seen, last, capped = set(before), 0.0, False
    try:
        while p.poll() is None:
            if time.monotonic() - t0 > args.cap: capped = True; break
            time.sleep(0.25)
            if time.monotonic() - last >= 0.5: seen |= set(others()); last = time.monotonic()
    finally:
        if p.poll() is None: stop_group(p)
    took = round(time.monotonic() - t0, 2)
    text = serial.read_text(errors='replace') if serial.exists() else ''
    r = parse(text)
    r.update(boot=i, seconds=took, capped=capped, exit=p.returncode, contended=sorted(seen),
             command=[str(c) for c in cmd])
    if args.emu == 'natemu': r['system_counts'] = system_counts(stderr.read_text(errors='replace'))
    return r


def boots_kvm(args, initrd: Path, cmdlines: list[str]) -> list[dict]:
    """Every boot on the x86 machine, in one container run (Debian's QEMU installed once), under a lock file there so no
    other reference boot runs beside it: the kernel and initramfs are copied to a run folder there, each boot's console
    comes back as OUT/boot-I.serial, and the folder is removed."""
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: raise SystemExit('--emu kvm needs NATEMU_X86_HOST=user@host (an x86-64 Linux machine with Docker and /dev/kvm)')
    ssh = ['ssh', '-o', 'BatchMode=yes', host]
    run = f'{KVM_DIR}/run-{os.getpid()}'; name = f'natemu-glmark2-ref-{os.getpid()}'
    subprocess.run([*ssh, f'mkdir -p {run}'], check=True)
    try:
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(args.kernel), str(initrd), f'{host}:{run}/'], check=True)
        qemu = [f'qemu-system-x86_64 -enable-kvm -cpu {QCPU} -nodefaults -machine pc,hpet=off,i8042=off,vmport=off '
                f'-m {args.ram * 1024} -smp {args.vcpus} -kernel /b/{args.kernel.name} -initrd /b/{initrd.name} '
                f'-append {shlex.quote(c)} -display none -monitor none -serial file:/b/boot-{i}.serial -no-reboot'
                for i, c in enumerate(cmdlines, 1)]
        loop = ' && '.join(f'echo "GLMARK2-HOST boot {i} start $(date +%s.%N)" && timeout {int(args.cap)} {q}; '
                           f'echo "GLMARK2-HOST boot {i} exit $? $(date +%s.%N)"' for i, q in enumerate(qemu, 1))
        guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/{SNAPSHOT}/ "
                 "trixie main' > /etc/apt/sources.list; apt-get -o Acquire::Retries=5 update -qq > /dev/null 2>&1 && "
                 "apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends qemu-system-x86 > /dev/null 2>&1 && "
                 'echo "GLMARK2-HOST $(qemu-system-x86_64 --version | head -1 | tr " " _) '
                 '$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | tr " " _) host-kernel=$(uname -r) load=$(cut -d" " -f1-3 /proc/loadavg | tr " " /)" && '
                 + loop)
        log = args.out / 'kvm.log'
        with open(log, 'wb') as f:
            # One reference boot at a time on that machine (the -kvm cases take one core here, so they could run at once):
            # its lock file, held for the whole container run.
            subprocess.run([*ssh, f'flock $HOME/{KVM_DIR}/.lock docker run --rm --name {name} --device /dev/kvm -v $HOME/{run}:/b {KVM_IMAGE} sh -c {shlex.quote(guest)}'],
                           stdout=f, stderr=subprocess.STDOUT, timeout=args.cap * len(cmdlines) + 900)
        out = []
        host_lines = log.read_text(errors='replace')
        for i in range(1, len(cmdlines) + 1):
            serial = args.out / f'boot-{i}.serial'; serial.unlink(missing_ok=True)
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', f'{host}:{run}/boot-{i}.serial', str(serial)])
            r = parse(serial.read_text(errors='replace') if serial.exists() else '')
            ex = re.search(rf'GLMARK2-HOST boot {i} exit (\d+) ([0-9.]+)', host_lines)
            st = re.search(rf'GLMARK2-HOST boot {i} start ([0-9.]+)', host_lines)
            r.update(boot=i, seconds=round(float(ex.group(2)) - float(st.group(1)), 2) if ex and st else None,
                     capped=bool(ex and int(ex.group(1)) == 124), exit=int(ex.group(1)) if ex else None, contended=[],
                     command=[qemu[i - 1]])
            out.append(r)
        facts = re.search(r'^GLMARK2-HOST (\S+) (\S+) host-kernel=(\S+) load=(\S+)', host_lines, re.M)
        if facts: out[0]['reference'] = dict(zip(('qemu', 'cpu', 'kernel', 'load'), facts.groups()))
        return out
    finally:
        subprocess.run([*ssh, f'docker rm -f {name} > /dev/null 2>&1; rm -rf {run}'], capture_output=True)


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    initrd = args.out / 'initrd.img'
    made = make_initrd(args.inputs, initrd)
    words = f' glcaps={CAPS[args.caps]}' if CAPS[args.caps] else ''
    cmdlines = [CMDLINE + words + (' glvalidate=1' if i == 1 else '') for i in range(1, args.boots + 1)]
    (args.out / 'bench.jsonl').unlink(missing_ok=True)
    boots = []
    try:
        remote = boots_kvm(args, initrd, cmdlines) if args.emu == 'kvm' else None
        for i, c in enumerate(cmdlines, 1):
            r = remote[i - 1] if remote else boot_local(args, i, initrd, c)
            r['problems'] = problems(r, args.caps, i == 1, args.vcpus) + (['capped'] if r['capped'] else [])
            boots.append(r)
            with open(args.out / 'bench.jsonl', 'a') as f: f.write(json.dumps({'name': args.name, 'emu': args.emu, 'caps': args.caps, **r}) + '\n')
            print(f'boot {i}: {r["seconds"]} s, score {r["score"]}, ' +
                  ' '.join(f'{n} {r["scenes"][n]["fps"]}' for n, _ in SCENES if n in r['scenes']) +
                  (f'; problems: {"; ".join(r["problems"])}' if r['problems'] else '') +
                  (f'; contended by {r["contended"]}' if r['contended'] else ''), flush=True)
            if r['problems']: break
    finally:
        initrd.unlink(missing_ok=True)
    ok = len(boots) == args.boots and not any(b['problems'] for b in boots)
    result = {'name': args.name, 'emu': args.emu, 'caps': args.caps, 'vcpus': args.vcpus, 'ram_gib': args.ram, 'boots': len(boots),
              'scenes': [f'{n}:{o}' if o else n for n, o in SCENES], 'duration_s': DURATION, 'size': SIZE, 'inputs': made,
              'kernel': digest(args.kernel), 'pass': ok, 'renderer': boots[0]['renderer'] if boots else None,
              'validation': boots[0]['validation'] if boots else None}
    if args.emu == 'natemu': result['natemu'] = digest(args.natemu)
    if args.emu == 'qemu': result['qemu'] = subprocess.run([str(args.qemu), '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    if args.emu == 'kvm' and boots: result['reference'] = boots[0].get('reference')
    if ok: result.update(summary(boots))
    write_json(args.out / 'result.json', result)
    if ok:
        s = result
        print('median over %d boots: score %s (%s-%s)' % (len(boots), s['score']['median'], s['score']['min'], s['score']['max']))
        for n, _ in SCENES:
            f, m = s['scenes'][n]['fps'], s['scenes'][n]['frame_ms']
            print(f'  {n:13} FPS {f["median"]:>6} ({f["min"]}-{f["max"]})  frame {m["median"]:.3f} ms ({m["min"]:.3f}-{m["max"]:.3f})')
    print('PASS' if ok else 'FAIL', args.out, flush=True)
    if not ok: return 1
    return 3 if any(b['contended'] for b in boots) else 0


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--emu', choices=('natemu', 'qemu', 'kvm'), required=True)
    r.add_argument('--name', default='glmark2')
    r.add_argument('--kernel', type=Path, required=True)
    r.add_argument('--inputs', type=Path, required=True, help='the glmark2-inputs folder (testkit/inputs.py:glmark2_inputs)')
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--caps', choices=tuple(CAPS), default='native', help="sse4.1: llvmpipe held to SSE4.1 (GALLIUM_OVERRIDE_CPU_CAPS)")
    r.add_argument('--boots', type=lambda v: max(1, int(v)), default=5, help='boots (the median per scene is reported)')
    r.add_argument('--vcpus', type=int, default=4); r.add_argument('--ram', type=int, default=4, help='GiB')
    r.add_argument('--cap', type=float, default=600, help='seconds per boot')
    r.add_argument('--fexc', type=Path); r.add_argument('--natemu', type=Path); r.add_argument('--runtime', type=Path)
    r.add_argument('--natemu-set', action='append', default=[], metavar='KEY=VALUE', help='natemu: passed on as --set KEY=VALUE (the bench harness turns the platform devices off with it)')
    r.add_argument('--qemu', type=Path)
    args = a.parse_args()
    if args.emu == 'natemu' and not (args.fexc and args.natemu and args.runtime): a.error('--emu natemu needs --fexc, --natemu and --runtime')
    if args.emu == 'qemu' and not args.qemu: a.error('--emu qemu needs --qemu')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
