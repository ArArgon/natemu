#!/usr/bin/env python3
"""libm.py WORKDIR: from the glibc test-wrapper logs (WORKDIR/wraplog/cmd.PID: cwd and argv, NUL-separated) of the native
`make check subdirs=math`, write WORKDIR/out/libm-cmds.txt (NAME<TAB>CWD<TAB>COMMAND, the exact command make ran,
shell-quoted; NAME is the test's path in the build tree, as in math/subdir-tests.sum) and WORKDIR/gb-min (the build
tree's shared objects and every test program, at their build-tree paths, stripped of nothing: the guest image carries
it at the same absolute path, so the commands run unchanged). Commands that are not math tests (the test-root setup)
are left out."""
import os, shlex, shutil, sys

W = sys.argv[1]; gb = os.path.join(W, 'gb'); lst = []; files = set()
for f in sorted(os.listdir(os.path.join(W, 'wraplog'))):
    for rec in open(os.path.join(W, 'wraplog', f), 'rb').read().split(b'\0\n'):
        a = [x.decode() for x in rec.split(b'\0') if x]
        if len(a) < 2: continue
        cwd, argv = a[0], a[1:]
        progs = [x for x in argv if x.startswith(gb + '/math/') and os.path.isfile(x)]
        if not progs or argv[0] == 'cp': continue
        name = os.path.relpath(progs[0], gb)
        lst.append((name, cwd, ' '.join(shlex.quote(x) for x in argv)))
        files.update(x for x in argv if x.startswith(gb + '/') and os.path.isfile(x))
for d, _, fs in os.walk(gb):
    for x in fs:
        if '.so' in x and not x.endswith(('.o', '.os', '.d')): files.add(os.path.join(d, x))
out = os.path.join(W, 'gb-min'); shutil.rmtree(out, ignore_errors=True)
for p in sorted(files):
    q = os.path.join(out, os.path.relpath(p, gb)); os.makedirs(os.path.dirname(q), exist_ok=True); shutil.copy2(p, q)
lst.sort()
open(os.path.join(W, 'out', 'libm-cmds.txt'), 'w').write(''.join('\t'.join(e) + '\n' for e in lst))
print(f'{len(lst)} commands, {len(files)} files')
