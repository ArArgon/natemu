"""What tests/bench/run_web_tooling.py and run_speedometer.py share: a guest made of a Debian userland input (a recipe
under tools/bench, built by testkit/inputs.py into the cache) and a small uncompressed cpio the runner appends (its init
and helpers), booted on the Debian 6.12.107 kernel the other guest benchmarks boot, under natemu or the pinned QEMU on
this Mac or under KVM on the x86 machine (as tests/bench/run_glmark2.py does); and the summary of natemu's timing log
(--perf-log) that the profile cases report.

The timing log's `vcpu` lines are each vCPU's totals at exit. perf_summary() sums them over the vCPUs: `cpu_ns` (the
vCPU threads' busy time; the resident's buckets other than `halted` add up to it), each resident bucket's time as a
share of it (`guest`: translated guest code; `shadow`: building shadow page tables; `abort`: stage-2 and shadow aborts;
`host`: exits to the host, split by what the vCPU thread did on the host meanwhile (`host_split`: its parts other than
`run`, hv_vcpu_run itself: `translate` serving compile requests, `log` writing the timing log, `exit`, `signal`, `port`,
`step` and the rest, and what no part accounts for, the world switches); `link`, `system`, `tlb` (CR3 loads and other TLB flushes), ...), the host's translation time
(`host.translate`: ns and count, with its request parts (`fexc`, `admit`, `lookup`, `disk_get`, ...) and the answers by
source: memory, the persistent compile cache on disk, fexc), and the state and K1 counters
that show how the shadow tables fare (state.evictions, state.shadow_resets, k1.page_faults, k1.cr3_loads,
k1.invlpgs and the rest of COUNTERS)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import io
import json
import os
import re
import shlex
import stat
import subprocess
import time
from pathlib import Path

from io_common import clone, digest, stop_group
from run_io5 import others
from bench.run import QCPU, QMACHINE                      # tools/bench/run.py: the guest benchmarks' board and CPU model
from play.mkcpio import entry

KVM_IMAGE = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'   # = testkit/artifacts.py BENCH_BUILDER
SNAPSHOT = '20261120T000000Z'    # = the recipes' SNAP: Debian's QEMU for the kvm reference


def dirs(*names: str) -> list:
    return [(n, stat.S_IFDIR | 0o755, b'') for n in names]


def tail_cpio(files: list) -> bytes:
    """An uncompressed newc cpio of (path, mode, data) entries, with /dev, /dev/console and the folders an init mounts
    on (/proc, /sys, /tmp) first."""
    out = io.BytesIO()
    items = [('dev', stat.S_IFDIR | 0o755, b'', (0, 0)), ('dev/console', stat.S_IFCHR | 0o600, b'', (5, 1)),
             ('proc', stat.S_IFDIR | 0o555, b'', (0, 0)), ('sys', stat.S_IFDIR | 0o555, b'', (0, 0)),
             ('tmp', stat.S_IFDIR | 0o1777, b'', (0, 0)),
             ('etc/hosts', stat.S_IFREG | 0o644, b'127.0.0.1 localhost\n', (0, 0)),
             *((n, m, d, (0, 0)) for n, m, d in files)]
    for ino, (name, mode, data, rdev) in enumerate(items, 1): entry(out, ino, name, mode, data, rdev)
    entry(out, 0, 'TRAILER!!!', 0)
    return out.getvalue()


def make_initrd(inputs: Path, dest: Path, tail: bytes) -> dict:
    """dest: the input's userland.cpio.zst (an APFS clone, its sha256 checked against the entry's provenance), zero-padded
    to 4 bytes (the kernel reads an appended archive only from a 4-byte boundary), then `tail`."""
    src = inputs / 'userland.cpio.zst'
    recorded = json.loads((inputs / 'provenance.json').read_text())
    sha = digest(src)
    if sha != recorded['userland_sha256']: raise SystemExit(f'{src}: sha256 {sha}, its entry recorded {recorded["userland_sha256"]}')
    dest.unlink(missing_ok=True)
    clone(src, dest)
    with open(dest, 'ab') as f:
        f.write(b'\0' * (-dest.stat().st_size % 4)); f.write(tail)
    versions = inputs / 'versions.txt'
    return {'userland_sha256': sha, 'builder': recorded.get('builder'),
            'versions': versions.read_text().strip().splitlines() if versions.is_file() else None}


def natemu_command(args, initrd: Path, cmdline: str, serial: Path, perf: Path | None = None,
                   cache: Path | str = 'off', cache_mb: int | None = None, sample: tuple | None = None) -> list:
    """natemu with its persistent translation cache `cache`: 'off' (the default, as the other guest benchmarks: every boot
    translates from nothing) or a folder, with `cache_mb` its cap in MiB (natemu's own default without); `perf`: its
    timing log there; `sample` (NAME, US): the EL0 sampler every US us into NAME.samples and NAME.objects (`--profile`,
    a resident built with `perf` and `profile`), as tools/bench/run.py --sample-us."""
    return [args.natemu, '--runtime', args.runtime, '--fexc', args.fexc, '--kernel', args.kernel, '--initrd', initrd,
            '--cmdline', cmdline, '--vcpus', str(args.vcpus), '--ram', f'{args.ram}G', '--serial', serial,
            '--compile-cache', cache, *(['--compile-cache-mb', str(cache_mb)] if cache_mb and cache != 'off' else []),
            *(['--perf-log', perf] if perf else []), *(w for kv in args.natemu_set for w in ('--set', kv)),
            *(['--profile', sample[0], '--profile-us', str(sample[1])] if sample else [])]


def elf_image(src: Path, dest: Path) -> dict:
    """dest: a newc cpio of the ELF files of the userland src (userland.cpio.zst), for tests/bench/sample_report.py
    --image, which finds sampled user blocks' guest bytes in an uncompressed cpio's ELF files (sample_symbols.Image)."""
    import tempfile
    from sample_symbols import cpio_files, zstd_tool
    import mmap
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix='.cpio') as raw:
        exe = zstd_tool()
        if exe is None:
            from compression import zstd
            with zstd.open(src, 'rb') as z:
                while chunk := z.read(1 << 24): raw.write(chunk)
        else:
            subprocess.run([exe, '-dc', str(src)], stdout=raw, check=True)
        raw.flush()
        with open(raw.name, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as d, open(dest, 'wb') as out:
            n = 0
            for ino, (name, (off, size)) in enumerate(sorted(cpio_files(d).items()), 1):
                if size >= 4 and d[off:off + 4] == b'\x7fELF':
                    entry(out, ino, name, stat.S_IFREG | 0o644, d[off:off + size]); n += 1
            entry(out, 0, 'TRAILER!!!', 0)
    return {'files': n, 'bytes': dest.stat().st_size}


def cache_size(d: Path) -> dict:
    """A compile-cache folder's packs: how many, and their bytes."""
    packs = list(d.glob('*.pack')) if d.is_dir() else []
    return {'packs': len(packs), 'bytes': sum(p.stat().st_size for p in packs)}


def qemu_command(args, initrd: Path, cmdline: str, serial: Path) -> list:
    """The pinned qemu-system-x86_64, TCG thread=multi, the guest benchmarks' board and CPU model."""
    return [args.qemu, '-nodefaults', '-machine', f'{QMACHINE},hpet=off,i8042=off,vmport=off', '-accel', 'tcg,thread=multi',
            '-cpu', QCPU, '-m', str(args.ram * 1024), '-smp', str(args.vcpus), '-kernel', args.kernel, '-initrd', initrd,
            '-append', cmdline, '-display', 'none', '-monitor', 'none', '-serial', f'file:{serial}', '-no-reboot']


class Stamper:
    """Writes the console lines a sampled boot's report cuts phases at, stamped on arrival with CLOCK_UPTIME_RAW ns (the
    samples' clock), as tools/bench/run.py's NAME.markers.tsv: `NS TAB TEXT`, TEXT being `marks`' replacement for the first
    line each of its patterns matches (`(regex, text)`, `\\1` taking a group), so tests/bench/sample_report.py reads them
    as NATEMU-BENCH-START, -END and -KTEXT lines."""

    def __init__(self, path: Path, marks: list):
        self.f, self.marks, self.done, self.lines = open(path, 'w'), [(re.compile(r), t) for r, t in marks], set(), 0

    def feed(self, text: str, ns: int | None = None):
        lines = text[:text.rfind('\n') + 1].splitlines()
        ns = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW) if ns is None else ns
        for line in lines[self.lines:]:
            for k, (rx, t) in enumerate(self.marks):
                if k not in self.done and (m := rx.search(line)):
                    self.done.add(k); self.f.write(f'{ns}\t{m.expand(t)}\n')
        self.lines = len(lines); self.f.flush()

    def close(self): self.f.close()


