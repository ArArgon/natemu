#!/usr/bin/env python3
"""The devices family's runner: stock Linux drivers against natemu's platform devices, through the `natemu` CLI.

prepare builds one small initramfs for every case: Debian's unchanged BusyBox and stock modules from the Debian kernel
package (MODULES, with every module they depend on), and an init that runs the case the kernel command line names
(`platform_case=NAME`). run boots one VM per case (120 s each) and judges it from the guest's serial markers and the
host's exit line; it never builds or fetches anything. self-check runs the judges on synthetic logs and the init
through `sh -n`, without a VM.

A case is three things that share its name: an entry in CASES (its extra kernel command line words and its judge), a
`case_<name>` shell function in INIT, and its line in INIT's dispatch at the end. The init's helpers: `fail REASON`
(prints PLATFORM-FAIL, the judge reports it, then powers off), `pass` (PLATFORM-PASS, then an S5 power-off: natemu exits
with `natemu: Power(1)`), `word W` (is W a word of the kernel command line). A module the case loads goes into MODULES.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import dataclasses
import fcntl
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from typing import Callable

from io_common import ROOT, VM_LOCK, clone, digest, stop_group, write_json

KVER = '6.12.107+deb13-amd64'
KERNEL_SHA = '2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08'        # the netboot bzImage (debian-netboot-linux)
KERNEL_DEB_SHA = '7794643cf4560de2a7e3b069e8ca8511ad2a568d652ea285f0fe051f79bc9e99'    # its package (debian-kernel-deb)
# Stock modules the cases load (module names; files are found by name in the kernel package, `-` and `_` alike), each
# with the modules its `depends=` names, transitively. pvpanic_mmio needs pvpanic; psmouse (the PS/2 task) needs none
# (serio, libps2 and i8042 are built in: CONFIG_SERIO_I8042=y, CONFIG_SERIO_LIBPS2=y).
MODULES = ('pvpanic_mmio', 'psmouse')
APPLETS = ('sh', 'mount', 'nproc', 'uname', 'modprobe', 'cat', 'grep', 'ls', 'basename', 'readlink', 'dmesg', 'sync',
           'poweroff', 'sleep', 'stty')
VM_SECONDS = 120
CMDLINE = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr'
PVPANIC_CAPABILITY = '/sys/bus/platform/devices/QEMU0001:00/capability'
# platform: the SMBIOS identity natemu's tables give (crates/host/src/boot/smbios.rs) and the structure types they hold,
# each once but type 4 (one per vCPU); with the runner's 2 GiB of RAM one type 17 and one type 19.
DMI_IDS = {'sys_vendor': 'natemu', 'product_name': 'natemu x86-64', 'bios_vendor': 'natemu'}
SMBIOS_TYPES = (0, 1, 3, 16, 17, 19, 32, 127)
BIOS_DATE = '11/23/2026'
# platform, PS/2: the stock i8042 driver finds the 8042 through ACPI (PNP0303 and PNP0F13) and the built-in atkbd binds
# the keyboard on serio0 (translated by the controller); the psmouse module binds the mouse on serio1.
PS2_KEYBOARD = ('AT Translated Set 2 keyboard', 'isa0060/serio0/input0')
PS2_MOUSE_PHYS = 'isa0060/serio1/input0'
PS2_PNP = re.compile(r'i8042: PNP: PS/2 Controller \[.*\] at 0x60,0x64 irq 1,12$')
PS2_FAILURES = re.compile(r"i8042: Can't read CTR|psmouse.*failed", re.I)

INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain_console() { console_mode=$(stty -F /dev/console -g) && stty -F /dev/console "$console_mode"; }
fail() { echo "PLATFORM-FAIL $*"; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
pass() { echo PLATFORM-PASS; sync; drain_console || fail console-drain; poweroff -f; fail poweroff-returned; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
cmdline=" $(cat /proc/cmdline) "
word() { case "$cmdline" in *" $1 "*) return 0;; esac; return 1; }
name=
for item in $cmdline; do case "$item" in platform_case=*) name=${item#*=};; esac; done
echo "PLATFORM-BEGIN case=$name cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '@KVER@' || fail kernel

# pvpanic-panic: the stock driver finds the ACPI device and its capability (PANICKED and CRASH_LOADED), then a panic
# (panic=0: the kernel does not reboot) must end the run through the device. platform_skip_capability (the runner's
# --skip-capability) goes straight to the panic.
case_pvpanic_panic() {
  modprobe pvpanic_mmio || fail modprobe-pvpanic_mmio
  if ! word platform_skip_capability; then
    test -e @CAPABILITY@ || fail "pvpanic capability file @CAPABILITY@ missing (no QEMU0001 device bound)"
    echo "PLATFORM-PVPANIC capability=$(cat @CAPABILITY@)"
  fi
  echo PLATFORM-TRIGGER sysrq-c
  drain_console || fail console-drain
  echo c > /proc/sysrq-trigger
  fail sysrq-trigger-returned
}

# platform: the kernel's DMI scan of F0000H-FFFFFH found natemu's SMBIOS 3.0 entry point (its dmesg lines), and the
# DMI identity and the structure table's entries in sysfs (CONFIG_DMIID=y, CONFIG_DMI_SYSFS=y: built in). Then PS/2:
# psmouse loaded, the input devices (serio binds asynchronously: up to 20 s for the mouse on serio1) and the dmesg
# lines of i8042, atkbd, psmouse and serio.
case_platform() {
  dmesg | grep -e 'SMBIOS ' -e 'DMI: ' | while read -r line; do echo "PLATFORM-DMESG $line"; done
  for id in sys_vendor product_name bios_vendor; do
    test -r /sys/class/dmi/id/$id || fail "/sys/class/dmi/id/$id missing (no DMI)"
    echo "PLATFORM-DMI $id=$(cat /sys/class/dmi/id/$id)"
  done
  test -d /sys/firmware/dmi/entries || fail "/sys/firmware/dmi/entries missing (no DMI)"
  echo PLATFORM-ENTRIES $(ls /sys/firmware/dmi/entries)
  modprobe psmouse || fail modprobe-psmouse
  n=0
  until grep -q '@MOUSE_PHYS@' /proc/bus/input/devices || test $n -ge 20; do sleep 1; n=$((n + 1)); done
  # Through cat: BusyBox read takes the file a byte at a time, which /proc/bus/input/devices did not finish in 110 s.
  cat /proc/bus/input/devices | while read -r line; do echo "PLATFORM-INPUT $line"; done
  dmesg | grep -e i8042 -e atkbd -e psmouse -e serio | while read -r line; do echo "PLATFORM-PS2 $line"; done
  pass
}

case "$name" in
  pvpanic-panic) case_pvpanic_panic;;
  platform) case_platform;;
  *) fail "unknown case '$name'";;
esac
fail "case $name returned"
'''.replace('@KVER@', KVER).replace('@CAPABILITY@', PVPANIC_CAPABILITY).replace('@MOUSE_PHYS@', PS2_MOUSE_PHYS)


@dataclasses.dataclass(frozen=True)
class Run:
    """What one VM left for its judge."""
    case: str
    vcpus: int
    serial: str                     # COM1, with \r removed
    stderr: str
    returncode: int | None
    timeout: bool                   # killed at VM_SECONDS
    after_trigger: float | None     # seconds from PLATFORM-TRIGGER appearing on the serial file to the host's exit
    options: frozenset = frozenset()   # runner options that change what the case checks ('skip-capability')


def common(run: Run) -> list:
    """What every case requires: the init started this case on the pinned kernel, it reported no failure, no timeout."""
    errors = []
    begin = re.findall(r'^PLATFORM-BEGIN case=(\S*) cpus=(\d+) kernel=(\S+)$', run.serial, re.M)
    if begin != [(run.case, str(run.vcpus), KVER)]:
        errors.append(f'the init did not start case {run.case} at {run.vcpus} vCPUs on {KVER} (PLATFORM-BEGIN: {begin})')
    errors += [f'guest: {why}' for why in re.findall(r'^PLATFORM-FAIL (.*)$', run.serial, re.M)]
    if run.timeout: errors.append(f'timed out: no stop within {VM_SECONDS} s')
    return errors


def stopped(run: Run, line: str, status: int) -> list:
    """The host's last line on standard error is `line` and it exited with `status`."""
    lines = [l for l in run.stderr.splitlines() if l.strip()]
    last = lines[-1] if lines else ''
    errors = [] if last == line else [f'host ended with {last!r}, expected {line!r}']
    if run.returncode != status: errors.append(f'exit status {run.returncode}, expected {status}')
    return errors


