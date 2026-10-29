#!/usr/bin/env python3
"""E1 census: does fexc slice1 code still touch emulator data? (the review's memwords approach, every word)

    python3 -B e1census.py [--before FEXC0] FEXC OUT.json PROGRAMS.json [PROGRAMS.json ...]

Same block walk as census.py, following literal exits (series11: guest-RIP moves into x0 before `svc #1`; older series:
guest-RIP literals). Every word of the code region is classified by exact encoding:
- load/store class: a literal load, or by base register, split into recorded guest access sites and everything else
  (`nonsite_*`: x28 frame fields, sp, anything else);
- writes to sp and to x25 (`mrs x25, nzcv`, the pre-NZCV save, counted apart from any other write);
- control transfers into or out of the resident: `svc #imm`, `brk #imm`, `bl`, `blr`, `br`, `ret`, and the placeholders
  the resident relocates (series11's stop check `bl .` at the entry, `b .` at a dispatch relocation).
Exit status 1 if any word touches emulator data (a non-site load/store, a literal load or an sp access or write) or, since
E1b (series12, stops by unlinking), if any block still has a stop check or any other `bl`, and since E3 (series13, no
address guard) if any address-guard word remains: a guard stub (`brk #1`) covering an access site, the branch that reaches
it (`tbnz x0, #63`, its far form `tbz; b`, or a `b` for a RIP-relative address) and the `eor x0, xA, xA, lsl #16` before
it (`address_guard_words`; the branch-target check of a computed exit is x86 semantics and stays, `target_check_words`).
With --before, the same census of FEXC0 (e.g. series12) and the word-count reduction."""
import json, sys, collections, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from census import compile_block, target


def scan(j, c, examples):
    code = bytes.fromhex(j['code']['hex'])
    end = j['code']['layout']['code_region_end']
    sites = {s['native_offset'] for s in j['sites'] if s['kind'] not in ('guard', 'split', 'divide')}
    dispatch = {r['offset'] for r in j['code']['relocations'] if r['kind'] == 'dispatch_branch'}
    for at in range(4, end, 4):
        w = int.from_bytes(code[at:at + 4], 'little')
        rd, rn = w & 31, w >> 5 & 31
        key = None
        if (w >> 25) & 5 == 4:                                   # load/store class
            if w & 0x3b000000 == 0x18000000:
                key = 'literal_load'
            else:
                base = 'sp' if rn == 31 else f'x{rn}'
                key = f'site_{base}' if at in sites else f'nonsite_{base}'
        elif w == 0xd53b4219:
            key = 'mrs_x25_nzcv'
        elif w & 0xffe0001f == 0xd4000001:
            key = f'svc_{w >> 5 & 0xffff}'
        elif w & 0xffe0001f == 0xd4200000:
            key = f'brk_{w >> 5 & 0xffff}'
        elif w == 0x94000000:
            key = 'bl_placeholder' if at == 4 else 'bl_placeholder_elsewhere'
        elif w & 0xfc000000 == 0x94000000:
            key = 'bl'
        elif w == 0x14000000:
            key = 'b_dispatch_placeholder' if at in dispatch else 'b_self'
        elif w & 0xfffffc1f == 0xd63f0000:
            key = 'blr'
        elif w & 0xfffffc1f == 0xd61f0000:
            key = 'br'
        elif w == 0xd65f03c0:
            key = 'ret'
        else:
            dp_imm, dp_reg = (w >> 26) & 7 == 4, (w >> 25) & 7 == 5
            flags = (w >> 29) & 1
            if (dp_imm or dp_reg) and rd == 31 and not flags and (w & 0x1f000000) in (0x11000000, 0x0b000000):
                key = 'sp_write'                                 # add/sub (immediate, extended) to sp
            elif (dp_imm or dp_reg) and rd == 25:
                key = 'x25_write_other'
        if key:
            c[key] += 1
            if key.startswith(('nonsite', 'literal', 'sp_', 'x25_write', 'bl_placeholder_e', 'b_self', 'blr', 'br', 'ret')) \
                    and len(examples[key]) < 5:
                examples[key].append((j['guest'].get('rip', '?') if isinstance(j['guest'], dict) else '?', at, hex(w)))
    # Guard stubs: an address guard covers an access site; a branch-target check covers none.
    words = {at: int.from_bytes(code[at:at + 4], 'little') for at in range(4, end, 4)}
    for st in (x for x in j['sites'] if x['kind'] == 'guard'):
        key = 'target_check_words' if st.get('access_native_offset') is None else 'address_guard_words'
        n = 1                                                   # the brk
        for at, w in words.items():
            if target(w, at)[0] != st['native_offset']:
                continue
            n += 1
            first = at - 4 if w & 0xfc000000 == 0x14000000 and words.get(at - 4, 0) & 0xfff8001f == 0xb6f80000 else at
            n += first != at
            e = words.get(first - 4, 0)
            n += e & 0xffe0fc1f == 0xca004000 and (e >> 16 & 31) == (e >> 5 & 31)   # eor x0, xA, xA, lsl #16
        c[key] += n


