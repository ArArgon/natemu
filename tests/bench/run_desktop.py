#!/usr/bin/env python3
"""desktop-kde-*: a KDE Plasma 6 Wayland desktop booted under natemu (or, for the real-x86 reference, under KVM on the
x86 machine), profiled where its time goes: the session's start, the idle desktop, the shell's interactions, app starts
(Konsole, Dolphin; the browsers) and Speedometer 3.1 in Chromium. A debug-tier profile (docs/testing.md, "The debug tier").

    tests/bench/run_desktop.py run --emu natemu|kvm --kernel BZIMAGE --inputs DESKTOP_INPUTS --out DIR [--name NAME]
        [--phases idle,ui,apps] [--boots 1] [--vcpus 4] [--ram 8] [--cap 2400] [--sp-iterations 5]
        [--fexc FEXC --natemu NATEMU --runtime RUNTIME [--perf-log] [--sample-us N] [--guest-perf]
         [--compile-cache off|warm [--compile-cache-mb N]] [--natemu-set KEY=VALUE ...]]             (natemu)
    tests/bench/run_desktop.py analyse --out DIR          (the analysis again, from a finished run's files)

**The guest** is the input desktop-inputs (tools/bench/desktop/container.sh): Debian 13's Plasma 6.3 Wayland session
(kwin-wayland, plasma-workspace, plasma-desktop, no recommends), Konsole, Dolphin, Chromium, Firefox ESR, Mesa's llvmpipe,
systemd and perf on an ext4 disk (the whole disk, root=/dev/vda), booted on the Debian 6.12.107 kernel the other guest
benchmarks boot with the image's initramfs, to which this script appends the harness (tests/bench/desktop: the driver,
the user's settings, the KWin script that reports windows, Speedometer's hook and server) as /de-harness. The disk is an
APFS clone of the input's, per boot, removed after it. systemd starts getty's autologin of user `de` on tty1, whose login
starts the session (startplasma-wayland); KWin draws on the Bochs display through DRM with Mesa's llvmpipe
(kms_swrast), 1920 x 1080. The driver (de-bench.service, root; tests/bench/desktop/driver.sh) waits for the shell, then
runs the phases: `idle` (20 s), `ui` (the launcher, the Overview, the virtual desktops), `apps` (Konsole and Dolphin, each
started twice; window moves, resizes, maximize, switches), `browsers` (Chromium and Firefox ESR, each started twice),
`speedometer` (Speedometer in Chromium). Its console lines (DE-...) carry the guest's uptime.

**natemu's machine**: 4 vCPUs, 8 GiB, the disk and the entropy device on PCI (what a configuration file gives), the
Bochs display (`--set display.device=bochs`, display.width/height) with the control socket (`--set control.socket`),
the guest kernel's mitigations at its defaults and natemu's speculation barriers on (`engine.speculation_barriers`, so
the guest leaves PTI off: speedometer-desktop-profile's desktop setting), the translation cache off unless
--compile-cache warm. The runner stays connected to the control socket for the whole boot and records every
`display_changed` notification (a page flip moves the scanout's offset: KWin's presented frames) with the host's
CLOCK_UPTIME_RAW, and writes a PNG snapshot at each of the driver's SNAP lines (OUT/boot-I-NAME.png).

--compile-cache warm: the persistent cache in OUT/compile-cache at natemu's default cap (512 MiB; --compile-cache-mb N
another), filled by boot 0 (the same workload, untimed, recorded as the fill), then the BOOTS boots reuse it, as a desktop's
next session would. The cache folder's packs and bytes after each boot are recorded.

--perf-log (with the `perf` resident): each boot's timing log OUT/boot-I.perf, its 5 ms `snap` lines (each vCPU's
resident time by bucket) and the per-vCPU request lines (OUT/boot-I.perf.vN). --sample-us N (one boot; the `perf` and
`profile` resident): natemu's EL0 sampler into OUT/NAME.samples and NAME.objects, the phases stamped in
OUT/NAME.markers.tsv for tests/bench/sample_report.py (NATEMU-BENCH-START at the session's start, a NATEMU-BENCH-PHASE
line at each window's end, -END after the last phase), and the image's ELF and symbol files as OUT/userland.cpio.
--guest-perf: the guest's own perf (cpu-clock, every CPU, 199 Hz) from the first phase after idle to the last, its
reports by process and object and by symbol printed at the end (OUT/boot-I.guest-perf.txt): the guest's view of which
process and library its CPU time went to, which natemu's samples (no address space in a record) cannot split.

**The analysis** (result.json, report.md): each window, from the driver's lines (the guest's uptime mapped to the host's
clock by the lower envelope of the lines' arrival stamps):
  session   the kernel to the session's start (autologin), the session's start to KWin's first frame (the first page
            flip after it), to plasmashell's first window, to the idle desktop (the CPUs quiet for 3 s)
  idle      20 s of the idle desktop: vCPU busy time per second, frames
  ui-*      each interaction, from its command to the CPUs quiet again
  app-*     each start from its command to the app's first window (KWin's time of the window's arrival), and on to the
            CPUs quiet again (settled)
For each: wall seconds; the resident's buckets summed over the vCPUs (from the snap lines: guest code, host, halted,
shadow, abort, link, tlb, ...) and the busy time (all but halted); translation requests begun in it (count, host time,
the parts fexc, admit, lookup, disk get, fetch, stage, keep; answers from memory, the disk cache and fexc; first-seen
code: requests whose guest bytes' hash the boot had not requested before); the guest's own counters (page faults, major
faults, the disk's reads, sectors, read and busy time, iowait jiffies); the frames presented and their gaps; and each
process's CPU time between the driver's PS lines. Exit 0 when every boot ran its phases to the end; 3 when another VM ran
during a boot (contended); 1 otherwise."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import bisect
import collections
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guest_userland as gu
from io_common import clone, digest, stop_group, write_json
from run_io5 import others
from bench.run import QCPU, QMACHINE
from play.mkcpio import entry

HARNESS = HERE / 'desktop'
SPEEDOMETER = HERE / 'speedometer'
KVER = '6.12.107+deb13-amd64'
WIDTH, HEIGHT = 1920, 1080
PHASES = ('idle', 'ui', 'apps', 'click', 'browsers', 'speedometer')
NATEMU_SETS = ['display.device=bochs', f'display.width={WIDTH}', f'display.height={HEIGHT}', 'engine.speculation_barriers=true',
               'machine.ps2=true']     # the keyboard the click phase types on (natemu_sets: in place of the harness's machine.ps2=false)
POLL = 0.02                  # s between reads of the console


def cmdline(args) -> str:
    """The guest kernel's command line: the disk as root, the console on ttyS0, quiet; the driver's phases (and under KVM,
    where nothing outside types, the guest's own uinput keyboard for the click phase)."""
    return (f'root=/dev/vda rw console=ttyS0 quiet loglevel=4 systemd.show_status=false nokaslr '
            f'de.phases={",".join(args.phases)} de.spiter={args.sp_iterations} de.guestperf={int(bool(args.guest_perf))}'
            + (' de.input=uinput' if args.emu == 'kvm' else '') + (' norandmaps' if args.norandmaps else ''))


def tail() -> bytes:
    """The harness as an uncompressed newc cpio appended to the initramfs: /de-harness, which the image's init-bottom
    script copies to the root's /run."""
    out, ino = io.BytesIO(), 0
    items = [('de-harness', stat.S_IFDIR | 0o755, b'')]
    for p in sorted(HARNESS.rglob('*')):
        rel = 'de-harness/' + p.relative_to(HARNESS).as_posix()
        if p.is_dir(): items.append((rel, stat.S_IFDIR | 0o755, b''))
        else: items.append((rel, stat.S_IFREG | (0o755 if p.suffix == '.sh' else 0o644), p.read_bytes()))
    for n in ('serve.py', 'hook.js'):
        items.append((f'de-harness/{n}', stat.S_IFREG | 0o644, (SPEEDOMETER / n).read_bytes()))
    for ino, (name, mode, data) in enumerate(items, 1): entry(out, ino, name, mode, data)
    entry(out, 0, 'TRAILER!!!', 0)
    return out.getvalue()


def make_initrd(inputs: Path, dest: Path) -> dict:
    """dest: the input's initrd.img (an APFS clone), zero-padded to 4 bytes, then the harness (tail())."""
    src = inputs / 'initrd.img'
    dest.unlink(missing_ok=True)
    clone(src, dest)
    with open(dest, 'ab') as f:
        f.write(b'\0' * (-dest.stat().st_size % 4)); f.write(tail())
    recorded = json.loads((inputs / 'provenance.json').read_text())
    versions = inputs / 'versions.txt'
    return {'disk_raw_sha256': recorded.get('disk_raw_sha256'), 'initrd_sha256': recorded.get('initrd_sha256'),
            'builder': recorded.get('builder'), 'versions': versions.read_text().strip().splitlines() if versions.is_file() else None}


def natemu_sets(args) -> list:
    """The harness's --natemu-set values (the platform devices off) and this case's (NATEMU_SETS), which win: natemu
    refuses a key set twice, so the harness's value of a key NATEMU_SETS sets (machine.ps2) is left out."""
    mine = {kv.split('=', 1)[0] for kv in NATEMU_SETS}
    return [kv for kv in args.natemu_set if kv.split('=', 1)[0] not in mine] + NATEMU_SETS


def natemu_command(args, initrd, disk, serial, sock, perf, cache, sample) -> list:
    return [args.natemu, '--runtime', args.runtime, '--fexc', args.fexc, '--kernel', args.kernel, '--initrd', initrd,
            '--cmdline', cmdline(args), '--vcpus', str(args.vcpus), '--ram', f'{args.ram}G', '--serial', serial,
            '--disk', disk, '--rng', '--virtio-transport', 'pci', '--compile-cache', cache,
            *(['--compile-cache-mb', str(args.compile_cache_mb)] if args.compile_cache_mb and cache != 'off' else []),
            *(['--perf-log', perf] if perf else []),
            *(w for kv in natemu_sets(args) + [f'control.socket={sock}'] for w in ('--set', kv)),
            *(['--profile', sample[0], '--profile-us', str(sample[1])] if sample else [])]


class Display(threading.Thread):
    """The control socket for the whole boot: every display_changed with its host stamp (CLOCK_UPTIME_RAW ns), and the
    snapshots asked for (snap(NAME) -> OUT/boot-I-NAME.png), on a second connection."""

    def __init__(self, path, alive, out, prefix):
        super().__init__(daemon=True)
        self.path, self.alive, self.out, self.prefix = path, alive, out, prefix
        self.events, self.snaps, self.errors, self.inputs = [], [], [], []
        self.queue, self.cv = [], threading.Condition()

    def run(self):
        from control_client import Control, Closed
        try: vm = Control.connect(self.path, timeout=120, alive=self.alive)
        except OSError as e: self.errors.append(f'control socket: {e}'); return
        threading.Thread(target=self.snapper, daemon=True).start()
        try:
            while True:
                m = vm.read()
                if isinstance(m, dict) and m.get('method') == 'display_changed':
                    self.events.append((time.clock_gettime_ns(time.CLOCK_UPTIME_RAW), m.get('params')))
        except (Closed, OSError, ValueError): pass
        finally: vm.close()

    def snap(self, name):
        with self.cv: self.queue.append(name); self.cv.notify()

    def input(self, words):
        """The driver's DE-INPUT line: `key CODE` (a tap) or `text STRING` (typed, US layout), sent in order with the snapshots."""
        with self.cv: self.queue.append(('input', words)); self.cv.notify()

    def snapper(self):
        from control_client import Control, RpcError
        vm = None
        while self.alive():
            with self.cv:
                if not self.queue: self.cv.wait(0.5); continue
                name = self.queue.pop(0)
            try:
                if vm is None: vm = Control.connect(self.path, timeout=10, alive=self.alive)
                t0 = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
                if isinstance(name, tuple):
                    from control_client import tap, type_text
                    words = name[1]
                    if words[0] == 'key': vm.send_input(tap(words[1]), timeout=30)
                    elif words[0] == 'text': type_text(vm, ' '.join(words[1:]))
                    self.inputs.append((t0, 'DE-INPUT-SENT ' + ' '.join(words)))
                    continue
                w, h, png = vm.snapshot(timeout=60)
                f = self.out / f'{self.prefix}-{name}.png'
                f.write_bytes(png)
                self.snaps.append({'name': name, 'file': f.name, 'width': w, 'height': h, 'ns': t0,
                                   'ms': round((time.clock_gettime_ns(time.CLOCK_UPTIME_RAW) - t0) / 1e6, 1)})
            except (OSError, RpcError, TimeoutError, ValueError) as e:
                self.errors.append(f'snapshot {name}: {e}'); vm = None


def boot_natemu(args, i, initrd) -> dict:
    """One boot: the disk cloned, natemu started, the console read every POLL s (each DE-/SP line stamped on arrival),
    the display followed, snapshots taken; the disk clone removed."""
    out = args.out
    disk = out / f'disk-{i}.raw'
    disk.unlink(missing_ok=True)
    clone(args.inputs / 'disk.raw', disk)
    serial = out / f'boot-{i}.serial'; serial.unlink(missing_ok=True)
    sockdir = Path(tempfile.mkdtemp(prefix='nde'))         # a short path: a socket's is at most 104 bytes
    sock = sockdir / 'c.sock'
    perf = out / f'boot-{i}.perf' if args.perf_log else None
    sample = (out / args.name, args.sample_us) if args.sample_us and i > 0 else None
    cache = args.cache_dir if args.cache_dir else 'off'
    cmd = [str(c) for c in natemu_command(args, initrd, disk, serial, sock, perf, cache, sample)]
    before = others()
    stamps, lines, pos, pending = [], [], 0, b''
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=open(out / f'boot-{i}.stdout', 'wb'),
                         stderr=open(out / f'boot-{i}.stderr', 'wb'), start_new_session=True)
    disp = Display(sock, lambda: p.poll() is None, out, f'boot-{i}')
    disp.start()
    seen, last, capped = set(before), 0.0, False
    try:
        while True:
            done = p.poll() is not None
            if serial.exists():
                with open(serial, 'rb') as f:
                    f.seek(pos); data = f.read(); pos += len(data)
                now = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
                pending += data
                *full, pending = pending.split(b'\n')
                for raw in full:
                    line = raw.decode(errors='replace').rstrip('\r')
                    lines.append(line)
                    k = line.find('DE-')
                    if k < 0 and line.startswith('SP '): k = 0
                    if k >= 0:
                        text = line[k:].strip()
                        stamps.append((now, text))
                        if text.startswith('DE-SNAP '): disp.snap(text.split()[1])
                        elif text.startswith('DE-INPUT '): disp.input([w for w in text.split()[1:] if not w.startswith('t=')])
            if done: break
            if time.monotonic() - t0 > args.cap: capped = True; break
            time.sleep(POLL)
            if time.monotonic() - last >= 0.5: seen |= set(others()); last = time.monotonic()
    finally:
        if p.poll() is None: stop_group(p)
        disk.unlink(missing_ok=True)
        disp.join(timeout=5)
        shutil.rmtree(sockdir, ignore_errors=True)
    if pending: lines.append(pending.decode(errors='replace'))
    stamps = sorted(stamps + disp.inputs, key=lambda x: x[0])
    return {'lines': lines, 'stamps': stamps, 'display': disp.events, 'snapshots': disp.snaps, 'display_errors': disp.errors,
            'seconds': round(time.monotonic() - t0, 2), 'capped': capped, 'exit': p.returncode,
            'contended': sorted(seen), 'command': cmd}