def judge_pvpanic_panic(run: Run) -> list:
    errors = common(run)
    if 'skip-capability' not in run.options:
        caps = re.findall(r'^PLATFORM-PVPANIC capability=(.*)$', run.serial, re.M)
        if caps != ['3']: errors.append(f'pvpanic capability {caps or "not reported"}, expected 3 (PANICKED and CRASH_LOADED)')
    errors += stopped(run, 'natemu: Power(4)', 2)
    if 'Kernel panic' not in run.serial: errors.append('no "Kernel panic" on the serial console')
    if run.after_trigger is None: errors.append('the guest never reached the trigger (PLATFORM-TRIGGER)')
    elif run.after_trigger > 30 and not run.timeout: errors.append(f'stopped {run.after_trigger:.1f} s after the trigger, limit 30 s')
    return errors


def judge_platform(run: Run) -> list:
    errors = common(run)
    dmesg = [re.sub(r'^\[[^]]*\] ?', '', l) for l in re.findall(r'^PLATFORM-DMESG (.*)$', run.serial, re.M)]
    if 'SMBIOS 3.0.0 present.' not in dmesg:
        errors.append(f'no "SMBIOS 3.0.0 present." in dmesg (its SMBIOS lines: {[l for l in dmesg if "SMBIOS" in l]})')
    ids = [l for l in dmesg if l.startswith('DMI: ') and not l.startswith('DMI: Memory')]
    if len(ids) != 1 or not (ids[0].startswith('DMI: natemu natemu x86-64, BIOS natemu ') and ids[0].endswith(' ' + BIOS_DATE)):
        errors.append(f'DMI identity line {ids}, expected one "DMI: natemu natemu x86-64, BIOS natemu VERSION {BIOS_DATE}"')
    got = dict(re.findall(r'^PLATFORM-DMI (\w+)=(.*)$', run.serial, re.M))
    if got != DMI_IDS: errors.append(f'/sys/class/dmi/id {got}, expected {DMI_IDS}')
    entries = re.findall(r'^PLATFORM-ENTRIES(.*)$', run.serial, re.M)
    counts = {}
    for e in (entries[0].split() if entries else []):
        counts[int(e.split('-')[0])] = counts.get(int(e.split('-')[0]), 0) + 1
    want = dict(sorted(({t: 1 for t in SMBIOS_TYPES} | {4: run.vcpus}).items()))
    if dict(sorted(counts.items())) != want:
        errors.append(f'/sys/firmware/dmi/entries by type {dict(sorted(counts.items())) if entries else "not listed"}, expected {want}')
    errors += judge_ps2(run)
    errors += stopped(run, 'natemu: Power(1)', 0)
    return errors