def boot_local(out: Path, i: int, cmd: list, cap: float, stamper: Stamper | None = None) -> dict:
    """One boot on this Mac, its console in OUT/boot-I.serial (the command's --serial or -serial file:), stdout and
    stderr beside it: the console text, the seconds it took, whether it hit `cap`, its exit status and the other VMs
    seen while it ran (contended). `stamper`: fed the console as it arrives (a sampled boot's markers)."""
    serial = out / f'boot-{i}.serial'
    serial.unlink(missing_ok=True)
    before = others()
    t0 = time.monotonic()
    p = subprocess.Popen([str(c) for c in cmd], stdin=subprocess.DEVNULL, stdout=open(out / f'boot-{i}.stdout', 'wb'),
                         stderr=open(out / f'boot-{i}.stderr', 'wb'), start_new_session=True)
    seen, last, capped = set(before), 0.0, False

    def stamp():
        if stamper and serial.exists(): stamper.feed(serial.read_text(errors='replace'))
    try:
        while p.poll() is None:
            if time.monotonic() - t0 > cap: capped = True; break
            time.sleep(0.25)
            stamp()
            if time.monotonic() - last >= 0.5: seen |= set(others()); last = time.monotonic()
        stamp()
    finally:
        if p.poll() is None: stop_group(p)
        if stamper: stamper.close()
    return {'text': serial.read_text(errors='replace') if serial.exists() else '', 'seconds': round(time.monotonic() - t0, 2),
            'capped': capped, 'exit': p.returncode, 'contended': sorted(seen), 'command': [str(c) for c in cmd]}


