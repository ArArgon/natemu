#!/usr/bin/env python3
"""Stock-Linux block coverage through the normal CLI: partitioned disks, guest reboot, a full guest filesystem and
host-side write failures. Correctness only (no timing claims); each VM gets 120 s; the VM lock is held shared.

    python3 tools/run_io_coverage.py --fexc FEXC --out DIR [--cases C ...] [--inputs BLOCK_INPUTS] [--transport mmio|pci]
    python3 tools/run_io_coverage.py self-check

The kernel, the stock Debian modules (virtio_mmio, virtio_blk, ext4 and its dependencies) and BusyBox come from a
tools/run_virtio_block.py prepare directory (--inputs, default the November 8 IO3 inputs); its /init is replaced by
INIT below, which runs the case from the initramfs and uses the disk as a data disk. Images are sparse, built here with
native e2fsprogs (mke2fs -E offset=, e2fsck 'IMAGE?offset=') and a Python GPT/MBR writer; each case gets an APFS clone.
Every case is a write launch then a fresh verify launch of the same image, and each launch is judged on the guest's
markers and on the host: exit status and stop, image changed or unchanged, offline fsck of every ext4, and where
stated the exact image bytes.

Cases (the guest kernel has CONFIG_EFI_PARTITION=y and CONFIG_MSDOS_PARTITION=y built in):
  gpt, mbr       three partitions (ext4, ext4, swap): the kernel's partition scan, both file systems mounted and
                 written at once, mkswap/swapon/swapoff, partition table bytes untouched, read-only relaunch.
  reboot-acpi    `reboot -f` with the ext4 mounted after sync (Linux's default reboot writes the FADT reset register).
                 natemu: the run ends with the stop `Power(2)` (exit 2) after the same disk drain and final full sync
                 as S5 (crates/host/src/pm.rs; REQUEST_POWER_POLL in crates/host/src/execution.rs).
  reboot-kbd     the same with reboot=k: the keyboard controller's reset pulse (FEH to port 64H; there is no 8042,
                 but that write is decoded, crates/host/src/pm.rs) ends the run the same way: Power(2), drain, sync.
  reboot-triple  the same with reboot=t: Linux loads an empty IDT and faults: the resident stop SHUTDOWN (16), exit 2.
                 A guest reboot ends the run: natemu does not reboot (docs/restructure/io-plan.md); the defined
                 behaviour checked here is the typed stop, no hang, and a relaunch that replays the journal and finds
                 the synced file, fsck clean afterwards.
  fs-full        ext4 filled to ENOSPC by dd: dd fails with ENOSPC, a further small write fails, sync succeeds, the
                 file system has no error, space returns after rm; fsck clean; relaunch reads the data.
  host-full      the image lives on a 32 MiB APFS disk image (hdiutil, no sudo): p1 ext4 is written first, then
                 O_DIRECT writes to the raw p2 exhaust the host volume (pwrite ENOSPC): the guest's dd gets EIO, the
                 kernel logs `I/O error, dev vda`, a FLUSH afterwards and later reads of p1 succeed, S5 succeeds and a
                 relaunch reads p1. (Had the full volume failed an fsync instead, every later FLUSH and S5 would fail,
                 correctly: a host sync failure is sticky, crates/host/src/virtio/block.rs. APFS reports ENOSPC at
                 pwrite, so they succeed.)
  host-limit     RLIMIT_FSIZE on the natemu process (SIGXFSZ ignored) makes every host pwrite ending past LIMIT fail
                 with EFBIG, deterministically: a 64 KiB O_DIRECT write (one 16-segment request) straddling LIMIT gets a
                 2048-byte short write then EFBIG (IOERR), two of four concurrent writers above LIMIT fail while the two
                 below succeed, a FLUSH afterwards and every later read succeed; the image is compared byte for byte
                 with its exact expected contents (the straddling request's landed 2048-byte prefix included: no
                 rollback is promised) and a relaunch without the limit reads it back.
Live negative controls (the judge must FAIL them): host-full-control (the host-full guest with the image on the normal
disk) and host-limit-control (the host-limit guest without the limit)."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid
import zlib

from io_common import ROOT, VM_LOCK, digest, stop_group, write_json
from raw_root import RAW_ROOT
from io_common import PCI_PROBE, patch, pci_errors, pci_self_test
from run_virtio_block import checked
from run_virtio_rng import KVER

INPUTS = RAW_ROOT / 'io3-block-20261108-01/inputs-01'
MiB = 1 << 20
SECTOR = 512
LIMIT = 32 * MiB + 2048          # host-limit: RLIMIT_FSIZE
PATTERN_AT = 32 * MiB            # host-limit: 64 KiB of pattern from here
APPLETS = ('sh', 'mount', 'umount', 'modprobe', 'cat', 'grep', 'awk', 'dd', 'wc', 'sha256sum', 'dmesg', 'sync',
           'poweroff', 'reboot', 'sleep', 'mkdir', 'cp', 'rm', 'stty', 'nproc', 'uname', 'mkswap', 'swapon', 'swapoff',
           'df', 'tail', 'test', 'ls')
CASES = ('gpt', 'mbr', 'reboot-acpi', 'reboot-kbd', 'reboot-triple', 'fs-full', 'host-full', 'host-limit')
CONTROLS = ('host-full-control', 'host-limit-control')
# The image each case starts from, and its extra kernel parameters.
IMAGE = {'gpt': 'gpt.raw', 'mbr': 'mbr.raw', 'reboot-acpi': 'data.raw', 'reboot-kbd': 'data.raw', 'reboot-triple': 'data.raw',
         'fs-full': 'data.raw', 'host-full': 'hostfull.raw', 'host-limit': 'limit.raw'}
EXTRA = {'reboot-kbd': ' reboot=k', 'reboot-triple': ' reboot=t'}
# The write launch's expected host stop (stderr's last `natemu: ...` line) and exit status; every verify launch: S5.
POWEROFF = ('natemu: Power(1)', 0)
STOP = {'reboot-acpi': ('natemu: Power(2)', 2), 'reboot-kbd': ('natemu: Power(2)', 2),
        'reboot-triple': ('natemu: Resident { stop: 16, status: 1, detail: 16 }', 2)}
# natemu's stderr line for each failed host call (block.rs), by write launch; none anywhere else.
BACKEND_ERRORS = {'host-full': 'virtio-block: No space left on device (os error 28)', 'host-limit': 'virtio-block: File too large (os error 27)'}
PARTS = {  # (first sector, sectors, GPT name) for gpt/mbr
    'table': [(2048, 40960, 'natemu-one'), (43008, 40960, 'natemu-two'), (83968, 32768, 'natemu-swap')],
    'hostfull': [(2048, 32768, 'natemu-fs'), (34816, 225280, 'natemu-raw')]}
GPT_LINUX, GPT_SWAP = '0FC63DAF-8483-4772-8E79-3D69D8477DE4', '0657FD6D-A4AB-43C4-84E5-0933C84B4F4F'

INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain_console() { console_mode=$(stty -F /dev/console -g) && stty -F /dev/console "$console_mode"; }
fail() { echo "COV-FAIL $*"; dmesg | tail -n 60; drain_console; poweroff -f; while :; do sleep 1; done; }
mount -t proc proc /proc && mount -t sysfs sysfs /sys && mount -t devtmpfs devtmpfs /dev || poweroff -f
exec </dev/console >/dev/console 2>&1
mount -t tmpfs tmpfs /tmp || fail tmpfs
case= phase=
for item in $(cat /proc/cmdline); do case "$item" in cov_case=*) case=${item#*=};; cov_phase=*) phase=${item#*=};; esac; done
echo "COV-BEGIN case=$case phase=$phase cpus=$(nproc) kernel=$(uname -r)"
for module in virtio_mmio virtio_blk crc32c_generic ext4; do modprobe "$module" || fail "module-$module"; done
wait_dev() { n=0; while test ! -b "$1" && test $n -lt 100; do sleep 0.1; n=$((n+1)); done; test -b "$1" || fail "no-$1"; }
sha() { s=$(sha256sum "$1") || fail "hash-$1"; echo "${s%% *}"; }
# The hash of COUNT BLOCK-byte blocks of /dev/vda from SKIP (O_DIRECT reads: the device, not the page cache).
rsha() { s=$(dd if=/dev/vda bs=$1 skip=$2 count=$3 iflag=direct 2>/dev/null | sha256sum) || fail read; echo "${s%% *}"; }
wait_dev /dev/vda
parts() {
  p=1
  while test $p -le $1; do
    wait_dev /dev/vda$p
    name=$(grep '^PARTNAME=' /sys/block/vda/vda$p/uevent); name=${name#PARTNAME=}
    echo "COV-PART n=$p start=$(cat /sys/block/vda/vda$p/start) size=$(cat /sys/block/vda/vda$p/size) name=${name:-none}"
    p=$((p+1))
  done
  test ! -e /sys/block/vda/vda$p || fail extra-partition
}
ioerrs() { dmesg | grep -c 'I/O error, dev vda'; }
case "$case" in
gpt|mbr)
  parts 3
  if test "$phase" = write; then
    mount -t ext4 /dev/vda1 /m1 || fail mount-p1
    mount -t ext4 /dev/vda2 /m2 || fail mount-p2
    test ! -e /m1/a.bin || fail reused-image
    cp /fixtures/a.bin /m1/a.bin && cp /fixtures/b.bin /m2/b.bin || fail copy
    sync || fail sync
    mkswap /dev/vda3 > /dev/null || fail mkswap
    swapon /dev/vda3 || fail swapon
    grep -q '^/dev/vda3 ' /proc/swaps || fail swaps
    swapoff /dev/vda3 || fail swapoff
    umount /m1 && umount /m2 || fail umount
  fi
  mount -t ext4 -o ro /dev/vda1 /m1 && mount -t ext4 -o ro /dev/vda2 /m2 || fail mount-ro
  echo "COV-DATA p1=$(sha /m1/a.bin) p2=$(sha /m2/b.bin)"
  echo "COV-SWAP sig=$(dd if=/dev/vda3 bs=1 skip=4086 count=10 2>/dev/null)"
  umount /m1 && umount /m2 || fail umount-ro;;
reboot-*)
  if test "$phase" = write; then
    mount -t ext4 /dev/vda /m1 || fail mount
    cp /fixtures/a.bin /m1/a.bin && sync || fail write
    echo "COV-DATA a=$(sha /m1/a.bin)"
    echo COV-REBOOT
    drain_console
    reboot -f
    sleep 10
    fail reboot-returned
  fi
  # Read-write: the journal of the file system mounted at the reboot is replayed.
  mount -t ext4 /dev/vda /m1 || fail mount
  echo "COV-DATA a=$(sha /m1/a.bin)"
  umount /m1 || fail umount;;
fs-full)
  if test "$phase" = write; then
    mount -t ext4 /dev/vda /m1 || fail mount
    cp /fixtures/a.bin /m1/keep.bin && sync || fail keep
    dd if=/dev/zero of=/m1/fill bs=1M 2>/tmp/dd.err; rc=$?
    grep -q 'No space left on device' /tmp/dd.err && enospc=1 || enospc=0
    avail=$(df -k /m1 | awk 'NR == 2 {print $4}')
    if echo more > /m1/more 2>/dev/null; then more=ok; else more=enospc; fi
    sync && sync_rc=0 || sync_rc=$?
    rm -f /m1/fill /m1/more && sync || fail rm
    cp /fixtures/b.bin /m1/after.bin && sync || fail after
    echo "COV-FULL dd_rc=$rc enospc=$enospc avail=$avail more=$more sync_rc=$sync_rc"
    umount /m1 || fail umount
  fi
  mount -t ext4 -o ro /dev/vda /m1 || fail mount-ro
  test ! -e /m1/fill || fail fill-returned
  echo "COV-DATA keep=$(sha /m1/keep.bin) after=$(sha /m1/after.bin)"
  umount /m1 || fail umount-ro;;
host-full)
  parts 2
  if test "$phase" = write; then
    mount -t ext4 /dev/vda1 /m1 || fail mount
    cp /fixtures/a.bin /m1/a.bin && sync || fail write
    umount /m1 || fail umount
    # 104 MiB of O_DIRECT writes to the raw partition: more than the host volume has left.
    dd if=/dev/zero of=/dev/vda2 bs=64k count=1664 oflag=direct conv=notrunc 2>/tmp/dd.err; rc=$?
    grep -q 'Input/output error' /tmp/dd.err && eio=1 || eio=0
    # A FLUSH after the failed write (blkdev fsync), then later reads.
    dd if=/dev/null of=/dev/vda2 conv=notrunc,fsync 2>/dev/null; flush_rc=$?
    echo "COV-HOSTFULL dd_rc=$rc eio=$eio flush_rc=$flush_rc ioerr_logs=$(ioerrs)"
  fi
  mount -t ext4 -o ro /dev/vda1 /m1 || fail mount-ro
  echo "COV-DATA a=$(sha /m1/a.bin)"
  umount /m1 || fail umount-ro;;
host-limit)
  if test "$phase" = write; then
    dd if=/fixtures/a.bin of=/dev/vda bs=64k oflag=direct conv=notrunc 2>/dev/null || fail below-write
    # One 64 KiB request from 32 MiB: the host limit is 2048 bytes into it.
    if dd if=/fixtures/b.bin of=/dev/vda bs=64k count=1 seek=512 oflag=direct conv=notrunc 2>/tmp/s.err; then straddle=ok; else straddle=fail; fi
    grep -q 'Input/output error' /tmp/s.err && seio=1 || seio=0
    # Four concurrent writers, two below the limit (2 and 3 MiB) and two above (37.5 and 43.75 MiB).
    dd if=/fixtures/b.bin of=/dev/vda bs=64k seek=32 oflag=direct conv=notrunc 2>/dev/null & w1=$!
    dd if=/fixtures/b.bin of=/dev/vda bs=64k seek=600 oflag=direct conv=notrunc 2>/dev/null & w2=$!
    dd if=/fixtures/b.bin of=/dev/vda bs=64k seek=48 oflag=direct conv=notrunc 2>/dev/null & w3=$!
    dd if=/fixtures/b.bin of=/dev/vda bs=64k seek=700 oflag=direct conv=notrunc 2>/dev/null & w4=$!
    wait $w1; r1=$?; wait $w2; r2=$?; wait $w3; r3=$?; wait $w4; r4=$?
    dd if=/dev/null of=/dev/vda conv=notrunc,fsync 2>/dev/null; flush_rc=$?
    echo "COV-LIMIT straddle=$straddle seio=$seio below=$r1,$r3 above=$r2,$r4 flush_rc=$flush_rc ioerr_logs=$(ioerrs)"
  fi
  echo "COV-READ a=$(rsha 65536 0 16) b2=$(rsha 65536 32 16) b3=$(rsha 65536 48 16) prefix=$(rsha 2048 16384 1) tail=$(rsha 2048 16385 31) above=$(rsha 65536 600 16)";;
*) fail case;;
esac
echo "COV-ERRORS ioerr_logs=$(ioerrs) fs_errors=$(dmesg | grep -c 'EXT4-fs error')"
echo COV-DMESG-BEGIN
dmesg
echo COV-DMESG-END
echo "COV-PASS case=$case phase=$phase"
drain_console || fail console-drain
poweroff -f
fail poweroff-returned
'''
# --transport pci: the PCI probe before the error summary (and before the deliberate reboot). INIT, the default, is unchanged.
INIT_PCI = patch(INIT, [
    ('case= phase=\n', PCI_PROBE.replace('@P@', 'COV') + 'case= phase=\n'),
    ('    echo COV-REBOOT\n', '    pci_probe\n    echo COV-REBOOT\n'),
    ('echo "COV-ERRORS ioerr_logs', 'pci_probe\necho "COV-ERRORS ioerr_logs')])


def pattern(label, size):
    return b''.join(hashlib.sha256(label + i.to_bytes(8, 'little')).digest() for i in range(size // 32))


def fixtures():
    a, b = pattern(b'natemu-cov-a', MiB), pattern(b'natemu-cov-b', MiB)
    return a, b, pattern(b'natemu-cov-limit', 64 << 10)


def sha(data): return hashlib.sha256(data).hexdigest()


def expected_values():
    """Every hash a guest marker must report."""
    a, b, p = fixtures()
    return {'a': sha(a), 'b': sha(b), 'prefix': sha(b[:2048]), 'tail': sha(p[2048:]), 'zero': sha(bytes(MiB))}


def gpt(path, total, parts):
    """A protective MBR, primary and backup GPT headers and 128 entries (deterministic GUIDs)."""
    def guid(name): return uuid.uuid5(uuid.NAMESPACE_URL, 'natemu-cov/' + name).bytes_le
    entries = bytearray(128 * 128)
    for i, (start, size, name) in enumerate(parts):
        kind = GPT_SWAP if name.endswith('swap') else GPT_LINUX
        struct.pack_into('<16s16sQQQ72s', entries, 128 * i, uuid.UUID(kind).bytes_le, guid(name), start, start + size - 1, 0,
                         name.encode('utf-16-le'))
    def header(mine, other, table):
        h = bytearray(struct.pack('<8sIIIIQQQQ16sQIII', b'EFI PART', 0x10000, 92, 0, 0, mine, other, 34, total - 34,
                                  guid(path.name), table, 128, 128, zlib.crc32(entries)))
        struct.pack_into('<I', h, 16, zlib.crc32(h))
        return bytes(h).ljust(SECTOR, b'\0')
    mbr = bytearray(SECTOR)
    struct.pack_into('<B3sB3sII', mbr, 446, 0, b'\0\2\0', 0xee, b'\xff\xff\xff', 1, min(total - 1, 0xffffffff))
    mbr[510:512] = b'\x55\xaa'
    with path.open('r+b') as f:
        for at, data in ((0, mbr), (SECTOR, header(1, total - 1, 2)), (2 * SECTOR, entries),
                         ((total - 33) * SECTOR, entries), ((total - 1) * SECTOR, header(total - 1, 1, total - 33))):
            f.seek(at); f.write(data)


def mbr(path, parts):
    table = bytearray(SECTOR)
    struct.pack_into('<I', table, 440, 0x6e61746d)
    for i, (start, size, name) in enumerate(parts):
        struct.pack_into('<B3sB3sII', table, 446 + 16 * i, 0, b'\xfe\xff\xff', 0x82 if name.endswith('swap') else 0x83,
                         b'\xfe\xff\xff', start, size)
    table[510:512] = b'\x55\xaa'
    with path.open('r+b') as f: f.write(table)


def table_bytes(path, total):
    """The partition table's sectors (GPT: both copies; MBR: sector 0)."""
    with path.open('rb') as f:
        head = f.read(34 * SECTOR); f.seek((total - 33) * SECTOR); return head + f.read()


