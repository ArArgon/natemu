#!/usr/bin/env python3
"""S1a check: fexc's access sites against the fixture programs and against the emitted code.

usage: check_sites.py [--known-ok] [--slice1] [FEXC_BINARY]      (default $FEXC, else the series15 build)

--slice1 compiles with `--profile slice1` (S1c): hardware TSO (plain ldr/str), exits through the dispatcher gate, block
cutting (no fixture instruction is cut since S3e admitted memory xchg, a `swap` site); the exit
literals (8-byte guest RIPs inside the code region) are not instructions and are masked from the disassembly.

For each program in fixtures/slice1/programs.json (compiled at its rip, 64-bit):
  1. EXPECTED vs SITES: the memory accesses the generator's `asm` implies (one per explicit memory operand,
     two for a read-modify-write, plus the implicit stack access of push/pop) must equal the exported sites per
     guest instruction (kind class and width). A fused push/pop pair counts for both of its guest instructions.
     `lea` has no access. Intent/rmw/segment must agree too.
  2. CODE vs SITES: disassembling the emitted code with `xcrun objdump`, every load/store whose base is not
     x28/sp (PC-relative literal loads have no base and are skipped) must be a site, and every site must sit on
     such an instruction of the matching width, base register and direction.
  3. the dense (default) and --sparse-pc tables give identical code bytes (sparse sites are unreliable: an instruction
     without a GuestOpcode is attributed to the previous one); the binary `--object` file agrees with the JSON.
Mismatches are reported, never skipped, in three kinds. `ERROR` is always a failure.
  ELIMINATED  a cmp/test/bt memory read whose flags are overwritten later has no native access at all (flag elimination
              deletes the flag computation, then dead-code elimination deletes the load): its fault is lost. Belongs to
              the precision work (S1b); fails unless --known-ok is given.
  NARROWED    bt/bts/btr/btc with a memory operand, whether the bit index is an immediate or a register, access ONE byte
              (the byte holding the bit), not the x86 operand width. ACCEPTED natemu policy (documented in the README):
              the fault, CR2 and A/D bits are those of that byte; never a failure.
Exit status 1 if any ERROR, or any ELIMINATED without --known-ok.
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def fixture_programs():
    """The programs of fixtures/slice1/programs.json (git-ignored, built on demand: tools/reference/build_slice1.py)."""
    path = REPO / 'fixtures/slice1/programs.json'
    if not path.exists():
        subprocess.run([sys.executable, '-B', str(REPO / 'tools/reference/build_slice1.py'), '--programs-only'], check=True)
    return json.loads(path.read_text())['programs']
FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series15/obj/fexc')
KNOWN_OK = False
PROFILE = []   # extra fexc arguments for every compile: ['--profile', 'slice1'] under --slice1

WIDTH = {'byte': 1, 'word': 2, 'dword': 4, 'qword': 8}
RMW_DST = {'add', 'adc', 'sub', 'sbb', 'and', 'or', 'xor', 'inc', 'dec', 'neg', 'not', 'shl', 'shr', 'sar', 'sal', 'rol',
           'ror', 'rcl', 'rcr', 'bts', 'btr', 'btc', 'shld', 'shrd', 'cmpxchg', 'xadd'}   # I1: shld/shrd, cmpxchg/xadd without LOCK
LOAD_DST = {'cmp', 'test', 'bt'}
LOAD_ONLY_1OP = {'mul', 'imul', 'div', 'idiv'}
WORD_REGS = {'ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di'} | {f'r{i}w' for i in range(8, 16)}
STUBS = ('guard', 'split', 'divide')   # slice1 check stubs (brk words after the body), not accesses: verify_precision.py checks them


def expected(asm):
    """[(class, width)] for one `asm` line, class in load/store/push/pop; None if the form is not understood."""
    mn, _, rest = asm.partition(' ')
    ops = [o.strip() for o in rest.split(',')] if rest else []
    mem = [i for i, o in enumerate(ops) if '[' in o]
    ptr = lambda o: WIDTH[o.split(' ptr')[0].split()[-1]]
    if mn == 'push':
        if mem:
            return [('load', ptr(ops[0])), ('push', ptr(ops[0]))]
        return [('push', 2 if ops[0] in WORD_REGS else 8)]
    if mn in ('pushfq', 'enter'):     # I1 (enter: nesting level 0, one push of RBP)
        return [('push', 8)]
    if mn == 'leave':                # I1: a plain load of [RBP] (slice1), then RSP and RBP
        return [('load', 8)]
    if mn.startswith('prefetch'):    # a hint: no access
        return []
    if mn == 'pop':
        if mem:
            return [('pop', ptr(ops[0])), ('store', ptr(ops[0]))]
        return [('pop', 2 if ops[0] in WORD_REGS else 8)]
    if not mem or mn == 'lea':
        return []
    assert len(mem) == 1, asm
    i, w = mem[0], ptr(ops[mem[0]])
    if mn == 'xchg':
        return [('swap', w)]
    if i > 0:
        return [('load', w)]
    if mn in ('mov', 'movabs', 'movnti') or mn.startswith('set'):
        return [('store', w)]
    if mn in RMW_DST:
        return [('load', w), ('store', w)]
    if mn in LOAD_DST or mn in LOAD_ONLY_1OP:
        return [('load', w)]
    return None


def site_classes(sites):
    """guest_offset -> sorted [(class, width)], splitting fused pairs between their two guest instructions."""
    out = {}
    for s in sites:
        k, w = s['kind'], s['width']
        if k in STUBS:
            continue
        if k in ('push_pair', 'pop_pair'):
            for g in (s['guest_offset'], s['guest_offset2']):
                out.setdefault(g, []).append((k[:-5], w // 2))
        elif k in ('load', 'store', 'push', 'pop'):
            out.setdefault(s['guest_offset'], []).append((k, w))
        else:
            out.setdefault(s['guest_offset'], []).append((k, w))
    return {g: sorted(v) for g, v in out.items()}


def native_access(w):
    """Width of a load/store mnemonic line, (direction, width, base, index, imm, writeback) or None."""
    m = re.match(r'(\w+)\s+(.*)$', w)
    if not m or not m.group(1).startswith(('ld', 'st', 'swp')) or '[' not in m.group(2):
        return None
    mn, ops = m.group(1), m.group(2)
    base = re.search(r'\[(x\d+|sp)', ops).group(1)
    if mn.startswith('swp'):        # swpal{b,h} Rs, Rt, [Rn]: a memory xchg
        return ('swap', 1 if mn.endswith('b') else 2 if mn.endswith('h') else 4 if ops.startswith('w') else 8, base, None, 0, 'none')
    regs = re.findall(r'\b([wxbhsdq])(?:\d+|zr)\b', ops.split('[')[0])
    if mn.endswith('b') and mn != 'ldaddb':
        width = 1
    elif mn.endswith('h'):
        width = 2
    elif mn in ('ldrsw', 'ldursw'):
        width = 4
    else:
        width = {'w': 4, 'x': 8, 'b': 1, 'h': 2, 's': 4, 'd': 8, 'q': 16}[regs[0]]
    if mn in ('ldp', 'stp'):
        width *= 2
    idx = re.search(r'\[x\d+, (x\d+)', ops)
    imm = re.search(r'#(-?(?:0x)?[0-9a-f]+)', ops.split('[')[1])
    wb = 'pre' if ops.rstrip().endswith('!') else 'post' if re.search(r'\],\s*#', ops) else 'none'
    return ('load' if mn.startswith('ld') else 'store', width, base, int(idx.group(1)[1:]) if idx else None,
            int(imm.group(1), 0) if imm else 0, wb)


def disassemble(code, end, tmp, literals=()):
    """{offset: text}; `literals` = (offset, size) data words inside the code region, emitted as .long and not disassembled."""
    data = {o + k for o, n in literals for k in range(0, n, 4)}
    asm = ['.text', '.globl _block', '_block:'] + ['.long 0x%08x' % int.from_bytes(code[o:o + 4], 'little') for o in range(0, end, 4)]
    (tmp / 'b.s').write_text('\n'.join(asm) + '\n')
    subprocess.run(['clang', '-c', '-arch', 'arm64', str(tmp / 'b.s'), '-o', str(tmp / 'b.o')], check=True)
    dis = subprocess.run(['xcrun', 'objdump', '-d', '--no-show-raw-insn', str(tmp / 'b.o')], capture_output=True, text=True, check=True).stdout
    return {int(m.group(1), 16): re.sub(r'\s*[;<].*$', '', m.group(2)).replace('\t', ' ')
            for m in re.finditer(r'^\s*([0-9a-f]+):\s+(.*)$', dis, re.M) if int(m.group(1), 16) not in data}


def run(hexs, rip, tmp, *extra):
    r = subprocess.run([FEXC, *PROFILE, '--hex', hexs, '--rip', rip, '--bitness', '64', *extra], capture_output=True, text=True, timeout=120)
    assert r.returncode in (0, 1) and r.stdout, r.stderr   # 1 = no code (the first instruction is cut)
    return json.loads(r.stdout)


def read_object(path):
    f = Path(path).read_bytes()
    h = struct.unpack_from('<8sIIIIQ14I20sBBBBQBB2sI', f, 0)
    assert h[0] == b'NFXOBJ\0\x01' and h[1] in (2, 3, 4) and h[2] == 128 and h[3] == len(f)   # 2: builds before the pre-NZCV header field; 4: pre-NZCV register (series11 on)
    (_, _, _, _, bitness, rip, gbytes, ginsns, coff, csize, body, marker, region_end, spill, soff, scount, poff, pcount, roff, rcount,
     sra, nsra, profile, end_reason, settings, fbits, fcount, dcache, _, pre_nzcv) = h
    sites = [struct.unpack_from('<IIIIi20B', f, soff + 40 * i) for i in range(scount)]
    return dict(code=f[coff:coff + csize], sites=sites, body=body, spill=spill, rip=rip, bitness=bitness, nsra=nsra, pcs=pcount, relocs=rcount,
                gbytes=gbytes, ginsns=ginsns, profile=profile, end_reason=end_reason, settings=settings, fbits=fbits, fcount=fcount, dcache=dcache, pre_nzcv=pre_nzcv, version=h[1])


END_REASONS = ['', 'end_of_input', 'block_ending_instruction', 'max_instructions', 'fex_instruction_limit', 'system_instruction',
               'unsupported_form', 'dispatch_failure', 'invalid_instruction', 'partial_decode', 'not_executable', 'bad_relocation',
               'unimplemented_instruction']


def object_vs_json(o, d):
    """Mismatches between the binary object header and the JSON (profile, settings, end reason, feature profile, counts)."""
    out = []
    st, g = d['settings'], d['guest']
    want = [('profile', 1 if st['profile'] == 'slice1' else 0), ('settings', st['tso'] + 2 * st['multiblock'] + 4 * st['dense_pc_table'] + 8 * st['end_marker']),
            ('end_reason', END_REASONS.index(g['end']['reason']) if g['end']['reason'] in END_REASONS else 255),
            ('ginsns', g['instructions_translated']), ('gbytes', g['bytes_translated']),
            ('fbits', sum(1 << i for i, v in enumerate(d['features']['host_features'].values()) if v)),
            ('fcount', len(d['features']['host_features'])), ('dcache', d['features']['dcache_line_log2']),
            ('pre_nzcv', d['state_abi']['state_offsets'].get('natemu_pre_nzcv_register', 0xFFFFFFFF)),
            ('pcs', len(d['pc_table']['entries'])), ('relocs', len(d['code']['relocations']))]
    for k, v in want:
        if k == 'pre_nzcv' and o['version'] < 4:
            continue
        if o[k] != v:
            out.append(f'object {k}={o[k]} but JSON says {v}')
    return out


def first_cut(asm):
    """Index of the first fixture instruction slice1 must cut, else None. Taken from the generator's `asm`, never from
    fexc. None since S3e: memory xchg, the only fixture form slice1 cut, is admitted."""
    return None


def asm0_cut(p):
    return first_cut(p['asm']) == 0


def code_vs_sites(sites, dis):
    """Messages for every disagreement between the sites and the disassembly `dis` ({offset: text}): a native load/store with a
    base other than x28/sp that is no site, a site not on a load/store, and a site whose width, base, operand form, writeback or
    transfer registers differ from the instruction. Independent of the guest program (needs no `asm`)."""
    out = []
    by_off = {s['native_offset']: s for s in sites if s['kind'] not in STUBS}
    native = {}
    for off, text in dis.items():
        a = native_access(text)
        if a and a[2] not in ('x28', 'sp'):
            native[off] = a
    for off in sorted(set(native) | set(by_off)):
        a, s = native.get(off), by_off.get(off)
        if a and not s:
            out.append(f'native +{off} "{dis[off]}" is a load/store with a non-frame base but no site')
        elif s and not a:
            out.append(f'site at +{off} ({s["ir_op"]}) is not on a load/store: "{dis.get(off)}"')
        else:
            dirn = 'load' if s['kind'] in ('load', 'pop', 'load_pair', 'pop_pair') else 'swap' if s['kind'] == 'swap' else 'store'
            if (a[0], a[1], a[2]) != (dirn, s['width'], f'x{s["base_reg"]}'):
                out.append(f'site +{off} ({s["kind"]} w{s["width"]} base x{s["base_reg"]}) vs "{dis[off]}"')
            if a[3] != s['index_reg'] or a[5] != s['writeback'] or (a[5] == 'none' and a[3] is None and a[4] != s['imm']):
                out.append(f'site +{off} operand form (index {s["index_reg"]} imm {s["imm"]} wb {s["writeback"]}) vs "{dis[off]}"')
            elif a[5] == 'pre' and not (a[4] == s['imm'] == s['writeback_delta']) or a[5] == 'post' and not (a[4] == s['writeback_delta'] and s['imm'] == 0):
                out.append(f'site +{off} writeback (imm {s["imm"]} delta {s["writeback_delta"]}) vs "{dis[off]}"')
            regs = [31 if r.endswith('zr') else int(r[1:]) for r in re.findall(r'\b([wxbhsdq](?:\d+|zr))\b', dis[off].split(' ', 1)[1].split('[')[0])]
            if s['kind'] == 'swap': regs = regs[::-1]        # swpal Rs, Rt; the record is [Rt, Rs]
            if regs != s['data_regs']:
                out.append(f'site +{off} data registers {s["data_regs"]} vs "{dis[off]}"')
    return out


def check_program(p, tmp):
    problems = []
    d = run(p['code'], p['rip'], tmp, '--object', str(tmp / 'o.bin'))
    if d['status'] == 'no_code':   # slice1: the program starts with a cut instruction
        g = d['guest']
        ok = PROFILE and g['instructions_translated'] == 0 and g['end']['reason'] == 'unsupported_form' and g['end']['rip'] == p['rip'] \
            and asm0_cut(p)
        return ([] if ok else [('ERROR', f"no code: {d.get('error')} {d.get('guest')}")]), 0
    sites = d['sites']
    insts = [i for i in d['guest']['instructions'] if i['translated']]
    asm = p['asm']
    want_end = 'end_of_input'
    if PROFILE:
        cut = first_cut(asm)
        if cut is not None:
            asm, want_end = asm[:cut], 'unsupported_form'
    if len(insts) != len(asm) or d['guest']['end']['reason'] != want_end:
        return [('ERROR', f"translated {len(insts)} of {len(asm)} instructions, end={d['guest']['end']['reason']} (want {want_end})")], 0
    if want_end == 'unsupported_form' and int(d['guest']['end']['rip'], 16) != int(p['rip'], 16) + sum(i['size'] for i in insts):
        return [('ERROR', 'cut RIP is not the end of the last translated instruction')], 0
    offs = [i['offset'] for i in insts]

    # 1. expected vs sites
    exp = {}
    for off, a in zip(offs, asm):
        e = expected(a)
        if e is None:
            problems.append(('ERROR', f'unclassified fixture instruction: {a}'))
            continue
        if e:
            exp[off] = sorted(e)
    got = site_classes(sites)
    asm_at = {o: a for o, a in zip(offs, asm)}
    for off in sorted(set(exp) | set(got)):
        if exp.get(off) == got.get(off):
            continue
        a = asm_at.get(off, '?')
        mn = a.split()[0]
        msg = f'guest +{off} "{a}": expected {exp.get(off)} sites {got.get(off)}'
        if got.get(off) is None and mn in LOAD_DST:
            problems.append(('ELIMINATED', msg))
        elif mn in ('bt', 'bts', 'btr', 'btc') and [(k, 1) for k, _ in exp[off]] == got.get(off):
            problems.append(('NARROWED', msg))
        else:
            problems.append(('ERROR', msg))
    for s in sites:
        a = asm_at.get(s['guest_offset'], '?')
        mn = a.split()[0]
        if s['kind'] in STUBS:
            continue
        if s['kind'] == 'load' and s['rmw'] == 'load' and s['intent'] != 'write':
            problems.append(('ERROR', f'rmw load without write intent at {s["native_offset"]}'))
        is_rmw = mn in RMW_DST and '[' in a.partition(',')[0]
        if s['kind'] in ('load', 'store') and (s['rmw'] != 'none') != is_rmw:
            problems.append(('ERROR', f'guest +{s["guest_offset"]} "{a}": rmw={s["rmw"]} but fixture rmw={is_rmw}'))
        if s['kind'] == 'load' and s['rmw'] == 'none' and s['intent'] != 'read':
            problems.append(('ERROR', f'plain load with write intent at {s["native_offset"]}'))
        if s['kind'] in ('store', 'push', 'push_pair') and s['intent'] != 'write':
            problems.append(('ERROR', f'store without write intent at {s["native_offset"]}'))
        # I7: an FS/GS-overridden operand is FS/GS (#GP), whatever its base; the implicit stack accesses stay SS.
        ss = s['kind'] in ('push', 'pop', 'push_pair', 'pop_pair') or \
            bool(re.search(r'\[(rsp|rbp)\b', a) and not re.search(r'\b[fg]s:\[', a) and s['kind'] in ('load', 'store')) or \
            (mn == 'leave' and s['kind'] == 'load')
        if (s['segment'] == 'ss') != ss:
            problems.append(('ERROR', f'guest +{s["guest_offset"]} "{a}": segment {s["segment"]}'))
        if not s['located']:
            problems.append(('ERROR', f'site at {s["native_offset"]} ({s["ir_op"]}) not located in its emitted range'))

    # 2. code vs sites
    code = bytes.fromhex(d['code']['hex'])
    end = d['code']['layout']['code_region_end']
    lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
    dis = disassemble(code, end, tmp, lits)
    problems += [('ERROR', m) for m in code_vs_sites(sites, dis)]

    # 3. dense vs sparse, and the binary object vs JSON
    if not PROFILE:   # slice1 refuses --sparse-pc
        sp = run(p['code'], p['rip'], tmp, '--sparse-pc')
        if sp['code']['hex'][:2 * end] != d['code']['hex'][:2 * end]:
            problems.append(('ERROR', 'sparse and dense PC tables give different code bytes'))
    o = read_object(tmp / 'o.bin')
    if o['code'] != code or o['spill'] != d['code']['layout']['spill_bytes'] or len(o['sites']) != len(sites) or o['rip'] != int(p['rip'], 16):
        problems.append(('ERROR', 'object header/code disagrees with the JSON'))
    kinds = ['load', 'store', 'load_pair', 'store_pair', 'push', 'pop', 'guard', 'split', 'push_pair', 'pop_pair', 'other', 'divide', 'swap']
    for rec, s in zip(o['sites'], sites):
        if (rec[0], rec[1], kinds[rec[5]], rec[6]) != (s['native_offset'], s['guest_offset'], s['kind'], s['width']):
            problems.append(('ERROR', f'object site {rec[:4]} disagrees with JSON {s["native_offset"]}'))
        if rec[24] != int(s['located']) or rec[4] != s['imm']:
            problems.append(('ERROR', f'object site at {s["native_offset"]}: located/imm disagree with JSON'))
        link = s.get('access_native_offset') if s['kind'] in STUBS else s['guest_offset2']   # a stub's guest_offset2 is its access
        if rec[2] != (0xFFFFFFFF if link is None else link):
            problems.append(('ERROR', f'object site at {s["native_offset"]}: guest_offset2/access_native_offset disagree with JSON'))
    problems += [('ERROR', m) for m in object_vs_json(o, d)]
    return problems, len(sites)


def main():
    global FEXC, KNOWN_OK, PROFILE
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    KNOWN_OK = '--known-ok' in sys.argv
    PROFILE = ['--profile', 'slice1'] if '--slice1' in sys.argv else []
    if args:
        FEXC = args[0]
    programs = fixture_programs()
    bad = 0
    count = {'ERROR': 0, 'ELIMINATED': 0, 'NARROWED': 0}
    total = 0
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for p in programs:
            problems, n = check_program(p, tmp)
            total += n
            for kind, msg in problems:
                count[kind] += 1
                print(f"{kind:10} {p['name']}: {msg}")
            bad += any(k == 'ERROR' or (k == 'ELIMINATED' and not KNOWN_OK) for k, _ in problems)
    print(f'{len(programs)} programs, {total} sites; {count["ERROR"]} errors, {count["ELIMINATED"]} eliminated loads, '
          f'{count["NARROWED"]} narrowed bt accesses; {len(programs) - bad} programs pass')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