def boots_kvm(args, initrd: Path, cmdlines: list, tag: str) -> list:
    """Every boot on the x86 machine NATEMU_X86_HOST (ssh, Docker, /dev/kvm), in one container run of the builds' pinned
    image with Debian's QEMU (at the recipes' snapshot) installed once, `-cpu` QCPU, under a lock file there
    (~/natemu-TAG-reference/.lock) so no other reference boot of this kind runs beside it. The kernel and initramfs are
    copied to a run folder there, each boot's console comes back as OUT/boot-I.serial, and the folder is removed. As
    boot_local's results, the reference's QEMU, CPU, kernel and load on the first."""
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: raise SystemExit('--emu kvm needs NATEMU_X86_HOST=user@host (an x86-64 Linux machine with Docker and /dev/kvm)')
    ssh = ['ssh', '-o', 'BatchMode=yes', host]
    base = f'natemu-{tag}-reference'; run = f'{base}/run-{os.getpid()}'; name = f'natemu-{tag}-ref-{os.getpid()}'
    mark = f'{tag.upper()}-HOST'
    subprocess.run([*ssh, f'mkdir -p {run}'], check=True)
    try:
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(args.kernel), str(initrd), f'{host}:{run}/'], check=True)
        qemu = [f'qemu-system-x86_64 -enable-kvm -cpu {QCPU} -nodefaults -machine pc,hpet=off,i8042=off,vmport=off '
                f'-m {args.ram * 1024} -smp {args.vcpus} -kernel /b/{args.kernel.name} -initrd /b/{initrd.name} '
                f'-append {shlex.quote(c)} -display none -monitor none -serial file:/b/boot-{i}.serial -no-reboot'
                for i, c in enumerate(cmdlines, 1)]
        loop = ' && '.join(f'echo "{mark} boot {i} start $(date +%s.%N)" && timeout {int(args.cap)} {q}; '
                           f'echo "{mark} boot {i} exit $? $(date +%s.%N)"' for i, q in enumerate(qemu, 1))
        guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/{SNAPSHOT}/ "
                 "trixie main' > /etc/apt/sources.list; apt-get -o Acquire::Retries=5 update -qq > /dev/null 2>&1 && "
                 "apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends qemu-system-x86 > /dev/null 2>&1 && "
                 f'echo "{mark} $(qemu-system-x86_64 --version | head -1 | tr " " _) '
                 '$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | tr " " _) host-kernel=$(uname -r) load=$(cut -d" " -f1-3 /proc/loadavg | tr " " /)" && '
                 + loop)
        log = args.out / 'kvm.log'
        with open(log, 'wb') as f:
            subprocess.run([*ssh, f'flock $HOME/{base}/.lock docker run --rm --name {name} --device /dev/kvm -v $HOME/{run}:/b {KVM_IMAGE} sh -c {shlex.quote(guest)}'],
                           stdout=f, stderr=subprocess.STDOUT, timeout=args.cap * len(cmdlines) + 900)
        host_lines = log.read_text(errors='replace')
        out = []
        for i in range(1, len(cmdlines) + 1):
            serial = args.out / f'boot-{i}.serial'; serial.unlink(missing_ok=True)
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', f'{host}:{run}/boot-{i}.serial', str(serial)])
            ex = re.search(rf'{mark} boot {i} exit (\d+) ([0-9.]+)', host_lines)
            st = re.search(rf'{mark} boot {i} start ([0-9.]+)', host_lines)
            out.append({'text': serial.read_text(errors='replace') if serial.exists() else '',
                        'seconds': round(float(ex.group(2)) - float(st.group(1)), 2) if ex and st else None,
                        'capped': bool(ex and int(ex.group(1)) == 124), 'exit': int(ex.group(1)) if ex else None,
                        'contended': [], 'command': [qemu[i - 1]]})
        facts = re.search(rf'^{mark} (\S+) (\S+) host-kernel=(\S+) load=(\S+)', host_lines, re.M)
        if facts and out: out[0]['reference'] = dict(zip(('qemu', 'cpu', 'kernel', 'load'), facts.groups()))
        return out
    finally:
        subprocess.run([*ssh, f'docker rm -f {name} > /dev/null 2>&1; rm -rf {run}'], capture_output=True)