def input_devices(serial: str) -> list:
    """/proc/bus/input/devices from the PLATFORM-INPUT lines: (name, phys) of each device."""
    devices, name, phys = [], None, None
    for line in [*re.findall(r'^PLATFORM-INPUT ?(.*)$', serial, re.M), '']:
        if not line.strip():
            if name is not None or phys is not None: devices.append((name, phys))
            name = phys = None
        elif m := re.fullmatch(r'N: Name="(.*)"', line.strip()): name = m.group(1)
        elif m := re.fullmatch(r'P: Phys=(.*)', line.strip()): phys = m.group(1)
    return devices


def judge_ps2(run: Run) -> list:
    """platform's PS/2 part: the keyboard on serio0, a PS/2 mouse on serio1, the controller found through ACPI, and
    no i8042 or psmouse failure in dmesg."""
    errors = []
    devices = input_devices(run.serial)
    if devices.count(PS2_KEYBOARD) != 1:
        errors.append(f'input devices {devices}: expected one {PS2_KEYBOARD[0]!r} on {PS2_KEYBOARD[1]}')
    mice = [n for n, p in devices if p == PS2_MOUSE_PHYS]
    if len(mice) != 1 or not re.search(r'PS/2 .*Mouse', mice[0] or ''):
        errors.append(f'input devices on {PS2_MOUSE_PHYS}: {mice}, expected one PS/2 mouse (psmouse probe failed?)')
    dmesg = [re.sub(r'^\[[^]]*\] ?', '', l) for l in re.findall(r'^PLATFORM-PS2 (.*)$', run.serial, re.M)]
    if not any(PS2_PNP.search(l) for l in dmesg):
        errors.append(f'no "i8042: PNP: PS/2 Controller [...] at 0x60,0x64 irq 1,12" in dmesg (its i8042 lines: '
                      f'{[l for l in dmesg if "i8042" in l]})')
    errors += [f'dmesg: {l}' for l in dmesg if PS2_FAILURES.search(l)]
    return errors


