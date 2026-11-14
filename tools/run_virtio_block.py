#!/usr/bin/env python3
"""Stock Linux installed ext4-root and persistence checks through the normal CLI.

prepare uses unchanged Debian modules/BusyBox and native e2fsprogs, without mounting
the image on the host. run makes an APFS clone per CPU case, boots it read/write,
checks it offline, then launches a fresh read-only boot and checks persisted data.
No builds, retries or VMs are performed by prepare/self-check. Each VM gets 120s.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import time

from run_virtio_rng import KVER, KERNEL_SHA, ROOT, stop_group
from io_common import PCI_PROBE, digest, pci_errors, write_json

KERNEL_DEB_SHA = '7794643cf4560de2a7e3b069e8ca8511ad2a568d652ea285f0fe051f79bc9e99'
DISK_BYTES = 512 << 20
PAYLOAD_BYTES = 4 << 20
MODULE_NAMES = ('virtio_mmio', 'virtio_blk', 'virtio-rng', 'crc32c_generic', 'crc16', 'mbcache', 'jbd2', 'ext4')
APPLETS = ('sh', 'mount', 'umount', 'switch_root', 'nproc', 'uname', 'modprobe', 'cat', 'grep', 'awk',
           'basename', 'readlink', 'dd', 'wc', 'cmp', 'sha256sum', 'dmesg', 'sync', 'poweroff', 'sleep',
           'mkdir', 'cp', 'mv', 'rm', 'stty', 'taskset', 'chmod', 'touch')
EXEC_ROUNDS, EXEC_WAVES, EXEC_PAGES = 8, 3, 64

BOOT_INIT = r'''#!/bin/sh
export PATH=/bin
fail() { echo "VIRTIO-BLOCK-FAIL bootstrap-$*"; dmesg; poweroff -f; while :; do sleep 1; done; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
phase= rng=0 rootdev=
for item in $(cat /proc/cmdline); do
  case "$item" in io_phase=*) phase=${item#*=};; io_rng=*) rng=${item#*=};; root=*) rootdev=${item#*=};; esac
done
case "$phase" in write) mode=rw;; verify) mode=ro;; *) fail phase;; esac
test "$rootdev" = /dev/vda || fail root-device
for module in virtio_mmio virtio_blk crc32c_generic ext4; do modprobe "$module" || fail "module-$module"; done
if test "$rng" = 1; then modprobe virtio_rng || fail module-rng; fi
test -b "$rootdev" || fail no-vda
mount -t ext4 -o "$mode" "$rootdev" /newroot || fail mount-root
test -x /newroot/sbin/init || fail no-installed-init
for fs in proc sys dev; do mount --move "/$fs" "/newroot/$fs" || fail "move-$fs"; done
exec switch_root /newroot /sbin/init
fail switch-root-returned
'''

ROOT_INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain_console() { console_mode=$(stty -F /dev/console -g) && stty -F /dev/console "$console_mode"; }
fail() { echo "VIRTIO-BLOCK-FAIL $*"; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
exec </dev/console >/dev/console 2>&1
mount -t tmpfs tmpfs /tmp || fail tmpfs
@PCI_PROBE@phase= rng=0 exec_mode=0 transport=mmio
for item in $(cat /proc/cmdline); do case "$item" in io_phase=*) phase=${item#*=};; io_rng=*) rng=${item#*=};; io_exec=*) exec_mode=${item#*=};; io_transport=*) transport=${item#*=};; esac; done
echo "VIRTIO-BLOCK-BEGIN phase=$phase cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '6.12.107+deb13-amd64' || fail kernel
case "$phase" in write) mode=rw;; verify) mode=ro;; *) fail phase;; esac
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 $mode,"*) ;; *) fail "root=$rootinfo";; esac
echo "VIRTIO-BLOCK-ROOT device=/dev/vda fs=ext4 mode=$mode"
test "$(cat /sys/block/vda/size)" = 1048576 || fail capacity
test "$(cat /sys/block/vda/queue/logical_block_size)" = 512 || fail sector
test "$(basename "$(readlink -f /sys/block/vda/device/driver)")" = virtio_blk || fail driver
test "$(cat /sys/block/vda/device/device)" = 0x0002 || fail device-id
echo VIRTIO-BLOCK-DEVICE driver=virtio_blk sectors=1048576 logical=512
if test "$transport" = mmio; then
acpi=
for d in /sys/bus/acpi/devices/LNRO0005:*; do
  test -e "$d/uid" || continue
  test "$(cat "$d/uid")" = 0 || continue
  test "$(cat "$d/hid")" = LNRO0005 || fail acpi-hid
  test -z "$acpi" || fail multiple-block-resources
  acpi=$d
done
test -n "$acpi" || fail acpi-block
grep -E 'f0000000-f0000fff' /proc/iomem || fail mmio-resource
echo VIRTIO-BLOCK-ACPI hid=LNRO0005 uid=0 base=f0000000 end=f0000fff
fi
sha256sum -c /fixtures/source.sha256 || fail source-hash
if test "$phase" = write; then
  test ! -e /persist/receipt || fail reused-write-image
  mkdir -p /persist/tree || fail mkdir
  dd if=/fixtures/payload.bin of=/persist/tree/staged.bin bs=4096 conv=fsync 2>/tmp/dd.log || fail write
  dd if=/fixtures/patch.bin of=/persist/tree/staged.bin bs=4096 seek=123 conv=notrunc,fsync 2>>/tmp/dd.log || fail overwrite
  mv /persist/tree/staged.bin /persist/final.bin || fail rename
  cp /persist/final.bin /persist/tree/disposable.bin || fail copy
  cmp /persist/final.bin /persist/tree/disposable.bin || fail compare
  rm /persist/tree/disposable.bin || fail unlink
  printf 'persisted-v1\n' > /persist/receipt || fail receipt
  sync || fail sync
fi
sha256sum -c /fixtures/expected.sha256 || fail persisted-hash
test "$(wc -c < /persist/final.bin)" -eq 4194304 || fail short-file
test "$(cat /persist/receipt)" = persisted-v1 || fail receipt-readback
test ! -e /persist/tree/disposable.bin || fail deleted-file-returned
hash=$(sha256sum /persist/final.bin); hash=${hash%% *}
echo "VIRTIO-BLOCK-DATA phase=$phase bytes=4194304 sha256=$hash"
if test "$rng" = 1; then
  dd if=/dev/hwrng of=/tmp/entropy bs=1024 count=1 iflag=fullblock 2>/tmp/rng.log || fail rng-read
  test "$(wc -c < /tmp/entropy)" -eq 1024 || fail rng-short
  dd if=/dev/zero of=/tmp/zero bs=1024 count=1 2>/dev/null || fail zero
  if cmp -s /tmp/entropy /tmp/zero; then fail rng-zero; fi
  hash=$(sha256sum /tmp/entropy); hash=${hash%% *}
  echo "VIRTIO-BLOCK-RNG bytes=1024 sha256=$hash"
fi
# io_exec=1: run disk-loaded code while every device is busy. Programs on the virtio disk run on
# every CPU at once after the page cache is dropped (so their code pages come back through
# completions into recycled frames), and are overwritten in place by the other program between
# rounds; meanwhile two CPU burners, an RNG reader and a block copy/verify loop keep running.
run_wave() {
  want=$1 pids= c=0
  echo 3 > /proc/sys/vm/drop_caches || fail drop-caches
  while test $c -lt $cpus; do taskset -c $c /persist/prog > /tmp/exec.$c & pids="$pids $!"; c=$((c+1)); done
  wait $pids || fail exec-status
  c=0
  while test $c -lt $cpus; do test "$(cat /tmp/exec.$c)" = "$want" || fail "exec-output-cpu$c-$(cat /tmp/exec.$c)"; c=$((c+1)); done
}
if test "$exec_mode" = 1; then
  cpus=$(nproc)
  if test "$phase" = write; then
    burn() { while test ! -e /tmp/stop; do :; done; }
    burn & loaders=$!
    burn & loaders="$loaders $!"
    ( n=0; while test ! -e /tmp/stop; do
        dd if=/dev/hwrng of=/tmp/rng.bin bs=4096 count=4 iflag=fullblock 2>/dev/null || { echo read > /tmp/rng.fail; break; }
        test "$(wc -c < /tmp/rng.bin)" -eq 16384 || { echo short > /tmp/rng.fail; break; }
        n=$((n+1)); echo $n > /tmp/rng.count; done ) & loaders="$loaders $!"
    ( n=0; while test ! -e /tmp/stop; do
        cp /fixtures/payload.bin /persist/load.bin && sync && echo 3 > /proc/sys/vm/drop_caches &&
          sha256sum -c /fixtures/load.sha256 > /dev/null || { echo copy > /tmp/block.fail; break; }
        n=$((n+1)); echo $n > /tmp/block.count; done; rm -f /persist/load.bin ) & loaders="$loaders $!"
    r=0
    while test $r -lt 8; do
      case $((r % 2)) in 0) v=a want=EXEC-A-OK;; *) v=b want=EXEC-B-OK;; esac
      cp /fixtures/prog-$v /persist/prog || fail exec-copy
      chmod 755 /persist/prog && sync || fail exec-sync
      k=0
      while test $k -lt 3; do run_wave $want; k=$((k+1)); done
      r=$((r+1))
    done
    touch /tmp/stop; wait $loaders
    test ! -e /tmp/rng.fail || fail "exec-load-rng-$(cat /tmp/rng.fail)"
    test ! -e /tmp/block.fail || fail "exec-load-block-$(cat /tmp/block.fail)"
    sync
    echo "VIRTIO-BLOCK-EXEC phase=write rounds=$r waves=3 cpus=$cpus last=$want rng=$(cat /tmp/rng.count) block=$(cat /tmp/block.count)"
  else
    run_wave EXEC-B-OK
    echo "VIRTIO-BLOCK-EXEC phase=verify cpus=$cpus last=EXEC-B-OK"
  fi
fi
if test "$transport" = pci; then pci_probe; fi
echo VIRTIO-BLOCK-INTERRUPTS-BEGIN
cat /proc/interrupts
echo VIRTIO-BLOCK-INTERRUPTS-END
sync
mount -o remount,ro / || fail remount-ro
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 ro,"*) ;; *) fail "shutdown-root=$rootinfo";; esac
echo VIRTIO-BLOCK-ROOT-READONLY
echo VIRTIO-BLOCK-DMESG-BEGIN
dmesg
echo VIRTIO-BLOCK-DMESG-END
echo "VIRTIO-BLOCK-PASS phase=$phase"
drain_console || fail console-drain
poweroff -f
fail poweroff-returned
'''.replace('@PCI_PROBE@', PCI_PROBE.replace('@P@', 'VIRTIO-BLOCK'))


def exec_program(variant):
    """A static x86-64 ELF of EXEC_PAGES code pages: each page adds its own constant to RBX and
    jumps to the next page; the last compares the sum and writes EXEC-<V>-OK (or -BAD) to stdout.
    A stale page of the other variant (or of any old frame) changes the sum."""
    sys.path.insert(0, str(ROOT / 'tools/reference'))
    import ref
    values = [int.from_bytes(hashlib.sha256(f'natemu-exec-{variant}-{i}'.encode()).digest()[:4], 'little') & 0x7fffffff for i in range(EXEC_PAGES)]
    name = variant.upper()
    asm = ['xor ebx, ebx']
    for i, value in enumerate(values):
        asm += [f'page{i}:', f'add rbx, {value:#x}']
        if i + 1 < EXEC_PAGES: asm += [f'jmp page{i + 1}', '.p2align 12, 0xcc']
    asm += [f'mov rax, {sum(values):#x}', 'cmp rbx, rax', 'jne bad', 'lea rsi, [rip + good]', 'mov edx, 10', 'jmp out',
            'bad:', 'lea rsi, [rip + wrong]', 'mov edx, 11', 'out:', 'mov eax, 1', 'mov edi, 1', 'syscall',
            'mov eax, 60', 'xor edi, edi', 'syscall', f'good: .ascii "EXEC-{name}-OK\\n"', f'wrong: .ascii "EXEC-{name}-BAD\\n"']
    code = ref.assemble('\n'.join(asm))
    base, offset = 0x400000, 0x1000
    header = struct.pack('<4sBBBBB7xHHIQQQIHHHHHH', b'\x7fELF', 2, 1, 1, 0, 0, 2, 0x3e, 1, base + offset, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack('<IIQQQQQQ', 1, 5, 0, base, base, offset + len(code), offset + len(code), 0x1000)
    return (header + segment).ljust(offset, b'\0') + code


def checked(command, log, env=None):
    started = time.monotonic()
    command = [str(x) for x in command]
    code, timed_out = None, False
    with log.open('xb') as output:
        try:
            code = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, env=env, timeout=120).returncode
        except subprocess.TimeoutExpired:
            # subprocess.run has killed and reaped its child. Preserve the native-tool failure
            # and convert it to the same checked failure the post-VM fsck path records below.
            timed_out = True
            output.write(b'\nNative tool exceeded its 120-second deadline.\n')
    write_json(log.with_suffix('.json'), {'command': command, 'exit': code, 'timeout': timed_out,
                                         'limit_seconds': 120, 'seconds': time.monotonic() - started})
    if timed_out or code: raise RuntimeError(f'{command[0]} {"timed out" if timed_out else "failed"}: {log}')


def install_busybox(root, binary):
    for name in ('bin', 'sbin', 'dev', 'proc', 'sys', 'tmp'):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binary, root / 'bin/busybox')
    (root / 'bin/busybox').chmod(0o755)
    for name in APPLETS: (root / 'bin' / name).symlink_to('busybox')


def prepare(args):
    args.out.mkdir(parents=True, exist_ok=False)
    rng = json.loads((args.rng_inputs / 'inputs.json').read_text())
    for name in ('linux', 'rootfs/bin/busybox'):
        assert digest(args.rng_inputs / name) == rng['outputs'][name], name
    assert digest(args.kernel_deb) == KERNEL_DEB_SHA, 'kernel package pin mismatch'
    assert digest(args.rng_inputs / 'linux') == KERNEL_SHA
    shutil.copyfile(args.rng_inputs / 'linux', args.out / 'linux')
    root, boot = args.out / 'rootfs', args.out / 'bootstrap'
    for tree in (root, boot): install_busybox(tree, args.rng_inputs / 'rootfs/bin/busybox')
    (boot / 'newroot').mkdir()
    (boot / 'init').write_text(BOOT_INIT); (boot / 'init').chmod(0o755)
    (root / 'sbin/init').write_text(ROOT_INIT); (root / 'sbin/init').chmod(0o755)
    for name in ('fixtures', 'persist'): (root / name).mkdir()
    payload = b''.join(hashlib.sha256(b'natemu-block-file-v1' + i.to_bytes(8, 'little')).digest() for i in range(PAYLOAD_BYTES // 32))
    patch = b''.join(hashlib.sha256(b'natemu-block-patch-v1' + i.to_bytes(8, 'little')).digest() for i in range(8192 // 32))
    expected = payload[:123 * 4096] + patch + payload[125 * 4096:]
    source_sha, expected_sha = hashlib.sha256(payload).hexdigest(), hashlib.sha256(expected).hexdigest()
    (root / 'fixtures/payload.bin').write_bytes(payload)
    (root / 'fixtures/patch.bin').write_bytes(patch)
    (root / 'fixtures/source.sha256').write_text(source_sha + '  /fixtures/payload.bin\n')
    (root / 'fixtures/expected.sha256').write_text(expected_sha + '  /persist/final.bin\n')
    (root / 'fixtures/load.sha256').write_text(source_sha + '  /persist/load.bin\n')
    for variant in 'ab':
        (root / f'fixtures/prog-{variant}').write_bytes(exec_program(variant)); (root / f'fixtures/prog-{variant}').chmod(0o755)
    archive = subprocess.check_output(['ar', 'p', str(args.kernel_deb), 'data.tar.xz'], timeout=30)
    modules = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        for member in package:
            basename = Path(member.name).name.removesuffix('.ko.xz')
            if basename not in MODULE_NAMES or not member.name.endswith('.ko.xz'): continue
            rel = member.name.split('/' + KVER + '/', 1)[1].removesuffix('.xz')
            compressed = package.extractfile(member).read(); data = lzma.decompress(compressed)
            assert ('vermagic=' + KVER).encode() in data
            dependencies = re.search(rb'depends=([^\0]*)', data).group(1).decode()
            dest = boot / 'lib/modules' / KVER / rel
            dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(data)
            modules[basename.replace('-', '_')] = {'path': rel, 'sha256': digest(dest), 'depends': dependencies.split(',') if dependencies else []}
    assert len(modules) == len(MODULE_NAMES), 'missing stock kernel module'
    deps = []
    for m in modules.values(): deps.append(m['path'] + ': ' + ' '.join(modules[d]['path'] for d in m['depends']) + '\n')
    (boot / 'lib/modules' / KVER / 'modules.dep').write_text(''.join(deps))
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec); spec.loader.exec_module(mkcpio)
    mkcpio.main(str(boot), str(args.out / 'bootstrap.cpio'))
    image = args.out / 'root.raw'
    with image.open('xb') as f: f.truncate(DISK_BYTES)
    config = args.e2fsprogs.parent.parent / 'etc/mke2fs.conf'
    if not config.exists(): config = Path('/opt/homebrew/etc/mke2fs.conf')
    shutil.copyfile(config, args.out / 'mke2fs.conf')
    tools = {name: args.e2fsprogs / 'sbin' / name for name in ('mke2fs', 'e2fsck', 'debugfs')}
    env = dict(os.environ, MKE2FS_CONFIG=str((args.out / 'mke2fs.conf').resolve()))
    checked([tools['mke2fs'], '-F', '-t', 'ext4', '-b', '4096', '-L', 'NATEMUROOT', '-U', '673af719-2222-4444-8888-12453af21736',
             '-E', 'root_owner=0:0', '-d', root, image], args.out / 'mke2fs.log', env)
    # Files originate in an unprivileged host directory; install normal root ownership using the
    # standard image editor, without mounting or inventing an ext4 writer.
    edits = ''.join(f'set_inode_field /{p.relative_to(root)} {field} 0\n'
                    for p in sorted(root.rglob('*')) for field in ('uid', 'gid'))
    (args.out / 'ownership.debugfs').write_text(edits)
    checked([tools['debugfs'], '-w', '-f', args.out / 'ownership.debugfs', image], args.out / 'ownership.log')
    checked([tools['e2fsck'], '-fn', image], args.out / 'initial-fsck.log')
    entries = {}
    for p in sorted(root.rglob('*')):
        mode = p.lstat().st_mode
        entries[str(p.relative_to(root))] = {'mode': oct(mode), 'installed_uid': 0, 'installed_gid': 0,
                                           **({'link': os.readlink(p)} if p.is_symlink() else {'sha256': digest(p)} if stat.S_ISREG(mode) else {})}
    manifest = {'purpose': 'installed ext4-root development and persistence check; not a performance claim',
                'disk_bytes': DISK_BYTES, 'payload_bytes': PAYLOAD_BYTES, 'source_sha256': source_sha, 'persisted_sha256': expected_sha,
                'kernel_package': {'path': str(args.kernel_deb.resolve()), 'sha256': KERNEL_DEB_SHA}, 'rng_inputs': rng,
                'modules': modules, 'rootfs': entries, 'producer': {str(p.resolve()): digest(p) for p in (Path(__file__), ROOT / 'tools/play/mkcpio.py')},
                'tools': {name: {'path': str(path.resolve()), 'sha256': digest(path)} for name, path in tools.items()},
                'outputs': {name: digest(args.out / name) for name in ('linux', 'bootstrap.cpio', 'root.raw', 'rootfs/sbin/init', 'bootstrap/init', 'mke2fs.conf')}}
    write_json(args.out / 'inputs.json', manifest)
    print(json.dumps({k: manifest[k] for k in ('disk_bytes', 'source_sha256', 'persisted_sha256', 'outputs')}, indent=2))


def judge(text, n, phase, expected_sha, rng, exec_mode=False, pci=False):
    errors = []
    def require(ok, message):
        if not ok: errors.append(message)
    require(re.findall(r'^VIRTIO-BLOCK-BEGIN phase=(\S+) cpus=(\d+) kernel=(\S+)$', text, re.M) == [(phase, str(n), KVER)], 'wrong/missing phase, CPUs or kernel')
    mode = 'rw' if phase == 'write' else 'ro'
    require(re.findall(r'^VIRTIO-BLOCK-ROOT device=/dev/vda fs=ext4 mode=(\S+)$', text, re.M) == [mode], 'not booted from installed ext4 root')
    require(re.findall(r'^VIRTIO-BLOCK-DEVICE driver=virtio_blk sectors=1048576 logical=512$', text, re.M), 'block identity/capacity missing')
    if not pci: require(re.findall(r'^VIRTIO-BLOCK-ACPI hid=LNRO0005 uid=0 base=f0000000 end=f0000fff$', text, re.M), 'ACPI block discovery missing')
    require(re.findall(r'^VIRTIO-BLOCK-DATA phase=(\S+) bytes=(\d+) sha256=([a-f0-9]{64})$', text, re.M) == [(phase, str(PAYLOAD_BYTES), expected_sha)], 'persisted file differs or short')
    for marker in ('INTERRUPTS-BEGIN', 'INTERRUPTS-END', 'ROOT-READONLY', 'DMESG-BEGIN', 'DMESG-END'):
        require(len(re.findall(r'^VIRTIO-BLOCK-' + marker + '$', text, re.M)) == 1, 'missing/duplicate ' + marker)
    require(re.findall(r'^VIRTIO-BLOCK-PASS phase=(\S+)$', text, re.M) == [phase], 'guest did not finish')
    interrupts = re.findall(r'^\s*\d+:\s+((?:\d+\s+){' + str(n) + r'}).*\b16-fasteoi\s+virtio\d+\s*$', text, re.M)
    if pci: errors += pci_errors(text, 'VIRTIO-BLOCK', n, ('block',) + (('rng',) if rng else ()))
    else: require(len(interrupts) == 1 and sum(map(int, interrupts[0].split())) > 0, 'no delivered block interrupt on GSI 16')
    if rng:
        hashes = re.findall(r'^VIRTIO-BLOCK-RNG bytes=1024 sha256=([a-f0-9]{64})$', text, re.M)
        require(len(hashes) == 1 and hashes[0] != hashlib.sha256(bytes(1024)).hexdigest(), 'combined RNG check failed')
    if exec_mode:
        if phase == 'write':
            found = re.findall(r'^VIRTIO-BLOCK-EXEC phase=write rounds=(\d+) waves=(\d+) cpus=(\d+) last=(\S+) rng=(\d+) block=(\d+)$', text, re.M)
            require(len(found) == 1 and found[0][:4] == (str(EXEC_ROUNDS), str(EXEC_WAVES), str(n), 'EXEC-B-OK')
                    and int(found[0][4]) > 0 and int(found[0][5]) > 0, 'disk-loaded code, RNG or block load check failed')
        else:
            require(re.findall(r'^VIRTIO-BLOCK-EXEC phase=verify cpus=(\d+) last=(\S+)$', text, re.M) == [(str(n), 'EXEC-B-OK')],
                    'persisted program did not run as the last written one')
    require(not re.search(r'VIRTIO-BLOCK-FAIL|BUG:|Oops:|Kernel panic|Call Trace:|EXT4-fs error|Buffer I/O error', text), 'guest/kernel/filesystem error')
    return errors


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    inputs = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in inputs['outputs'].items(): assert digest(args.inputs / name) == sha, name
    fsck = Path(inputs['tools']['e2fsck']['path'])
    assert digest(fsck) == inputs['tools']['e2fsck']['sha256'], 'e2fsck binary changed'
    write_json(args.out / 'manifest.json', {'inputs': inputs, 'cpus': args.cpus, 'rng': args.rng, 'transport': args.transport, 'seconds_per_vm': 120,
               'artifacts': {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))}, 'source_root': str(ROOT)})
    pci = args.transport == 'pci'
    if pci and 'pci_probe' not in (args.inputs / 'rootfs/sbin/init').read_text(): raise SystemExit('--transport pci needs inputs prepared by this version')
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for n in args.cpus:
            work = args.out / f'n{n}'; work.mkdir()
            image = work / 'root.raw'
            checked(['cp', '-c', args.inputs / 'root.raw', image], work / 'clone.log')
            baseline = digest(image)
            assert baseline == inputs['outputs']['root.raw']
            for phase in ('write', 'verify'):
                case = work / phase; case.mkdir(); before = digest(image)
                command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
                           '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / 'bootstrap.cpio').resolve()),
                           '--disk', str(image.resolve()), '--vcpus', str(n), '--ram', '2G', '--compile-cache', 'off',
                           '--serial', str((case / 'serial.log').resolve()), '--cmdline',
                           f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr root=/dev/vda io_phase={phase} io_rng={int(args.rng)}'
                           + (' io_exec=1' if args.exec else '') + (' io_transport=pci' if pci else '')]
                if pci: command += ['--virtio-transport', 'pci']
                if args.rng: command += ['--rng']
                if args.exec: command += ['--perf-log', str((case / 'perf.log').resolve())]
                write_json(case / 'command.json', command)
                started, timeout = time.monotonic(), False
                env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
                with (case / 'stdout.log').open('wb') as stdout, (case / 'stderr.log').open('wb') as stderr:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
                    try: process.wait(timeout=120)
                    except subprocess.TimeoutExpired: timeout = True; stop_group(process)
                    except BaseException: stop_group(process); raise
                serial = case / 'serial.log'
                text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
                errors = judge(text, n, phase, inputs['persisted_sha256'], args.rng, args.exec, pci)
                io_counts = None
                if args.exec:
                    perf = case / 'perf.log'
                    lines = [l for l in perf.read_text(errors='replace').splitlines() if l.startswith('io ')] if perf.exists() else []
                    if len(lines) == 1: io_counts = json.loads(lines[0][3:])
                    else: errors.append('missing io perf line')
                after = digest(image)
                if timeout or process.returncode != 0: errors.append(f'host rc={process.returncode} timeout={timeout}')
                if 'natemu: Power(1)' not in (case / 'stderr.log').read_text(errors='replace'): errors.append('normal poweroff missing')
                if phase == 'write' and after == baseline: errors.append('disk did not change after writes')
                if phase == 'verify' and after != before: errors.append('read-only relaunch changed disk bytes')
                try: checked([fsck, '-fn', image], case / 'fsck.log')
                except RuntimeError as error: errors.append(f'offline e2fsck: {error}')
                result = {'vcpus': n, 'phase': phase, 'seconds': time.monotonic() - started, 'returncode': process.returncode,
                          'timeout': timeout, 'disk_before': before, 'disk_after': after, 'io_counts': io_counts, 'errors': errors, 'pass': not errors}
                write_json(case / 'result.json', result); results.append(result); write_json(args.out / 'results.json', results)
                print(json.dumps(result), flush=True)
                if errors: return 1
    return 0


def self_check():
    for phase, mode in (('write', 'rw'), ('verify', 'ro')):
        sha = 'a' * 64
        text = f'VIRTIO-BLOCK-BEGIN phase={phase} cpus=2 kernel={KVER}\nVIRTIO-BLOCK-ROOT device=/dev/vda fs=ext4 mode={mode}\n'
        text += 'VIRTIO-BLOCK-DEVICE driver=virtio_blk sectors=1048576 logical=512\nVIRTIO-BLOCK-ACPI hid=LNRO0005 uid=0 base=f0000000 end=f0000fff\n'
        text += f'VIRTIO-BLOCK-DATA phase={phase} bytes={PAYLOAD_BYTES} sha256={sha}\n'
        text += 'VIRTIO-BLOCK-RNG bytes=1024 sha256=' + 'b' * 64 + '\n'
        for marker in ('INTERRUPTS-BEGIN', 'INTERRUPTS-END', 'ROOT-READONLY', 'DMESG-BEGIN', 'DMESG-END'):
            text += 'VIRTIO-BLOCK-' + marker + '\n'
            if marker == 'INTERRUPTS-BEGIN': text += ' 16: 20 0 IO-APIC 16-fasteoi virtio0\n'
        text += f'VIRTIO-BLOCK-PASS phase={phase}\n'
        assert not judge(text, 2, phase, sha, True)
        for old, new in [('cpus=2', 'cpus=1'), ('fs=ext4', 'fs=tmpfs'), ('bytes=4194304', 'bytes=64'),
                         (sha, 'c' * 64), ('16: 20 0', '16: 0 0'), ('ROOT-READONLY', 'missing'), ('PASS phase=', 'missing phase=')]:
            assert judge(text.replace(old, new), 2, phase, sha, True), old
        assert judge(text + 'EXT4-fs error synthetic\n', 2, phase, sha, True)
        line = (f'VIRTIO-BLOCK-EXEC phase=write rounds={EXEC_ROUNDS} waves={EXEC_WAVES} cpus=2 last=EXEC-B-OK rng=3 block=2\n' if phase == 'write'
                else 'VIRTIO-BLOCK-EXEC phase=verify cpus=2 last=EXEC-B-OK\n')
        assert judge(text, 2, phase, sha, True, True) and not judge(text + line, 2, phase, sha, True, True)
        bad = [('last=EXEC-B-OK', 'last=EXEC-A-OK'), ('cpus=2 last', 'cpus=1 last')]
        if phase == 'write': bad += [('rng=3', 'rng=0'), ('block=2', 'block=0'), (f'rounds={EXEC_ROUNDS}', 'rounds=1')]
        for old, new in bad: assert judge(text + line.replace(old, new), 2, phase, sha, True, True), old
        assert judge(text + line + 'VIRTIO-BLOCK-FAIL exec-output-cpu1-EXEC-A-BAD\n', 2, phase, sha, True, True)
    pci_text = ('VIRTIO-BLOCK-PCI-BEGIN\nVIRTIO-BLOCK-PCI-DEV 0000:00:00.0 0x8086 0x1237 0x060000\nVIRTIO-BLOCK-PCI-DEV 0000:00:01.0 0x1af4 0x1042 0x010000\n'
                'VIRTIO-BLOCK-PCI-DEV 0000:00:03.0 0x1af4 0x1044 0x00ff00\n'
                'VIRTIO-BLOCK-PCI-IRQ  27:          0          0  PCI-MSIX-0000:00:01.0   0-edge      virtio0-config\n'
                'VIRTIO-BLOCK-PCI-IRQ  28:          0        161  PCI-MSIX-0000:00:01.0   1-edge      virtio0-req.0\n'
                'VIRTIO-BLOCK-PCI-IRQ  30:          9          0  PCI-MSIX-0000:00:03.0   1-edge      virtio2-input\nVIRTIO-BLOCK-PCI-END\n')
    base = text.replace(' 16: 20 0 IO-APIC 16-fasteoi virtio0\n', '').replace('VIRTIO-BLOCK-ACPI hid=LNRO0005 uid=0 base=f0000000 end=f0000fff\n', '')
    assert not judge(base + pci_text, 2, 'verify', sha, True, pci=True)
    for old, new in (('0x1042', '0x1041'), ('161', '0'), ('9          0  PCI', '0          0  PCI'), ('PCI-END', 'PCI-X')):
        assert judge(base + pci_text.replace(old, new), 2, 'verify', sha, True, pci=True), old
    assert judge(base + pci_text.replace('VIRTIO-BLOCK-PCI-END', 'VIRTIO-BLOCK-PCI-DMESG Cannot find an available gap\nVIRTIO-BLOCK-PCI-END'), 2, 'verify', sha, True, pci=True)
    for script in (BOOT_INIT, ROOT_INIT): subprocess.run(['sh', '-n'], input=script, text=True, check=True)
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as temporary:
        log = Path(temporary) / 'timeout.log'
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['fsck-control'], 120)):
            try: checked(['fsck-control'], log)
            except RuntimeError as error: assert 'timed out' in str(error)
            else: raise AssertionError('timeout did not become a recorded checked failure')
        outcome = json.loads(log.with_suffix('.json').read_text())
        assert outcome['command'] == ['fsck-control'] and outcome['timeout'] and outcome['exit'] is None
        assert outcome['limit_seconds'] == 120 and '120-second deadline' in log.read_text()
    for variant in 'ab':
        program = exec_program(variant)
        assert program[:4] == b'\x7fELF' and len(program) > EXEC_PAGES * 4096 - 4096 and f'EXEC-{variant.upper()}-OK'.encode() in program
    assert exec_program('a') != exec_program('b')
    print('Shell syntax, 16 negative parser controls, exec judge controls and native-tool timeout retention passed; no VM launched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('rng-inputs', 'kernel-deb', 'e2fsprogs', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    runner = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out', 'lock'): runner.add_argument('--' + name, type=Path, required=True)
    runner.add_argument('--cpus', nargs='+', type=int, choices=(1, 2, 4, 8, 16), default=[1])
    runner.add_argument('--rng', action='store_true')
    runner.add_argument('--transport', choices=('mmio', 'pci'), default='mmio', help='virtio transport; pci needs inputs from this version of prepare')
    runner.add_argument('--exec', action='store_true', help='also run disk-loaded programs on every CPU under RNG, block and CPU load (inputs from this prepare; needs --rng)')
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': return prepare(args)
    if args.command == 'self-check': return self_check()
    if len(args.cpus) != len(set(args.cpus)): parser.error('duplicate CPU cases')
    if args.exec and not args.rng: parser.error('--exec needs --rng')
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