# The counters perf_summary() reports, summed over vCPUs: (section, key) of a `vcpu` line.
COUNTERS = [('state', 'evictions'), ('state', 'shadow_resets'), ('state', 'compiles'), ('state', 'disk_hits'),
            ('state', 'systems'),
            ('k1', 'page_faults'), ('k1', 'cr3_loads'), ('k1', 'global_flushes'), ('k1', 'kept_resets'), ('k1', 'invlpgs'),
            ('k1', 'killed'), ('k1', 'tlb_us'), ('k1', 'page_fault_us'), ('k1', 'invlpg_us')]


def perf_summary(text: str) -> dict | None:
    """The timing log's `vcpu` lines summed over the vCPUs (module docstring); None if it has none (no `perf` resident,
    or natemu did not exit cleanly)."""
    vcpus = [json.loads(l[5:]) for l in text.splitlines() if l.startswith('vcpu {')]
    if not vcpus: return None
    cpu = sum(v['cpu_ns'] for v in vcpus)
    buckets: dict = {}
    for v in vcpus:
        for k, (ns, n) in v.get('resident', {}).get('buckets', {}).items():
            b = buckets.setdefault(k, [0, 0]); b[0] += ns; b[1] += n
    halted = buckets.pop('halted', [0, 0])
    shares = {k: {'ns': ns, 'count': n, 'share': round(ns / cpu, 4) if cpu else None}
              for k, (ns, n) in sorted(buckets.items(), key=lambda kv: -kv[1][0])}
    host: dict = {}
    for v in vcpus:
        for k, (ns, n) in v.get('host', {}).items():
            h = host.setdefault(k, [0, 0]); h[0] += ns; h[1] += n
    counters = {f'{s}.{k}': sum(v.get(s, {}).get(k, 0) for v in vcpus) for s, k in COUNTERS}
    parts: dict = {}
    for v in vcpus:
        for k, ns in v.get('parts', {}).items(): parts[k] = parts.get(k, 0) + ns
    sources = [sum(v.get('sources', [0, 0, 0])[i] for v in vcpus) for i in range(3)]
    tlb = buckets.get('tlb', [0, 0])
    hb = buckets.get('host', [0, 0])[0]
    parts_h = {k: ns for k, (ns, _) in sorted(host.items(), key=lambda kv: -kv[1][0]) if k != 'run' and ns}
    split = {k: {'ns': ns, 'of_host': round(ns / hb, 4) if hb else None} for k, ns in parts_h.items()}
    split['unaccounted'] = {'ns': hb - sum(parts_h.values()), 'of_host': round((hb - sum(parts_h.values())) / hb, 4) if hb else None}
    loads = counters['k1.cr3_loads'] + counters['k1.global_flushes']
    return {'vcpus': len(vcpus), 'cpu_ns': cpu, 'halted_ns': halted[0], 'buckets': shares,
            'host': {k: {'ns': ns, 'count': n} for k, (ns, n) in host.items()}, 'host_split': split,
            'translate': {'ns': host.get('translate', [0, 0])[0], 'count': host.get('translate', [0, 0])[1],
                          'parts_ns': dict(sorted(parts.items(), key=lambda kv: -kv[1])),
                          'answers': dict(zip(('memory', 'disk', 'fexc'), sources))},
            'counters': counters,
            'cr3': {'loads': counters['k1.cr3_loads'], 'tlb_ns': tlb[0], 'tlb_share': round(tlb[0] / cpu, 4) if cpu else None,
                    'ns_per_flush': round(tlb[0] / loads) if loads else None}}


