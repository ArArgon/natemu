#!/usr/bin/env python3
"""Copy the kernel and initramfs out of a raw GPT disk image (for natemu's direct kernel boot) and print the root= option.

    python3 tools/boot_files.py DISK [OUTDIR]

Finds the partition whose /boot holds a vmlinuz-VERSION (the newest when several), writes OUTDIR/vmlinuz and
OUTDIR/initrd.img (OUTDIR defaults to DISK's directory) and prints `root=PARTUUID=...` for that partition. Assumes /boot
lives on the root filesystem, as in Debian's cloud images. Reads ext2/3/4 with Homebrew e2fsprogs' debugfs.
"""
import pathlib
import re
import struct
import subprocess
import sys
import uuid

DEBUGFS = '/opt/homebrew/opt/e2fsprogs/sbin/debugfs'
SECTOR = 512


def partitions(head):
    """(byte offset, PARTUUID) of each GPT entry in HEAD, the disk's first 34 sectors."""
    if head[SECTOR:SECTOR + 8] != b'EFI PART':
        raise ValueError('no GPT header at LBA 1')
    table, count, size = struct.unpack_from('<QII', head, SECTOR + 72)
    out = []
    for i in range(count):
        entry = head[table * SECTOR + i * size:][:size]
        if len(entry) < 40 or entry[:16] == bytes(16):
            continue
        out.append((struct.unpack_from('<Q', entry, 32)[0] * SECTOR, str(uuid.UUID(bytes_le=entry[16:32]))))
    return out


def debugfs(disk, offset, request):
    return subprocess.run([DEBUGFS, '-R', request, f'{disk}?offset={offset}'],
                          capture_output=True, text=True).stdout


def version_key(v):
    return [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', v)]


def main(disk, outdir=None):
    disk = pathlib.Path(disk).resolve()
    outdir = pathlib.Path(outdir or disk.parent)
    with open(disk, 'rb') as f:
        head = f.read(34 * SECTOR)
    for offset, partuuid in partitions(head):
        versions = re.findall(r'\bvmlinuz-(\S+)', debugfs(disk, offset, 'ls /boot'))
        if not versions:
            continue
        v = max(versions, key=version_key)
        for name in ('vmlinuz', 'initrd.img'):
            debugfs(disk, offset, f'dump /boot/{name}-{v} {outdir / name}')
            if not (outdir / name).stat().st_size:
                sys.exit(f'could not copy /boot/{name}-{v}')
        print(f'root=PARTUUID={partuuid}')
        return
    sys.exit(f'no partition of {disk} has /boot/vmlinuz-*')


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__.strip().splitlines()[2].strip())
    main(*sys.argv[1:])
