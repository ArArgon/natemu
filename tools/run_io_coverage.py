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
  trim           DISCARD and WRITE_ZEROES end to end on the gpt image's unused third partition: a 1 MiB pattern, then
                 `blkdiscard` (BusyBox: DISCARD) over 128 KiB and a BLKZEROOUT ioctl (a tiny static program, ZEROOUT; BusyBox
                 has no -z) over an unaligned range, and `fstrim` on the mounted ext4; the host's image bytes are exact
                 (the discarded 4 KiB-aligned range reads as zeros, the zeroed range too, the rest of the pattern stays),
                 fsck clean, the files written before the trim intact, the device's discard/write-zeroes limits non-zero.
  host-limit     RLIMIT_FSIZE on the natemu process (SIGXFSZ ignored) makes every host pwrite ending past LIMIT fail
                 with EFBIG, deterministically: a 64 KiB O_DIRECT write (one 16-segment request) straddling LIMIT gets a
                 2048-byte short write then EFBIG (IOERR), two of four concurrent writers above LIMIT fail while the two
                 below succeed, a FLUSH afterwards and every later read succeed; the image is compared byte for byte
                 with its exact expected contents (the straddling request's landed 2048-byte prefix included: no
                 rollback is promised) and a relaunch without the limit reads it back.
  multidisk      (not in the default list; `--cases multidisk`, with the default --transport mmio) three disks of 8, 16 and 24 MiB
                 listed in a config file, launched four ways: all MMIO, all PCI, disks MMIO with the entropy device on PCI,
                 disks PCI with the entropy device on MMIO. The guest checks that /dev/vda, vdb, vdc are the listed disks in
                 order (size, GET_ID serial = host device and inode, the first 4 KiB), that there is no vdd, reads each disk
                 (rough speed line) and writes a block to each; the host checks every image's bytes.
  readonly       (not in the default list; `--cases readonly`, with the default --transport mmio) read-only disks end to end,
                 on MMIO and on PCI, in three positions: [rw, ISO, raw ext4] and [ISO, rw]. The ISO is made by `hdiutil
                 makehybrid -iso -joliet`; the guest needs isofs.ko, taken from the pinned Debian kernel package
                 (--kernel-deb). The guest checks the device's read-only flag (1; the writable disk 0), no write cache,
                 no discard, mounts the ISO (iso9660) and the raw ext4 read-only and reads them, and every write (dd to the
                 device, a write in the file system) fails; the writable disk gets its block; the host checks that the ISO and
                 the ext4 image are byte-identical afterwards. A last launch pair runs two VMs at once on one read-only ISO
                 (the shared lock): both boot, and while both hold it a writable lock on the file and a writable launch are refused.
  sharedintx     (not in the default list; `--cases sharedintx`, with the default --transport mmio) PCI functions sharing
                 INTx lines (design step 6), every device on PCI from a config file (disks, NIC, entropy). load: `pci=nomsi`
                 (INTx only), 8 disks at 1 and 4 vCPUs and 29 disks (every slot) at 4 vCPUs; the guest checks the disks
                 are at their slots in list order and that IO-APIC lines carry several virtio devices, then in four
                 overlapping rounds writes a 4 MiB random pattern to every disk at once (direct I/O, 4/16/64 KiB blocks)
                 while reading back the previous round and the entropy device; every read must match, no I/O error or
                 timeout, and the host checks each image's three copies against the guest's hash. stale: MSI on, 8 disks
                 at 2 vCPUs: slot 1 is rebound with MSI disallowed (INTx on line 16, which slot 4 shares while on MSI-X);
                 slot 4 is unbound and rebound (Linux resets it first), then its MSI-X Enable and Interrupt Disable are
                 cleared under its bound driver without a reset, so its stale ISR asserts the shared line; slot 1's disk
                 must keep working at normal speed after each step, and Linux must not disable the line.
  qcow2, qcow2-trim  (not in the default list; need qemu-img, else a clear refusal) the gpt and trim guests on a qcow2 disk: the
                 raw image is converted with `qemu-img convert -O qcow2 -o preallocation=metadata` (free space allocated up
                 front, as in the runners' --image-format qcow2) and launched with --disk. The host checks that read bytes
                 (image changed, offline fsck, partition table, the trim byte-exactness) run on a temporary raw conversion,
                 deleted after each launch; added: `qemu-img check` finds nothing, and `qemu-img compare` of the qcow2 with that
                 raw copy is identical. qcow2-trim also needs `qemu-img map` to show the DISCARDed 128 KiB holding no data and
                 the 1 MiB pattern's data clusters shrunk from 16 to at most 12 (the discard and the ZEROOUT's whole clusters).
                 Their judge (qcow2_errors) is self-checked with mutations; no live control.
  overlay        (not in the default list; needs qemu-img) the gpt guest on a qcow2 overlay over a raw base (an APFS clone of
                 the prepared gpt image): the write launch names an overlay path that does not exist with the base as its
                 `backing` in a generated config file, so natemu creates it; the verify launch boots the same overlay again.
                 After each launch, besides the qcow2 checks above (on the overlay's raw view), the base's hash is unchanged
                 and `qemu-img info` names the base's absolute path and format raw. Then refusals, each needing a nonzero exit,
                 its message and an unchanged overlay: the overlay given by --disk alone (the message names the base and the key
                 to add), and an overlay made by `qemu-img create -b BASE -F raw` launched with another file as its backing.
                 Its judge (overlay_errors, refusal_errors) is self-checked with mutations.
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

from io_common import STARTUP_LINE, ROOT, VM_LOCK, digest, stop_group, write_json
from raw_root import RAW_ROOT
from io_common import PCI_PROBE, patch, pci_errors, pci_self_test, qcow2_convert, qcow2_problems, raw_of, need_qemu_img
from run_virtio_block import checked, KERNEL_DEB_SHA
from run_virtio_rng import KVER

KERNEL_DEB = RAW_ROOT / 'io3-block-20261108-01/prerequisites/linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb'
INPUTS = RAW_ROOT / 'io3-block-20261108-01/inputs-01'
MiB = 1 << 20
SECTOR = 512
LIMIT = 32 * MiB + 2048          # host-limit: RLIMIT_FSIZE
PATTERN_AT = 32 * MiB            # host-limit: 64 KiB of pattern from here
APPLETS = ('sh', 'mount', 'umount', 'modprobe', 'cat', 'grep', 'awk', 'dd', 'wc', 'sha256sum', 'dmesg', 'sync',
           'poweroff', 'reboot', 'sleep', 'readlink', 'mkdir', 'cp', 'rm', 'stty', 'nproc', 'uname', 'mkswap', 'swapon', 'swapoff',
           'df', 'tail', 'test', 'ls', 'blockdev', 'hexdump', 'timeout')
CASES = ('gpt', 'mbr', 'reboot-acpi', 'reboot-kbd', 'reboot-triple', 'fs-full', 'host-full', 'host-limit', 'trim')
CONTROLS = ('host-full-control', 'host-limit-control')
# qcow2 cases: the gpt and trim guests on a preallocated qcow2 image; the host checks read a temporary raw conversion.
QCOW2_CASES = {'qcow2': 'gpt', 'qcow2-trim': 'trim', 'overlay': 'gpt'}
QCOW_CLUSTER = 65536
# The image each case starts from, and its extra kernel parameters.
IMAGE = {'trim': 'gpt.raw', 'gpt': 'gpt.raw', 'mbr': 'mbr.raw', 'reboot-acpi': 'data.raw', 'reboot-kbd': 'data.raw', 'reboot-triple': 'data.raw',
         'fs-full': 'data.raw', 'host-full': 'hostfull.raw', 'host-limit': 'limit.raw'}
EXTRA = {'reboot-kbd': ' reboot=k', 'reboot-triple': ' reboot=t'}
# The write launch's expected host stop (stderr's last `natemu: ...` line) and exit status; every verify launch: S5.
POWEROFF = ('natemu: Power(1)', 0)
STOP = {'reboot-acpi': ('natemu: Power(2)', 2), 'reboot-kbd': ('natemu: Power(2)', 2),
        'reboot-triple': ('natemu: Resident { stop: 16, status: 1, detail: 16 }', 2)}
# natemu's stderr line for each failed host call (block.rs), by write launch; none anywhere else.
BACKEND_ERRORS = {'host-full': 'virtio-block: No space left on device (os error 28)', 'host-limit': 'virtio-block: File too large (os error 27)'}
# trim: the third partition's pattern (1 MiB of fixture a), a DISCARD range and a ZEROOUT range, in bytes from its start.
TRIM_DISCARD, TRIM_ZERO = (262144, 131072), (524800, 201216)
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
trim)
  parts 3
  if test "$phase" = write; then
    mount -t ext4 /dev/vda1 /m1 || fail mount-p1
    cp /fixtures/a.bin /m1/a.bin && cp /fixtures/b.bin /m1/b.bin && sync || fail copy
    rm /m1/b.bin && sync || fail rm
    fstrim -v /m1 > /tmp/fstrim.out 2>&1; fstrim_rc=$?
    umount /m1 || fail umount
    dd if=/fixtures/a.bin of=/dev/vda3 bs=64k oflag=direct conv=notrunc 2>/dev/null || fail pattern
    blkdiscard -o 262144 -l 131072 /dev/vda3; discard_rc=$?
    zeroout; zeroout_rc=$?
    sync
    echo "COV-TRIM fstrim_rc=$fstrim_rc discard_rc=$discard_rc zeroout_rc=$zeroout_rc discard_max=$(cat /sys/block/vda/queue/discard_max_bytes) zeroes_max=$(cat /sys/block/vda/queue/write_zeroes_max_bytes)"
    echo "COV-FSTRIM $(cat /tmp/fstrim.out)"
  fi
  mount -t ext4 -o ro /dev/vda1 /m1 || fail mount-ro
  echo "COV-DATA a=$(sha /m1/a.bin) b=$(test -e /m1/b.bin && echo present || echo gone)"
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
multidisk)
  hsha() { s=$(dd if=$1 bs=4096 count=1 2>/dev/null | sha256sum) || fail read; echo "${s%% *}"; }
  for d in vda vdb vdc; do wait_dev /dev/$d; done
  for d in vda vdb vdc; do echo "COV-DISK $d size=$(cat /sys/block/$d/size) serial=$(cat /sys/block/$d/serial) head=$(hsha /dev/$d) dev=$(readlink -f /sys/block/$d/device)"; done
  test ! -e /sys/block/vdd && echo "COV-NODISK vdd" || echo "COV-EXTRADISK vdd"
  t0=$(cat /proc/uptime)
  for pass in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do for d in vda vdb vdc; do dd if=/dev/$d of=/dev/null bs=1M count=8 iflag=direct 2>/dev/null || fail read-$d; done; done
  t1=$(cat /proc/uptime)
  echo "COV-SPEED t0=${t0%% *} t1=${t1%% *}"
  dd if=/fixtures/a.bin of=/dev/vda bs=512 count=1 skip=0 seek=8 conv=notrunc 2>/dev/null || fail write-a
  dd if=/fixtures/b.bin of=/dev/vdb bs=512 count=1 skip=0 seek=8 conv=notrunc 2>/dev/null || fail write-b
  dd if=/fixtures/a.bin of=/dev/vdc bs=512 count=1 skip=1 seek=8 conv=notrunc 2>/dev/null || fail write-c
  sync;;
