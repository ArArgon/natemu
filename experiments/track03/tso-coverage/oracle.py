#!/usr/bin/env python3
"""Allowed-outcome sets for the litmus programs (litmus.py) and the verdict logic for probe output.

x86-TSO is modelled operationally: one FIFO store buffer per thread, loads read the thread's own newest
buffered store of the location first, else memory; a store leaves its buffer at any time; `dsb` (handler
fence) waits for an empty buffer.  SC is the same machine with immediate flushing.  Outcomes are digit-packed
exactly like the guest code: digit 0 = value 0, 1 = value 1, 2 = value 2, 3 = any other value (torn).
`python3 oracle.py --self-check` pins the model against textbook results.
"""
import json, sys
from functools import lru_cache
from litmus import ALL, LITMUS


def outcomes(lit, tso):
    # swpal ('swp') and the half-width store pair ('sth') are single stores in the model
    threads = [tuple(('st',) + tuple(o[1:]) if o[0] in ('swp', 'sth') else tuple(o) for o in t) for t in lit['threads']]
    regs = sorted({o[1] for t in threads for o in t if o[0] == 'ld'})
    n = len(threads)
    seen, finals = set(), set()
    start = (tuple([0] * n), (), tuple(() for _ in range(n)), (0, 0))   # pcs, regvals, buffers, (x, y)

    def step(state):
        pcs, rv, bufs, mem = state
        rvd = dict(rv)
        for t in range(n):
            if bufs[t]:   # flush oldest
                loc, val = bufs[t][0]
                m = list(mem); m['xy'.index(loc)] = val
                nb = list(bufs); nb[t] = bufs[t][1:]
                yield (pcs, rv, tuple(nb), tuple(m))
            if pcs[t] == len(threads[t]): continue
            op = threads[t][pcs[t]]
            npc = list(pcs); npc[t] += 1; npc = tuple(npc)
            if op[0] == 'st':
                loc, val = op[1], op[2]
                if tso:
                    nb = list(bufs); nb[t] = bufs[t] + ((loc, val),)
                    yield (npc, rv, tuple(nb), mem)
                else:
                    m = list(mem); m['xy'.index(loc)] = val
                    yield (npc, rv, bufs, tuple(m))
            elif op[0] == 'ld':
                loc = op[2]
                v = None
                for l, val in reversed(bufs[t]):
                    if l == loc: v = val; break
                if v is None: v = mem['xy'.index(loc)]
                d = dict(rvd); d[op[1]] = v
                yield (npc, tuple(sorted(d.items())), bufs, mem)
            elif op[0] == 'dsb':
                if not bufs[t]: yield (npc, rv, bufs, mem)
    stack = [start]
    while stack:
        s = stack.pop()
        if s in seen: continue
        seen.add(s)
        pcs, rv, bufs, mem = s
        if all(pcs[t] == len(threads[t]) for t in range(n)) and not any(bufs):
            d = dict(rv)
            finals.add(tuple(mem['xy'.index(o[4])] if o.startswith('mem:') else d[o] for o in lit['outputs']))
        stack.extend(step(s))
    return {sum(v << (2 * i) for i, v in enumerate(f)) for f in finals}


def decode(key, n):
    return tuple((key >> (2 * i)) & 3 for i in range(n))


def mix_forbidden(variant):
    """keys that are forbidden outcomes of a mixed-size cell (decode: d0 | d1 << 2)."""
    if variant == 'st8-ld4lohi': ok = {0, 1 << 2, 1 | 1 << 2}        # lo-new/hi-old is impossible (loads in order)
    elif variant == 'st8-ld4hilo': ok = {0, 1, 1 | 1 << 2}
    elif variant == 'st4lohi-ld8': ok = {0, 1, 3}                    # hi-only impossible
    else: ok = {0, 2, 3}                                             # lo-only impossible
    return ok


def check_cell(cell, rows):
    """cell: cells.json entry; rows: list of 'cell' result rows (same cell, several timing profiles)."""
    out = dict(name=cell['name'], litmus=cell['litmus'], cls=cell['cls'], width=cell['width'], iters=sum(r['iters'] for r in rows))
    if cell['kind'] == 'atom':
        res = [[0] * 5 for _ in range(4)]
        for r in rows:
            for i, x in enumerate(r['res']):
                for k in (0, 1, 4): res[i][k] += x[k]
        out.update(writer_stores=res[0][0], reads=sum(x[0] for x in res[1:]), torn=sum(x[1] for x in res[1:]),
                   transitions=sum(x[4] for x in res[1:]))
        firsts = [x[2] for r in rows for x in r['res'][1:] if x[1]]
        out['first_torn'] = firsts[:3]
        return out
    ops = ALL.get(cell['litmus'])
    hist = {}
    for r in rows:
        for k, v in r['hist'].items(): hist[int(k)] = hist.get(int(k), 0) + v
    out['trials'] = sum(hist.values())
    nout = 2 if cell['litmus'] == 'mix' else len(ops['outputs'])
    forbidden = torn = weak = 0
    bad = {}
    if cell['litmus'] == 'mix':
        ok = mix_forbidden(cell['variant'])
        for k, v in hist.items():
            if k not in ok: forbidden += v; bad[str(decode(k, nout))] = v
        out.update(forbidden=forbidden, torn=0, weak=0, bad=bad)
        return out
    tso, sc = outcomes(ops, True), outcomes(ops, False)
    for k, v in hist.items():
        d = decode(k, nout)
        if 3 in d: torn += v
        elif k not in tso: forbidden += v; bad[str(d)] = v
        elif k not in sc: weak += v
    out.update(forbidden=forbidden, torn=torn, weak=weak, bad=bad, tso_allowed=len(tso), sc_allowed=len(sc))
    return out


def self_check():
    t = {n: outcomes(LITMUS[n], True) for n in LITMUS}
    s = {n: outcomes(LITMUS[n], False) for n in LITMUS}
    pack = lambda *d: sum(v << (2 * i) for i, v in enumerate(d))
    assert pack(0, 0) in t['sb'] and pack(0, 0) not in s['sb']               # store buffering
    assert pack(1, 0) not in t['mp'] and pack(1, 0) not in s['mp'] and len(t['mp']) == 3
    assert pack(1, 1) not in t['lb'] and len(t['lb']) == 3
    assert pack(1, 1, 0) not in t['wrc'] and len(t['wrc']) == 7
    assert pack(1, 0, 1, 0) not in t['iriw'] and len(t['iriw']) == 15
    assert pack(1, 1) not in t['2+2w'] and pack(1, 1) not in s['2+2w']
    assert pack(1, 2) not in t['s'] and len(t['s']) == 3                       # r0=1 and final x=2
    assert len(t['r']) == 4 and len(s['r']) == 3                              # R: x86 allows the SB-like outcome
    assert all(decode(k, 4)[0] == 1 and decode(k, 4)[2] == 1 for k in t['sbf'])  # store forwarding
    assert pack(1, 0) not in t['corr']
    assert pack(2, 1, 1) not in t['cow'] and pack(2, 1, 2) not in t['cow']
    print('oracle self-check ok', {n: len(v) for n, v in t.items()})


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-check']: self_check()