@dataclasses.dataclass(frozen=True)
class Case:
    words: tuple                    # kernel command line words beyond CMDLINE and platform_case=NAME
    judge: Callable[[Run], list]


CASES = {
    'pvpanic-panic': Case(('panic=0',), judge_pvpanic_panic),
    'platform': Case((), judge_platform),
}
TRIGGER = re.compile(r'^PLATFORM-TRIGGER ', re.M)


def busybox_from(deb: Path) -> bytes:
    archive = subprocess.check_output(['ar', 'p', str(deb), 'data.tar.xz'], timeout=10)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        members = [m for m in package if m.name.removeprefix('./') in ('usr/bin/busybox', 'bin/busybox') and m.isfile()]
        assert len(members) == 1, 'one unchanged BusyBox executable required'
        busybox = package.extractfile(members[0]).read()
    assert busybox[:4] == b'\x7fELF' and int.from_bytes(busybox[18:20], 'little') == 62, 'not an x86-64 BusyBox'
    return busybox


def module_closure(deb: Path, names) -> dict:
    """Each module `names` lists and every module they depend on, from the kernel package: name -> (path under
    lib/modules/KVER, uncompressed .ko, its direct dependencies)."""
    archive = lzma.decompress(subprocess.check_output(['ar', 'p', str(deb), 'data.tar.xz'], timeout=60))   # plain tar: cheap to seek
    prefix = f'usr/lib/modules/{KVER}/'
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as package:
        index = {}
        for m in package.getmembers():
            rel = m.name.removeprefix('./')
            if m.isfile() and rel.startswith(prefix) and rel.endswith('.ko.xz'):
                index[Path(rel).name.removesuffix('.ko.xz').replace('-', '_')] = (rel.removeprefix(prefix).removesuffix('.xz'), m)
        out, todo = {}, [n.replace('-', '_') for n in names]
        while todo:
            name = todo.pop(0)
            if name in out: continue
            assert name in index, f'{name}: no such module in the kernel package (built in, or not built?)'
            rel, member = index[name]
            data = lzma.decompress(package.extractfile(member).read())
            assert data[:4] == b'\x7fELF' and ('vermagic=' + KVER).encode() in data, f'{name}: not a {KVER} module'
            found = re.search(rb'(?:^|\0)depends=([^\0]*)', data)
            assert found, f'{name}: no depends= in its modinfo'
            deps = [d.replace('-', '_') for d in found.group(1).decode().split(',') if d]
            out[name] = (rel, data, deps)
            todo += deps
    return out