readonly)
  for item in $(cat /proc/cmdline); do case "$item" in cov_iso=*) iso=${item#*=};; cov_ext=*) ext=${item#*=};; cov_rw=*) rw=${item#*=};; esac; done
  modprobe isofs || fail module-isofs
  for d in $rw $iso $ext; do wait_dev /dev/$d; done
  for d in $rw $iso $ext; do echo "COV-RO $d ro=$(cat /sys/block/$d/ro) size=$(cat /sys/block/$d/size) cache=$(cat /sys/block/$d/queue/write_cache | tr ' ' _) discard=$(cat /sys/block/$d/queue/discard_max_bytes) zeroes=$(cat /sys/block/$d/queue/write_zeroes_max_bytes)"; done
  mount -t iso9660 -o ro /dev/$iso /m1 || fail mount-iso
  files=0; sums=
  for f in /m1/*; do files=$((files+1)); sums="$sums$(sha $f)"; done
  mount -o remount,rw /m1 2>/dev/null
  echo x > /m1/new 2>/dev/null; write_rc=$?
  umount /m1 || fail umount-iso
  # The flag is 1 (checked above). Try to clear it (BLKROSET): the kernel keeps a driver-set read-only flag (ro_after stays 1), so
  # a guest cannot reach the device with a write; the device's own refusal (I/O error) is a unit test (block.rs). A missing
  # tool (rc 127) is a failure of the check, not a refused write.
  blockdev --setrw /dev/$iso; setrw_rc=$?
  dd if=/fixtures/a.bin of=/dev/$iso bs=512 count=1 oflag=direct conv=notrunc 2>/dev/null; dd_rc=$?
  echo "COV-ISO files=$files sum=$sums setrw_rc=$setrw_rc ro_after=$(cat /sys/block/$iso/ro) dd_rc=$dd_rc write_rc=$write_rc"
  if test -n "$ext"; then
    mount -t ext4 -o ro /dev/$ext /m2 || fail mount-ext
    lost=$(ls /m2)
    mount -o remount,rw /m2 2>/dev/null
    echo x > /m2/new 2>/dev/null; write_rc=$?
    umount /m2 || fail umount-ext
    blockdev --setrw /dev/$ext; setrw_rc=$?
    dd if=/fixtures/a.bin of=/dev/$ext bs=512 count=1 oflag=direct conv=notrunc 2>/dev/null; dd_rc=$?
    echo "COV-EXT ls=$lost setrw_rc=$setrw_rc ro_after=$(cat /sys/block/$ext/ro) dd_rc=$dd_rc write_rc=$write_rc"
  fi
  dd if=/fixtures/a.bin of=/dev/$rw bs=512 count=1 seek=8 conv=notrunc 2>/dev/null || fail write-rw
  sync
  echo "COV-RWERR n=$(dmesg | grep -c "I/O error, dev $rw")"
  if test "$phase" = share; then n=0; while test $n -lt 25; do echo "COV-HOLD $(cat /proc/uptime)"; sleep 1; n=$((n+1)); done; fi;;
sharedintx)
  for item in $(cat /proc/cmdline); do case "$item" in cov_disks=*) list=${item#*=};; esac; done
  IFS=,; set -- $list; IFS=' '; disks="$*"
  modprobe virtio-rng || fail module-virtio-rng
  for d in $disks; do wait_dev /dev/$d; done
  for d in $disks; do echo "COV-DISK $d dev=$(readlink -f /sys/block/$d/device)"; done
  echo "COV-NDISKS $(ls /sys/block | grep -c '^vd')"
  irqs() { grep -E 'virtio|MSI' /proc/interrupts | while read -r line; do echo "COV-IRQ $line"; done; }
  irqs
  if test "$phase" = load; then
    for d in $disks; do dd if=/dev/urandom of=/tmp/$d bs=1M count=4 2>/dev/null || fail pattern; echo "COV-PAT $d $(sha /tmp/$d)"; done
    t0=$(cat /proc/uptime)
    # Round r writes copy r (r < 3) on every disk at once while reading back copy r - 1 and the entropy device.
    for r in 0 1 2 3; do
      bs=$((4096 << (r % 3 * 2)))
      for d in $disks; do
        if test $r -lt 3; then (dd if=/tmp/$d of=/dev/$d bs=$bs seek=$((r * 4194304 / bs)) oflag=direct conv=notrunc 2>/dev/null || echo "COV-IOFAIL write-$d-$r") & fi
        if test $r -gt 0; then (p=$((r - 1)); s=$(dd if=/dev/$d bs=$bs skip=$((p * 4194304 / bs)) count=$((4194304 / bs)) iflag=direct 2>/dev/null | sha256sum); test "${s%% *}" = "$(sha /tmp/$d)" || echo "COV-IOFAIL read-$d-$p") & fi
      done
      (dd if=/dev/hwrng of=/dev/null bs=64 count=64 2>/dev/null || echo "COV-IOFAIL hwrng-$r") &
      wait
    done
    t1=$(cat /proc/uptime)
    echo "COV-LOAD t0=${t0%% *} t1=${t1%% *}"
  else
    P=/sys/bus/pci/devices; D=/sys/bus/pci/drivers/virtio-pci; A=0000:00:01.0; B=0000:00:04.0
    blk() { n=0; while test $n -lt 100; do for x in $P/$1/virtio*/block/*; do test -e "$x" && { echo ${x##*/}; return; }; done; sleep 0.1; n=$((n+1)); done; }
    byte() { hexdump -s $2 -n 1 -e '1/1 "%u"' $P/$1/config; }
    asha=$(sha /fixtures/a.bin)
    cat > /tmp/chk <<'EOF'