def targets(j):
    """Literal-exit targets: guest-RIP moves into x0 (series11) or guest-RIP literals (older series)."""
    for r in j['code']['relocations']:
        if r['kind'] == 'guest_rip_move' and r['register'] == 0 or r['kind'] == 'guest_rip_literal':
            yield int(r['guest_rip'], 16)


def census(fexc, corpora):
    total, examples = collections.Counter(), collections.defaultdict(list)
    with tempfile.TemporaryDirectory() as d:
        for path in corpora:
            progs = json.load(open(path))
            progs = progs['programs'] if 'programs' in progs else progs['benchmarks']
            for p in progs:
                code = bytes.fromhex(p['code'])
                r = p.get('rip', 0x100000)
                base = int(r, 16) if isinstance(r, str) else r
                todo, seen = [base], set()
                while todo:
                    rip = todo.pop()
                    if rip in seen or not base <= rip < base + len(code):
                        continue
                    seen.add(rip)
                    j = compile_block(fexc, code[rip - base:], rip, d)
                    total['status:' + j['status']] += 1
                    if j['status'] != 'ok' or not j['code']['hex']:
                        continue
                    total['blocks'] += 1
                    total['guest_instructions'] += j['guest']['instructions_translated']
                    total['words'] += j['code']['layout']['code_region_end'] // 4 - 1
                    total['spill_bytes_nonzero'] += j['code']['layout']['spill_bytes'] != 0
                    scan(j, total, examples)
                    todo.extend(targets(j))
    return total, examples


def main():
    args = sys.argv[1:]
    before = None
    if args[:1] == ['--before']:
        before, args = args[1], args[2:]
    fexc, out, *corpora = args
    total, examples = census(fexc, corpora)
    touching = {k: v for k, v in total.items() if k.startswith(('nonsite_', 'literal_load', 'sp_write', 'site_sp'))}
    calls = {k: v for k, v in total.items() if k == 'bl' or k.startswith('bl_')}
    guards = total['address_guard_words']
    result = {'total': dict(sorted(total.items())), 'emulator_data_words': touching, 'stop_checks_and_calls': calls,
              'address_guard_words': guards, 'examples': examples}
    for k, v in sorted(total.items()):
        print(f'{k} {v}')
    if before:
        old, _ = census(before, corpora)
        same = all(old[k] == total[k] for k in ('blocks', 'guest_instructions'))
        result['before'] = {'fexc': before, 'total': dict(sorted(old.items())), 'same_blocks_and_instructions': same,
                            'word_reduction': old['words'] - total['words'],
                            'word_reduction_pct': 100 * (old['words'] - total['words']) / old['words']}
        print(f"before ({before}): {old['words']} words, {old['address_guard_words']} address-guard words, "
              f"{old['blocks']} blocks, {old['guest_instructions']} guest instructions")
        print(f"words {old['words']} -> {total['words']}: -{old['words'] - total['words']} "
              f"({result['before']['word_reduction_pct']:.1f}%); same blocks and guest instructions: {same}")
    json.dump(result, open(out, 'w'), indent=1)
    print('words touching emulator data:', touching or 'none')
    print('stop checks and other bl:', calls or 'none')
    print('address-guard words:', guards)
    return 1 if touching or calls or guards else 0


if __name__ == '__main__':
    sys.exit(main())