def modules_dep(modules: dict) -> str:
    """modules.dep: each module's transitive dependencies, nearest first (modprobe loads them from the end)."""
    def chain(name):
        got = []
        for d in modules[name][2]:
            for x in [d, *chain(d)]:
                if x not in got: got.append(x)
        return got
    return ''.join(f'{rel}:' + ''.join(' ' + modules[d][0] for d in chain(n)) + '\n' for n, (rel, _, _) in sorted(modules.items()))


def prepare(args):
    args.out.mkdir(parents=True, exist_ok=False)
    assert digest(args.kernel) == KERNEL_SHA, 'not the pinned Debian kernel'
    assert digest(args.kernel_deb) == KERNEL_DEB_SHA, 'not the pinned Debian kernel package'
    clone(args.kernel, args.out / 'linux')
    root = args.out / 'rootfs'
    for name in ('bin', 'dev', 'proc', 'sys', 'tmp', 'lib/modules/' + KVER):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / 'bin/busybox').write_bytes(busybox_from(args.busybox_deb))
    (root / 'bin/busybox').chmod(0o755)
    for applet in APPLETS: (root / 'bin' / applet).symlink_to('busybox')
    modules = module_closure(args.kernel_deb, MODULES)
    for rel, data, _ in modules.values():
        dest = root / 'lib/modules' / KVER / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (root / 'lib/modules' / KVER / 'modules.dep').write_text(modules_dep(modules))
    (root / 'init').write_text(INIT)
    (root / 'init').chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mkcpio)
    mkcpio.main(str(root), str(args.out / 'platform.cpio'))
    manifest = {'purpose': 'stock Linux drivers against natemu platform devices (tests/devices)', 'kernel_version': KVER,
                'sources': {p.name: digest(p) for p in (args.kernel, args.kernel_deb, args.busybox_deb)},
                'producer': {str(p.relative_to(ROOT)): digest(p) for p in (Path(__file__).resolve(), ROOT / 'tools/play/mkcpio.py')},
                'modules': {n: {'path': rel, 'sha256': digest(root / 'lib/modules' / KVER / rel), 'depends': deps}
                            for n, (rel, _, deps) in sorted(modules.items())},
                'outputs': {str(p.relative_to(args.out)): digest(p)
                            for p in (args.out / 'linux', args.out / 'platform.cpio', root / 'init', root / 'bin/busybox')}}
    write_json(args.out / 'inputs.json', manifest)
    print(json.dumps(manifest, indent=2))


