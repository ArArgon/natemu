#!/usr/bin/env python3
"""Static census of check words in fexc slice1 code: what each class costs per block and per access.

    python3 -B census.py FEXC OUT.json PROGRAMS.json [PROGRAMS.json ...]

Compiles every block reachable by literal exits from each program's RIP (inside its code), classifies each word of the
code region by exact encoding (the forms admission/structure.rs pins) and sums. Divide checks are counted only by their
branch word (a lower bound); everything unmatched is `body`."""
import json, subprocess, sys, tempfile, collections, pathlib

def compile_block(fexc, code, rip, d):
    out = pathlib.Path(d) / 'j.json'
    subprocess.run([fexc, '--profile', 'slice1', '--hex', code.hex(), '--rip', hex(rip), '--out', out], capture_output=True, check=True)
    return json.loads(out.read_text())

def target(w, at):
    sx = lambda v, b: v - (1 << b) if v >> (b - 1) & 1 else v
    if w & 0xfc000000 == 0x14000000: return at + 4 * sx(w & 0x3ffffff, 26), 'b'
    if w & 0xff000010 == 0x54000000 or w & 0x7e000000 == 0x34000000: return at + 4 * sx(w >> 5 & 0x7ffff, 19), 'c'
    if w & 0x7e000000 == 0x36000000: return at + 4 * sx(w >> 5 & 0x3fff, 14), 'c'
    return None, None

def writes_x1(w):
    return w & 31 == 1 and (w & 0x1f000000 in (0x11000000, 0x0b000000) or w & 0x1f800000 == 0x12800000)

def classify(j):
    code = bytes.fromhex(j['code']['hex']); lay = j['code']['layout']
    end = lay['code_region_end']
    W = {at: int.from_bytes(code[at:at + 4], 'little') for at in range(4, end, 4)}
    lits = set()
    for r in j['code']['relocations']:
        if r['kind'] == 'guest_rip_literal': lits |= {r['offset'], r['offset'] + 4}
    for at in lits: W.pop(at, None)
    cls = {}
    stubs = {at: (w >> 5 & 0xffff) for at, w in W.items() if w & 0xffe0001f == 0xd4200000}
    for at in stubs: cls[at] = 'stub'
    access_sites = sorted((s['native_offset'], s.get('base_reg')) for s in j['sites'] if s['kind'] not in ('guard', 'split', 'divide'))
    cls[4] = cls[8] = 'prologue_header'; cls[12] = 'prologue_stop'
    for at, w in W.items():
        t, kind = target(w, at)
        if t not in stubs: continue
        imm = stubs[t]
        if imm == 1:                                        # guard: [x1 address] eor x0,A,A,lsl #16; tbnz x0,#63 (or tbz +8; b)
            first = at
            if kind == 'b' and W.get(at - 4, 0) & 0xfff8001f == 0xb6f80000: first = at - 4
            e = W.get(first - 4, 0)
            if e & 0xffe0fc1f == 0xca004000 and (e >> 16 & 31) == (e >> 5 & 31):
                a = e >> 5 & 31; first -= 4
                nxt = [s for s in access_sites if s[0] > at]
                exit_guard = any(W.get(x, 0) & 0xffffffe0 == 0xf9000f80 and W[x] & 31 == a for x in range(at + 4, at + 24))
                c = 'target_guard' if exit_guard and not (nxt and nxt[0][0] < at + 24) else 'guard'
                if a == 1 and c == 'guard' and not (nxt and nxt[0][1] == 1):
                    p = first - 4
                    while writes_x1(W.get(p, 0)): cls[p] = 'guard_addr'; p -= 4
            else:
                c = 'guard'                                   # `b G` for a non-canonical constant address
            for x in range(first, at + 4, 4): cls[x] = c
        elif imm == 2:                                        # split: add x0,A,#w-1; eor x0,x0,A; tbnz w0,#12 (or b S)
            first = at - 8 if W[at] & 0xfff8001f == 0x37600000 else at
            for x in range(first, at + 4, 4): cls[x] = 'split'
        elif imm == 3:
            cls[at] = 'divide_branch'
    for at, w in W.items():
        if w & 0xff00001f == 0x58000000 and W.get(at + 4) == 0xf9000f80:      # link point
            for x in range(at, at + 16, 4): cls[x] = 'exit_link'
        if w & 0xffffffe0 == 0xf9000f80 and W.get(at + 8) == 0xd61f0020 and cls.get(at) is None:
            for x in range(at, at + 12, 4): cls[x] = 'exit_computed'
        nx = W.get(at + 4, 0)
        if w & 0xffffffe0 == 0xd53b4200 and nx & 0xffc003e0 == 0xb9000380 and nx & 31 == w & 31:  # pre-NZCV save
            cls[at] = cls[at + 4] = 'pre_nzcv'
    c = collections.Counter(cls.get(at, 'body') for at in W)
    accesses = len(access_sites)
    for site in j['sites']: c['site_' + site['kind']] += 1
    return c, accesses, j['guest']['instructions_translated']

def main():
    fexc, out, *corpora = sys.argv[1:]
    total = collections.Counter(); per = {}
    with tempfile.TemporaryDirectory() as d:
        for path in corpora:
            progs = json.load(open(path))
            progs = progs['programs'] if 'programs' in progs else progs['benchmarks']
            cc = collections.Counter()
            for p in progs:
                code = bytes.fromhex(p['code']); r = p.get('rip', 0x100000); base = int(r, 16) if isinstance(r, str) else r
                todo, seen = [base], set()
                while todo:
                    rip = todo.pop()
                    if rip in seen or not base <= rip < base + len(code): continue
                    seen.add(rip)
                    j = compile_block(fexc, code[rip - base:], rip, d)
                    if j['status'] != 'ok' or not j['code']['hex']: continue
                    c, acc, ins = classify(j)
                    c['accesses'] += acc; c['guest_insns'] += ins; c['blocks'] += 1
                    cc.update(c)
                    for r in j['code']['relocations']:
                        if r['kind'] == 'guest_rip_literal': todo.append(int(r['guest_rip'], 16))
            per[path] = dict(cc); total.update(cc)
    per['total'] = dict(total)
    json.dump(per, open(out, 'w'), indent=1)
    keys = ['body', 'prologue_header', 'prologue_stop', 'guard', 'guard_addr', 'split', 'target_guard', 'divide_branch', 'pre_nzcv', 'exit_link', 'exit_computed', 'stub']
    for name, c in per.items():
        words = sum(c.get(k, 0) for k in keys)
        print(f"== {pathlib.Path(name).parent.name}/{pathlib.Path(name).name}: blocks {c.get('blocks')} guest insns {c.get('guest_insns')} accesses {c.get('accesses')} words {words} ({words / max(1, c.get('guest_insns', 1)):.2f}/insn)")
        print('   ' + ', '.join(f"{k} {c.get(k, 0)} ({100 * c.get(k, 0) / max(1, words):.1f}%)" for k in keys))
        a = max(1, c.get('accesses', 1))
        print(f"   per access: guard {c.get('guard', 0) / a:.2f} + guard_addr {c.get('guard_addr', 0) / a:.2f} + split {c.get('split', 0) / a:.2f} words")

if __name__ == '__main__':
    main()