def perf_lines(s: dict | None) -> list:
    """perf_summary() as text: the buckets of at least 0.5% of cpu_ns, host.translate, the counters."""
    if not s: return ['no vcpu lines in the timing log (a `perf` resident and --perf-log?)']
    out = [f'cpu_ns {s["cpu_ns"] / 1e9:.1f} s over {s["vcpus"]} vCPUs (halted {s["halted_ns"] / 1e9:.1f} s besides)']
    for k, b in s['buckets'].items():
        if b['share'] and b['share'] >= 0.005: out.append(f'  {k:12} {100 * b["share"]:5.1f}%  {b["ns"] / 1e9:8.2f} s  {b["count"]:>12,}')
    t = s['translate']
    out.append(f'host.translate {t["ns"] / 1e9:.2f} s, {t["count"]:,} translations' +
               (f', {t["ns"] / t["count"] / 1e3:.1f} us each' if t['count'] else ''))
    if t.get('parts_ns'):
        out.append('  parts: ' + ', '.join(f'{k} {ns / 1e9:.2f} s' for k, ns in t['parts_ns'].items() if ns >= 1e7))
        out.append('  answers: ' + ', '.join(f'{k} {n:,}' for k, n in t['answers'].items()))
    if s.get('host_split') and 'host' in s['buckets']:
        out.append(f'host bucket {s["buckets"]["host"]["ns"] / 1e9:.2f} s: ' +
                   ', '.join(f'{k} {100 * (v["of_host"] or 0):.0f}%' for k, v in s['host_split'].items() if abs(v['of_host'] or 0) >= 0.005))
    c = s['cr3']
    out.append(f'CR3 loads {c["loads"]:,}: tlb bucket {c["tlb_ns"] / 1e9:.2f} s ({100 * (c["tlb_share"] or 0):.1f}% of cpu_ns)' +
               (f', {c["ns_per_flush"]:,} ns per flush' if c['ns_per_flush'] else ''))
    out += [f'  {k:20} {v:>14,}' for k, v in s['counters'].items()]
    return out