def boots_kvm(args, initrd, n) -> list:
    """Every boot on the x86 machine NATEMU_X86_HOST under KVM, in one container run of the builds' pinned image with
    Debian's QEMU (gu.boots_kvm's pattern): the same kernel, initramfs and disk (sent compressed, unpacked there per boot),
    QEMU's bochs-display at the same size, virtio-blk and virtio-rng on PCI, -cpu QCPU. Console lines come back with the
    guest's own uptimes, which the analysis uses alone (no host stamps, no frames, no snapshots)."""
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: raise SystemExit('--emu kvm needs NATEMU_X86_HOST=user@host (an x86-64 Linux machine with Docker and /dev/kvm)')
    ssh = ['ssh', '-o', 'BatchMode=yes', host]
    base = 'natemu-desktop-reference'; run = f'{base}/run-{os.getpid()}'; name = f'natemu-desktop-ref-{os.getpid()}'
    subprocess.run([*ssh, f'mkdir -p {run}'], check=True)
    try:
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(args.kernel), str(initrd), str(args.inputs / 'disk.raw.zst'),
                        f'{host}:{run}/'], check=True)
        qemu = [f'zstd -q -d -f /b/disk.raw.zst -o /b/disk.raw && timeout {int(args.cap)} qemu-system-x86_64 -enable-kvm '
                f'-cpu {QCPU} -nodefaults -machine pc,hpet=off,vmport=off -m {args.ram * 1024} -smp {args.vcpus} '
                f'-kernel /b/{args.kernel.name} -initrd /b/{initrd.name} -append {shlex.quote(cmdline(args))} '
                f'-drive file=/b/disk.raw,if=none,id=d0,format=raw,cache=writeback -device virtio-blk-pci,drive=d0 '
                f'-device virtio-rng-pci -device bochs-display,xres={WIDTH},yres={HEIGHT} -display none -monitor none '
                f'-serial file:/b/boot-{i}.serial -no-reboot; echo "DESKTOP-HOST boot {i} exit $? $(date +%s.%N)"; rm -f /b/disk.raw'
                for i in range(1, n + 1)]
        guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/{gu.SNAPSHOT}/ "
                 "trixie main' > /etc/apt/sources.list; apt-get -o Acquire::Retries=5 update -qq > /dev/null 2>&1 && "
                 "apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends qemu-system-x86 zstd > /dev/null 2>&1 && "
                 'echo "DESKTOP-HOST $(qemu-system-x86_64 --version | head -1 | tr " " _) '
                 '$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | tr " " _) host-kernel=$(uname -r)" && '
                 + ' ; '.join(f'echo "DESKTOP-HOST boot {i} start $(date +%s.%N)"; {q}' for i, q in enumerate(qemu, 1)))
        log = args.out / 'kvm.log'
        with open(log, 'wb') as f:
            subprocess.run([*ssh, f'flock $HOME/{base}/.lock docker run --rm --name {name} --device /dev/kvm -v $HOME/{run}:/b '
                                  f'{gu.KVM_IMAGE} sh -c {shlex.quote(guest)}'],
                           stdout=f, stderr=subprocess.STDOUT, timeout=args.cap * n + 1200)
        host_lines = log.read_text(errors='replace')
        out = []
        for i in range(1, n + 1):
            serial = args.out / f'boot-{i}.serial'; serial.unlink(missing_ok=True)
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', f'{host}:{run}/boot-{i}.serial', str(serial)])
            ex = re.search(rf'DESKTOP-HOST boot {i} exit (\d+) ([0-9.]+)', host_lines)
            st = re.search(rf'DESKTOP-HOST boot {i} start ([0-9.]+)', host_lines)
            text = serial.read_text(errors='replace') if serial.exists() else ''
            lines = [l.rstrip('\r') for l in text.splitlines()]
            out.append({'lines': lines, 'stamps': [(None, l[l.find('DE-'):].strip()) for l in lines if 'DE-' in l] +
                        [(None, l.strip()) for l in lines if l.startswith('SP ')], 'display': [], 'snapshots': [],
                        'display_errors': [], 'seconds': round(float(ex.group(2)) - float(st.group(1)), 2) if ex and st else None,
                        'capped': bool(ex and int(ex.group(1)) == 124), 'exit': int(ex.group(1)) if ex else None,
                        'contended': [], 'command': [qemu[i - 1]]})
        facts = re.search(r'^DESKTOP-HOST (\S+) (\S+) host-kernel=(\S+)', host_lines, re.M)
        if facts and out: out[0]['reference'] = dict(zip(('qemu', 'cpu', 'kernel'), facts.groups()))
        return out
    finally:
        subprocess.run([*ssh, f'docker rm -f {name} > /dev/null 2>&1; rm -rf {run}'], capture_output=True)


