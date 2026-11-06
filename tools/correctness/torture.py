#!/usr/bin/env python3
"""torture.py build DIR CC: compile DIR/gcc.c-torture/execute/*.c and its ieee/*.c (gcc.c-torture/execute of Debian's gcc-14
source) at -O0 and -O2, each -static, into DIR/bin/NAME.OPT, the way gcc's DejaGnu harness builds them for x86_64-linux:
execute.exp's default flag -w (replaced by dg-options), plus dg-additional-options; ieee.exp's -fno-inline plus the
`lappend additional_flags` of the test's .x file. Option directives with a { target ... } selector apply only when the
selector names x86_64 (and not ia32) or fpic; dg-skip-if and the effective-target requirements hold on x86_64-linux for
every test of this version (checked by hand: the only *-*-* skips are for -O1 and -fomit-frame-pointer in the option
string), so none is skipped. A test that does not compile or link is listed in DIR/compile-failed.txt and left out.
Then DIR/list.txt (the tests built, sorted) and DIR/tests-K.tar.zst chunks of CHUNK tests (the guest unpacks one at a
time; static binaries are about 800 kB each)."""
import concurrent.futures as cf, os, re, shlex, subprocess, sys

CHUNK = 200
OPTS = ('O0', 'O2')

def directives(src):
    text = open(src, errors='replace').read()
    opts, add = None, []
    for kind, body in re.findall(r'\{\s*dg-(options|additional-options)\s+(.*?)\}\s*\*/', text):
        m = re.match(r'(?:"([^"]*)"|\{\s*"([^"]*)"\s*\})\s*(.*)$', body.strip())
        flags, sel = (m.group(1) if m.group(1) is not None else m.group(2)), m.group(3).strip()
        if sel and not (('x86_64' in sel and 'ia32' not in sel) or sel.strip('{} ') == 'target fpic'):
            continue
        if kind == 'options': opts = shlex.split(flags)
        else: add += shlex.split(flags)
    return opts, add

def xflags(src):
    x = src[:-2] + '.x'
    if not os.path.exists(x): return []
    out = []
    for line in open(x):
        if line.startswith('lappend additional_flags'):
            out += [a.strip('"') for a in line.split()[2:]]
    return out

def jobs(d):
    for sub, base in (('', ['-w']), ('ieee', ['-w', '-fno-inline'])):
        sd = os.path.join(d, 'gcc.c-torture', 'execute', sub)
        for f in sorted(os.listdir(sd)):
            if not f.endswith('.c'): continue
            src = os.path.join(sd, f); opts, add = directives(src)
            flags = (opts if opts is not None else ['-w']) + add
            if sub: flags = ['-fno-inline'] + flags + xflags(src)
            name = (sub + '_' if sub else '') + f[:-2]
            for o in OPTS:
                yield name + '.' + o, src, ['-' + o] + flags

def build(d, cc):
    os.makedirs(os.path.join(d, 'bin'), exist_ok=True)
    def one(j):
        name, src, flags = j
        cmd = shlex.split(cc) + flags + ['-static', '-o', os.path.join(d, 'bin', name), src, '-lm']
        r = subprocess.run(cmd, capture_output=True, text=True)
        return name, r.returncode, ' '.join(cmd), r.stderr[-400:]
    ok, bad, log = [], [], open(os.path.join(d, 'commands.txt'), 'w')
    with cf.ThreadPoolExecutor(4) as ex:
        for name, rc, cmd, err in ex.map(one, list(jobs(d))):
            log.write(f'{name}\t{rc}\t{cmd}\n')
            (ok if rc == 0 else bad).append(name)
            if rc: print(f'compile-failed {name}: {err.strip().splitlines()[-1] if err.strip() else rc}')
    ok.sort(); bad.sort()
    open(os.path.join(d, 'list.txt'), 'w').write(''.join(n + '\n' for n in ok))
    open(os.path.join(d, 'compile-failed.txt'), 'w').write(''.join(n + '\n' for n in bad))
    for k in range(0, len(ok), CHUNK):
        part = f'tests-{k // CHUNK:02d}.tar.zst'
        subprocess.run(f'tar -C {d}/bin -cf - {" ".join(ok[k:k + CHUNK])} | zstd -q -19 -T4 --long=27 -o {d}/{part}', shell=True, check=True)
    print(f'built {len(ok)}, failed {len(bad)}, chunks {(len(ok) + CHUNK - 1) // CHUNK}')

if __name__ == '__main__':
    if sys.argv[1] == 'build': build(sys.argv[2], sys.argv[3])