dd if=/fixtures/a.bin of=/dev/$1 bs=4k seek=256 oflag=direct conv=notrunc 2>/dev/null || exit 1
s=$(dd if=/dev/$1 bs=4k skip=256 count=256 iflag=direct 2>/dev/null | sha256sum) || exit 1
test "${s%% *}" = "$2"
EOF
    # One 1 MiB write and read-back on disk $1, at most 30 s: "rc=0 s=SECONDS" when it matched.
    chk() { t0=$(cat /proc/uptime); timeout 30 sh /tmp/chk $1 $asha; rc=$?; t1=$(cat /proc/uptime); echo "rc=$rc s=$(awk "BEGIN{printf \"%.2f\", ${t1%% *} - ${t0%% *}}")"; }
    echo $A > $D/unbind || fail unbind-a
    echo 0 > $P/$A/msi_bus || fail msi-bus
    echo $A > $D/bind || fail bind-a
    a=$(blk $A); b=$(blk $B); test -n "$a" && test -n "$b" || fail names
    echo "COV-STALE-DEVS a=$a b=$b irq_a=$(cat $P/$A/irq)"
    irqs
    echo "COV-STALE step=base a=[$(chk $a)] b=[$(chk $b)]"
    dd if=/dev/$b of=/dev/null bs=4k count=64 iflag=direct 2>/dev/null
    echo $B > $D/unbind || fail unbind-b
    echo "COV-STALE step=unbound a=[$(chk $a)]"
    echo $B > $D/bind || fail bind-b
    b=$(blk $B); test -n "$b" || fail rebind-b
    echo "COV-STALE step=rebound a=[$(chk $a)] b=[$(chk $b)]"
    # Leave slot 4's ISR bit set (a driver on MSI-X never reads it), then MSI-X Enable and Interrupt Disable off.
    dd if=/dev/$b of=/dev/null bs=4k count=64 iflag=direct 2>/dev/null
    cap=$(byte $B 52)
    while test "$cap" -ne 0 && test "$(byte $B $cap)" -ne 17; do cap=$(byte $B $((cap + 1))); done
    test "$cap" -ne 0 || fail msix-cap
    echo "COV-STALE-CFG cap=$cap ctl_hi=$(byte $B $((cap + 3))) cmd_hi=$(byte $B 5)"
    printf '\000' | dd of=$P/$B/config bs=1 seek=$((cap + 3)) conv=notrunc 2>/dev/null || fail msix-off
    printf '\000' | dd of=$P/$B/config bs=1 seek=5 conv=notrunc 2>/dev/null || fail intx-on
    echo "COV-STALE-CFG after ctl_hi=$(byte $B $((cap + 3))) cmd_hi=$(byte $B 5)"
    sleep 2
    echo "COV-STALE step=msix-off a=[$(chk $a)]"
    sleep 2
    echo "COV-STALE step=msix-off-later a=[$(chk $a)]"
    dmesg | grep -E 'nobody cared|Disabling IRQ' | while read -r l; do echo "COV-STALE-DMESG $l"; done
  fi
  irqs
  echo "COV-SHARED-ERRORS io=$(dmesg | grep -c 'I/O error') timeouts=$(dmesg | grep -ciE 'timed? ?out') unhandled=$(dmesg | grep -cE 'nobody cared|Disabling IRQ')";;
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


def zeroout_program():
    """A static x86-64 ELF: open /dev/vda3 read-write and BLKZEROOUT (0x127f) the TRIM_ZERO range; exit status 0, the
    ioctl's errno, or 100 when the open fails. (BusyBox's blkdiscard has no -z.)"""
    path = b'/dev/vda3\0'
    code = bytearray(b'\xb8\x02\x00\x00\x00')                  # mov eax, 2 (open)
    lea_rdi = len(code); code += b'\x48\x8d\x3d\0\0\0\0'      # lea rdi, [rip+path]
    code += b'\xbe\x02\x00\x00\x00\x31\xd2\x0f\x05'         # mov esi, 2 (O_RDWR); xor edx, edx; syscall
    code += b'\x48\x85\xc0'                                 # test rax, rax
    js = len(code); code += b'\x78\0'                         # js fail
    code += b'\x89\xc7\xb8\x10\x00\x00\x00\xbe\x7f\x12\x00\x00'  # mov edi, eax; mov eax, 16; mov esi, 0x127f
    lea_rdx = len(code); code += b'\x48\x8d\x15\0\0\0\0'      # lea rdx, [rip+range]
    code += b'\x0f\x05\xf7\xd8\x89\xc7\xeb\x05'                 # syscall; neg eax; mov edi, eax; jmp done
    code[js + 1] = len(code) - (js + 2)
    code += b'\xbf\x64\x00\x00\x00'                         # fail: mov edi, 100
    code += b'\xb8\x3c\x00\x00\x00\x0f\x05'                 # done: mov eax, 60; syscall
    path_at = len(code); code += path
    range_at = len(code); code += struct.pack('<QQ', *TRIM_ZERO)
    struct.pack_into('<i', code, lea_rdi + 3, path_at - (lea_rdi + 7))
    struct.pack_into('<i', code, lea_rdx + 3, range_at - (lea_rdx + 7))
    base, head = 0x400000, 64 + 56
    elf = b'\x7fELF\x02\x01\x01' + bytes(9) + struct.pack('<HHIQQQIHHHHHH', 2, 62, 1, base + head, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    size = head + len(code)
    elf += struct.pack('<IIQQQQQQ', 1, 7, 0, base, base, size, size, 0x1000)
    return elf + bytes(code)


def prepare(out, inputs, transport='mmio', kernel_deb=None):
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
    for name in ('blkdiscard', 'fstrim'):
        if not (tree / 'bin' / name).is_symlink(): (tree / 'bin' / name).symlink_to('busybox')
    (tree / 'bin/zeroout').write_bytes(zeroout_program()); (tree / 'bin/zeroout').chmod(0o755)
    for d in ('m1', 'm2', 'fixtures'): (tree / d).mkdir()
    a, b, p = fixtures()
    (tree / 'fixtures/a.bin').write_bytes(a); (tree / 'fixtures/b.bin').write_bytes(b)
    if kernel_deb:    # isofs.ko (and cdrom.ko, its dependency) for the readonly case, from the pinned package
        import io, lzma, tarfile
        assert digest(kernel_deb) == KERNEL_DEB_SHA, 'kernel package pin mismatch'
        with tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['ar', 'p', str(kernel_deb), 'data.tar.xz'], timeout=60))) as package:
            for member in package:
                name = Path(member.name).name
                if name not in ('isofs.ko.xz', 'cdrom.ko.xz'): continue
                data = lzma.decompress(package.extractfile(member).read())
                assert ('vermagic=' + KVER).encode() in data, name
                rel = 'kernel/' + member.name.split(f'/{KVER}/kernel/', 1)[1].removesuffix('.xz')
                depends = re.search(rb'depends=([^\0]*)', data).group(1).decode()
                assert depends in ('', 'cdrom'), (name, depends)
                dest = tree / 'lib/modules' / KVER / rel
                dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(data)
                with (tree / 'lib/modules' / KVER / 'modules.dep').open('a') as f:
                    f.write(f'{rel}: ' + ('kernel/drivers/cdrom/cdrom.ko' if depends else '') + '\n')
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
    if case in ('gpt', 'mbr', 'trim'): return [s * SECTOR for s, _, _ in PARTS['table'][:2]]
    if case.startswith('host-full'): return [PARTS['hostfull'][0][0] * SECTOR]
    if case.startswith('host-limit') or case in ('multidisk', 'readonly'): return []
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
    elif case == 'trim':
        if phase == 'write':
            require(one(r'^COV-TRIM fstrim_rc=(\d+) discard_rc=(\d+) zeroout_rc=(\d+) discard_max=(\d+) zeroes_max=(\d+)$')
                    in {('0', '0', '0', '2147483648', '2147483648')}, 'fstrim, DISCARD or ZEROOUT failed, or the device limits are not offered')
            require(host['exact'], 'the discarded and zeroed ranges, or the rest of the pattern, differ on the host')
        require(one(r'^COV-DATA a=(\S+) b=(\S+)$') == (expect['a'], 'gone'), 'file contents differ')
        require(io_errors == 0, 'I/O errors logged')
    elif case == 'multidisk':
        disks = re.findall(r'^COV-DISK (\S+) size=(\d+) serial=(\S+) head=(\S+) dev=(\S+)$', text, re.M)
        require([d[:4] for d in disks] == [(f'vd{n}', str(size // SECTOR), serial, head) for n, (size, serial, head) in zip('abc', host['disks'])],
                f'guest disks {disks}, host disks {host["disks"]}')
        # The transport the file asked for, and (PCI) the slots: 00:01.0, then 04.0, 05.0; (MMIO) the ACPI virtio-mmio node.
        if host['transport'] == 'pci': want = [rf'/pci0000:00/0000:00:{slot:02x}\.0/virtio\d+$' for slot in (1, 4, 5)]
        else: want = [r'/LNRO0005:\d+/virtio\d+$'] * 3
        require(len(disks) == 3 and all(re.search(w, d[4]) for w, d in zip(want, disks)), f'disks are not on {host["transport"]}: {[d[4] for d in disks]}')
        require(one(r'^COV-NODISK vdd$') is not None, 'a fourth disk appeared')
        require(host['written'], 'a disk did not receive its own block (or another one did)')
        require(io_errors == 0, 'I/O errors logged')
    elif case == 'readonly':
        ro = re.findall(r'^COV-RO (\S+) ro=(\d) size=(\d+) cache=(\S+) discard=(\d+) zeroes=(\d+)$', text, re.M)
        want = [(dev, str(int(r)), str(sectors)) for dev, r, sectors in host['layout']]
        require([d[:3] for d in ro] == want, f'guest disks {ro}, expected {want}')
        require(all((d[3] == 'write_through' and d[4] == d[5] == '0') if d[1] == '1' else (d[3] == 'write_back' and d[4] != '0' and d[5] != '0') for d in ro) and len(ro) == len(want),
                f'a read-only disk offers a write cache, discard or write-zeroes, or the writable one does not: {ro}')
        # Each read-only device: BLKROSET was accepted (not rc 127) and did not clear the flag; the write failed (not rc 127).
        refused = lambda f: f[0] == '0' and f[1] == '1' and f[2] not in ('0', '127') and f[3] not in ('0', '127')
        iso = one(r'^COV-ISO files=(\d+) sum=(\S*) setrw_rc=(\d+) ro_after=(\d+) dd_rc=(\d+) write_rc=(\d+)$')
        require(iso is not None and iso[:2] == ('1', host['iso_sha']) and refused(iso[2:]), f'ISO contents, or the write was not refused: {iso}')
        if host['has_ext']:
            ext = one(r'^COV-EXT ls=(.*) setrw_rc=(\d+) ro_after=(\d+) dd_rc=(\d+) write_rc=(\d+)$')
            require(ext is not None and ext[0] == 'lost+found' and refused(ext[1:]), f'ext4 listing, or the write was not refused: {ext}')
        require(host['ro_unchanged'], 'a read-only image changed on the host')
        require(host['written'], 'the writable disk did not receive its block')
        if phase == 'share': require(len(re.findall(r'^COV-HOLD ', text, re.M)) >= 20, 'guest did not hold to the end')
        require(one(r'^COV-RWERR n=0$') is not None, 'I/O errors on the writable disk')
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


def trim_exact(image):
    """The third partition after the trim write launch: the pattern with the DISCARD and ZEROOUT ranges zero."""
    a, _, _ = fixtures()
    want = bytearray(a + bytes(2 * MiB - len(a)))
    for at, n in (TRIM_DISCARD, TRIM_ZERO): want[at:at + n] = bytes(n)
    with image.open('rb') as f:
        f.seek(PARTS['table'][2][0] * SECTOR); return f.read(2 * MiB) == bytes(want)


def last_stop(stderr):
    lines = [l for l in stderr.splitlines() if l.startswith('natemu: ') and not STARTUP_LINE.match(l)]
    return lines[-1] if lines else None


def fsck(e2fsck, image, offsets, log):
    results = []
    for i, offset in enumerate(offsets):
        with (log.parent / f'{log.name}-{i}.log').open('wb') as f:
            results.append(subprocess.run([str(e2fsck), '-fn', f'{image}?offset={offset}'], stdout=f, stderr=subprocess.STDOUT, timeout=120).returncode)
    return results


def qcow2_data_bytes(image, start, length):
    """Bytes of [START, START+LENGTH) that `qemu-img map` reports as data (allocated, not zero) in a qcow2 IMAGE."""
    out = subprocess.run([need_qemu_img(), 'map', '--output=json', '-f', 'qcow2', str(image)], capture_output=True, text=True, check=True).stdout
    return sum(max(0, min(e['start'] + e['length'], start + length) - max(e['start'], start)) for e in json.loads(out) if e['data'] and not e.get('zero'))


def qcow2_host(image, raw, base, phase, start_clusters):
    """The qcow2 facts after a launch: qemu-img check findings, compare result against the raw conversion, trim map numbers."""
    compare = subprocess.run([need_qemu_img(), 'compare', '-f', 'qcow2', '-F', 'raw', str(image), str(raw)], capture_output=True, text=True).returncode
    found, report = qcow2_problems(image)
    trim = None
    if base == 'trim' and phase == 'write':
        start = PARTS['table'][2][0] * SECTOR
        trim = {'discard_data': qcow2_data_bytes(image, start + TRIM_DISCARD[0], TRIM_DISCARD[1]), 'pattern_data': qcow2_data_bytes(image, start, MiB)}
    return {'check': found, 'compare_rc': compare, 'trim': trim, 'allocated_clusters': report.get('allocated-clusters'), 'start_clusters': start_clusters}


def qcow2_errors(q, base, phase):
    errors = list(q['check'])
    if q['compare_rc'] != 0: errors.append(f'qemu-img compare: exit {q["compare_rc"]}')
    if base == 'trim' and phase == 'write':
        t = q['trim']
        if t is None: errors.append('no trim map')
        else:
            if t['discard_data']: errors.append(f'discarded range still holds {t["discard_data"]} data bytes')
            if t['pattern_data'] > 12 * QCOW_CLUSTER: errors.append(f'pattern still holds {t["pattern_data"] // QCOW_CLUSTER} data clusters (max 12 after the discard and zero ranges)')
            if q['allocated_clusters'] is None or q['allocated_clusters'] >= q['start_clusters']: errors.append(f'allocated clusters did not shrink ({q["start_clusters"]} -> {q["allocated_clusters"]})')
            if t['pattern_data'] == 0: errors.append('no pattern data at all (the map is wrong or the write never landed)')
    return errors


def overlay_host(image, base, base_sha):
    """The overlay facts after a launch: the base unchanged, and the backing file and format `qemu-img info` reads from the header."""
    info = json.loads(subprocess.run([need_qemu_img(), 'info', '--output=json', str(image)], capture_output=True, text=True, check=True).stdout)
    return {'base_unchanged': digest(base) == base_sha, 'backing': info.get('backing-filename'), 'backing_format': info.get('backing-filename-format'),
            'want_backing': str(base.resolve())}


def overlay_errors(o):
    errors = [] if o['base_unchanged'] else ['the base image changed']
    if o['backing'] != o['want_backing']: errors.append(f'backing file {o["backing"]!r}, not {o["want_backing"]!r}')
    if o['backing_format'] != 'raw': errors.append(f'backing format {o["backing_format"]!r}, not raw')
    return errors


def overlay_config(path, disk, backing):
    q = lambda p: json.dumps(str(p))
    path.write_text(f'[[disk]]\nid = "disk0"\npath = {q(disk)}\n' + (f'backing = {q(backing)}\n' if backing else ''))
    return path


def refusal_errors(r):
    """A refused launch: nonzero exit before any boot, the expected message, the overlay's bytes unchanged."""
    errors = [] if r['rc'] not in (0, None) and not r['timeout'] else [f'not refused (exit {r["rc"]}, timeout {r["timeout"]})']
    if not all(m in r['stderr'] for m in r['messages']): errors.append(f'stderr lacks {r["messages"]}: {r["stderr"][-300:]!r}')
    if r['booted']: errors.append('the guest booted')
    if not r['unchanged']: errors.append('the overlay changed')
    return errors


def overlay_refusals(args, work, image, base):
    """The overlay without its base (--disk alone), and a qemu-img overlay whose config names another file as its backing."""
    other = work / 'other.raw'; checked(['cp', '-c', base, other], work / 'clone-other.log')
    made = work / 'made.qcow2'
    subprocess.run([need_qemu_img(), 'create', '-q', '-f', 'qcow2', '-b', str(base.resolve()), '-F', 'raw', str(made)], check=True, capture_output=True)
    named = str(base.resolve())
    results = []
    for name, disk, config, messages in (
            ('no-backing', image, None, [f'add backing = "{named}"', f'--set disk.disk0.backing={named}']),
            ('mismatch', made, overlay_config(work / 'mismatch.toml', made, other), [f'is based on {named}, not on the backing {other}'])):
        launch_dir = work / name; launch_dir.mkdir()
        before = digest(disk)
        rc, timeout, seconds = launch(args, 'gpt', 'verify', disk, launch_dir, 0, config)
        serial = launch_dir / 'serial.log'
        r = {'name': name, 'rc': rc, 'timeout': timeout, 'stderr': (launch_dir / 'stderr.log').read_text(errors='replace'), 'messages': messages,
             'booted': serial.exists() and 'COV-BEGIN' in serial.read_text(errors='replace'), 'unchanged': digest(disk) == before}
        r['errors'] = refusal_errors(r); r['seconds'] = round(seconds, 1)
        write_json(launch_dir / 'result.json', r); results.append(r)
        print(json.dumps({'case': 'overlay', 'refusal': name, 'errors': r['errors']}), flush=True)
    for leftover in (other, made): leftover.unlink(missing_ok=True)
    return results


def launch(args, case, phase, image, work, limit, config=None):
    """One launch; CONFIG (a config file listing the disk) replaces --disk IMAGE and makes the transport explicit."""
    disk = ['--config', str(config), '--virtio-transport', args.transport] if config else ['--disk', str(image)]
    command = [str(args.natemu), '--runtime', str(args.runtime), '--fexc', str(args.fexc), '--kernel', str(args.prepared / 'linux'),
               '--initrd', str(args.prepared / 'initramfs.cpio')] + disk + ['--vcpus', '2', '--ram', '1G',
               '--compile-cache', 'off', '--serial', str(work / 'serial.log'), '--cmdline',
               f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr cov_case={case} cov_phase={phase}' + EXTRA.get(case, '')]
    if args.transport == 'pci' and not config: command += ['--virtio-transport', 'pci']
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


MULTI_SIZES = (8 * MiB, 16 * MiB, 24 * MiB)
# (name, [disk transport, other-devices transport]); the file's `machine.transport` has disk, nic and rng.
MULTI_LAUNCHES = (('mmio', 'mmio', 'mmio'), ('pci', 'pci', 'pci'), ('disks-mmio-rng-pci', 'mmio', 'pci'), ('disks-pci-rng-mmio', 'pci', 'mmio'))


def multidisk_images(work):
    """Three sparse images, each with a distinct first 4 KiB: (paths, the host facts the guest must report)."""
    paths = []
    for i, size in enumerate(MULTI_SIZES):
        path = work / f'd{i}.raw'
        with path.open('xb') as f: f.truncate(size); f.write(pattern(b'natemu-multi-%d' % i, 4096))
        paths.append(path)
    return paths


def multidisk_facts(paths):
    """What the guest must see per disk (size, GET_ID serial: device and inode as raw.rs formats them, first 4 KiB)."""
    out = []
    for path in paths:
        st = path.stat()
        out.append((st.st_size, f'{st.st_dev & 0xffffffff:08x}{st.st_ino & 0xffffffffffff:012x}', sha(path.read_bytes()[:4096])))
    return out


def multidisk_written(paths):
    """Each image holds its own 512-byte block at 4096 and nothing else changed in the first 16 KiB."""
    a, b, _ = fixtures()
    want = (a[:512], b[:512], a[512:1024])
    for i, (path, block) in enumerate(zip(paths, want)):
        data = path.read_bytes()[:16384]
        if data[:4096] != pattern(b'natemu-multi-%d' % i, 4096) or data[4096:4608] != block or any(data[4608:]): return False
    return True


def multidisk_launch(args, name, disk_t, other_t, work):
    """One three-disk launch from a config file (the only way to list several disks); returns the result entry."""
    paths = multidisk_images(work)
    facts = multidisk_facts(paths)
    q = lambda p: json.dumps(str(p))
    cmdline = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr cov_case=multidisk cov_phase=write'
    config = work / 'vm.toml'
    config.write_text(f"""[machine]
ram = "1G"
vcpus = 2
transport = {{ disk = "{disk_t}", nic = "{other_t}", rng = "{other_t}" }}
[serial]
output = {q(work / 'serial.log')}
[engine]
fexc = {q(args.fexc)}
runtime = {q(args.runtime)}
compile_cache = "off"
[boot.kernel]
image = {q(args.prepared / 'linux')}
initrd = {q(args.prepared / 'initramfs.cpio')}
cmdline = {q(cmdline)}
""" + ''.join(f'[[disk]]\nid = "d{i}"\npath = {q(p)}\n' for i, p in enumerate(paths)) + '[[rng]]\nid = "rng0"\n')
    command = [str(args.natemu), '--config', str(config)]
    write_json(work / 'command.json', {'command': command})
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    started, timeout = time.monotonic(), False
    with (work / 'stdout.log').open('wb') as out, (work / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
        try: process.wait(timeout=120)
        except subprocess.TimeoutExpired: timeout = True; stop_group(process)
        except BaseException: stop_group(process); raise
    seconds = time.monotonic() - started
    text = (work / 'serial.log').read_text(errors='replace').replace('\r', '') if (work / 'serial.log').exists() else ''
    stderr = (work / 'stderr.log').read_text(errors='replace')
    host = {'rc': process.returncode, 'timeout': timeout, 'stop': last_stop(stderr),
            'backend_errors': [l for l in stderr.splitlines() if l.startswith('virtio-block: ')],
            'changed': True, 'fsck': [], 'table': True, 'exact': None,
            'disks': facts, 'transport': disk_t, 'written': multidisk_written(paths)}
    errors = judge('multidisk', 'write', text, host, expected_values(), False)
    speed = re.search(r'^COV-SPEED t0=([\d.]+) t1=([\d.]+)$', text, re.M)
    mibs = round(384 / max(float(speed[2]) - float(speed[1]), 0.01), 1) if speed else None
    for path in paths: path.unlink()
    entry = {'phase': name, 'seconds': round(seconds, 1), 'read_mib_s': mibs, 'rc': process.returncode, 'stop': host['stop'], 'errors': errors}
    print(json.dumps({'case': 'multidisk', **entry}), flush=True)
    return entry


def run_multidisk(args):
    work = args.out / 'multidisk'; work.mkdir()
    result = {'case': 'multidisk', 'launches': []}
    for name, disk_t, other_t in MULTI_LAUNCHES:
        launch_dir = work / name; launch_dir.mkdir()
        result['launches'].append(multidisk_launch(args, name, disk_t, other_t, launch_dir))
    result['pass'] = not any(l['errors'] for l in result['launches'])
    write_json(work / 'result.json', result)
    return result


# The readonly case. Layouts: disks as (role, guest device) in list order; roles rw = writable raw, iso, ext = read-only raw ext4.
RO_LAYOUTS = {'rw-iso-ext': (('rw', 'vda'), ('iso', 'vdb'), ('ext', 'vdc')), 'iso-rw': (('iso', 'vda'), ('rw', 'vdb'))}
RO_LAUNCHES = (('mmio', 'mmio', 'rw-iso-ext'), ('pci', 'pci', 'rw-iso-ext'), ('mmio-iso-first', 'mmio', 'iso-rw'), ('pci-iso-first', 'pci', 'iso-rw'))
RO_RW_SIZE = 8 * MiB


def readonly_iso(work):
    """An ISO (hdiutil makehybrid) with one file: (path, the file's sha256)."""
    src = work / 'iso-src'; src.mkdir()
    data = pattern(b'natemu-iso', 64 << 10)
    (src / 'PAYLOAD.BIN').write_bytes(data)
    checked(['hdiutil', 'makehybrid', '-iso', '-joliet', '-o', work / 'ro.iso', src], work / 'hdiutil.log')
    shutil.rmtree(src)
    return work / 'ro.iso', sha(data)


def readonly_config(args, work, name, transport, disks, cmdline):
    """A config file with DISKS ((path, readonly) in list order); returns the command line."""
    q = lambda p: json.dumps(str(p))
    config = work / f'{name}.toml'
    config.write_text(f"""[machine]
ram = "1G"
vcpus = 2
transport = {{ disk = "{transport}", nic = "{transport}", rng = "{transport}" }}
[serial]
output = {q(work / (name + '-serial.log'))}
[engine]
fexc = {q(args.fexc)}
runtime = {q(args.runtime)}
compile_cache = "off"
[boot.kernel]
image = {q(args.prepared / 'linux')}
initrd = {q(args.prepared / 'initramfs.cpio')}
cmdline = {q(cmdline)}
""" + ''.join(f'[[disk]]\nid = "d{i}"\npath = {q(p)}\nreadonly = {"true" if ro else "false"}\n' for i, (p, ro) in enumerate(disks)))
    return [str(args.natemu), '--config', str(config)]


def readonly_judge(work, name, phase, process, timeout, rw, iso, iso_sha, ext, layout, before):
    """(errors, host facts) of one finished launch."""
    log = work / (name + '-serial.log')
    text = log.read_text(errors='replace').replace('\r', '') if log.exists() else ''
    stderr = (work / (name + '-stderr.log')).read_text(errors='replace')
    a = fixtures()[0]
    data = rw.read_bytes()[:16384]
    size = {'rw': RO_RW_SIZE, 'iso': iso.stat().st_size, 'ext': ext.stat().st_size if ext else 0}
    host = {'rc': process.returncode, 'timeout': timeout, 'stop': last_stop(stderr),
            'backend_errors': [l for l in stderr.splitlines() if l.startswith('virtio-block: ')],
            'changed': phase == 'write', 'fsck': [], 'table': True, 'exact': None,
            'layout': [(dev, role != 'rw', size[role] // SECTOR) for role, dev in sorted(layout, key=lambda d: ('rw', 'iso', 'ext').index(d[0]))],  # the guest prints rw, iso, ext
            'iso_sha': iso_sha, 'has_ext': ext is not None,
            'written': data[8 * 512:9 * 512] == a[:512] and not any(data[:4096]) and not any(data[4608:]),
            'ro_unchanged': before == (digest(iso), digest(ext) if ext else None)}
    return judge('readonly', phase, text, host, expected_values(), False), host


def readonly_launch(args, name, transport, layout_name, work):
    iso, iso_sha = readonly_iso(work)
    layout = RO_LAYOUTS[layout_name]
    rw = work / 'rw.raw'
    with rw.open('xb') as f: f.truncate(RO_RW_SIZE)
    ext = None
    if any(role == 'ext' for role, _ in layout):
        ext = work / 'ext.raw'; checked(['cp', '-c', args.prepared / 'data.raw', ext], work / 'clone.log')
    paths = {'rw': (rw, False), 'iso': (iso, True), 'ext': (ext, True)}
    dev = {role: d for role, d in layout}
    cmdline = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr cov_case=readonly cov_phase=write cov_rw=%s cov_iso=%s' % (dev['rw'], dev['iso'])
    if ext: cmdline += ' cov_ext=' + dev['ext']
    before = (digest(iso), digest(ext) if ext else None)
    command = readonly_config(args, work, name, transport, [paths[role] for role, _ in layout], cmdline)
    started, timeout = time.monotonic(), False
    with (work / (name + '-stdout.log')).open('wb') as out, (work / (name + '-stderr.log')).open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try: process.wait(timeout=120)
        except subprocess.TimeoutExpired: timeout = True; stop_group(process)
        except BaseException: stop_group(process); raise
    errors, host = readonly_judge(work, name, 'write', process, timeout, rw, iso, iso_sha, ext, layout, before)
    for path in (rw, iso, ext):
        if path: path.unlink()
    entry = {'phase': name, 'seconds': round(time.monotonic() - started, 1), 'rc': process.returncode, 'stop': host['stop'], 'errors': errors}
    print(json.dumps({'case': 'readonly', **entry}), flush=True)
    return entry


def readonly_share(args, work):
    """Two VMs at once on one read-only ISO under the shared VM lock; while both hold it, a writable lock and a writable launch are refused."""
    iso, iso_sha = readonly_iso(work)
    layout = RO_LAYOUTS['iso-rw']
    before = (digest(iso), None)
    cmdline = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr cov_case=readonly cov_phase=share cov_rw=vdb cov_iso=vda'
    vms, errors, started = [], [], time.monotonic()
    def holds(n):    # the guest prints a COV-HOLD line every second for 25 s while it has the ISO
        log = work / f'vm{n}-serial.log'
        return log.read_text(errors='replace').count('COV-HOLD ') if log.exists() else 0
    try:
        for n in (1, 2):
            rw = work / f'rw{n}.raw'
            with rw.open('xb') as f: f.truncate(RO_RW_SIZE)
            command = readonly_config(args, work, f'vm{n}', 'mmio', [(iso, True), (rw, False)], cmdline)
            with (work / f'vm{n}-stdout.log').open('wb') as out, (work / f'vm{n}-stderr.log').open('wb') as err:
                vms.append((subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True), rw))
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline and not (holds(1) and holds(2)) and all(p.poll() is None for p, _ in vms): time.sleep(0.5)
        if not (holds(1) and holds(2)): errors.append('both guests did not reach the hold point')
        else:
            seen = (holds(1), holds(2))
            with iso.open('rb') as f:    # the advisory lock the VMs use (flock): a writer is refused, another reader is not
                try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); errors.append('an exclusive lock on the shared ISO succeeded')
                except BlockingIOError: pass
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            command = readonly_config(args, work, 'writer', 'mmio', [(iso, False)], cmdline)
            writer = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try: _, werr = writer.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                stop_group(writer); errors.append('a writable launch on the shared ISO did not exit (it was not refused by the lock)')
            else:
                if writer.returncode == 0 or b'disk lock: held by another process' not in werr:
                    errors.append(f'a writable launch on the shared ISO was not refused by the lock (rc={writer.returncode}): {werr[-300:]!r}')
            # Both VMs held the lock at once: each is still running and prints a further COV-HOLD line after the tests above.
            end = time.monotonic() + 15
            while time.monotonic() < end and not (holds(1) > seen[0] and holds(2) > seen[1]): time.sleep(0.5)
            if not (holds(1) > seen[0] and holds(2) > seen[1] and all(p.poll() is None for p, _ in vms)): errors.append('the two VMs did not both keep the shared ISO while the lock was tested')
        for n, (process, rw) in enumerate(vms, 1):
            timeout = False
            try: process.wait(timeout=120)
            except subprocess.TimeoutExpired: timeout = True; stop_group(process)
            e, _ = readonly_judge(work, f'vm{n}', 'share', process, timeout, rw, iso, iso_sha, None, layout, before)
            errors += [f'vm{n}: {m}' for m in e]
    finally:
        for process, rw in vms:
            if process.poll() is None: stop_group(process)
            rw.unlink(missing_ok=True)
        iso.unlink(missing_ok=True)
    entry = {'phase': 'share', 'seconds': round(time.monotonic() - started, 1), 'errors': errors}
    print(json.dumps({'case': 'readonly', **entry}), flush=True)
    return entry


def run_readonly(args):
    work = args.out / 'readonly'; work.mkdir()
    result = {'case': 'readonly', 'launches': []}
    for name, transport, layout in RO_LAUNCHES:
        launch_dir = work / name; launch_dir.mkdir()
        result['launches'].append(readonly_launch(args, name, transport, layout, launch_dir))
    share = work / 'share'; share.mkdir()
    result['launches'].append(readonly_share(args, share))
    result['pass'] = not any(l['errors'] for l in result['launches'])
    write_json(work / 'result.json', result)
    return result


# The sharedintx case: (name, disks, vCPUs, phase, extra kernel parameters). Disk k is at slot 1 (k = 0) or 3 + k.
SHARED_LAUNCHES = (('nomsi-8-n1', 8, 1, 'load', ' pci=nomsi'), ('nomsi-8-n4', 8, 4, 'load', ' pci=nomsi'),
                   ('nomsi-29-n4', 29, 4, 'load', ' pci=nomsi'), ('stale-8-n2', 8, 2, 'stale', ''))
SHARED_SIZE = 16 * MiB
SHARED_SECONDS = 300


def vd_name(k):
    """Linux's name of the k-th virtio disk (virtblk_name_format: vda..vdz, vdaa..)."""
    return 'vd' + (chr(96 + k // 26) if k >= 26 else '') + chr(97 + k % 26)


def sharedintx_judge(text, host, phase, n):
    """Errors of one sharedintx launch."""
    errors = []
    def require(ok, message):
        if not ok: errors.append(message)
    def one(regex):
        found = re.findall(regex, text, re.M)
        return found[0] if len(found) == 1 else None
    require(one(r'^COV-BEGIN case=(\S+) phase=(\S+) cpus=\d+ kernel=(\S+)$') == ('sharedintx', phase, KVER), 'wrong/missing begin')
    require(not re.search(r'COV-FAIL|COV-IOFAIL|BUG:|Oops:|Kernel panic|Call Trace:|Buffer I/O error', text), 'guest, kernel or I/O error')
    require(not host['timeout'], 'VM timed out')
    require(host['stop'] == POWEROFF[0] and host['rc'] == POWEROFF[1], f"stop {host['stop']!r} rc={host['rc']}")
    require(not host['backend_errors'], f"host call failures {host['backend_errors']}")
    require(one(r'^COV-PASS case=(\S+) phase=(\S+)$') == ('sharedintx', phase), 'guest did not finish')
    disks = re.findall(r'^COV-DISK (\S+) dev=(\S+)$', text, re.M)
    want = [(vd_name(k), 1 if k == 0 else 3 + k) for k in range(n)]
    require(len(disks) == n and all(d == name and re.search(rf'/0000:00:{slot:02x}\.0/virtio\d+$', dev) for (d, dev), (name, slot) in zip(disks, want)),
            f'disks not at their slots in list order: {disks}')
    require(one(r'^COV-NDISKS (\d+)$') == str(n), 'a disk is missing or extra')
    summary = one(r'^COV-SHARED-ERRORS io=(\d+) timeouts=(\d+) unhandled=(\d+)$')
    require(summary == ('0', '0', '0'), f'I/O errors, timeouts or an unhandled (disabled) line: {summary}')
    irq = re.findall(r'^COV-IRQ (\d+):.*?IO-APIC\s+(\d+)-fasteoi\s+(.*)$', text, re.M)
    shared = {line for _, line, names in irq if len(re.findall(r'virtio\d+', names)) > 1}
    if phase == 'load':
        require(not re.search(r'^COV-IRQ .*PCI-MSI', text, re.M), 'an MSI under pci=nomsi')
        # Two disks on 16, a disk and the entropy device on 18 (the NIC on 17 has no driver in this initramfs).
        require({'16', '18'} <= shared, f'lines 16 and 18 not shared: shared lines {sorted(shared)}')
        require(re.search(r'^COV-LOAD t0=[\d.]+ t1=[\d.]+$', text, re.M) is not None, 'load did not finish')
        require(host['written'], 'an image does not hold its three copies of the guest pattern')
    else:
        steps = dict(re.findall(r'^COV-STALE step=(\S+) (.*)$', text, re.M))
        require(list(steps) == ['base', 'unbound', 'rebound', 'msix-off', 'msix-off-later'], f'stale steps {list(steps)}')
        for step, line in steps.items():
            for dev, rc, sec in re.findall(r'(\w)=\[rc=(\d+) s=([\d.]+)\]', line):
                require(rc == '0' and float(sec) < 5, f'{step}: disk {dev} rc={rc} after {sec} s')
        require(one(r'^COV-STALE-DEVS a=(\S+) b=(\S+) irq_a=16$') is not None, 'slot 1 not on INTx line 16')
        require(re.search(r'^COV-STALE-DMESG', text, re.M) is None, 'Linux reported or disabled an unhandled line')
    return errors


def sharedintx_launch(args, name, n, vcpus, phase, extra, work):
    """One launch, every device on PCI from a config file; returns the result entry."""
    paths = []
    for k in range(n):
        path = work / f'd{k}.raw'
        with path.open('xb') as f: f.truncate(SHARED_SIZE)
        paths.append(path)
    names = [vd_name(k) for k in range(n)]
    q = lambda p: json.dumps(str(p))
    cmdline = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr cov_case=sharedintx cov_phase={phase} cov_disks={",".join(names)}' + extra
    config = work / 'vm.toml'
    config.write_text(f"""[machine]
ram = "1G"
vcpus = {vcpus}
transport = {{ disk = "pci", nic = "pci", rng = "pci" }}
[serial]
output = {q(work / 'serial.log')}
[engine]
fexc = {q(args.fexc)}
runtime = {q(args.runtime)}
compile_cache = "off"
[boot.kernel]
image = {q(args.prepared / 'linux')}
initrd = {q(args.prepared / 'initramfs.cpio')}
cmdline = {q(cmdline)}
""" + ''.join(f'[[disk]]\nid = "d{k}"\npath = {q(p)}\n' for k, p in enumerate(paths)) + '[[nic]]\nid = "net0"\n[[rng]]\nid = "rng0"\n')
    command = [str(args.natemu), '--config', str(config)]
    write_json(work / 'command.json', {'command': command})
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    started, timeout = time.monotonic(), False
    with (work / 'stdout.log').open('wb') as out, (work / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
        try: process.wait(timeout=SHARED_SECONDS)
        except subprocess.TimeoutExpired: timeout = True; stop_group(process)
        except BaseException: stop_group(process); raise
    seconds = time.monotonic() - started
    text = (work / 'serial.log').read_text(errors='replace').replace('\r', '') if (work / 'serial.log').exists() else ''
    stderr = (work / 'stderr.log').read_text(errors='replace')
    pats = dict(re.findall(r'^COV-PAT (\S+) (\S+)$', text, re.M))
    def written():
        for d, path in zip(names, paths):
            data = path.read_bytes()
            if d not in pats or any(sha(data[i * 4 * MiB:(i + 1) * 4 * MiB]) != pats[d] for i in range(3)) or any(data[12 * MiB:]): return False
        return True
    host = {'rc': process.returncode, 'timeout': timeout, 'stop': last_stop(stderr),
            'backend_errors': [l for l in stderr.splitlines() if l.startswith('virtio-block: ')], 'written': phase == 'load' and written()}
    errors = sharedintx_judge(text, host, phase, n)
    load = re.search(r'^COV-LOAD t0=([\d.]+) t1=([\d.]+)$', text, re.M)
    for path in paths: path.unlink()
    entry = {'phase': name, 'seconds': round(seconds, 1), 'load_seconds': round(float(load[2]) - float(load[1]), 1) if load else None,
             'stale': re.findall(r'^COV-STALE.*$', text, re.M), 'rc': process.returncode, 'stop': host['stop'], 'errors': errors}
    print(json.dumps({'case': 'sharedintx', **entry}), flush=True)
    return entry


def run_sharedintx(args):
    work = args.out / 'sharedintx'; work.mkdir()
    result = {'case': 'sharedintx', 'launches': []}
    for name, n, vcpus, phase, extra in SHARED_LAUNCHES:
        launch_dir = work / name; launch_dir.mkdir()
        result['launches'].append(sharedintx_launch(args, name, n, vcpus, phase, extra, launch_dir))
    result['pass'] = not any(l['errors'] for l in result['launches'])
    write_json(work / 'result.json', result)
    return result


def run_case(args, case):
    """Both launches of CASE (a control: of the case it controls); returns the result."""
    if case == 'multidisk': return run_multidisk(args)
    if case == 'sharedintx': return run_sharedintx(args)
    if case == 'readonly': return run_readonly(args)
    qcow = case in QCOW2_CASES
    if qcow: need_qemu_img(f'--cases {case}')
    base = QCOW2_CASES.get(case) or case.removesuffix('-control')
    work = args.out / case; work.mkdir()
    volume = None
    if base == 'host-full' and case == base:
        volume = work / 'volume'
        checked(['hdiutil', 'create', '-size', '32m', '-fs', 'APFS', '-volname', 'natemucov', '-type', 'SPARSE', work / 'volume.sparseimage'], work / 'hdiutil-create.log')
        volume.mkdir()
        checked(['hdiutil', 'attach', '-nobrowse', '-mountpoint', volume, work / 'volume.sparseimage'], work / 'hdiutil-attach.log')
    result = {'case': case, 'launches': []}
    try:
        image = (volume or work) / ('disk.qcow2' if qcow else 'disk.raw'); scratch = work / 'view.raw'
        overlay = work / 'base.raw' if case == 'overlay' else None    # the overlay's base; natemu creates the overlay
        if overlay: checked(['cp', '-c', args.prepared / IMAGE[base], overlay], work / 'clone.log'); base_sha = digest(overlay)
        elif volume: sparse_copy(args.prepared / IMAGE[base], image)
        elif qcow: qcow2_convert(args.prepared / IMAGE[base], image, 'qcow2', 'preallocation=metadata')
        else: checked(['cp', '-c', args.prepared / IMAGE[base], image], work / 'clone.log')
        total = os.path.getsize(args.prepared / IMAGE[base]) // SECTOR
        with raw_of(overlay or image, qcow and not overlay, scratch) as raw: table = table_bytes(raw, total)
        start_clusters = qcow2_problems(image)[1].get('allocated-clusters') if qcow and not overlay else None
        expect = expected_values()
        for phase in ('write', 'verify'):
            launch_dir = work / phase; launch_dir.mkdir()
            if overlay and not image.exists(): before = base_sha    # the new overlay reads as its base
            else:
                with raw_of(image, qcow, scratch) as raw: before = digest(raw)
            limit = LIMIT if case == 'host-limit' and phase == 'write' else 0
            config = overlay_config(launch_dir / 'vm.toml', image, overlay) if overlay else None
            rc, timeout, seconds = launch(args, base, phase, image, launch_dir, limit, config)
            text = (launch_dir / 'serial.log').read_text(errors='replace').replace('\r', '') if (launch_dir / 'serial.log').exists() else ''
            stderr = (launch_dir / 'stderr.log').read_text(errors='replace')
            if overlay and not image.exists():
                errors = ['natemu did not create the overlay']
                entry = {'phase': phase, 'rc': rc, 'stderr': (launch_dir / 'stderr.log').read_text(errors='replace')[-500:], 'errors': errors}
                result['launches'].append(entry); print(json.dumps({'case': case, **entry}), flush=True); break
            with raw_of(image, qcow, scratch) as raw:    # the raw bytes: the image itself, or the qcow2's temporary conversion
                after = digest(raw)
                host = {'rc': rc, 'timeout': timeout, 'stop': last_stop(stderr),
                        'backend_errors': [l for l in stderr.splitlines() if l.startswith('virtio-block: ')],
                        'changed': after != before, 'fsck': fsck(args.e2fsck, raw, ext4_offsets(base), launch_dir / 'fsck'),
                        'table': table_bytes(raw, total) == table,
                        'exact': raw.read_bytes() == limit_image() if base == 'host-limit' and phase == 'write' else trim_exact(raw) if base == 'trim' and phase == 'write' else None}
                q = qcow2_host(image, raw, base, phase, start_clusters) if qcow else None
            errors = judge(base, phase, text, host, expect, args.transport == 'pci') + (qcow2_errors(q, base, phase) if qcow else [])
            if qcow: host['qcow2'] = q
            if overlay: host['overlay'] = o = overlay_host(image, overlay, base_sha); errors += overlay_errors(o)
            entry = {'phase': phase, 'seconds': round(seconds, 1), 'disk_before': before, 'disk_after': after, **host, 'errors': errors}
            if base == 'host-full' and volume and phase == 'write':
                entry['volume_free_kib'] = shutil.disk_usage(volume).free // 1024
            write_json(launch_dir / 'result.json', entry); result['launches'].append(entry)
            print(json.dumps({'case': case, **entry}), flush=True)
            if errors: break
        if overlay and len(result['launches']) == 2 and not any(l['errors'] for l in result['launches']):
            result['refusals'] = overlay_refusals(args, work, image, overlay)
            if any(r['errors'] for r in result['refusals']): result['launches'][-1]['errors'] += ['a refusal launch failed']
    finally:
        if volume:
            subprocess.run(['hdiutil', 'detach', str(volume)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            (work / 'volume.sparseimage').unlink(missing_ok=True)
        for leftover in (work / 'disk.raw', work / 'disk.qcow2', work / 'view.raw', work / 'base.raw', work / 'other.raw', work / 'made.qcow2'): leftover.unlink(missing_ok=True)
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
    inputs = prepare(args.out / 'prepared', args.inputs, args.transport, args.kernel_deb if 'readonly' in args.cases else None)
    args.prepared, args.e2fsck = args.out / 'prepared', Path(inputs['e2fsck'])
    write_json(args.out / 'manifest.json', {'cases': args.cases, 'seconds_per_vm': 120, 'source_root': str(ROOT),
               'artifacts': {str(p): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))}, 'inputs': inputs})
    results = []
    with VM_LOCK.open('a') as lock:
        if not os.environ.get('NATEMU_VM_LOCK_HELD'):   # run_suite holds it for its steps (a serial step's exclusive lock would block this)
            fcntl.flock(lock, fcntl.LOCK_SH)    # correctness only: shared with other correctness runs (tools/vmlock.py)
        for case in args.cases:
            results.append(run_case(args, case)); write_json(args.out / 'results.json', results)
            print(f"{case}: {'PASS' if results[-1]['pass'] else 'FAIL'}", flush=True)
    for name in ('gpt.raw', 'mbr.raw', 'hostfull.raw', 'data.raw', 'limit.raw'): (args.prepared / name).unlink()
    return 0 if all(r['pass'] for r in results) else 1


def self_check():
    """Synthetic passing launches for every case and phase, then mutations the judge must reject. No VM."""
    expect, controls = expected_values(), 0
    assert last_stop('natemu: cpu: x\nnatemu: Power(1)\nnatemu: code cache: pool on, disk off\n') == 'natemu: Power(1)'
    assert last_stop('natemu: cpu: x\nnatemu: disk lock: held\n') == 'natemu: disk lock: held', 'errors shaped topic: value are stops'
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
        elif case == 'trim':
            t += ['COV-TRIM fstrim_rc=0 discard_rc=0 zeroout_rc=0 discard_max=2147483648 zeroes_max=2147483648'] if phase == 'write' else []
            t += [f'COV-DATA a={expect["a"]} b=gone']
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
    # readonly and multidisk: the judge rejects a missing read-only flag, a writable result, a changed host image, a missing or
    # reordered disk, a wrong transport or slot, and every changed marker value.
    count = [0]
    def tail(case): return ['COV-ERRORS ioerr_logs=0 fs_errors=0', 'COV-DMESG-BEGIN', 'COV-DMESG-END', f'COV-PASS case={case} phase=write']
    def judged(case, lines, host): return judge(case, 'write', '\n'.join(lines) + '\n', host, expect)
    def rejected(case, lines, host): assert judged(case, lines, host), (case, lines, host); count[0] += 1
    def mutate(lines, host, case, prefixes, bad_hosts):
        for i, line in enumerate(lines):
            if line.split()[0] not in prefixes: continue
            for field in line.split()[1:]:
                key, _, value = field.partition('=')
                if key == 'dev': continue    # the virtio instance number is free; the transport and the slots are judged below
                new = {'0': '1', '1': '0'}.get(value, '0' if value.isdigit() else value[:-1] + ('1' if value.endswith('0') else '0'))
                rejected(case, lines[:i] + [line.replace(field, f'{key}={new}')] + lines[i + 1:], host)
        for k, v in bad_hosts: rejected(case, lines, dict(host, **{k: v}))
    base = {'rc': 0, 'timeout': False, 'stop': POWEROFF[0], 'changed': True, 'fsck': [], 'table': True, 'exact': None, 'backend_errors': []}
    begin = lambda case: f'COV-BEGIN case={case} phase=write cpus=2 kernel={KVER}'
    rw = 'COV-RO vda ro=0 size=16384 cache=write_back discard=2147483648 zeroes=2147483648'
    iso, ext = 'COV-RO vdb ro=1 size=1800 cache=write_through discard=0 zeroes=0', 'COV-RO vdc ro=1 size=131072 cache=write_through discard=0 zeroes=0'
    refusal = 'setrw_rc=0 ro_after=1 dd_rc=1 write_rc=1'
    good = [begin('readonly'), rw, iso, ext, f'COV-ISO files=1 sum={"ab" * 32} {refusal}', f'COV-EXT ls=lost+found {refusal}', 'COV-RWERR n=0'] + tail('readonly')
    host = dict(base, layout=[('vda', False, 16384), ('vdb', True, 1800), ('vdc', True, 131072)], iso_sha='ab' * 32, has_ext=True, written=True, ro_unchanged=True)
    assert not judged('readonly', good, host), judged('readonly', good, host)
    mutate(good, host, 'readonly', ('COV-RO', 'COV-ISO', 'COV-EXT', 'COV-RWERR'),
           [('written', False), ('ro_unchanged', False), ('iso_sha', 'cd' * 32),
            ('layout', [('vda', False, 16384), ('vdb', False, 1800), ('vdc', True, 131072)]), ('layout', [('vda', True, 16384), ('vdb', True, 1800), ('vdc', True, 131072)]),
            ('layout', [('vda', False, 16384), ('vdc', True, 131072), ('vdb', True, 1800)])])
    for bad in (good[:2] + good[3:], good[:2] + [good[3], good[2]] + good[4:], [l for l in good if not l.startswith('COV-EXT')],
                [l.replace('setrw_rc=0', 'setrw_rc=127') for l in good], [l.replace('dd_rc=1', 'dd_rc=127') for l in good]):
        rejected('readonly', bad, host)
    facts = [(size, f'{i:08x}{i + 1:012x}', sha(bytes([i]))) for i, size in enumerate((8 * MiB, 16 * MiB, 24 * MiB))]
    def multi(slots):
        dev = (lambda n: '/LNRO0005:0%d/virtio%d' % (n, n)) if slots is None else (lambda n: '/pci0000:00/0000:00:%02x.0/virtio%d' % (slots[n], n))
        return [begin('multidisk')] + [f'COV-DISK vd{c} size={f[0] // SECTOR} serial={f[1]} head={f[2]} dev={dev(n)}' for n, (c, f) in enumerate(zip('abc', facts))] + ['COV-NODISK vdd'] + tail('multidisk')
    for transport, lines in (('mmio', multi(None)), ('pci', multi((1, 4, 5)))):
        host = dict(base, disks=facts, transport=transport, written=True)
        assert not judged('multidisk', lines, host), (transport, judged('multidisk', lines, host))
        mutate(lines, host, 'multidisk', ('COV-DISK',), [('written', False), ('disks', facts[1:] + facts[:1]), ('disks', facts[:2]), ('transport', 'pci' if transport == 'mmio' else 'mmio')])
        rejected('multidisk', lines[:2] + lines[3:], host)
        rejected('multidisk', lines[:2] + [lines[3], lines[2]] + lines[4:], host)
    rejected('multidisk', multi((1, 5, 4)), dict(base, disks=facts, transport='pci', written=True))
    # sharedintx: the judge rejects a missing, extra or misplaced disk, an MSI under pci=nomsi, an unshared line, I/O
    # failures, errors, a disabled line, an unfinished load, a bad image, a slow or failed stale step, a missing step.
    def shared(phase, n):
        lines = [f'COV-BEGIN case=sharedintx phase={phase} cpus=2 kernel={KVER}']
        lines += [f'COV-DISK {vd_name(k)} dev=/sys/devices/pci0000:00/0000:00:{1 if k == 0 else 3 + k:02x}.0/virtio{k}' for k in range(n)]
        lines += [f'COV-NDISKS {n}', 'COV-IRQ 16:        6   IO-APIC  16-fasteoi   virtio0, virtio3',
                  'COV-IRQ 18:        5   IO-APIC  18-fasteoi   virtio5, virtio2', 'COV-IRQ 19:        3   IO-APIC  19-fasteoi   virtio6']
        if phase == 'load': lines += ['COV-LOAD t0=3.05 t1=6.04']
        else:
            lines += ['COV-STALE-DEVS a=vda b=vdb irq_a=16', 'COV-STALE step=base a=[rc=0 s=0.05] b=[rc=0 s=0.03]',
                      'COV-STALE step=unbound a=[rc=0 s=0.03]', 'COV-STALE step=rebound a=[rc=0 s=0.03] b=[rc=0 s=0.02]',
                      'COV-STALE step=msix-off a=[rc=0 s=0.05]', 'COV-STALE step=msix-off-later a=[rc=0 s=0.05]']
        return lines + ['COV-SHARED-ERRORS io=0 timeouts=0 unhandled=0', f'COV-PASS case=sharedintx phase={phase}']
    def shared_rejected(phase, n, lines, host):
        assert sharedintx_judge('\n'.join(lines) + '\n', host, phase, n), (phase, lines, host); count[0] += 1
    for phase in ('load', 'stale'):
        n, lines = 8, shared(phase, 8)
        host = {'rc': 0, 'timeout': False, 'stop': POWEROFF[0], 'backend_errors': [], 'written': phase == 'load'}
        assert not sharedintx_judge('\n'.join(lines) + '\n', host, phase, n), sharedintx_judge('\n'.join(lines) + '\n', host, phase, n)
        for k, v in (('rc', 1), ('timeout', True), ('stop', 'natemu: Cancelled'), ('backend_errors', ['virtio-block: x'])):
            shared_rejected(phase, n, lines, dict(host, **{k: v}))
        shared_rejected(phase, 9, lines, host)
        for i, line in enumerate(lines):
            for old, new in (('io=0', 'io=1'), ('timeouts=0', 'timeouts=2'), ('unhandled=0', 'unhandled=1'), ('COV-NDISKS 8', 'COV-NDISKS 9'),
                             ('00:05.0', '00:06.0'), ('vdb dev', 'vdc dev'),
                             ('rc=0 s=0.05', 'rc=143 s=30.02'), ('rc=0 s=0.03', 'rc=0 s=9.10'), ('irq_a=16', 'irq_a=24'),
                             ('COV-PASS', 'COV-NOPASS'), ('COV-LOAD t0', 'COV-NOLOAD t0')):
                if old in line: shared_rejected(phase, n, lines[:i] + [line.replace(old, new, 1)] + lines[i + 1:], host)
            if line.startswith(('COV-DISK', 'COV-STALE step')): shared_rejected(phase, n, lines[:i] + lines[i + 1:], host)
        shared_rejected(phase, n, lines + ['COV-IOFAIL read-vdc-1'], host)
        shared_rejected(phase, n, lines + ['[    4.38] Call Trace:'], host)
        if phase == 'load':    # sharing is required under load only
            for old, new in (('virtio0, virtio3', 'virtio0'), ('virtio5, virtio2', 'virtio5')):
                shared_rejected(phase, n, [l.replace(old, new) for l in lines], host)
            shared_rejected(phase, n, lines, dict(host, written=False))
            shared_rejected(phase, n, lines + ['COV-IRQ 24:        3   PCI-MSIX-0000:00:01.0   1-edge      virtio0-req.0'], host)
        else:
            shared_rejected(phase, n, lines + ['COV-STALE-DMESG [    4.38] Disabling IRQ #16'], host)
    # qcow2: the host judge accepts a clean launch and rejects each finding.
    for base, phase, good in (('gpt', 'write', {'check': [], 'compare_rc': 0, 'trim': None}),
                              ('trim', 'write', {'check': [], 'compare_rc': 0, 'trim': {'discard_data': 0, 'pattern_data': 12 * QCOW_CLUSTER}, 'allocated_clusters': 803, 'start_clusters': 1024}),
                              ('trim', 'verify', {'check': [], 'compare_rc': 0, 'trim': None})):
        assert not qcow2_errors(good, base, phase), (base, phase)
        bad = [dict(good, check=['qemu-img check: leaks=1']), dict(good, compare_rc=1)]
        if good['trim'] and phase == 'write':
            bad += [dict(good, trim=None), dict(good, trim=dict(good['trim'], discard_data=QCOW_CLUSTER)), dict(good, trim=dict(good['trim'], pattern_data=13 * QCOW_CLUSTER)),
                    dict(good, trim=dict(good['trim'], pattern_data=0)), dict(good, allocated_clusters=1024)]
        for b in bad: assert qcow2_errors(b, base, phase), (base, phase, b); controls += 1
    # overlay: the host judge rejects a changed base, another backing file or format; the refusal judge an exit 0, a timeout,
    # a missing message, a boot, a changed overlay.
    good = {'base_unchanged': True, 'backing': '/x/base.raw', 'backing_format': 'raw', 'want_backing': '/x/base.raw'}
    assert not overlay_errors(good)
    for b in (dict(good, base_unchanged=False), dict(good, backing='/x/other.raw'), dict(good, backing=None), dict(good, backing_format='qcow2')):
        assert overlay_errors(b), b; controls += 1
    good = {'rc': 1, 'timeout': False, 'stderr': 'natemu: disk x: x.qcow2 is based on /b; add backing = "/b" to this disk (or --set disk.disk0.backing=/b)\n',
            'messages': ['add backing = "/b"', '--set disk.disk0.backing=/b'], 'booted': False, 'unchanged': True}
    assert not refusal_errors(good), refusal_errors(good)
    for b in (dict(good, rc=0), dict(good, rc=None, timeout=True), dict(good, stderr='natemu: disk lock: held by another process'),
              dict(good, stderr=good['stderr'].replace('disk0', 'ID')), dict(good, booted=True), dict(good, unchanged=False)):
        assert refusal_errors(b), b; controls += 1
    assert qcow2_data_bytes.__doc__ and set(QCOW2_CASES.values()) <= set(CASES)
    controls += count[0]
    pci_self_test()
    for script in (INIT, INIT_PCI): subprocess.run(['sh', '-n'], input=script, text=True, check=True)
    print(f'PASS: {len(CASES)} cases x 2 launches judged; {controls} negative judge controls; readonly/multidisk/sharedintx judges; GPT/limit-oracle/sparse-copy checks; shell syntax. No VM.')


def main():
    if sys.argv[1:] == ['self-check']: self_check(); return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fexc', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=INPUTS, help='a tools/run_virtio_block.py prepare directory')
    parser.add_argument('--transport', choices=('mmio', 'pci'), default='mmio', help='virtio transport (--virtio-transport); pci adds a guest probe and its checks')
    parser.add_argument('--kernel-deb', type=Path, default=KERNEL_DEB, help='the pinned Debian kernel package (the readonly case takes isofs.ko from it)')
    parser.add_argument('--cases', nargs='+', choices=CASES + CONTROLS + tuple(QCOW2_CASES) + ('multidisk', 'readonly', 'sharedintx'), default=list(CASES + CONTROLS))
    args = parser.parse_args()
    if {'multidisk', 'readonly', 'sharedintx'} & set(args.cases) and args.transport != 'mmio': parser.error('multidisk, readonly and sharedintx set their own transports; leave --transport at mmio')
    if len(set(args.cases)) != len(args.cases): parser.error('duplicate cases')
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