# --- the analysis ------------------------------------------------------------------------------------------------------
TIME = re.compile(r'(?<![\w.])t=([0-9]+(?:\.[0-9]+)?)')


def uptime(text):
    """A driver line's guest uptime (its last t=), or None."""
    m = TIME.findall(text)
    return float(m[-1]) if m else None


def guest_lines(stamps):
    """[(host ns or None, guest uptime s or None, text)] of the driver's lines."""
    return [(ns, uptime(text), text) for ns, text in stamps]


class Clock:
    """Guest uptime -> host ns: each line arrives after it was printed, so the smallest (stamp - uptime) among the lines
    within WINDOW s of a time is that time's offset (the lower envelope; the two clocks are one counter, so the offset
    only drifts with the guest's TSC calibration)."""
    WINDOW = 60.0

    def __init__(self, lines):
        self.pts = sorted((t, ns - t * 1e9) for ns, t, _ in lines if ns is not None and t is not None)
        self.ts = [t for t, _ in self.pts]

    def __bool__(self): return bool(self.pts)

    def ns(self, t):
        if not self.pts or t is None: return None
        i, j = bisect.bisect_left(self.ts, t - self.WINDOW), bisect.bisect_right(self.ts, t + self.WINDOW)
        near = self.pts[i:j] or [min(self.pts, key=lambda p: abs(p[0] - t))]
        return int(t * 1e9 + min(o for _, o in near))


def kv(text):
    return dict(m.groups() for m in re.finditer(r'(\w[\w.-]*)=(\S+)', text))


def windows(lines):
    """The measured windows from the driver's lines: [{name, kind, t0, t1 (guest s), ...}]."""
    out, begin, stat_at = [], {}, {}
    first, enter = {}, None
    for _, t, text in lines:
        w = text.split()
        if not w: continue
        tag = w[0]
        if tag not in first: first[tag] = (t, text)
        if tag == 'DE-INPUT' and w[1:3] == ['key', 'Enter']: enter = t       # the click (the runner sends it on arrival)
        if tag == 'DE-CLICKMARKS':
            k = kv(text)
            m = {n: float(k[n]) for n in ('dbus', 'unit', 'exec', 'at') if re.fullmatch(r'[0-9.]+', k.get(n, ''))}
            c = next((l['t0'] for l in reversed(out) if l['name'] == w[1] + ':start'), None)
            if c is not None:
                marks = [('click', c)] + sorted(m.items(), key=lambda kv: kv[1])
                for (a, ta), (b, tb) in zip(marks, marks[1:]):
                    out.append({'name': f'{w[1]}:{a}-to-{b}', 'kind': 'mark', 't0': ta, 't1': tb})
                if 'exec' in m: out.append({'name': f'{w[1]}:click-to-exec', 'kind': 'mark', 't0': c, 't1': m['exec']})
        if tag == 'DE-BEGIN': begin[w[1]] = t
        elif tag == 'DE-END' and w[1] in begin:
            name = w[1]
            d = {'name': name, 'kind': name.split('-')[0], 't0': begin[name], 't1': t, **{k: v for k, v in kv(text).items() if k != 't'}}
            if name.startswith('app-'):
                m = next((l for l in reversed(out) if l['name'] == name + ':start'), None)
                if m: d['mapped'] = m['t1']
            out.append(d)
        elif tag == 'DE-MAPPED' and w[1] in begin:
            k = kv(text)
            at = float(k['at']) if 'at' in k else None
            t0 = enter if w[1].startswith('click-') and enter is not None else float(k.get('started', begin[w[1]]))
            out.append({'name': w[1] + ':start', 'kind': 'start', 't0': t0, 't1': at, 'timeout': 'timeout' in w, 'pid': k.get('pid')})
        elif tag == 'DE-SETTLED' and w[1] in begin:
            m = next((l for l in reversed(out) if l['name'] == w[1] + ':start'), None)
            if m and m['t1'] is not None:
                out.append({'name': w[1] + ':settle', 'kind': 'settle', 't0': m['t1'], 't1': t, 'quiet': kv(text).get('quiet')})
    # The session: from the autologin's start.
    s = first.get('DE-SESSION')
    if s:
        t_s = s[0]
        for tag, name in (('DE-SHELL-MAPPED', 'session:shell'), ('DE-IDLE-READY', 'session:idle-ready')):
            if tag in first:
                k = kv(first[tag][1])
                t1 = float(k['at']) if 'at' in k else first[tag][0]
                if tag == 'DE-IDLE-READY': t1 = (t1 or 0) - 3.0            # quiet for the 3 s before the line
                out.append({'name': name, 'kind': 'session', 't0': t_s, 't1': t1})
    return [w for w in out if w['t0'] is not None and w['t1'] is not None and w['t1'] >= w['t0']], first


