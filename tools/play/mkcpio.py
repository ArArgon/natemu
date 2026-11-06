#!/usr/bin/env python3
"""mkcpio.py ROOTDIR OUT.cpio: a deterministic newc cpio of ROOTDIR (sorted paths, uid/gid 0, mtime 0, inodes in order)
plus a /dev/console character node (5,1), which the kernel needs to open the initial console from an initramfs and
which an unprivileged user cannot create on disk. Uncompressed: the kernel copies it without decompressing."""
import os, stat, sys

def entry(out, ino, name, mode, data=b'', rdev=(0, 0)):
    name_b = name.encode() + b'\0'
    hdr = '070701' + ''.join('%08X' % v for v in (
        ino, mode, 0, 0, 1, 0, len(data), 0, 0, rdev[0], rdev[1], len(name_b), 0))
    out.write(hdr.encode() + name_b)
    out.write(b'\0' * (-(110 + len(name_b)) % 4))
    out.write(data)
    out.write(b'\0' * (-len(data) % 4))

def main(root, path):
    names = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for n in sorted(dirs + files):
            names.append(os.path.relpath(os.path.join(d, n), root))
    names = sorted(set(names) | {'dev', 'dev/console'})
    with open(path, 'wb') as out:
        for ino, rel in enumerate(names, 1):
            full = os.path.join(root, rel)
            if rel == 'dev/console':
                entry(out, ino, rel, stat.S_IFCHR | 0o600, rdev=(5, 1)); continue
            if rel == 'dev' and not os.path.lexists(full):
                entry(out, ino, rel, stat.S_IFDIR | 0o755); continue
            st = os.lstat(full)
            perm = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                entry(out, ino, rel, stat.S_IFLNK | 0o777, os.readlink(full).encode())
            elif stat.S_ISDIR(st.st_mode):
                entry(out, ino, rel, stat.S_IFDIR | perm)
            elif stat.S_ISREG(st.st_mode):
                entry(out, ino, rel, stat.S_IFREG | perm, open(full, 'rb').read())
            else:
                sys.exit(f'unsupported file type: {rel}')
        entry(out, 0, 'TRAILER!!!', 0)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