def boot(args, name: str, work: Path) -> Run:
    """One VM for case `name`: started, watched for the trigger marker, stopped at VM_SECONDS."""
    case = CASES[name]
    words = [*case.words, f'platform_case={name}', *(['platform_skip_capability'] if args.skip_capability else [])]
    command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
               '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / 'platform.cpio').resolve()),
               '--vcpus', str(args.vcpus), '--ram', '2G', '--compile-cache', 'off', '--serial', str((work / 'serial.log').resolve()),
               '--cmdline', ' '.join([CMDLINE, *words])]
    for setting in args.set: command += ['--set', setting]
    write_json(work / 'command.json', command)
    env = {k: v for k, v in os.environ.items() if k not in ('NATEMU_PERF_LOG', 'NATEMU_COMPILE_CACHE')}   # settings the CLI reads (deprecated)
    serial = work / 'serial.log'
    started, timeout, triggered = time.monotonic(), False, None
    with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
        try:
            while process.poll() is None:
                if triggered is None and serial.exists() and TRIGGER.search(serial.read_text(errors='replace').replace('\r', '')):
                    triggered = time.monotonic()
                if time.monotonic() - started >= VM_SECONDS:
                    timeout = True
                    stop_group(process)
                    break
                time.sleep(0.1)
        except BaseException:
            stop_group(process)
            raise
    ended = time.monotonic()
    text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
    if triggered is None and TRIGGER.search(text): triggered = ended      # it appeared between the last look and the exit
    return Run(name, args.vcpus, text, (work / 'stderr.log').read_text(errors='replace'), process.returncode, timeout,
               None if triggered is None else ended - triggered, frozenset(['skip-capability'] if args.skip_capability else []))


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.inputs / name) == sha, name
    write_json(args.out / 'manifest.json', {'artifacts': {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))},
                                            'inputs': manifest, 'cases': args.case, 'set': args.set, 'vcpus': args.vcpus,
                                            'skip_capability': args.skip_capability, 'seconds_per_vm': VM_SECONDS})
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)          # the VM lock, shared as the other Linux runners hold it
        for name in args.case:
            work = args.out / name
            work.mkdir()
            r = boot(args, name, work)
            errors = CASES[name].judge(r)
            result = {'name': name, 'ok': not errors, 'errors': errors, 'returncode': r.returncode, 'timeout': r.timeout,
                      'after_trigger': r.after_trigger, 'options': sorted(r.options)}
            write_json(work / 'result.json', result)
            results.append(result)
            write_json(args.out / 'results.json', {'results': results})
            print(json.dumps(result), flush=True)
    return int(any(not r['ok'] for r in results))