def stats_lines(lines):
    """DE-STAT LABEL k=v ... -> {LABEL: {k: number}}."""
    out = {}
    for _, _, text in lines:
        if text.startswith('DE-STAT '):
            w = text.split()
            out[w[1]] = {k: float(v) for k, v in kv(text).items() if re.fullmatch(r'[0-9.]+', v)}
    return out


STAT_PAIRS = {'start': ('begin', 'mapped'), 'settle': ('mapped', 'settled'), 'app': ('begin', 'settled'), 'click': ('begin', 'settled'),
              'ui': ('begin', 'end'), 'idle': ('begin', 'end')}


def guest_delta(stats, w):
    """The guest's counters over a window (its STAT lines' differences), or None."""
    kind = w['kind']
    base = w['name'].split(':')[0]
    pair = STAT_PAIRS.get(kind)
    if not pair: return None
    a, b = stats.get(f'{base}-{pair[0]}'), stats.get(f'{base}-{pair[1]}')
    if not a or not b: return None
    d = {k: b[k] - a[k] for k in a if k in b and k not in ('t', 'memavail')}
    jiff = {k: d.get(k, 0) for k in ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')}
    out = {'cpu_busy_s': round((jiff['user'] + jiff['nice'] + jiff['system'] + jiff['irq'] + jiff['softirq'] + jiff['steal']) / 100, 2),
           'iowait_s': round(jiff['iowait'] / 100, 2), 'idle_s': round(jiff['idle'] / 100, 2),
           'user_s': round(jiff['user'] / 100, 2), 'system_s': round((jiff['system'] + jiff['irq'] + jiff['softirq']) / 100, 2),
           'pgfault': int(d.get('pgfault', 0)), 'pgmajfault': int(d.get('pgmajfault', 0)),
           'disk_reads': int(d.get('rd', 0)), 'disk_read_mib': round(d.get('rsect', 0) * 512 / 2**20, 1),
           'disk_read_ms': int(d.get('rms', 0)), 'disk_busy_ms': int(d.get('ioms', 0)), 'disk_writes': int(d.get('wr', 0))}
    return out


def ps_blocks(lines):
    """[(label, t, {pid: (name, cpu jiffies, minflt, majflt)})] from the driver's PS blocks."""
    out, cur = [], None
    for _, t, text in lines:
        if text.startswith('DE-PS-BEGIN '): cur = (text.split()[1], t, {})
        elif text.startswith('DE-PS-END ') and cur: out.append(cur); cur = None
        elif text.startswith('DE-PS ') and cur:
            w = text.split()
            k = kv(text)
            try: cur[2][int(w[1])] = (w[2], int(k['cpu']), int(k.get('minflt', 0)), int(k.get('majflt', 0)), int(k.get('sw', 0)))
            except (IndexError, KeyError, ValueError): pass
    return out


def ps_phases(blocks):
    """Each PS block's CPU seconds by process name since the previous block (processes that ended meanwhile are not
    seen: their time is lost)."""
    out, prev = [], {}
    for label, t, procs in blocks:
        by, flt, sw = collections.Counter(), collections.Counter(), collections.Counter()
        for pid, (name, cpu, mn, mj, s) in procs.items():
            p = prev.get(pid)
            same = p is not None and p[0] == name
            by[name] += cpu - (p[1] if same else 0)
            flt[name] += mn - (p[2] if same else 0)
            sw[name] += s - (p[4] if same else 0)
        out.append({'label': label, 't': t, 'cpu_s': {n: round(c / 100, 2) for n, c in by.most_common(25) if c > 0},
                    'minflt': {n: c for n, c in flt.most_common(10) if c > 0},
                    'switches_in': {n: c for n, c in sw.most_common(12) if c > 0}})
        prev = procs
    return out


def map_lines(lines):
    """[(pid, lo, hi, 'exe' or 'anon', path)] of the driver's DE-MAP lines."""
    out = []
    for _, _, text in lines:
        if text.startswith('DE-MAP '):
            w = text.split()
            try:
                lo, hi = (int(x, 16) for x in w[2].split('-'))
                out.append((int(w[1]), lo, hi, 'anon' if w[4].startswith('[anon') or 'memfd' in w[4] else 'exe', w[4]))
            except (IndexError, ValueError): pass
    return out


def snaps(perf):
    """The timing log's `snap NS v0b0 v0b1 ...` lines: [(ns, [values])] (sample_report.snaps)."""
    out = []
    with open(perf, errors='replace') as f:
        for line in f:
            if line.startswith('snap '):
                w = line.split()
                out.append((int(w[1]), list(map(int, w[2:]))))
    return out


SNAP_COUNTERS = ('cr3_loads', 'global_flushes', 'invlpgs', 'page_faults', 'suspended', 'relinked')   # = host SNAP_COUNTERS
SWITCH_BUCKETS = ('tlb', 'relink', 'link', 'abort', 'shadow', 'revalidate')


def snapc(perf):
    """The timing log's `snapc NS ...` lines (crates/host/src/execution.rs Blocks::snap_counts): [(ns, [values])]."""
    out = []
    with open(perf, errors='replace') as f:
        for line in f:
            if line.startswith('snapc '):
                w = line.split()
                out.append((int(w[1]), list(map(int, w[2:]))))
    return out


def counts_between(sc, t0, t1, nb):
    """The counters summed over vCPUs between the last snapc lines at or before t0 and t1: SNAP_COUNTERS and each
    bucket's entries (`entries`), or None."""
    times = [t for t, _ in sc]
    i, j = bisect.bisect_right(times, t0) - 1, bisect.bisect_right(times, t1) - 1
    if j < 0 or j <= i: return None
    b = sc[j][1]; a = sc[i][1] if i >= 0 else [0] * len(b)
    per = len(SNAP_COUNTERS) + nb
    n = len(b) // per
    d = [sum(b[v * per + x] - a[v * per + x] for v in range(n)) for x in range(per)]
    return dict(zip(SNAP_COUNTERS, d[:len(SNAP_COUNTERS)])), d[len(SNAP_COUNTERS):]


def stretches(sl, t0, t1, names, vcpus=None):
    """The critical path's view of [t0, t1): each snap interval (5 ms) classed by how many vCPUs were busy in it (the
    sum over vCPUs of the busy share, busy being every bucket but halted): `idle` under 0.25, `one` (about one vCPU busy,
    the rest halted: a serial stretch, what a hand-off between processes looks like) up to 1.25, `many` above. Per class:
    wall seconds, busy seconds, every bucket summed over the vCPUs, the switch-related ones and their share."""
    n = len(names); h = names.index('halted')
    times = [t for t, _ in sl]
    i, j = max(0, bisect.bisect_right(times, t0) - 1), bisect.bisect_right(times, t1) - 1
    out = {c: {'wall_s': 0.0, 'buckets': collections.Counter()} for c in ('idle', 'one', 'many')}
    for k in range(i, j):
        (ta, a), (tb, b) = sl[k], sl[k + 1]
        dt = tb - ta
        if dt <= 0: continue
        nv = len(a) // n
        d = [b[x] - a[x] for x in range(nv * n)]
        busy = sum(sum(d[v * n:(v + 1) * n]) - d[v * n + h] for v in range(nv)) / dt
        c = 'idle' if busy < 0.25 else 'one' if busy <= 1.25 else 'many'
        out[c]['wall_s'] += dt / 1e9
        for x, name in enumerate(names):
            out[c]['buckets'][name] += sum(d[v * n + x] for v in range(nv))
    res = {}
    for c, e in out.items():
        bk = {k: v / 1e9 for k, v in e['buckets'].items()}
        busy = sum(bk.values()) - bk.get('halted', 0)
        res[c] = {'wall_s': round(e['wall_s'], 3), 'busy_s': round(busy, 3),
                  'buckets_s': {k: round(v, 3) for k, v in sorted(bk.items(), key=lambda kv: -kv[1]) if v >= 0.0005},
                  'switch_s': {k: round(bk.get(k, 0), 3) for k in SWITCH_BUCKETS},
                  'switch_share': round(sum(bk.get(k, 0) for k in SWITCH_BUCKETS) / busy, 4) if busy else None,
                  'guest_share': round(bk.get('guest', 0) / busy, 4) if busy else None}
    return res


def bucket_names():
    from sample_report import bucket_names as names
    return names()


def buckets_between(sl, t0, t1, names):
    from sample_report import buckets_between as between
    return between(sl, t0, t1, names)


PARTS = ('fetch', 'lookup', 'disk_get', 'fexc', 'disk_put', 'admit', 'decode', 'stage', 'keep')


class Requests:
    """The translation requests of a boot (OUT/boot-I.perf.vN lines, crates/host/src/perf.rs Recorder::request): per
    request its start (ns), total ns, parts, which source answered (memory, disk, fexc) and whether its guest bytes' hash
    was new to the boot (first-seen), sorted by start."""

    def __init__(self, perf: Path):
        rows, seen = [], set()
        files = sorted(perf.parent.glob(perf.name + '.v*'))
        raw = []
        for f in files:
            with open(f, errors='replace') as h:
                for line in h:
                    w = line.split()
                    if len(w) < 26: continue
                    try:
                        raw.append((int(w[24]), int(w[19]), tuple(int(x) for x in w[10:19]),
                                    2 if int(w[7]) else 1 if int(w[6]) else 0 if int(w[5]) else -1, w[9], w[4], int(w[1], 16),
                                    int(w[2], 16)))
                    except ValueError: continue
        raw.sort()
        for began, total, parts, src, h, result, cr3, rip in raw:
            new = h not in seen and h != '0'
            seen.add(h)
            rows.append((began, total, parts, src, new, result, cr3, rip))
        self.rows = rows
        self.began = [r[0] for r in rows]
        self.files = len(files)
        self.owner = None

    def attribute(self, maps: list, names: dict):
        """Which process each request's address space is: a user request whose RIP lies in a process's own program (as
        the driver's DE-MAP lines list it; PIE programs load at a random base) votes its CR3 to that process's name; JIT
        code is a request in a process's anonymous executable mapping. owner(cr3, rip) -> (name, in JIT code)."""
        exe, anon = [], []
        for pid, lo, hi, kind in maps:
            (anon if kind == 'anon' else exe).append((lo, hi, names.get(pid, f'pid{pid}')))
        exe.sort(); anon.sort()
        starts, astarts = [e[0] for e in exe], [a[0] for a in anon]
        def find(table, keys, rip):
            k = bisect.bisect_right(keys, rip) - 1
            return {n for lo, hi, n in table[max(0, k - 8):k + 1] if lo <= rip < hi}
        votes = collections.defaultdict(collections.Counter)
        for r in self.rows:
            if r[7] >> 47: continue
            for n in find(exe, starts, r[7]) or find(anon, astarts, r[7]): votes[r[6]][n] += 1
        cr3 = {c: v.most_common(1)[0][0] for c, v in votes.items()}
        def owner(c, rip):
            if rip >> 47: return 'kernel', 0
            n = cr3.get(c, 'unknown')
            return n, int(n in find(anon, astarts, rip))
        self.owner = owner
        self.cr3_names = len(cr3)

    def between(self, a, b):
        i, j = bisect.bisect_left(self.began, a), bisect.bisect_left(self.began, b)
        sel = self.rows[i:j]
        parts = [0] * len(PARTS)
        src = collections.Counter()
        new = 0
        for _, _, p, s, n, _, _, _ in sel:
            for k, v in enumerate(p): parts[k] += v
            src[s] += 1; new += n
        out = {'count': len(sel), 'host_s': round(sum(r[1] for r in sel) / 1e9, 3),
               'parts_s': {k: round(v / 1e9, 3) for k, v in zip(PARTS, parts) if v >= 1e6},
               'answers': {'memory': src[0], 'disk': src[1], 'fexc': src[2], 'none': src[-1]}, 'first_seen': new,
               'address_spaces': len({r[6] for r in sel})}
        if self.owner:
            by = collections.defaultdict(lambda: [0, 0, 0, 0])      # requests, first-seen, host ns, of them in JIT code
            for r in sel:
                name, jit = self.owner(r[6], r[7])
                e = by[name]; e[0] += 1; e[1] += r[4]; e[2] += r[1]; e[3] += jit
            out['by_process'] = {n: {'count': c, 'first_seen': f, 'host_s': round(t / 1e9, 3), 'jit': j}
                                 for n, (c, f, t, j) in sorted(by.items(), key=lambda kv: -kv[1][2])[:15]}
        return out


def frames(events, a, b):
    """The page flips (display_changed with a picture) in [a, b): count, per second, and the gaps between them (ms)."""
    ts = [ns for ns, p in events if a <= ns < b and p and p.get('enabled')]
    gaps = [(y - x) / 1e6 for x, y in zip(ts, ts[1:])]
    s = (b - a) / 1e9
    return {'count': len(ts), 'per_s': round(len(ts) / s, 2) if s > 0 else None,
            'gap_ms_median': round(statistics.median(gaps), 1) if gaps else None,
            'gap_ms_p90': round(sorted(gaps)[int(0.9 * (len(gaps) - 1))], 1) if gaps else None,
            'gap_ms_max': round(max(gaps), 1) if gaps else None}


def analyse_boot(b: dict, perf: Path | None) -> dict:
    lines = guest_lines(b['stamps'])
    wins, first = windows(lines)
    clock = Clock(lines)
    st = stats_lines(lines)
    sl = bnames = req = sc = None
    if perf and perf.exists():
        sl, bnames, sc = snaps(perf), bucket_names(), snapc(perf)
        r = Requests(perf)
        if r.files:
            req = r
            ml = map_lines(lines)
            names = {pid: path.rsplit('/', 1)[-1] for pid, _, _, kind, path in ml if kind == 'exe'}   # the program, where no PS line names it
            names.update({pid: n for _, _, procs in ps_blocks(lines) for pid, (n, *_) in procs.items()})
            req.attribute([m[:4] for m in ml], names)
    out = []
    for w in wins:
        d = {k: v for k, v in w.items()}
        d['seconds'] = round(w['t1'] - w['t0'], 3)
        g = guest_delta(st, w)
        if g: d['guest'] = g
        a, z = clock.ns(w['t0']), clock.ns(w['t1'])
        if a is not None and z is not None and z > a:
            d['host_ns'] = [a, z]
            if sl:
                bk = buckets_between(sl, a, z, bnames)
                if bk:
                    busy = sum(bk.values()) - bk.get('halted', 0)
                    d['buckets_s'] = {k: round(v, 3) for k, v in sorted(bk.items(), key=lambda kv: -kv[1]) if v >= 0.0005}
                    d['busy_s'] = round(busy, 3)
                    d['busy_vcpus'] = round(busy / d['seconds'], 3) if d['seconds'] else None
                    sw = sum(bk.get(k, 0) for k in SWITCH_BUCKETS)
                    d['switch'] = {'buckets_s': {k: round(bk.get(k, 0), 3) for k in SWITCH_BUCKETS}, 'seconds': round(sw, 3),
                                   'share_of_busy': round(sw / busy, 4) if busy else None}
                    d['stretches'] = stretches(sl, a, z, bnames)
                    if sc:
                        c = counts_between(sc, a, z, len(bnames))
                        if c:
                            d['switch'].update({k: v for k, v in c[0].items()},
                                               cr3_loads_per_s=round(c[0]['cr3_loads'] / d['seconds'], 1) if d['seconds'] else None,
                                               entries={k: c[1][bnames.index(k)] for k in SWITCH_BUCKETS if k in bnames})
            if req: d['translate'] = req.between(a, z)
            if b['display']:
                d['frames'] = frames(b['display'], a, z)
                if w['kind'] == 'start':     # the first frame KWin presented after the window arrived
                    nxt = next((ns for ns, p in b['display'] if ns >= z and p and p.get('enabled')), None)
                    if nxt is not None: d['first_frame_after_map_s'] = round((nxt - z) / 1e9, 3)
        out.append(d)
    session = {}
    if 'DE-SESSION' in first:
        t_s = first['DE-SESSION'][0]
        session['session_start_s'] = t_s
        a = clock.ns(t_s)
        flips = [ns for ns, p in b['display'] if a is not None and ns >= a and p and p.get('enabled')]
        if flips and clock:
            # the first frame after the session's start, as guest uptime
            off = a - t_s * 1e9
            session['first_frame_s'] = round((flips[0] - off) / 1e9 - t_s, 2)
    for w in out:
        if w['name'] == 'session:shell': session['shell_mapped_s'] = w['seconds']
        if w['name'] == 'session:idle-ready': session['idle_desktop_s'] = w['seconds']
    info = [t for _, _, t in lines if t.startswith('DE-INFO ')]
    if sl:
        # the boot (the first snap to the session's start) as a window of its own
        t_s = first.get('DE-SESSION', (None,))[0]
        a, z = sl[0][0], clock.ns(t_s) if t_s is not None else None
        if z and z > a:
            bk = buckets_between(sl, a, z, bnames) or {}
            busy = sum(bk.values()) - bk.get('halted', 0)
            d = {'name': 'boot', 'kind': 'boot', 'seconds': round((z - a) / 1e9, 3), 'host_ns': [a, z],
                 'buckets_s': {k: round(v, 3) for k, v in sorted(bk.items(), key=lambda kv: -kv[1]) if v >= 0.0005},
                 'busy_s': round(busy, 3), 'stretches': stretches(sl, a, z, bnames)}
            sw = sum(bk.get(k, 0) for k in SWITCH_BUCKETS)
            d['switch'] = {'buckets_s': {k: round(bk.get(k, 0), 3) for k in SWITCH_BUCKETS}, 'seconds': round(sw, 3),
                           'share_of_busy': round(sw / busy, 4) if busy else None}
            c = counts_between(sc, a, z, len(bnames)) if sc else None
            if c: d['switch'].update(c[0], cr3_loads_per_s=round(c[0]['cr3_loads'] / d['seconds'], 1))
            if req: d['translate'] = req.between(a, z)
            out.insert(0, d)
    return {'windows': out, 'session': session, 'processes': ps_phases(ps_blocks(lines)), 'info': info,
            'driver': first.get('DE-DRIVER', (None, None))[1], 'kwin_script': first.get('DE-KWIN-SCRIPT', (None, None))[1],
            'requests_files': req.files if req else None}


def sp_results(lines):
    done = next((l for l in lines if l.startswith('SP /done ')), None)
    res = None
    if done:
        try: res = json.loads(done[len('SP /done '):])
        except ValueError: res = {'error': 'unreadable /done line'}
    logs = []
    for l in lines:
        if l.startswith('SP /log '):
            try: logs.append(json.loads(l[len('SP /log '):]))
            except ValueError: pass
    gpu = next((d for d in logs if 'webgl' in d or 'webgl_error' in d), None)
    return {'score': (res or {}).get('score'), 'score_text': (res or {}).get('scoreText'),
            'per_iteration': (res or {}).get('perIteration'), 'error': (res or {}).get('error'), 'ua': (res or {}).get('ua'),
            'webgl': gpu, 'page_errors': [d for d in logs if 'error' in d][:10]} if (res or gpu) else None


EXPECTED_WINDOWS = {
    'idle': ('idle',),
    'ui': ('ui-launcher-open', 'ui-launcher-close', 'ui-overview-open', 'ui-overview-close',
           'ui-desktop-next', 'ui-desktop-prev'),
    'apps': ('app-konsole-1:start', 'app-konsole-1:settle', 'app-konsole-1',
             'app-konsole-2:start', 'app-konsole-2:settle', 'app-konsole-2',
             'app-dolphin-1:start', 'app-dolphin-1:settle', 'app-dolphin-1',
             'app-dolphin-2:start', 'app-dolphin-2:settle', 'app-dolphin-2',
             'ui-window-place', 'ui-window-move', 'ui-window-grow',
             'ui-window-maximize', 'ui-window-restore', 'ui-window-switch'),
    'click': ('click-konsole-1:start', 'click-konsole-1:settle', 'click-konsole-1',
              'click-konsole-2:start', 'click-konsole-2:settle', 'click-konsole-2',
              'click-dolphin-1:start', 'click-dolphin-1:settle', 'click-dolphin-1',
              'click-dolphin-2:start', 'click-dolphin-2:settle', 'click-dolphin-2'),
    'browsers': ('app-chromium-1:start', 'app-chromium-1:settle', 'app-chromium-1',
                 'app-chromium-2:start', 'app-chromium-2:settle', 'app-chromium-2',
                 'app-firefox-1:start', 'app-firefox-1:settle', 'app-firefox-1',
                 'app-firefox-2:start', 'app-firefox-2:settle', 'app-firefox-2'),
    'speedometer': ('app-chromium-3:start', 'app-chromium-3:settle', 'app-chromium-3'),
}


def _geom(val):
    if not val or val == 'none': return None
    parts = val.split(',')
    if len(parts) != 4: return None
    try: return tuple(int(p) for p in parts)
    except ValueError: return None


def _ui_effect_ok(name, k):
    if k.get('effect') != 'ok': return False
    if name in ('ui-overview-open', 'ui-overview-close'):
        return (k.get('before_ov'), k.get('after_ov')) == (('0', '1') if name == 'ui-overview-open' else ('1', '0'))
    if name in ('ui-desktop-next', 'ui-desktop-prev'):
        bd, ad = k.get('before_desk'), k.get('after_desk')
        bdi, adi = k.get('before_deskid'), k.get('after_deskid')
        try: bd_i, ad_i = int(bd), int(ad)
        except (TypeError, ValueError): return False
        if bdi in (None, 'none') or adi in (None, 'none') or bdi == adi: return False
        return ad_i > bd_i if name == 'ui-desktop-next' else ad_i < bd_i
    bw, aw = k.get('before_win'), k.get('after_win')
    if bw in (None, 'none') or aw in (None, 'none') or bw != aw: return False
    if k.get('before_normal') != '1' or k.get('after_normal') != '1': return False
    if name == 'ui-window-switch':
        bc, ac = k.get('before_cls'), k.get('after_cls')
        if bc in (None, 'none') or ac != bc: return False
        if k.get('switches') != '4' or k.get('distinct') != '2': return False
        opid, epid = k.get('other_pid'), k.get('expected_other_pid')
        if opid in (None, 'none') or (epid not in (None, 'none') and opid != epid): return False
        seq = [s for s in k.get('seq', '').split(',') if s and s != 'none']
        if len(seq) != 4 or len(set(seq)) != 2 or seq[-1] != aw: return False
        prev = bw
        for item in seq:
            if item == prev: return False
            prev = item
        return True
    bg, ag = _geom(k.get('before_geom')), _geom(k.get('after_geom'))
    if not bg or not ag: return False
    bx, by, bw_, bh_ = bg
    ax, ay, aw_, ah_ = ag
    if name == 'ui-window-place':
        return ag != bg and ag == (80, 60, 900, 600)
    if name == 'ui-window-move':
        return (aw_, ah_) == (bw_, bh_) and ax - bx == 480 and ay - by == 240
    if name == 'ui-window-grow':
        return aw_ - bw_ == 360 and ah_ > bh_
    if name == 'ui-window-maximize':
        return k.get('before_max') == '0' and k.get('after_max') == '3' and aw_ > bw_ and ah_ > bh_
    if name == 'ui-window-restore':
        return k.get('before_max') == '3' and k.get('after_max') == '0' and aw_ < bw_ and ah_ < bh_
    return False


def problems(args, b, a) -> list:
    out = []
    texts = [t for _, t in b['stamps']]
    driver_lines = [t for t in texts if t.startswith('DE-DRIVER ')]
    if not driver_lines: out.append('missing DE-DRIVER header')
    else:
        d_phases = [p for p in kv(driver_lines[0]).get('phases', '').split(',') if p]
        if set(d_phases) != set(args.phases): out.append(f'phases mismatch: driver={d_phases} args={list(args.phases)}')
    if not any(t.startswith('DE-END-ALL') for t in texts): out.append('the driver did not reach its end (no DE-END-ALL)')
    if b.get('exit') != 0: out.append(f'vm exit: {b.get("exit")}')
    if any(t.startswith('DE-SHELL-MAPPED timeout') for t in texts): out.append("plasmashell's window never came")
    if any(t.startswith('DE-KWIN-SCRIPT missing') for t in texts): out.append("KWin's demarker script reported nothing")
    elif not any(t.startswith('DE-KWIN-SCRIPT ') for t in texts): out.append("KWin's demarker script missing (no DE-KWIN-SCRIPT)")
    idle_lines = [t for t in texts if t.startswith('DE-IDLE-READY ')]
    if not idle_lines: out.append('session never reached idle (no DE-IDLE-READY)')
    elif any(kv(t).get('quiet') != 'ok' for t in idle_lines):
        out.append('session never reached idle (DE-IDLE-READY not quiet=ok)')
    if (a.get('session') or {}).get('idle_desktop_s') is None: out.append('session never reached idle')
    for t in texts:
        w = t.split()
        if t.startswith('DE-MAPPED') and 'timeout' in t: out.append(f'no window: {w[1] if len(w) > 1 else t}')
        if len(w) >= 2 and w[0] == 'DE-END' and w[1].startswith('ui-') and kv(t).get('quiet') != 'ok':
            out.append(f'ui timeout: {w[1]} (quiet={kv(t).get("quiet", "missing")})')
        if len(w) >= 2 and w[0] == 'DE-SETTLED':
            q = kv(t).get('quiet')
            if w[1] == 'app-chromium-3':
                if q not in ('ok', 'timeout'): out.append(f'settle error: {w[1]} (quiet={q or "missing"})')
            elif q != 'ok':
                out.append(f'settle timeout: {w[1]} (quiet={q or "missing"})')
        if t.startswith('DE-KILLED '): out.append(f'close killed: {t}')
        if t.startswith('DE-QUIET-TIMEOUT '): out.append(f'quiet timeout: {t}')
    if any(t.startswith(('DE-SP-TIMEOUT', 'SP-TIMEOUT')) for t in texts): out.append('Speedometer: timeout (DE-SP-TIMEOUT)')
    if any(t.startswith(('DE-SP-NO-HOOK', 'SP-NO-HOOK')) for t in texts): out.append('Speedometer: hook missing (DE-SP-NO-HOOK)')
    for ph in args.phases:
        if ph in ('idle', 'ui', 'apps', 'click', 'browsers') and not any(w['kind'] == {'apps': 'app', 'browsers': 'app'}.get(ph, ph)
                                                               for w in a['windows']):
            out.append(f'phase {ph}: no windows')
    win_names = {w['name'] for w in a['windows']}
    expected = ['session:shell', 'session:idle-ready']
    for ph in args.phases: expected.extend(EXPECTED_WINDOWS.get(ph, ()))
    for name in expected:
        if name not in win_names: out.append(f'missing row: {name}')
    dk = kv(driver_lines[0]) if driver_lines else {}
    dv_raw = dk.get('v')
    if dv_raw is not None and (not dv_raw.isdigit() or int(dv_raw) < 2):
        out.append(f'invalid DE-DRIVER version: {dv_raw}')
    driver_v = int(dv_raw) if dv_raw and dv_raw.isdigit() else 0
    ui_effects = collections.defaultdict(list)
    for t in texts:
        if t.startswith('DE-UI-EFFECT ') and len(t.split()) >= 2:
            ui_effects[t.split()[1]].append(kv(t))
    if driver_v >= 2 or dv_raw is not None or ui_effects:
        expected_ui = []
        if 'ui' in args.phases: expected_ui += ['ui-overview-open', 'ui-overview-close', 'ui-desktop-next', 'ui-desktop-prev']
        if 'apps' in args.phases:
            expected_ui += ['ui-window-place', 'ui-window-move', 'ui-window-grow',
                            'ui-window-maximize', 'ui-window-restore', 'ui-window-switch']
        for name in expected_ui:
            recs = ui_effects.get(name, [])
            if not recs: out.append(f'ui effect missing: {name}')
            elif len(recs) > 1: out.append(f'ui effect duplicate: {name}')
            elif not _ui_effect_ok(name, recs[0]):
                out.append(f'ui no-op: {name} ({recs[0].get("effect", "unchanged")})')
    a['ui_effects_verified'] = bool(driver_v >= 2)
    if 'speedometer' in args.phases and not (a.get('speedometer') or {}).get('score'): out.append('Speedometer: no score')
    if b.get('capped'): out.append('capped')
    return out


def markers_for_sampler(path: Path, b: dict, a: dict):
    """OUT/NAME.markers.tsv for tests/bench/sample_report.py: KTEXT, START (the session's start), a PHASE line at each
    window's end (the phase from the previous line: the gaps between windows are phases of their own), END."""
    lines = guest_lines(b['stamps'])
    clock = Clock(lines)
    rows = []
    for ns, t, text in lines:
        if text.startswith('DE-KTEXT '): rows.append((ns, f'NATEMU-BENCH-KTEXT {text.split()[1]}'))
    s = next((t for _, t, x in lines if x.startswith('DE-SESSION')), None)
    if s is not None: rows.append((clock.ns(s), 'NATEMU-BENCH-START'))
    ws = sorted((w for w in a['windows'] if w['kind'] in ('session', 'idle', 'ui', 'start', 'settle') or w['name'] == 'session:idle-ready'),
                key=lambda w: w['t0'])
    last = s
    for w in ws:
        if w['kind'] == 'session' and w['name'] != 'session:idle-ready': continue
        if last is not None and w['t0'] > last + 0.05: rows.append((clock.ns(w['t0']), 'NATEMU-BENCH-PHASE name=between'))
        rows.append((clock.ns(w['t1']), f'NATEMU-BENCH-PHASE name={w["name"].replace(":", "/")}'))
        last = w['t1']
    e = next((t for _, t, x in lines if x.startswith('DE-END-ALL')), None)
    if e is not None: rows.append((clock.ns(e), 'NATEMU-BENCH-END'))
    with open(path, 'w') as f:
        for ns, text in sorted((r for r in rows if r[0] is not None), key=lambda r: r[0]): f.write(f'{ns}\t{text}\n')


def report_md(result) -> str:
    """result.json as text: per boot the session, then a table of the windows."""
    out = [f'# {result["name"]} ({result["emu"]})', '',
           f'{result["vcpus"]} vCPUs, {result["ram_gib"]} GiB, phases {",".join(result["phases"])}, compile cache {result["compile_cache"]}', '']
    for b in result['boots']:
        out += [f'## boot {b["boot"]}{" (fills the compile cache)" if b.get("fill") else ""}: {b["seconds"]} s', '']
        if b.get('compile_cache'): out.append(f'compile cache after it: {b["compile_cache"]["packs"]} packs, {b["compile_cache"]["bytes"] / 2**20:.0f} MiB')
        a = b.get('analysis') or {}
        if a.get('session'): out.append('session: ' + ', '.join(f'{k} {v}' for k, v in a['session'].items()))
        if b.get('speedometer'): out.append(f'Speedometer: {b["speedometer"].get("score_text")} (webgl {b["speedometer"].get("webgl")})')
        out += ['', '| window | s | busy vCPU-s | guest | host | halted | shadow+abort | translate n | host s | fexc s | first-seen | mem/disk/fexc | faults (maj) | disk MiB / busy ms | frames | CR3/s | switch s (of busy) | one-busy s / switch share |',
                '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
        for w in a.get('windows', []):
            bk, tr, g, fr = w.get('buckets_s') or {}, w.get('translate'), w.get('guest'), w.get('frames')
            cells = [w['name'], w['seconds'], w.get('busy_s', ''), bk.get('guest', ''), bk.get('host', ''), bk.get('halted', ''),
                     round(bk.get('shadow', 0) + bk.get('abort', 0), 3) if bk else '']
            cells += [tr['count'], tr['host_s'], tr['parts_s'].get('fexc', ''), tr['first_seen'],
                      '/'.join(str(tr['answers'][k]) for k in ('memory', 'disk', 'fexc'))] if tr else [''] * 5
            cells += [f"{g['pgfault']} ({g['pgmajfault']})", f"{g['disk_read_mib']} / {g['disk_busy_ms']}"] if g else ['', '']
            cells += [f"{fr['count']} ({fr['per_s']}/s)"] if fr else ['']
            sw, one = w.get('switch') or {}, (w.get('stretches') or {}).get('one')
            cells += [sw.get('cr3_loads_per_s', ''), f"{sw['seconds']} ({sw['share_of_busy']})" if sw else '',
                      f"{one['wall_s']} / {one['switch_share']}" if one else '']
            out.append('| ' + ' | '.join(str(c) for c in cells) + ' |')
        for p in a.get('processes', []):
            out.append(f'- CPU by process, {p["label"]}: ' + ', '.join(f'{n} {s}' for n, s in list(p['cpu_s'].items())[:12]))
        for i in a.get('info', [])[:20]: out.append(f'- {i}')
        if b.get('problems'): out.append(f'- problems: {"; ".join(b["problems"])}')
        out.append('')
    return '\n'.join(out) + '\n'


def write_guest_perf(out: Path, i: int, lines: list):
    g = [l[l.find('DE-GPERF ') + 9:] for l in lines if 'DE-GPERF ' in l]
    if g: (out / f'boot-{i}.guest-perf.txt').write_text('\n'.join(g) + '\n')


def analyse(args) -> int:
    """The analysis again from a finished run's files (result.json's boots, their markers, display and timing logs)."""
    result = json.loads((args.out / 'result.json').read_text())
    pargs = argparse.Namespace(phases=result.get('phases', []))
    for r in result['boots']:
        i = r['boot']
        stamps_data = json.loads((args.out / f'boot-{i}.markers.json').read_text())
        serial = args.out / f'boot-{i}.serial'
        lines = serial.read_text(errors='replace').splitlines() if serial.exists() else [t for _, t in stamps_data]
        b = {'stamps': stamps_data,
             'display': [tuple(json.loads(l)) for l in (args.out / f'boot-{i}.display.jsonl').read_text().splitlines()],
             'lines': lines,
             'capped': r.get('capped', False),
             'exit': r.get('exit')}
        prev_analysis = r.get('analysis') or {}
        prev_wins = {w['name']: w for w in prev_analysis.get('windows', [])}
        perf = args.out / f'boot-{i}.perf'
        a = analyse_boot(b, perf if perf.exists() else None)
        if a.get('requests_files') is None and prev_analysis.get('requests_files') is not None:
            a['requests_files'] = prev_analysis['requests_files']
            for w in a['windows']:
                if 'translate' not in w and 'translate' in prev_wins.get(w['name'], {}):
                    w['translate'] = prev_wins[w['name']]['translate']
        a['speedometer'] = sp_results(b['lines'])
        r['analysis'], r['speedometer'] = a, a['speedometer']
        r['problems'] = problems(pargs, b, a)
    ok = bool(result['boots']) and not any(r['problems'] for r in result['boots'])
    result['pass'] = ok
    write_json(args.out / 'result.json', result)
    (args.out / 'report.md').write_text(report_md(result))
    print(args.out / 'report.md')
    print('PASS' if ok else 'FAIL', args.out)
    if not ok: return 1
    return 3 if any(r.get('contended') for r in result['boots']) else 0


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    initrd = args.out / 'initrd.img'
    made = make_initrd(args.inputs, initrd)
    args.cache_dir = args.out / 'compile-cache' if args.compile_cache == 'warm' else None
    if args.cache_dir and args.cache_dir.exists(): shutil.rmtree(args.cache_dir)
    first = 0 if args.cache_dir else 1
    if args.sample_us: gu.elf_image(args.inputs / 'elves.cpio.zst', args.out / 'userland.cpio')
    boots = []
    try:
        remote = boots_kvm(args, initrd, args.boots) if args.emu == 'kvm' else None
        for i in range(first, args.boots + 1):
            b = remote[i - 1] if remote else boot_natemu(args, i, initrd)
            perf = args.out / f'boot-{i}.perf' if args.perf_log else None
            (args.out / f'boot-{i}.display.jsonl').write_text(''.join(json.dumps([ns, p]) + '\n' for ns, p in b['display']))
            (args.out / f'boot-{i}.markers.json').write_text(json.dumps(b['stamps']))
            a = analyse_boot(b, perf)
            a['speedometer'] = sp_results(b['lines'])
            if args.sample_us and i > 0: markers_for_sampler(args.out / f'{args.name}.markers.tsv', b, a)
            write_guest_perf(args.out, i, b['lines'])
            r = {'boot': i, 'fill': i == 0, 'seconds': b['seconds'], 'exit': b['exit'], 'capped': b['capped'],
                 'contended': b['contended'], 'snapshots': b['snapshots'], 'display_errors': b['display_errors'],
                 'display_changes': len(b['display']), 'analysis': a, 'speedometer': a['speedometer'],
                 'command': b['command'], 'reference': b.get('reference')}
            if args.cache_dir: r['compile_cache'] = gu.cache_size(args.cache_dir)
            if perf and perf.exists(): r['perf'] = gu.perf_summary(perf.read_text(errors='replace'))
            r['problems'] = problems(args, b, a)
            boots.append(r)
            starts = [w for w in a['windows'] if w['kind'] == 'start']
            print(f'boot {i}{" (fills the compile cache)" if i == 0 else ""}: {b["seconds"]} s; session {a["session"]}; ' +
                  ', '.join(f'{w["name"].split(":")[0]} {w["seconds"]:.2f} s' for w in starts) +
                  (f'; Speedometer {a["speedometer"].get("score_text")}' if a['speedometer'] else '') +
                  (f'; compile cache {r["compile_cache"]["packs"]} packs, {r["compile_cache"]["bytes"] / 2**20:.0f} MiB' if args.cache_dir else '') +
                  (f'; problems: {"; ".join(r["problems"])}' if r['problems'] else '') +
                  (f'; contended by {b["contended"]}' if b['contended'] else ''), flush=True)
            for line in gu.perf_lines(r.get('perf')) if perf else []: print('  ' + line)
    finally:
        initrd.unlink(missing_ok=True)
        if args.cache_dir: shutil.rmtree(args.cache_dir, ignore_errors=True)
    ok = bool(boots) and not any(b['problems'] for b in boots)
    result = {'name': args.name, 'emu': args.emu, 'vcpus': args.vcpus, 'ram_gib': args.ram, 'phases': args.phases,
              'compile_cache': args.compile_cache, 'compile_cache_mb': args.compile_cache_mb, 'cmdline': cmdline(args),
              'natemu_set': natemu_sets(args), 'inputs': made, 'kernel': digest(args.kernel), 'pass': ok,
              'boots': boots}
    if args.emu == 'natemu': result.update(natemu=digest(args.natemu), runtime=digest(args.runtime))
    write_json(args.out / 'result.json', result)
    (args.out / 'report.md').write_text(report_md(result))
    print('PASS' if ok else 'FAIL', args.out, flush=True)
    if not ok: return 1
    return 3 if any(b['contended'] for b in boots) else 0


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--emu', choices=('natemu', 'kvm'), required=True)
    r.add_argument('--name', default='desktop')
    r.add_argument('--kernel', type=Path, required=True)
    r.add_argument('--inputs', type=Path, required=True, help='the desktop-inputs folder (testkit/inputs.py:desktop_inputs)')
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--phases', type=lambda v: [p for p in v.split(',') if p], default=['idle', 'ui', 'apps'])
    r.add_argument('--boots', type=lambda v: max(1, int(v)), default=1)
    r.add_argument('--vcpus', type=int, default=4); r.add_argument('--ram', type=int, default=8, help='GiB')
    r.add_argument('--cap', type=float, default=2400, help='seconds per boot')
    r.add_argument('--sp-iterations', type=int, default=5)
    r.add_argument('--fexc', type=Path); r.add_argument('--natemu', type=Path); r.add_argument('--runtime', type=Path)
    r.add_argument('--perf-log', action='store_true')
    r.add_argument('--sample-us', type=int)
    r.add_argument('--guest-perf', action='store_true', help="the guest's perf over the phases after idle")
    r.add_argument('--norandmaps', action='store_true', help="an experiment: the guest's user address-space randomization off (`norandmaps`)")
    r.add_argument('--compile-cache', choices=('off', 'warm'), default='off')
    r.add_argument('--compile-cache-mb', type=int, help="warm: the cache's cap in MiB (natemu's default without)")
    r.add_argument('--natemu-set', action='append', default=[], metavar='KEY=VALUE')
    an = sub.add_parser('analyse')
    an.add_argument('--out', type=Path, required=True)
    args = a.parse_args()
    if args.cmd == 'analyse': return analyse(args)
    if bad := [p for p in args.phases if p not in PHASES]: a.error(f'unknown phases {bad}: {PHASES}')
    if args.emu == 'natemu' and not (args.fexc and args.natemu and args.runtime): a.error('--emu natemu needs --fexc, --natemu and --runtime')
    if args.emu == 'kvm' and (args.perf_log or args.sample_us or args.compile_cache != 'off'): a.error('kvm: no --perf-log, --sample-us or --compile-cache')
    if args.sample_us is not None:
        if args.sample_us < 1 or args.boots != 1 or args.compile_cache != 'off': a.error('--sample-us N: N at least 1, --boots 1, no compile cache')
        args.perf_log = True
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    return run(args)


if __name__ == '__main__':
    if not (len(sys.argv) > 1 and sys.argv[1] == 'analyse'):
        testenv.require_framework(__file__)
    sys.exit(main())
