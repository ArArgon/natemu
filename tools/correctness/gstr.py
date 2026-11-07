#!/usr/bin/env python3
"""gstr.py WORKDIR: libm.py for glibc's string and wcsmbs tests. From the test-wrapper logs (WORKDIR/wraplog/cmd.PID) of
the native `make check subdirs=string` / `subdirs=wcsmbs`, write WORKDIR/out/gstr-cmds.txt (NAME<TAB>CWD<TAB>COMMAND,
NAME the test program's path in the build tree, as in SUBDIR/subdir-tests.sum) and WORKDIR/gb-min: every build-tree
file a command names (also inside VAR=a:b values, e.g. LOCPATH, GCONV_PATH; a named directory is copied whole, minus
objects) and every shared object, at their build-tree paths, so the guest runs the commands unchanged."""
import os, re, shlex, shutil, sys

W = sys.argv[1]; gb = os.path.join(W, 'gb'); lst = []; files = set()
SUBS = tuple(os.path.join(gb, s) + '/' for s in ('string', 'wcsmbs'))
OBJ = ('.o', '.os', '.d', '.oS', '.a')
for f in sorted(os.listdir(os.path.join(W, 'wraplog'))):
    for rec in open(os.path.join(W, 'wraplog', f), 'rb').read().split(b'\0\n'):
        a = [x.decode() for x in rec.split(b'\0') if x]
        if len(a) < 2: continue
        cwd, argv = a[0], a[1:]
        progs = [x for x in argv if x.startswith(SUBS) and os.path.isfile(x) and os.access(x, os.X_OK)]
        if not progs or argv[0] == 'cp': continue
        lst.append((os.path.relpath(progs[0], gb), cwd, ' '.join(shlex.quote(x) for x in argv)))
        for tok in argv:
            for p in re.split(r'[=:]', tok):
                if not p.startswith(gb + '/'): continue
                if os.path.isfile(p): files.add(p)
                elif os.path.isdir(p) and p.rstrip('/') != gb:
                    for d, _, fs in os.walk(p):
                        files.update(os.path.join(d, x) for x in fs if not x.endswith(OBJ))
for d, _, fs in os.walk(gb):
    for x in fs:
        if '.so' in x and not x.endswith(OBJ): files.add(os.path.join(d, x))
out = os.path.join(W, 'gb-min'); shutil.rmtree(out, ignore_errors=True)
for p in sorted(files):
    q = os.path.join(out, os.path.relpath(p, gb)); os.makedirs(os.path.dirname(q), exist_ok=True); shutil.copy2(p, q)
lst.sort()
open(os.path.join(W, 'out', 'gstr-cmds.txt'), 'w').write(''.join('\t'.join(e) + '\n' for e in lst))
print(f'{len(lst)} commands, {len(files)} files')