def sparse_copy(src, dst):
    """Copy SRC to DST writing only nonzero 64 KiB blocks (cp across volumes would allocate the holes)."""
    with open(src, 'rb') as i, open(dst, 'xb') as o:
        size = os.fstat(i.fileno()).st_size
        o.truncate(size)
        for at in range(0, size, 64 << 10):
            block = i.read(64 << 10)
            if block.count(0) != len(block): os.pwrite(o.fileno(), block, at)


def prepare(out, inputs, transport='mmio'):
    """Kernel, initramfs and the base images in OUT; returns the manifest."""
    out.mkdir(parents=True)
    manifest = json.loads((inputs / 'inputs.json').read_text())
    for name in ('linux', 'bootstrap/init', 'mke2fs.conf'): assert digest(inputs / name) == manifest['outputs'][name], name
    shutil.copyfile(inputs / 'linux', out / 'linux')
    tree = out / 'initramfs'
    shutil.copytree(inputs / 'bootstrap', tree, symlinks=True)
    shutil.rmtree(tree / 'newroot'); (tree / 'init').unlink()
    for name in APPLETS:
        if not (tree / 'bin' / name).is_symlink(): (tree / 'bin' / name).symlink_to('busybox')
    for d in ('m1', 'm2', 'fixtures'): (tree / d).mkdir()
    a, b, p = fixtures()
    (tree / 'fixtures/a.bin').write_bytes(a); (tree / 'fixtures/b.bin').write_bytes(b)
    (tree / 'init').write_text(INIT_PCI if transport == 'pci' else INIT); (tree / 'init').chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec); spec.loader.exec_module(mkcpio)
    mkcpio.main(str(tree), str(out / 'initramfs.cpio'))
    tools = {k: Path(v['path']) for k, v in manifest['tools'].items()}
    for k, v in manifest['tools'].items(): assert digest(tools[k]) == v['sha256'], k
    env = dict(os.environ, MKE2FS_CONFIG=str((inputs / 'mke2fs.conf').resolve()))
    def image(name, size):
        path = out / name
        with path.open('xb') as f: f.truncate(size)
        return path
    def mkfs(path, start, sectors, label):
        checked([tools['mke2fs'], '-F', '-q', '-t', 'ext4', '-b', '4096', '-L', label, '-U', str(uuid.uuid5(uuid.NAMESPACE_URL, label)),
                 '-E', f'offset={start * SECTOR},lazy_itable_init=0,lazy_journal_init=0,root_owner=0:0', path, f'{sectors // 2}k'],
                out / f'mke2fs-{path.stem}-{label}.log', env)
    for name, write in (('gpt.raw', gpt), ('mbr.raw', None)):
        path = image(name, 64 * MiB)
        for start, sectors, label in PARTS['table'][:2]: mkfs(path, start, sectors, label)
        if write: gpt(path, 64 * MiB // SECTOR, PARTS['table'])
        else: mbr(path, PARTS['table'])
    path = image('hostfull.raw', 128 * MiB)
    mkfs(path, *PARTS['hostfull'][0])
    gpt(path, 128 * MiB // SECTOR, PARTS['hostfull'])
    mkfs(image('data.raw', 64 * MiB), 0, 64 * MiB // SECTOR, 'natemu-data')
    path = image('limit.raw', 64 * MiB)
    with path.open('r+b') as f: f.seek(PATTERN_AT); f.write(p)
    result = {'purpose': 'block coverage inputs; not a performance claim', 'block_inputs': str(inputs.resolve()),
              'tools': manifest['tools'], 'e2fsck': str(tools['e2fsck']),
              'outputs': {n: digest(out / n) for n in ('linux', 'initramfs.cpio', 'gpt.raw', 'mbr.raw', 'hostfull.raw', 'data.raw', 'limit.raw')}}
    write_json(out / 'inputs.json', result)
    return result


def ext4_offsets(case):
    """Byte offsets of the image's ext4 file systems."""
    if case in ('gpt', 'mbr'): return [s * SECTOR for s, _, _ in PARTS['table'][:2]]
    if case.startswith('host-full'): return [PARTS['hostfull'][0][0] * SECTOR]
    if case.startswith('host-limit'): return []
    return [0]


def limit_image():
    """host-limit's exact image after its write launch."""
    a, b, p = fixtures()
    image = bytearray(64 * MiB)
    image[:MiB] = a; image[2 * MiB:3 * MiB] = b; image[3 * MiB:4 * MiB] = b
    image[PATTERN_AT:PATTERN_AT + len(p)] = p
    image[PATTERN_AT:LIMIT] = b[:LIMIT - PATTERN_AT]
    return bytes(image)


def judge(case, phase, text, host, expect, pci=False):
    """Errors for one launch: guest serial TEXT, HOST facts (rc, stop, timeout, changed, fsck, table, exact)."""
    errors = []
    def require(ok, message):
        if not ok: errors.append(message)
    def one(regex):
        found = re.findall(regex, text, re.M)
        return found[0] if len(found) == 1 else None
    require(one(r'^COV-BEGIN case=(\S+) phase=(\S+) cpus=\d+ kernel=(\S+)$') == (case, phase, KVER), 'wrong/missing begin, case, phase or kernel')
    require(not re.search(r'COV-FAIL|BUG:|Oops:|Kernel panic|Call Trace:|EXT4-fs error|Buffer I/O error|GPT:', text), 'guest/kernel/filesystem error')
    require(not host['timeout'], 'VM timed out')
    if pci: errors += pci_errors(text, 'COV', 2, ('block',))
    reboot = case.startswith('reboot-') and phase == 'write'
    stop, rc = STOP.get(case, POWEROFF) if phase == 'write' else POWEROFF
    require(host['stop'] == stop and host['rc'] == rc, f"stop {host['stop']!r} rc={host['rc']}, expected {stop!r} rc={rc}")
    require(host['fsck'] == [0] * len(ext4_offsets(case)) or reboot, f"offline e2fsck results {host['fsck']}")
    failed_calls = host['backend_errors']
    if phase == 'write' and case == 'host-limit': require(failed_calls == [BACKEND_ERRORS[case]] * 3, f'host call failures {failed_calls}')
    elif phase == 'write' and case == 'host-full':
        require(len(failed_calls) >= 1 and set(failed_calls) == {BACKEND_ERRORS[case]}, f'host call failures {failed_calls}')
    else: require(not failed_calls, f'host call failures {failed_calls}')
    if not reboot:
        require(one(r'^COV-PASS case=(\S+) phase=(\S+)$') == (case, phase), 'guest did not finish')
        require(one(r'^COV-ERRORS ioerr_logs=(\d+) fs_errors=(\d+)$') is not None, 'error summary missing')
        for marker in ('DMESG-BEGIN', 'DMESG-END'): require(one(r'^COV-' + marker + '$') is not None, 'missing/duplicate ' + marker)
    # A write launch changes the image; a verify launch only if it replays a journal (reboot cases mount rw).
    if phase == 'write': require(host['changed'], 'write launch left the image unchanged')
    elif not case.startswith('reboot-'): require(not host['changed'], 'read-only relaunch changed the image')
    errors_line = one(r'^COV-ERRORS ioerr_logs=(\d+) fs_errors=(\d+)$')
    io_errors = int(errors_line[0]) if errors_line else -1
    require(errors_line is None or errors_line[1] == '0', 'ext4 errors logged')
    if case in ('gpt', 'mbr'):
        names = [n for _, _, n in PARTS['table']] if case == 'gpt' else ['none'] * 3
        parts = re.findall(r'^COV-PART n=(\d) start=(\d+) size=(\d+) name=(\S+)$', text, re.M)
        require(parts == [(str(i + 1), str(s), str(n), names[i]) for i, (s, n, _) in enumerate(PARTS['table'])], f'partitions seen {parts}')
        require(re.search(r'^\[[ \d.]+\]  vda: vda1 vda2 vda3$', text, re.M) is not None, 'kernel partition scan line missing')
        require(one(r'^COV-DATA p1=(\S+) p2=(\S+)$') == (expect['a'], expect['b']), 'partition file contents differ')
        require(one(r'^COV-SWAP sig=(\S*)$') == 'SWAPSPACE2', 'swap signature missing')
        require(host['table'], 'partition table bytes changed')
        require(io_errors == 0, 'I/O errors logged')
    elif case.startswith('reboot-'):
        require(one(r'^COV-DATA a=(\S+)$') == expect['a'], 'synced file differs or missing')
        if reboot:
            require(one(r'^COV-REBOOT$') is not None and one(r'^COV-PASS ') is None, 'guest did not reach its reboot')
        else: require(io_errors == 0, 'I/O errors logged')
    elif case == 'fs-full':
        if phase == 'write':
            require(one(r'^COV-FULL dd_rc=(\d+) enospc=(\d) avail=(\d+) more=(\S+) sync_rc=(\d+)$') == ('1', '1', '0', 'enospc', '0'),
                    'ENOSPC not reported, space left, a later write succeeded or sync failed')
        require(one(r'^COV-DATA keep=(\S+) after=(\S+)$') == (expect['a'], expect['b']), 'file contents differ')
        require(io_errors == 0, 'I/O errors logged')
    elif case == 'host-full':
        if phase == 'write':
            full = one(r'^COV-HOSTFULL dd_rc=(\d+) eio=(\d) flush_rc=(\d+) ioerr_logs=(\d+)$')
            require(full is not None and full[0] != '0' and full[1:3] == ('1', '0') and int(full[3]) >= 1,
                    f'host ENOSPC not seen as an I/O error, or FLUSH failed ({full})')
        else: require(io_errors == 0, 'I/O errors logged on relaunch')
        require(one(r'^COV-DATA a=(\S+)$') == expect['a'], 'data written before the failure differs')
    elif case == 'host-limit':
        if phase == 'write':
            require(one(r'^COV-LIMIT straddle=(\S+) seio=(\d) below=(\S+) above=(\S+) flush_rc=(\d+) ioerr_logs=(\d+)$')
                    == ('fail', '1', '0,0', '1,1', '0', '3'), 'host write failures not confined to their own requests')
            require(host['exact'], 'image differs from its exact expected contents')
        else: require(io_errors == 0, 'I/O errors logged on relaunch')
        require(one(r'^COV-READ a=(\S+) b2=(\S+) b3=(\S+) prefix=(\S+) tail=(\S+) above=(\S+)$')
                == (expect['a'], expect['b'], expect['b'], expect['prefix'], expect['tail'], expect['zero']), 'read-back differs')
    return errors


def last_stop(stderr):
    lines = [l for l in stderr.splitlines() if l.startswith('natemu: ')]
    return lines[-1] if lines else None


def fsck(e2fsck, image, offsets, log):
    results = []
    for i, offset in enumerate(offsets):
        with (log.parent / f'{log.name}-{i}.log').open('wb') as f:
            results.append(subprocess.run([str(e2fsck), '-fn', f'{image}?offset={offset}'], stdout=f, stderr=subprocess.STDOUT, timeout=120).returncode)
    return results


def launch(args, case, phase, image, work, limit):
    command = [str(args.natemu), '--runtime', str(args.runtime), '--fexc', str(args.fexc), '--kernel', str(args.prepared / 'linux'),
               '--initrd', str(args.prepared / 'initramfs.cpio'), '--disk', str(image), '--vcpus', '2', '--ram', '1G',
               '--compile-cache', 'off', '--serial', str(work / 'serial.log'), '--cmdline',
               f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr cov_case={case} cov_phase={phase}' + EXTRA.get(case, '')]
    if args.transport == 'pci': command += ['--virtio-transport', 'pci']
    write_json(work / 'command.json', {'command': command, 'rlimit_fsize': limit})
    def limited():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)    # inherited across exec: the write fails with EFBIG instead
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    started, timeout = time.monotonic(), False
    with (work / 'stdout.log').open('wb') as out, (work / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True,
                                   preexec_fn=limited if limit else None)
        try: process.wait(timeout=120)
        except subprocess.TimeoutExpired: timeout = True; stop_group(process)
        except BaseException: stop_group(process); raise
    return process.returncode, timeout, time.monotonic() - started


def run_case(args, case):
    """Both launches of CASE (a control: of the case it controls); returns the result."""
    base = case.removesuffix('-control')
    work = args.out / case; work.mkdir()
    volume = None
    if base == 'host-full' and case == base:
        volume = work / 'volume'
        checked(['hdiutil', 'create', '-size', '32m', '-fs', 'APFS', '-volname', 'natemucov', '-type', 'SPARSE', work / 'volume.sparseimage'], work / 'hdiutil-create.log')
        volume.mkdir()
        checked(['hdiutil', 'attach', '-nobrowse', '-mountpoint', volume, work / 'volume.sparseimage'], work / 'hdiutil-attach.log')
    result = {'case': case, 'launches': []}
    try:
        image = (volume or work) / 'disk.raw'
        if volume: sparse_copy(args.prepared / IMAGE[base], image)
        else: checked(['cp', '-c', args.prepared / IMAGE[base], image], work / 'clone.log')
        total = os.path.getsize(image) // SECTOR
        table = table_bytes(image, total)
        expect = expected_values()
        for phase in ('write', 'verify'):
            launch_dir = work / phase; launch_dir.mkdir()
            before = digest(image)
            limit = LIMIT if case == 'host-limit' and phase == 'write' else 0
            rc, timeout, seconds = launch(args, base, phase, image, launch_dir, limit)
            text = (launch_dir / 'serial.log').read_text(errors='replace').replace('\r', '') if (launch_dir / 'serial.log').exists() else ''
            after = digest(image)
            stderr = (launch_dir / 'stderr.log').read_text(errors='replace')
            host = {'rc': rc, 'timeout': timeout, 'stop': last_stop(stderr),
                    'backend_errors': [l for l in stderr.splitlines() if l.startswith('virtio-block: ')],
                    'changed': after != before, 'fsck': fsck(args.e2fsck, image, ext4_offsets(base), launch_dir / 'fsck'),
                    'table': table_bytes(image, total) == table,
                    'exact': image.read_bytes() == limit_image() if base == 'host-limit' and phase == 'write' else None}
            errors = judge(base, phase, text, host, expect, args.transport == 'pci')
            entry = {'phase': phase, 'seconds': round(seconds, 1), 'disk_before': before, 'disk_after': after, **host, 'errors': errors}
            if base == 'host-full' and volume and phase == 'write':
                entry['volume_free_kib'] = shutil.disk_usage(volume).free // 1024
            write_json(launch_dir / 'result.json', entry); result['launches'].append(entry)
            print(json.dumps({'case': case, **entry}), flush=True)
            if errors: break
    finally:
        if volume:
            subprocess.run(['hdiutil', 'detach', str(volume)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            (work / 'volume.sparseimage').unlink(missing_ok=True)
        for leftover in (work / 'disk.raw',): leftover.unlink(missing_ok=True)
    failed = any(l['errors'] for l in result['launches'])
    # A control passes when the judge rejects it.
    result['pass'] = failed if case in CONTROLS else not failed and len(result['launches']) == 2
    write_json(work / 'result.json', result)
    return result


def run(args):
    args.out = args.out.resolve(); args.out.mkdir(parents=True, exist_ok=False)
    args.inputs = args.inputs.resolve(strict=True)
    if os.environ.get('NATEMU_PREBUILT') == '1':
        target = Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target'))
        args.natemu, args.runtime = target / 'release/natemu', target / 'aarch64-unknown-none/release/natemu-runtime'
    else:
        from run_boot_smoke import build
        args.natemu, args.runtime = build()
    args.fexc = Path(args.fexc).resolve(strict=True)
    inputs = prepare(args.out / 'prepared', args.inputs, args.transport)
    args.prepared, args.e2fsck = args.out / 'prepared', Path(inputs['e2fsck'])
    write_json(args.out / 'manifest.json', {'cases': args.cases, 'seconds_per_vm': 120, 'source_root': str(ROOT),
               'artifacts': {str(p): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))}, 'inputs': inputs})
    results = []
    with VM_LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)    # correctness only: shared with other correctness runs (tools/vmlock.py)
        for case in args.cases:
            results.append(run_case(args, case)); write_json(args.out / 'results.json', results)
            print(f"{case}: {'PASS' if results[-1]['pass'] else 'FAIL'}", flush=True)
    for name in ('gpt.raw', 'mbr.raw', 'hostfull.raw', 'data.raw', 'limit.raw'): (args.prepared / name).unlink()
    return 0 if all(r['pass'] for r in results) else 1


def self_check():
    """Synthetic passing launches for every case and phase, then mutations the judge must reject. No VM."""
    expect, controls = expected_values(), 0
    def text(case, phase):
        t = [f'COV-BEGIN case={case} phase={phase} cpus=2 kernel={KVER}']
        if case in ('gpt', 'mbr'):
            names = [n for _, _, n in PARTS['table']] if case == 'gpt' else ['none'] * 3
            t += [f'COV-PART n={i + 1} start={s} size={n} name={names[i]}' for i, (s, n, _) in enumerate(PARTS['table'])]
            t += [f'COV-DATA p1={expect["a"]} p2={expect["b"]}', 'COV-SWAP sig=SWAPSPACE2']
        elif case.startswith('reboot-'):
            t += [f'COV-DATA a={expect["a"]}'] + (['COV-REBOOT'] if phase == 'write' else [])
        elif case == 'fs-full':
            t += ['COV-FULL dd_rc=1 enospc=1 avail=0 more=enospc sync_rc=0'] if phase == 'write' else []
            t += [f'COV-DATA keep={expect["a"]} after={expect["b"]}']
        elif case == 'host-full':
            t += ['COV-HOSTFULL dd_rc=1 eio=1 flush_rc=0 ioerr_logs=4'] if phase == 'write' else []
            t += [f'COV-DATA a={expect["a"]}']
        else:
            t += ['COV-LIMIT straddle=fail seio=1 below=0,0 above=1,1 flush_rc=0 ioerr_logs=3'] if phase == 'write' else []
            t += [f'COV-READ a={expect["a"]} b2={expect["b"]} b3={expect["b"]} prefix={expect["prefix"]} tail={expect["tail"]} above={expect["zero"]}']
        if not (case.startswith('reboot-') and phase == 'write'):
            errors = 3 if case == 'host-limit' and phase == 'write' else 4 if case == 'host-full' and phase == 'write' else 0
            t += [f'COV-ERRORS ioerr_logs={errors} fs_errors=0', 'COV-DMESG-BEGIN', '[    1.000000]  vda: vda1 vda2 vda3', 'COV-DMESG-END',
                  f'COV-PASS case={case} phase={phase}']
        return '\n'.join(t) + '\n'
    for case in CASES:
        for phase in ('write', 'verify'):
            stop, rc = STOP.get(case, POWEROFF) if phase == 'write' else POWEROFF
            host = {'rc': rc, 'timeout': False, 'stop': stop, 'changed': phase == 'write', 'fsck': [0] * len(ext4_offsets(case)),
                    'table': True, 'exact': True, 'backend_errors': [BACKEND_ERRORS[case]] * 3 if case in BACKEND_ERRORS and phase == 'write' else []}
            good = text(case, phase)
            assert not judge(case, phase, good, host, expect), (case, phase, judge(case, phase, good, host, expect))
            # Host-side mutations.
            for key, value in (('rc', 7), ('timeout', True), ('stop', 'natemu: Cancelled'), ('changed', phase != 'write')):
                if key == 'changed' and case.startswith('reboot-') and phase == 'verify': continue
                assert judge(case, phase, good, dict(host, **{key: value}), expect), (case, phase, key); controls += 1
            if ext4_offsets(case) and not (case.startswith('reboot-') and phase == 'write'):
                assert judge(case, phase, good, dict(host, fsck=[4] + host['fsck'][1:]), expect); controls += 1
            if case in ('gpt', 'mbr'):
                assert judge(case, phase, good, dict(host, table=False), expect); controls += 1
            # A missing, extra or different host call failure.
            other = 'virtio-block: Input/output error (os error 5)'
            for calls in ((host['backend_errors'][1:] if case == 'host-limit' and phase == 'write' else [other]), host['backend_errors'] + [other]):
                assert judge(case, phase, good, dict(host, backend_errors=calls), expect), (case, phase, calls); controls += 1
            if case == 'host-limit' and phase == 'write':
                assert judge(case, phase, good, dict(host, exact=False), expect); controls += 1
            # Guest-side mutations: every marker value the case judges (the I/O error count of a write launch that
            # injects host failures is only bounded by its own marker).
            lines = good.splitlines()
            for i, line in enumerate(lines):
                if not line.startswith('COV-') or line.split()[0] in ('COV-BEGIN', 'COV-PART', 'COV-DMESG-BEGIN', 'COV-DMESG-END'): continue
                for field in line.split()[1:]:
                    key, _, value = field.partition('=')
                    if line.startswith('COV-ERRORS') and key == 'ioerr_logs' and case.startswith('host-') and phase == 'write': continue
                    new = {'0': '1', '1': '0'}.get(value, '0' if value.isdigit() else value[:-1] + ('1' if value.endswith('0') else '0'))
                    bad = '\n'.join(lines[:i] + [line.replace(field, f'{key}={new}')] + lines[i + 1:])
                    assert judge(case, phase, bad, host, expect), (case, phase, line, field, new); controls += 1
            for bad in (good.replace('case=' + case, 'case=x', 1), good + 'COV-FAIL synthetic\n', good + 'EXT4-fs error synthetic\n',
                        good + 'Buffer I/O error synthetic\n', good + 'GPT:Primary header thinks Alt. header is not at the end\n'):
                assert judge(case, phase, bad, host, expect), (case, phase); controls += 1
            if not (case.startswith('reboot-') and phase == 'write'):
                assert judge(case, phase, good.replace('COV-PASS', 'COV-GONE'), host, expect); controls += 1
            if case in ('gpt', 'mbr'):
                for bad in (good.replace('n=3 ', 'n=4 '), good.replace(' vda: vda1 vda2 vda3', ' vda: vda1 vda2'),
                            good.replace('name=natemu-one', 'name=none') if case == 'gpt' else good.replace('name=none', 'name=natemu-one', 1)):
                    assert judge(case, phase, bad, host, expect), case; controls += 1
    # PCI transport: the probe block is required and judged.
    probe = ('COV-PCI-BEGIN\nCOV-PCI-DEV 0000:00:00.0 0x8086 0x1237 0x060000\nCOV-PCI-DEV 0000:00:01.0 0x1af4 0x1042 0x010000\n'
             'COV-PCI-IRQ  28:          0        161  PCI-MSIX-0000:00:01.0   1-edge      virtio0-req.0\nCOV-PCI-END\n')
    good = text('gpt', 'verify').replace('COV-ERRORS', probe + 'COV-ERRORS', 1)
    host = {'rc': 0, 'timeout': False, 'stop': POWEROFF[0], 'changed': False, 'fsck': [0] * len(ext4_offsets('gpt')), 'table': True, 'exact': None, 'backend_errors': []}
    assert not judge('gpt', 'verify', good, host, expect, True), judge('gpt', 'verify', good, host, expect, True)
    assert judge('gpt', 'verify', text('gpt', 'verify'), host, expect, True)
    for old, new in (('0x1042', '0x1041'), ('161', '0'),
                     ('COV-PCI-END', "COV-PCI-DMESG pci 0000:00:01.0: BAR 0 [mem 0xe0010000-0xe0017fff]: can't claim; no compatible bridge window\nCOV-PCI-END")):
        assert judge('gpt', 'verify', good.replace(old, new), host, expect, True), old; controls += 1
    # The live controls' outcomes: a host-full guest whose write did not fail, a host-limit guest with no limit.
    good = text('host-full', 'write').replace('dd_rc=1 eio=1', 'dd_rc=0 eio=0').replace('ioerr_logs=4', 'ioerr_logs=0')
    host = {'rc': 0, 'timeout': False, 'stop': POWEROFF[0], 'changed': True, 'fsck': [0], 'table': True, 'exact': None, 'backend_errors': []}
    assert judge('host-full', 'write', good, host, expect); controls += 1
    # Exact-image oracle and table helpers.
    image = limit_image(); a, b, p = fixtures()
    assert image[PATTERN_AT:LIMIT] == b[:2048] and image[LIMIT:PATTERN_AT + 65536] == p[2048:] and image[4 * MiB:PATTERN_AT] == bytes(PATTERN_AT - 4 * MiB)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'g.raw'
        with path.open('xb') as f: f.truncate(64 * MiB)
        gpt(path, 64 * MiB // SECTOR, PARTS['table'])
        data = path.read_bytes()
        header = bytearray(data[SECTOR:SECTOR + 92]); crc = struct.unpack_from('<I', header, 16)[0]; struct.pack_into('<I', header, 16, 0)
        assert crc == zlib.crc32(header) and data[SECTOR:SECTOR + 8] == b'EFI PART' and data[-SECTOR:-SECTOR + 8] == b'EFI PART'
        assert struct.unpack_from('<I', data, SECTOR + 88)[0] == zlib.crc32(data[2 * SECTOR:2 * SECTOR + 128 * 128])
        assert data[-33 * SECTOR:-SECTOR] == data[2 * SECTOR:34 * SECTOR] and data[510:512] == b'\x55\xaa' and data[450] == 0xee
        copy = Path(tmp) / 'c.raw'; sparse_copy(path, copy); assert copy.read_bytes() == data
    pci_self_test()
    for script in (INIT, INIT_PCI): subprocess.run(['sh', '-n'], input=script, text=True, check=True)
    print(f'PASS: {len(CASES)} cases x 2 launches judged; {controls} negative judge controls; GPT/limit-oracle/sparse-copy checks; shell syntax. No VM.')


def main():
    if sys.argv[1:] == ['self-check']: self_check(); return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fexc', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=INPUTS, help='a tools/run_virtio_block.py prepare directory')
    parser.add_argument('--transport', choices=('mmio', 'pci'), default='mmio', help='virtio transport (--virtio-transport); pci adds a guest probe and its checks')
    parser.add_argument('--cases', nargs='+', choices=CASES + CONTROLS, default=list(CASES + CONTROLS))
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases): parser.error('duplicate cases')
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
