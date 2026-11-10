#!/usr/bin/env python3
"""Prepare and exercise stock Linux virtio-MMIO entropy through the normal natemu CLI.

prepare extracts unchanged BusyBox and Debian modules from retained inputs and builds a
small deterministic initramfs. run consumes existing retained-source build artifacts;
it never builds, fetches, retries, or changes a production limit. Each VM has 120 seconds.
These are development checks, not entropy-quality or performance measurements.
"""
import argparse
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tarfile
import time
from io_common import digest, stop_group, ROOT

KVER = '6.12.107+deb13-amd64'
KERNEL_SHA = '2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08'
MODULES = ('kernel/drivers/virtio/virtio_mmio.ko', 'kernel/drivers/char/hw_random/virtio-rng.ko')

INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain_console() { console_mode=$(/bin/busybox stty -F /dev/console -g) && /bin/busybox stty -F /dev/console "$console_mode"; }
fail() { echo "VIRTIO-RNG-FAIL $*"; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
echo "VIRTIO-RNG-BEGIN cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '6.12.107+deb13-amd64' || fail kernel
modprobe virtio_mmio || fail modprobe-mmio
modprobe virtio_rng || fail modprobe-rng
acpi=
for d in /sys/bus/acpi/devices/LNRO0005:*; do
  test -e "$d/hid" || continue
  test "$(cat "$d/hid")" = LNRO0005 || fail hid
  test "$(cat "$d/uid")" = 2 || fail uid
  test -z "$acpi" || fail multiple-acpi-devices
  acpi=$d
done
test -n "$acpi" || fail no-acpi-device
echo "VIRTIO-RNG-ACPI hid=$(cat "$acpi/hid") uid=$(cat "$acpi/uid")"
grep -E 'f0002000-f0002fff' /proc/iomem || fail mmio-resource
echo VIRTIO-RNG-RESOURCE base=f0002000 end=f0002fff
find_rng() {
  virtio=
  for d in /sys/bus/virtio/devices/*; do
    test -e "$d/device" || continue
    test "$(cat "$d/device")" = 0x0004 || continue
    test "$(basename "$(readlink -f "$d/driver")")" = virtio_rng || fail rng-driver
    test -z "$virtio" || fail multiple-rngs
    virtio=$d
  done
  test -n "$virtio" || fail no-rng
  current=$(cat /sys/class/misc/hw_random/rng_current)
  case "$current" in virtio_rng.*) ;; *) fail "current=$current";; esac
  test -c /dev/hwrng || fail no-hwrng
  echo "VIRTIO-RNG-DRIVER device=$(basename "$virtio") current=$current"
}
read_rng() {
  dd if=/dev/hwrng of="/tmp/$1" bs=4096 count=1 iflag=fullblock 2>/tmp/dd.log || fail "read-$1"
  test "$(wc -c < "/tmp/$1")" -eq 4096 || fail "short-$1"
  if cmp -s "/tmp/$1" /tmp/zero; then fail "zero-$1"; fi
  hash=$(sha256sum "/tmp/$1"); hash=${hash%% *}
  echo "VIRTIO-RNG-DATA name=$1 bytes=4096 sha256=$hash"
}
dd if=/dev/zero of=/tmp/zero bs=4096 count=1 2>/dev/null || fail zero-fixture
find_rng
platform=$(dirname "$(readlink -f "$virtio")")
driver=$(readlink -f "$platform/driver")
name=$(basename "$platform")
test "$(basename "$driver")" = virtio-mmio || fail platform-driver
read_rng before-a
read_rng before-b
echo "$name" > "$driver/unbind" || fail unbind
test ! -e "$platform/driver" || fail still-bound
echo "VIRTIO-RNG-UNBOUND device=$name"
echo "$name" > "$driver/bind" || fail rebind
find_rng
echo "VIRTIO-RNG-REBOUND device=$name"
read_rng after-a
read_rng after-b
for a in before-a before-b after-a after-b; do
  for b in before-a before-b after-a after-b; do
    test "$a" = "$b" && continue
    if cmp -s "/tmp/$a" "/tmp/$b"; then fail "duplicate-$a-$b"; fi
  done
done
echo VIRTIO-RNG-INTERRUPTS-BEGIN
cat /proc/interrupts
echo VIRTIO-RNG-INTERRUPTS-END
echo VIRTIO-RNG-DMESG-BEGIN
dmesg
echo VIRTIO-RNG-DMESG-END
echo VIRTIO-RNG-PASS
sync
drain_console || fail console-drain
poweroff -f
fail poweroff-returned
'''


def cpio_files(data):
    at = 0
    while at < len(data):
        if data[at:at + 6] not in (b'070701', b'070702'):
            raise ValueError(f'not newc at {at}')
        fields = [int(data[at + 6 + i * 8:at + 14 + i * 8], 16) for i in range(13)]
        length, namesize = fields[6], fields[11]
        name_end = at + 110 + namesize
        if namesize < 1 or name_end > len(data) or data[name_end - 1] != 0:
            raise ValueError('invalid cpio name')
        name = data[at + 110:name_end - 1].decode()
        start = (name_end + 3) & ~3
        end = start + length
        if end > len(data): raise ValueError('truncated cpio data')
        if name == 'TRAILER!!!': return
        yield name.removeprefix('./'), fields[1], data[start:end]
        at = (end + 3) & ~3
    raise ValueError('missing cpio trailer')


def prepare(args):
    args.out.mkdir(parents=True, exist_ok=False)
    assert digest(args.kernel) == KERNEL_SHA, 'not the pinned Debian kernel'
    kernel = args.out / 'linux'
    kernel.write_bytes(args.kernel.read_bytes())
    root = args.out / 'rootfs'
    for name in ('bin', 'dev', 'proc', 'sys', 'tmp', 'lib/modules/' + KVER):
        (root / name).mkdir(parents=True, exist_ok=True)
    archive = subprocess.check_output(['ar', 'p', str(args.busybox_deb), 'data.tar.xz'], timeout=10)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        members = [m for m in package if m.name.removeprefix('./') in ('usr/bin/busybox', 'bin/busybox') and m.isfile()]
        assert len(members) == 1, 'one unchanged BusyBox executable required'
        busybox = package.extractfile(members[0]).read()
    assert busybox[:4] == b'\x7fELF' and int.from_bytes(busybox[18:20], 'little') == 62
    assert b'fullblock' in busybox, 'BusyBox dd needs fullblock for short hwrng reads'
    (root / 'bin/busybox').write_bytes(busybox)
    (root / 'bin/busybox').chmod(0o755)
    for applet in ('sh', 'mount', 'nproc', 'uname', 'modprobe', 'cat', 'grep', 'basename', 'dirname', 'readlink',
                   'dd', 'wc', 'cmp', 'sha256sum', 'dmesg', 'sync', 'poweroff', 'sleep'):
        (root / 'bin' / applet).symlink_to('busybox')
    wanted = {f'usr/lib/modules/{KVER}/{m}.xz': m for m in MODULES}
    extracted = {}
    for name, mode, data in cpio_files(gzip.decompress(args.installer.read_bytes())):
        if name not in wanted: continue
        assert stat.S_ISREG(mode)
        module = lzma.decompress(data)
        assert module[:4] == b'\x7fELF' and ('vermagic=' + KVER).encode() in module
        assert b'depends=\0' in module, 'unexpected loadable module dependency'
        dest = root / 'lib/modules' / KVER / wanted[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(module)
        extracted[name] = {'compressed_sha256': hashlib.sha256(data).hexdigest(), 'module_sha256': digest(dest)}
    assert len(extracted) == len(wanted), 'required stock modules absent from installer'
    (root / 'lib/modules' / KVER / 'modules.dep').write_text(''.join(m + ':\n' for m in MODULES))
    (root / 'init').write_text(INIT)
    (root / 'init').chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mkcpio)
    mkcpio.main(str(root), str(args.out / 'rng.cpio'))
    manifest = {'purpose': 'stock Linux virtio-rng development check', 'kernel_version': KVER,
                'sources': {str(p.resolve()): digest(p) for p in (args.kernel, args.installer, args.busybox_deb, Path(__file__), ROOT / 'tools/play/mkcpio.py')},
                'modules': extracted, 'builtin': ['CONFIG_VIRTIO=y', 'CONFIG_HW_RANDOM=y'],
                'outputs': {str(p.relative_to(args.out)): digest(p) for p in (kernel, args.out / 'rng.cpio', root / 'init', root / 'bin/busybox')}}
    (args.out / 'inputs.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


def judge(text, n):
    errors = []
    def require(condition, message):
        if not condition: errors.append(message)
    require(re.findall(r'^VIRTIO-RNG-BEGIN cpus=(\d+) kernel=(\S+)$', text, re.M) == [(str(n), KVER)], 'CPU count or kernel mismatch')
    require(re.findall(r'^VIRTIO-RNG-ACPI hid=LNRO0005 uid=2$', text, re.M) == ['VIRTIO-RNG-ACPI hid=LNRO0005 uid=2'], 'missing ACPI discovery')
    require(re.findall(r'^VIRTIO-RNG-RESOURCE base=f0002000 end=f0002fff$', text, re.M) == ['VIRTIO-RNG-RESOURCE base=f0002000 end=f0002fff'], 'missing MMIO resource')
    require(len(re.findall(r'^VIRTIO-RNG-DRIVER device=virtio\d+ current=virtio_rng\.\d+$', text, re.M)) == 2, 'driver not bound before and after reset')
    unbound = re.findall(r'^VIRTIO-RNG-UNBOUND device=(\S+)$', text, re.M)
    require(len(unbound) == 1 and re.findall(r'^VIRTIO-RNG-REBOUND device=(\S+)$', text, re.M) == unbound, 'unbind/rebind missing or mismatched')
    rows = re.findall(r'^VIRTIO-RNG-DATA name=(\S+) bytes=(\d+) sha256=([0-9a-f]{64})$', text, re.M)
    require([r[0] for r in rows] == ['before-a', 'before-b', 'after-a', 'after-b'], 'missing or duplicate read results')
    require(len(rows) == 4 and all(r[1] == '4096' for r in rows), 'short entropy transfer')
    hashes = {r[2] for r in rows}
    require(len(hashes) == 4 and hashlib.sha256(bytes(4096)).hexdigest() not in hashes, 'zero or repeated entropy payload')
    for marker in ('INTERRUPTS-BEGIN', 'INTERRUPTS-END', 'DMESG-BEGIN', 'DMESG-END', 'PASS'):
        require(len(re.findall(r'^VIRTIO-RNG-' + marker + '$', text, re.M)) == 1, 'missing/duplicate ' + marker)
    interrupts = re.findall(r'^\s*\d+:\s+((?:\d+\s+){' + str(n) + r'}).*\b18-fasteoi\s+virtio\d+\s*$', text, re.M)
    require(len(interrupts) == 1 and sum(map(int, interrupts[0].split())) > 0, 'no delivered virtio interrupt on GSI 18')
    require(not re.search(r'VIRTIO-RNG-FAIL|BUG:|Oops:|Kernel panic|Call Trace:', text), 'guest or kernel error')
    return errors


def self_check():
    text = f'VIRTIO-RNG-BEGIN cpus=2 kernel={KVER}\n'
    text += 'VIRTIO-RNG-ACPI hid=LNRO0005 uid=2\nVIRTIO-RNG-RESOURCE base=f0002000 end=f0002fff\n'
    text += 'VIRTIO-RNG-DRIVER device=virtio0 current=virtio_rng.0\n'
    for name, letter in (('before-a', 'a'), ('before-b', 'b')):
        text += f'VIRTIO-RNG-DATA name={name} bytes=4096 sha256={letter * 64}\n'
    text += 'VIRTIO-RNG-UNBOUND device=LNRO0005:00\nVIRTIO-RNG-DRIVER device=virtio1 current=virtio_rng.0\nVIRTIO-RNG-REBOUND device=LNRO0005:00\n'
    for name, letter in (('after-a', 'c'), ('after-b', 'd')):
        text += f'VIRTIO-RNG-DATA name={name} bytes=4096 sha256={letter * 64}\n'
    for marker in ('INTERRUPTS-BEGIN', 'INTERRUPTS-END', 'DMESG-BEGIN', 'DMESG-END', 'PASS'):
        text += 'VIRTIO-RNG-' + marker + '\n'
        if marker == 'INTERRUPTS-BEGIN': text += ' 18: 32 0 IO-APIC 18-fasteoi virtio1\n'
    assert not judge(text, 2), judge(text, 2)
    mutations = [('cpus=2', 'cpus=1'), (KVER, 'wrong'), ('hid=LNRO0005', 'hid=OTHER'),
                 ('base=f0002000', 'base=f0003000'), ('bytes=4096', 'bytes=64'), ('d' * 64, 'a' * 64),
                 ('d' * 64, hashlib.sha256(bytes(4096)).hexdigest()), ('VIRTIO-RNG-PASS', 'missing'),
                 ('REBOUND device=LNRO0005:00', 'REBOUND device=other'), ('name=after-a', 'name=before-a'),
                 ('18: 32 0', '18: 0 0'), ('18-fasteoi', '19-fasteoi')]
    for old, new in mutations: assert judge(text.replace(old, new), 2), old
    assert judge(text + 'VIRTIO-RNG-FAIL synthetic\n', 2)
    assert judge(text + 'BUG: synthetic\n', 2)
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    print('Guest shell syntax and 14 negative parser controls passed; no VM launched.')


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.inputs / name) == sha, name
    artifacts = {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))}
    sources = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'crates').rglob('*')) if p.is_file()}
    (args.out / 'manifest.json').write_text(json.dumps({'artifacts': artifacts, 'inputs': manifest, 'source_root': str(ROOT),
                                                      'source_sha256': sources, 'cpus': args.cpus, 'seconds_per_vm': 120}, indent=2) + '\n')
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for n in args.cpus:
            work = args.out / f'n{n}'
            work.mkdir()
            command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
                       '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / 'rng.cpio').resolve()),
                       '--rng', '--vcpus', str(n), '--ram', '2G', '--compile-cache', 'off', '--serial', str((work / 'serial.log').resolve()),
                       '--cmdline', 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr']
            (work / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
            started, timeout = time.monotonic(), False
            env = dict(os.environ)
            env.pop('NATEMU_PERF_LOG', None)
            with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
                try: process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    timeout = True
                    stop_group(process)
                except BaseException:
                    stop_group(process)
                    raise
            serial = work / 'serial.log'
            text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
            errors = judge(text, n)
            if timeout or process.returncode != 0: errors.append(f'host rc={process.returncode} timeout={timeout}')
            if 'natemu: Power(1)' not in (work / 'stderr.log').read_text(errors='replace'): errors.append('normal S5 poweroff missing')
            result = {'vcpus': n, 'seconds': time.monotonic() - started, 'returncode': process.returncode, 'timeout': timeout, 'errors': errors, 'pass': not errors}
            (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            results.append(result)
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps(result), flush=True)
            if errors: break
    return int(len(results) != len(args.cpus) or any(not r['pass'] for r in results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('kernel', 'installer', 'busybox-deb', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    runner = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out', 'lock'): runner.add_argument('--' + name, type=Path, required=True)
    runner.add_argument('--cpus', nargs='+', type=int, choices=(1, 2, 4), default=[1, 2])
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': return prepare(args)
    if args.command == 'self-check': return self_check()
    if len(args.cpus) != len(set(args.cpus)): parser.error('duplicate CPU cases')
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
