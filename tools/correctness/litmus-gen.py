#!/usr/bin/env python3
"""litmus-gen.py OUT CATALOGUE LTX86 DIY: the x86 litmus corpus, one directory per family under OUT (litmus7 wants test
names unique within one run). Families:
  cat86, cat64   herdtools7 7.58 catalogue/x86/tests and catalogue/x86_64/tests (the x86-TSO paper's tests)
  ltx2/3/4, ltxco  litmus-tests-x86 (litmus-tests org, a6dadce) BASIC_{2,3,4}_THREAD and CO: loads, stores, MFENCE
  rmw            DIY: diy7-generated cycles with Rmw (XCHG) edges
  lk-add, lk-inc, lk-xor, lk-xchg, lk-cmpxchg   every X86_64 test of cat64, ltx2 and ltx3 that has an MFENCE, with
                 each MFENCE of thread P replaced by a locked RMW on P's own location fP: LOCK ADD $0, LOCK INC, LOCK XOR $0,
                 XCHG %r15 (implicitly locked), LOCK CMPXCHG (rax saved in r13, set to 0, restored: fP stays 0 so the
                 compare succeeds and the store happens). x86-TSO orders a locked instruction as MFENCE does.
Also writes OUT/threads.txt (FAMILY/NAME THREADS)."""
import os, re, shutil, sys

out, cat, ltx, diy = sys.argv[1:5]
FORMS = {
    'add': lambda p: [f'lock addq $0,(f{p})'],
    'inc': lambda p: [f'lock incq (f{p})'],
    'xor': lambda p: [f'lock xorq $0,(f{p})'],
    'xchg': lambda p: [f'xchgq %r15,(f{p})'],
    'cmpxchg': lambda p: ['movq %rax,%r13', 'movq $0,%rax', f'lock cmpxchgq (f{p}),%r14', 'movq %r13,%rax'],
}

def parse(text):
    """-> (head lines, init block text, threads as lists of instructions, tail text) for the column format."""
    lines = text.split('\n')
    i = next(k for k, l in enumerate(lines) if l.strip().startswith('{'))
    j = next(k for k in range(i, len(lines)) if '}' in lines[k])
    head, init = lines[:i], '\n'.join(lines[i:j + 1])
    k = j + 1
    while not lines[k].strip(): k += 1
    hdr = [c.strip() for c in lines[k].strip().rstrip(';').split('|')]
    assert all(re.fullmatch(r'P\d+', c) for c in hdr), lines[k]
    threads = [[] for _ in hdr]; k += 1
    while k < len(lines) and lines[k].strip().endswith(';') and not re.match(r'\s*(~?exists|forall|locations)', lines[k]):
        cells = lines[k].strip().rstrip(';').split('|')
        for t, c in enumerate(cells):
            if c.strip(): threads[t].append(c.strip())
        k += 1
    return head, init, hdr, threads, '\n'.join(lines[k:])

def emit(head, init, hdr, threads, tail):
    rows = max(len(t) for t in threads)
    cols = [[h] + t + [''] * (rows - len(t)) for h, t in zip(hdr, threads)]
    w = [max(len(x) for x in c) for c in cols]
    body = [' ' + ' | '.join(c[r].ljust(w[i]) for i, c in enumerate(cols)) + ' ;' for r in range(rows + 1)]
    return '\n'.join(head + [init] + body) + '\n' + tail

def put(fam, name, text, nthreads, threads_out):
    d = f'{out}/{fam}'; os.makedirs(d, exist_ok=True)
    if os.path.exists(f'{d}/{name}.litmus'): return     # same name from two sources (cat64 and ltx2/3): the first stays
    open(f'{d}/{name}.litmus', 'w').write(text)
    threads_out.append(f'{fam}/{name} {nthreads}')

def nthreads(text):
    return len(re.findall(r'\bP\d+\b', next(l for l in text.split('\n') if re.match(r'\s*P0\b', l))))

shutil.rmtree(out, ignore_errors=True); os.makedirs(out); thr = []
src = {'cat86': f'{cat}/x86/tests', 'cat64': f'{cat}/x86_64/tests', 'ltx2': f'{ltx}/BASIC_2_THREAD',
       'ltx3': f'{ltx}/BASIC_3_THREAD', 'ltx4': f'{ltx}/BASIC_4_THREAD', 'ltxco': f'{ltx}/CO', 'rmw': diy}
tests = {}
for fam, d in src.items():
    for f in sorted(os.listdir(d)):
        if not f.endswith('.litmus'): continue
        t = open(f'{d}/{f}').read(); name = f[:-7]
        put(fam, name, t, nthreads(t), thr); tests[fam, name] = t
for form, rep in FORMS.items():
    fam = f'lk-{form}'
    for (sfam, name), t in tests.items():
        if sfam not in ('cat64', 'ltx2', 'ltx3') or 'mfence' not in t: continue
        head, init, hdr, threads, tail = parse(t)
        new = [sum((rep(p) if ins == 'mfence' else [ins] for ins in th), []) for p, th in enumerate(threads)]
        used = sorted({p for p, th in enumerate(threads) if 'mfence' in th})
        init2 = init.replace('{', '{\n' + ''.join(f'uint64_t f{p}; ' for p in used) + '\n', 1)
        nm = f'{name}+{form}'
        head2 = [re.sub(r'^(X86_64\s+)\S+', lambda m: m.group(1) + nm, l) if l.startswith('X86_64') else l for l in head]
        head2 = [l for l in head2 if not re.match(r'(Cycle|Relax|Safe|Generator|Prefetch|Com|Orig)=|"', l)]
        put(fam, nm, emit(head2, init2, hdr, new, tail), len(hdr), thr)
open(f'{out}/threads.txt', 'w').write('\n'.join(thr) + '\n')
from collections import Counter
print(dict(Counter(x.split('/')[0] for x in thr)), len(thr))