def self_check():
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    for name in CASES:
        assert f'\n  {name}) case_{name.replace("-", "_")};;\n' in INIT, f'{name}: no dispatch line in INIT'
        assert f'\ncase_{name.replace("-", "_")}() {{\n' in INIT, f'{name}: no case function in INIT'
    begin = f'PLATFORM-BEGIN case=pvpanic-panic cpus=1 kernel={KVER}\n'
    serial = begin + 'PLATFORM-PVPANIC capability=3\nPLATFORM-TRIGGER sysrq-c\nsysrq: Trigger a crash\nKernel panic - not syncing: sysrq triggered crash\n'
    good = Run('pvpanic-panic', 1, serial, 'natemu: cpu: x\nnatemu: Power(4)\n', 2, False, 1.5)
    judge = CASES['pvpanic-panic'].judge
    assert not judge(good), judge(good)
    skipped = dataclasses.replace(good, serial=serial.replace('PLATFORM-PVPANIC capability=3\n', ''), options=frozenset(['skip-capability']))
    assert not judge(skipped), judge(skipped)
    swap = lambda old, new: dataclasses.replace(good, serial=good.serial.replace(old, new))
    bad = {'capability 0': swap('capability=3', 'capability=0'),
           'no capability': swap('PLATFORM-PVPANIC capability=3\n', ''),
           'guest failure': swap('PLATFORM-TRIGGER', f'PLATFORM-FAIL pvpanic capability file {PVPANIC_CAPABILITY} missing\nPLATFORM-TRIGGER'),
           'no panic': swap('Kernel panic', 'Kernel'),
           'other case': swap('case=pvpanic-panic', 'case=platform'),
           'other kernel': swap(KVER, '6.1.0'),
           'S5': dataclasses.replace(good, stderr='natemu: Power(1)\n', returncode=0),
           'reset': dataclasses.replace(good, stderr='natemu: Power(2)\n'),
           'not last': dataclasses.replace(good, stderr='natemu: Power(4)\nnatemu: later\n'),
           'status': dataclasses.replace(good, returncode=1),
           'timeout': dataclasses.replace(good, timeout=True, stderr='natemu: Cancelled\n', after_trigger=118.0),
           'slow': dataclasses.replace(good, after_trigger=30.5),
           'no trigger': dataclasses.replace(good, after_trigger=None)}
    for what, r in bad.items(): assert judge(r), f'{what}: passed'
    assert any('missing' in e for e in judge(bad['guest failure'])), judge(bad['guest failure'])
    assert any('timed out' in e for e in judge(bad['timeout'])), judge(bad['timeout'])
    # platform, at 2 vCPUs: a passing log, then one break of each thing it checks.
    entries = '0-0 1-0 127-0 16-0 17-0 19-0 3-0 32-0 4-0 4-1'
    serial = (f'PLATFORM-BEGIN case=platform cpus=2 kernel={KVER}\n'
              '[    0.000000] PLATFORM-DMESG-NOT-THIS SMBIOS 2.8 present.\n'
              'PLATFORM-DMESG [    0.000000] SMBIOS 3.0.0 present.\n'
              f'PLATFORM-DMESG [    0.000000] DMI: natemu natemu x86-64, BIOS natemu 0.1.0 {BIOS_DATE}\n'
              'PLATFORM-DMESG [    0.012345] DMI: Memory slots populated: 1/1\n'
              'PLATFORM-DMI sys_vendor=natemu\nPLATFORM-DMI product_name=natemu x86-64\nPLATFORM-DMI bios_vendor=natemu\n'
              f'PLATFORM-ENTRIES {entries}\n'
              'PLATFORM-INPUT I: Bus=0019 Vendor=0000 Product=0001 Version=0000\nPLATFORM-INPUT N: Name="Power Button"\n'
              'PLATFORM-INPUT P: Phys=LNXPWRBN/button/input0\nPLATFORM-INPUT \n'
              'PLATFORM-INPUT I: Bus=0011 Vendor=0001 Product=0001 Version=ab41\n'
              'PLATFORM-INPUT N: Name="AT Translated Set 2 keyboard"\nPLATFORM-INPUT P: Phys=isa0060/serio0/input0\n'
              'PLATFORM-INPUT S: Sysfs=/devices/platform/i8042/serio0/input/input1\nPLATFORM-INPUT \n'
              'PLATFORM-INPUT I: Bus=0011 Vendor=0002 Product=0005 Version=0000\n'
              'PLATFORM-INPUT N: Name="ImPS/2 Generic Wheel Mouse"\nPLATFORM-INPUT P: Phys=isa0060/serio1/input0\n'
              'PLATFORM-INPUT \n'
              'PLATFORM-PS2 [    0.512345] i8042: PNP: PS/2 Controller [PNP0303:KBD,PNP0f13:MOU] at 0x60,0x64 irq 1,12\n'
              'PLATFORM-PS2 [    0.523456] serio: i8042 KBD port at 0x60,0x64 irq 1\n'
              'PLATFORM-PS2 [    0.523457] serio: i8042 AUX port at 0x60,0x64 irq 12\n'
              'PLATFORM-PS2 [    0.601234] input: AT Translated Set 2 keyboard as /devices/platform/i8042/serio0/input/input1\n'
              'PLATFORM-PASS\n')
    good = Run('platform', 2, serial, 'natemu: Power(1)\n', 0, False, None)
    judge = CASES['platform'].judge
    assert not judge(good), judge(good)
    swap = lambda old, new: dataclasses.replace(good, serial=good.serial.replace(old, new))
    platform_bad = {'no SMBIOS line': swap('SMBIOS 3.0.0 present.', 'Booting'),
                    'SMBIOS 3.2.0': swap('SMBIOS 3.0.0', 'SMBIOS 3.2.0'),
                    'other product': swap('natemu natemu x86-64, BIOS', 'natemu natemu, BIOS'),
                    'other date': swap(f'0.1.0 {BIOS_DATE}', '0.1.0 01/01/2026'),
                    'no DMI line': swap('DMI: natemu', 'XMI: natemu'),
                    'sysfs vendor': swap('sys_vendor=natemu', 'sys_vendor=QEMU'),
                    'sysfs missing': swap('PLATFORM-DMI bios_vendor=natemu\n', ''),
                    'one type 4': swap(' 4-1', ''),
                    'no type 32': swap(' 32-0', ''),
                    'no type 127': swap('127-0 ', ''),
                    'extra type 11': swap(' 4-0', ' 11-0 4-0'),
                    'no entries': swap(f'PLATFORM-ENTRIES {entries}\n', ''),
                    'guest failure': swap('PLATFORM-PASS', 'PLATFORM-FAIL /sys/firmware/dmi/entries missing (no DMI)'),
                    'one vCPU': dataclasses.replace(good, vcpus=1),
                    'panic': dataclasses.replace(good, stderr='natemu: Power(4)\n', returncode=2),
                    'stopped': dataclasses.replace(good, stderr='natemu: UnsupportedMmio\n', returncode=2),
                    'timeout': dataclasses.replace(good, timeout=True, stderr='natemu: Cancelled\n', returncode=2),
                    'no keyboard': swap('N: Name="AT Translated Set 2 keyboard"', 'N: Name="Power Button"'),
                    'untranslated keyboard': swap('AT Translated Set 2 keyboard"', 'AT Set 2 keyboard"'),
                    'keyboard elsewhere': swap('Phys=isa0060/serio0/input0', 'Phys=isa0060/serio2/input0'),
                    'no mouse': swap('PLATFORM-INPUT P: Phys=isa0060/serio1/input0\n', ''),
                    'not a mouse': swap('ImPS/2 Generic Wheel Mouse', 'Some Touchscreen'),
                    'no input list': dataclasses.replace(good, serial=re.sub(r'^PLATFORM-INPUT.*\n', '', serial, flags=re.M)),
                    'no PNP': swap('i8042: PNP: PS/2 Controller', 'i8042: PNP: No PS/2 controller found.'),
                    'no aux irq': swap('at 0x60,0x64 irq 1,12', 'at 0x60,0x64 irq 1'),
                    "can't read CTR": swap('PLATFORM-PASS', "PLATFORM-PS2 [    0.5] i8042: Can't read CTR while initializing i8042\nPLATFORM-PASS"),
                    'psmouse failed': swap('PLATFORM-PASS', 'PLATFORM-PS2 [    1.2] psmouse serio1: Failed to enable mouse on isa0060/serio1\nPLATFORM-PASS')}
    for what, r in platform_bad.items(): assert judge(r), f'platform {what}: passed'
    assert any('PS/2 mouse' in e for e in judge(platform_bad['no mouse'])), judge(platform_bad['no mouse'])
    assert any('SMBIOS 3.0.0' in e for e in judge(platform_bad['no SMBIOS line'])), judge(platform_bad['no SMBIOS line'])
    print(f'Init shell syntax, {len(CASES)} case dispatch(es) and {len(bad) + len(platform_bad)} negative judge controls '
          'passed; no VM launched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('kernel-deb', 'kernel', 'busybox-deb', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    runner = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out'): runner.add_argument('--' + name, type=Path, required=True)
    runner.add_argument('--lock', type=Path, default=VM_LOCK, help='the VM lock file, held shared (default: the state root\'s)')
    runner.add_argument('--case', nargs='+', choices=sorted(CASES), required=True, help='one VM each, in this order')
    runner.add_argument('--set', action='append', default=[], metavar='KEY=VALUE', help="passed to the natemu CLI's --set")
    runner.add_argument('--vcpus', type=int, default=1)
    runner.add_argument('--skip-capability', action='store_true',
                        help='pvpanic-panic: panic without checking the capability file first (a failure demonstration)')
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': return prepare(args)
    if args.command == 'self-check': return self_check()
    if len(args.case) != len(set(args.case)): parser.error('duplicate cases')
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    raise SystemExit(main())
