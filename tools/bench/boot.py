#!/usr/bin/env python3
"""One Debian boot timed to its end-point line, natemu or QEMU (from natemu-raw/qcmp-20261103-01/boot.py; the timing and
record are that script's, natemu now runs as the `natemu` stub with named arguments).

    python3 tools/bench/boot.py --out DIR --name NAME --emu natemu|qemu-multi|qemu-single --vcpus N --kernel BZIMAGE
        [--initrd FILE] [--kaslr] [--cache warm|off --cache-dir DIR] [--fexc FEXC] [--natemu STUB] [--runtime FILE]
        [--qemu QEMU]

The same harness for both emulators: the serial file polled every 50 ms from process start to the end-point line
("VFS: Unable to mount root fs" without an initrd, the Debian installer's "Select a language" with it), then SIGINT;
nothing else polled on either side but `vms.sh` every 0.5 s for a foreign VM process (another natemu stub, natemu-host or
QEMU, not this boot's own children), recorded as `contended`. natemu: the stub (`--natemu`, default target/release/natemu;
its resident `--runtime`, default the one built beside it), `--fexc` required; `--cache warm` uses the compile cache
`--cache-dir`, `off` none (`--compile-cache off`). QEMU: -nodefaults, the pc board without HPET, i8042 and vmport (natemu
has none; PIC, PIT, RTC, I/O APIC and the PIIX4 PM block stay), COM1 only, the CPU model closest to natemu's profile that
TCG boots (QCPU). Exit 0 reached, 1 not, 3 reached but contended. Appends one JSON line to DIR/boots.jsonl; serial to
DIR/NAME.serial. Run it under the VM lock with no other VM running.
"""
import argparse, json, os, pathlib, re, signal, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HERE = pathlib.Path(__file__).resolve().parent
# natemu's /proc/cpuinfo flags but: QEMU lacks xsavec (TCG implements no XSAVEC), nonstop_tsc and tsc_known_freq (TCG offers
# no invtsc and no leaf 15H).
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip'


def foreign(me):
    """Foreign VM processes (`vms.sh`): not `me` and not one of its children."""
    found = set()
    for line in subprocess.run(['sh', HERE / 'vms.sh'], capture_output=True, text=True).stdout.splitlines():
        pid, ppid, command = (line.split(None, 2) + [''])[:3]
        if int(pid) != me and int(ppid) != me: found.add(f'{pid} {command.strip()[:160]}')
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for name in ('--out', '--name', '--emu', '--vcpus', '--kernel'): ap.add_argument(name, required=True)
    ap.add_argument('--initrd')
    ap.add_argument('--kaslr', action='store_true')
    ap.add_argument('--cache', choices=('warm', 'off'))
    ap.add_argument('--cache-dir')
    ap.add_argument('--fexc')
    ap.add_argument('--natemu', default=str(ROOT / 'target/release/natemu'))
    ap.add_argument('--runtime')
    ap.add_argument('--qemu', default='/opt/homebrew/bin/qemu-system-x86_64')
    a = ap.parse_args()
    out, name, emu, n, initrd, kaslr, cache = a.out, a.name, a.emu, a.vcpus, a.initrd, a.kaslr, a.cache
    cmdline = 'console=ttyS0 earlyprintk=serial mitigations=off' + ('' if kaslr else ' nokaslr')
    os.makedirs(out, exist_ok=True)
    serial = f'{out}/{name}.serial'
    env = dict(os.environ)
    env.pop('NATEMU_PERF_LOG', None)
    if emu == 'natemu':
        if not a.fexc or (cache == 'warm' and not a.cache_dir): ap.error('--emu natemu needs --fexc (and --cache-dir with --cache warm)')
        cmd = [a.natemu, *(['--runtime', a.runtime] if a.runtime else []), '--fexc', a.fexc, '--kernel', a.kernel, '--cmdline', cmdline,
               '--vcpus', n] + (['--initrd', initrd] if initrd else []) + ['--serial', serial, '--compile-cache', a.cache_dir if cache == 'warm' else 'off']
    else:
        cmd = [a.qemu, '-nodefaults', '-machine', 'pc,hpet=off,i8042=off,vmport=off', '-accel', f"tcg,thread={emu.split('-')[1]}", '-cpu', QCPU,
               '-m', '2048', '-smp', n, '-kernel', a.kernel, '-append', cmdline, '-display', 'none', '-monitor', 'none',
               '-serial', f'file:{serial}', '-no-reboot'] + (['-initrd', initrd] if initrd else [])
    target = 'Select a language' if initrd else 'VFS: Unable to mount root fs'
    cap = 150 if initrd else 120
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=open(f'{out}/{name}.stdout', 'w'), stderr=open(f'{out}/{name}.stderr', 'w'))
    wall, text, others, last = None, '', set(), 0.0
    while time.monotonic() - t0 < cap and p.poll() is None:
        time.sleep(0.05)
        if time.monotonic() - last >= 0.5:
            others |= foreign(p.pid); last = time.monotonic()
        try:
            text = open(serial, errors='replace').read()
        except FileNotFoundError:
            continue
        if target in text:
            wall = time.monotonic() - t0
            break
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
    text = open(serial, errors='replace').read() if os.path.exists(serial) else ''

    def stamp(pattern):
        m = re.search(r'\[\s*([0-9]+\.[0-9]+)\][^\n]*' + pattern, text)
        return float(m.group(1)) if m else None
    kaslr_line = re.search(r'KASLR (disabled|enabled)', text)
    rec = dict(name=name, emu=emu, vcpus=int(n), initrd=bool(initrd), kaslr=kaslr, cache=cache, reached=wall is not None,
               wall_s=round(wall, 3) if wall else None, panic_guest_s=stamp('Kernel panic'), init_guest_s=stamp('Run /init'),
               kaslr_line=kaslr_line.group(1) if kaslr_line else False,
               cpus=re.findall(r'Brought up \d+ node, \d+ CPUs?', text)[:1], contended=sorted(others), rc=p.returncode,
               cmd=[str(c) for c in cmd], stderr=open(f'{out}/{name}.stderr').read()[-300:])
    open(f'{out}/boots.jsonl', 'a').write(json.dumps(rec) + '\n')
    print(json.dumps({k: rec[k] for k in ('name', 'reached', 'wall_s', 'contended')}))
    sys.exit(1 if not rec['reached'] else 3 if others else 0)


if __name__ == '__main__':
    main()
