#!/usr/bin/env python3
"""S1b1 check: the slice-1 precision guarantee, verified statically on the code fexc emits (profile slice1).

usage: verify_precision.py [--before FEXC0] [CORPUS.json ...]      (fexc from $FEXC, else the series1 build)

Corpora: fixtures/slice1/programs.json, the single-instruction matrix of scan_skips.py, the adversarial programs below
(one per precision patch), and every CORPUS.json given (gen_slice1.py format, e.g. a fresh `--seed` corpus).

For every compile, from the disassembly, the sites, the dense PC table and the static register (SRA) map:
  P1  between the native start of a site's guest instruction (its PC-table entry) and the site, no instruction writes a
      guest static register (the SRA map: 16 GPRs, PF x26, AF x27, the xmm registers), or stores to guest memory, or
      stores to the x28 frame other than the pre-NZCV save; the window holds no other guest instruction's entry;
  P2  no NZCV write in that window unless the site says nzcv=frame, and then `mrs xT, nzcv; str wT, [x28, #natemu_pre_nzcv]`
      comes first in the window, before any NZCV write;
  P3  a load site's destination is not a static register (DDI0487 D1.4.1.4: UNKNOWN after an abort); a writeback base
      (push/pop on RSP) is allowed, the access itself is its only write;
  P4  at most one store-class site (store, store_pair, push) per guest instruction, and it is the instruction's last site;
      no fused pair sites; every site says completed_effects=none;
  P5  no site can be skipped by a forward branch inside its instruction (scan_skips.skips);
  P6  for annotated programs (`insns`, the generator's flag annotations): for each site and each arithmetic flag, the last
      earlier instruction of the block that writes the flag, if it defines it (tools/reference/flags.py rules), has a
      native write of its location in its own range (NZCV for CF/ZF/SF/OF, x26 for PF, x27 for AF): no flag write that a
      later site observes was eliminated;
  P7  for programs with `asm` (fixtures, generated, adversarial): check_sites' comparison of expected accesses, sites,
      emitted code and the binary object, with no eliminated access.
Every mnemonic in a P1/P2 window must be classified below; an unknown one is reported, never assumed harmless.
Also prints the cost (native instructions and code bytes per translated guest instruction), and with --before the same
for an earlier build (e.g. s1c, without the patches) on the same inputs.
Exit status 1 on any violation.
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / 'tools/reference'))
import check_sites, scan_skips  # noqa: E402
import flags as F  # noqa: E402

FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series1/obj/fexc')

NZCV_WRITERS = {'adds', 'adcs', 'subs', 'sbcs', 'ands', 'bics', 'negs', 'ngcs', 'cmp', 'cmn', 'tst', 'ccmp', 'ccmn', 'fcmp',
                'fcmpe', 'fccmp', 'fccmpe', 'setf8', 'setf16', 'rmif', 'cfinv', 'axflag', 'xaflag'}
NO_DEST = {'cmp', 'cmn', 'tst', 'ccmp', 'ccmn', 'fcmp', 'fcmpe', 'fccmp', 'fccmpe', 'setf8', 'setf16', 'rmif', 'cfinv', 'axflag',
           'xaflag', 'msr', 'b', 'br', 'ret', 'cbz', 'cbnz', 'tbz', 'tbnz', 'nop', 'dmb', 'dsb', 'isb', 'brk', 'udf', 'hint', 'prfm'}
PAIR_LOADS = {'ldp', 'ldnp', 'ldpsw', 'ldxp', 'ldaxp'}
DATA = {'mov', 'movk', 'movz', 'movn', 'add', 'sub', 'and', 'orr', 'eor', 'bic', 'orn', 'eon', 'lsl', 'lsr', 'asr', 'ror', 'lslv',
        'lsrv', 'asrv', 'rorv', 'mul', 'madd', 'msub', 'mneg', 'smulh', 'umulh', 'smull', 'umull', 'smaddl', 'umaddl', 'sdiv', 'udiv',
        'csel', 'csinc', 'csinv', 'csneg', 'cset', 'csetm', 'cinc', 'cinv', 'cneg', 'sxtb', 'sxth', 'sxtw', 'uxtb', 'uxth', 'ubfx',
        'sbfx', 'ubfiz', 'sbfiz', 'bfi', 'bfxil', 'bfc', 'bfm', 'ubfm', 'sbfm', 'extr', 'rev', 'rev16', 'rev32', 'rbit', 'clz', 'cls',
        'adc', 'sbc', 'neg', 'ngc', 'mvn', 'mrs', 'adr', 'adrp', 'bl', 'blr', 'cnt', 'fmov', 'uaddlv'} | NZCV_WRITERS | PAIR_LOADS


def reg(tok):
    """('x', n) for a general register, ('v', n) for a SIMD/FP register, None for zr/sp/anything else."""
    tok = tok.strip().split('.')[0]
    if m := re.fullmatch(r'[wx](\d+)', tok):
        return ('x', int(m[1]))
    if m := re.fullmatch(r'[bhsdqv](\d+)', tok):
        return ('v', int(m[1]))
    return None


def effects(text):
    """(mnemonic, written registers, writes NZCV, native access or None) of one disassembled instruction."""
    mn, _, ops = text.partition(' ')
    pre = ops.split('[')[0]
    toks = [t for t in pre.split(',') if t.strip()]
    acc = check_sites.native_access(text)
    dests = set()
    if mn.startswith('st') and acc:
        if mn.startswith(('stxr', 'stlxr', 'stxp', 'stlxp')):
            dests.add(reg(toks[0]))
    elif mn in PAIR_LOADS:
        dests |= {reg(toks[0]), reg(toks[1])}
    elif mn in ('bl', 'blr'):
        dests.add(('x', 30))
    elif mn not in NO_DEST and not mn.startswith('b.') and toks:
        dests.add(reg(toks[0]))
    if acc and acc[5] != 'none':
        dests.add(reg(acc[2]))
    nzcv = mn in NZCV_WRITERS or (mn == 'msr' and 'nzcv' in ops.lower())
    known = mn in DATA or mn in NO_DEST or mn.startswith('b.') or bool(acc)
    return mn, {d for d in dests if d}, nzcv, acc, known


def flag_writes(a):
    """(written, defined) arithmetic-flag masks of one annotated instruction (flags.py rules); a shift or rotate by an
    unknown count may write nothing, so it defines nothing."""
    op = a['op']
    if op in F.NO_FLAGS:
        return 0, 0
    if op in F.SHIFTS | F.ROTATES:
        if a.get('count') is None:
            return F.ALL6, 0
        if not a['count'] & (0x3f if a['width'] == 64 else 0x1f):
            return 0, 0
    w = {'inc': F.ALL6 & ~F.CF, 'dec': F.ALL6 & ~F.CF}.get(op, F.CF | F.OF if op in F.ROTATES else
                                                         F.CF | F.OF | F.SF | F.AF | F.PF if op in F.BITTEST else F.ALL6)
    try:
        return w, w & ~F.step(a, 0)
    except ValueError:
        return w, 0


def stale_flags(d, insns, dis, order):
    """P6 violations: a flag write a later site observes has no native write in its instruction's range."""
    starts = {}
    for e in d['pc_table']['entries']:
        starts[e['guest_offset']] = max(starts.get(e['guest_offset'], -1), e['native_offset'])
    gl = [i['offset'] for i in d['guest']['instructions'] if i['translated']]
    ends = [starts.get(g, None) for g in gl[1:]] + [max(starts.values())]
    wrote = []
    for k, g in enumerate(gl):
        eff = [effects(dis[o]) for o in order if starts[g] <= o < ends[k]]
        wrote.append({'nzcv': any(e[2] for e in eff), 'x26': any(('x', 26) in e[1] for e in eff),
                      'x27': any(('x', 27) in e[1] for e in eff)})
    out = []
    site_insns = sorted({gl.index(s['guest_offset']) for s in d['sites']})
    for k in site_insns:
        for f, loc in ((F.CF, 'nzcv'), (F.ZF, 'nzcv'), (F.SF, 'nzcv'), (F.OF, 'nzcv'), (F.PF, 'x26'), (F.AF, 'x27')):
            i = next((i for i in range(k - 1, -1, -1) if flag_writes(insns[i])[0] & f), None)
            if i is not None and flag_writes(insns[i])[1] & f and not wrote[i][loc]:
                out.append(f'guest +{gl[k]} has a site, but flag {f:#x} defined by guest +{gl[i]} ({insns[i]["op"]}) has no native '
                           f'{loc} write (eliminated)')
    return out


def verify(d, tmp, insns=None):
    """Violations of P1-P6 for one compile result (P6 needs the generator's annotations `insns`)."""
    out = []
    code = bytes.fromhex(d['code']['hex'])
    end = d['code']['layout']['code_region_end']
    lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
    dis = check_sites.disassemble(code, end, tmp, lits)
    order = sorted(dis)
    sabi = d['state_abi']
    sra = {('x', n) for n in sabi['static_registers_gpr_order_rax_rcx_rdx_rbx_rsp_rbp_rsi_rdi_r8_to_r15_then_pf_af']}
    sra |= {('v', n) for n in sabi['static_registers_xmm0_up']}
    pre = sabi['state_offsets'].get('natemu_pre_nzcv')
    entries = [(e['native_offset'], e['guest_offset']) for e in d['pc_table']['entries']]
    name = lambda r: ('x%d' if r[0] == 'x' else 'v%d') % r[1]
    by_insn = {}
    for s in d['sites']:
        g, at, start = s['guest_offset'], s['native_offset'], s['insn_native_offset']
        tag = f'site +{at} ({s["kind"]}, guest +{g})'
        by_insn.setdefault(g, []).append(s)
        if (start, g) not in entries:
            out.append(f'{tag}: instruction start +{start} is not a PC-table entry of guest +{g}')
        if any(start < n <= at and og != g for n, og in entries):
            out.append(f'{tag}: another guest instruction starts between +{start} and the site')
        if s['completed_effects'] != 'none':
            out.append(f'{tag}: completed_effects={s["completed_effects"]}')
        if s['kind'] in ('push_pair', 'pop_pair', 'other'):
            out.append(f'{tag}: kind {s["kind"]} (fused or undescribed access)')
        saved, first_nzcv, mrs = None, None, {}
        for off in (o for o in order if start <= o < at):
            text = dis[off]
            mn, dests, nzcv, acc, known = effects(text)
            if not known:
                out.append(f'{tag}: unclassified instruction +{off} "{text}"')
            for r in dests:   # mrs = registers currently holding the NZCV read at the instruction start
                mrs.pop(r, None)
            if mn == 'mrs' and 'nzcv' in text.lower() and first_nzcv is None:
                mrs.update({r: off for r in dests})
            if nzcv and first_nzcv is None:
                first_nzcv = off
            if acc and acc[0] == 'store':
                if acc[2] == 'x28' and acc[4] == pre and acc[5] == 'none' and text.split()[1].startswith('w') and \
                        reg(text.split()[1].rstrip(',')) in mrs and first_nzcv is None and saved is None:
                    saved = off
                elif acc[2] != 'sp':
                    out.append(f'{tag}: store +{off} "{text}" before the site')
            bad = dests & sra
            if bad:
                out.append(f'{tag}: +{off} "{text}" writes static register {",".join(sorted(map(name, bad)))} before the site')
        if first_nzcv is not None and s['nzcv'] != 'frame':
            out.append(f'{tag}: NZCV written at +{first_nzcv} "{dis[first_nzcv]}" before the site, nzcv={s["nzcv"]}')
        if s['nzcv'] == 'frame' and saved is None:
            out.append(f'{tag}: nzcv=frame but no pre-NZCV save before the first NZCV write')
        if s['kind'] in ('load', 'pop', 'load_pair', 'pop_pair') and s['data_class'] == 'gpr':
            bad = {('x', r) for r in s['data_regs']} & sra
            if bad:
                out.append(f'{tag}: load destination {",".join(sorted(map(name, bad)))} is a static register')
    for g, ss in by_insn.items():
        stores = [s for s in ss if s['kind'] in ('store', 'store_pair', 'push', 'push_pair')]
        if len(stores) > 1:
            out.append(f'guest +{g}: {len(stores)} store sites')
        if stores and stores[-1]['native_offset'] != max(s['native_offset'] for s in ss):
            out.append(f'guest +{g}: the store site +{stores[-1]["native_offset"]} is not the last access of the instruction')
    out += [f'skippable site: {s}' for s in scan_skips.skips(d, tmp)]
    if insns:
        out += stale_flags(d, insns, dis, order)
    return out


def cost(d, tmp):
    """(native instructions, code bytes) of the whole code region; literals are not instructions."""
    end = d['code']['layout']['code_region_end']
    lits = sum(r['size'] for r in d['code']['relocations'] if r['size'])
    return (end - lits) // 4, end


def compile_(fexc, hexs, rip):
    r = subprocess.run([fexc, '--profile', 'slice1', '--hex', hexs, '--rip', rip], capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


ADVERSARIAL = {   # one or more per precision patch; registers point into the generator's data/stack pages (not executed)
    'pooled-constant-store-then-gpr': ['mov qword ptr [rbx], 0x12345', 'mov rax, 0x12345'],
    'pooled-constant-twice': ['mov qword ptr [rbx], 0x12345', 'mov qword ptr [rsi], 0x12345', 'mov rax, 0x12345'],
    'cmp-mem-dead-flags': ['cmp qword ptr [rbx], 5', 'add rax, rcx'],
    'test-bt-mem-dead-flags': ['test dword ptr [rsi], 1', 'sub rax, rax', 'bt qword ptr [rdi], 3', 'xor eax, eax'],
    'push-pop-pairs': ['push rax', 'push rbx', 'pop rcx', 'pop rdx', 'push 7', 'push rsp', 'pop rsp'],
    'rmw-add-carry-in-out': ['sub rax, rcx', 'add qword ptr [rbx], rcx', 'adc rdx, 0'],
    'rmw-forms': ['add byte ptr [rbx], 1', 'and qword ptr [rbx], rcx', 'inc qword ptr [rsi]', 'neg dword ptr [rdi]',
                  'sub rax, rdx', 'sbb qword ptr [rbx], rax', 'shl qword ptr [rbx], cl', 'btc qword ptr [rdi], rax', 'not word ptr [rsi]'],
    'flags-before-store-kept': ['add rax, rcx', 'mov qword ptr [rbx], rdx', 'sub rax, rcx'],
    'load-destinations': ['mov rax, qword ptr [rbx]', 'mov ecx, dword ptr [rsi]', 'movzx edx, byte ptr [rdi]', 'pop r8',
                          'cmovb r9, qword ptr [rbx]', 'imul r10, qword ptr [rsi]'],
    'memory-stack-forms': ['pop qword ptr [rbx]', 'push qword ptr [rsi]', 'setb byte ptr [rbx]', 'xchg rax, rcx', 'mov qword ptr [rdi], rax'],
    'undefined-flags-before-access': ['add rax, rcx', 'shl qword ptr [rbx], 3', 'mul qword ptr [rsi]', 'imul rax, qword ptr [rbx]'],
}


def adversarial():
    import ref, gen_slice1
    progs = []
    for name, asm in ADVERSARIAL.items():
        progs.append({'name': name, 'code': ref.assemble('\n'.join(asm)).hex(), 'rip': '0x100000', 'asm': asm,
                      'insns': [gen_slice1.annotate(a, 0 if a.endswith(', cl') else None) | ({'count': None} if a.endswith(', cl') else {})
                                for a in asm]})
    return progs


def run_corpus(label, progs, tmp, before=None):
    """Verify every program; returns (violations, compiled, totals)."""
    bad, ok, sites = 0, 0, 0
    tot = {'g': 0, 'i': 0, 'b': 0, 'g0': 0, 'i0': 0, 'b0': 0}
    for p in progs:
        d = compile_(FEXC, p['code'], p['rip'])
        if d['status'] != 'ok':
            continue
        ok += 1
        sites += len(d['sites'])
        problems = verify(d, tmp, p.get('insns'))
        if 'asm' in p:
            check_sites.FEXC, check_sites.PROFILE = FEXC, ['--profile', 'slice1']
            cp, _ = check_sites.check_program(p, tmp)
            problems += [f'{k}: {m}' for k, m in cp if k != 'NARROWED']
        for m in problems:
            print(f'  {label} {p["name"]}: {m}')
        bad += bool(problems)
        g = d['guest']['instructions_translated']
        i, b = cost(d, tmp)
        tot['g'] += g; tot['i'] += i; tot['b'] += b
        if before:
            d0 = compile_(before, p['code'], p['rip'])
            if d0['status'] == 'ok' and d0['guest']['instructions_translated'] == g:
                i0, b0 = cost(d0, tmp)
                tot['g0'] += g; tot['i0'] += i0; tot['b0'] += b0
    line = f'{label}: {len(progs)} programs, {ok} compiled, {sites} sites, {bad} with violations'
    if tot['g']:
        line += f'; per guest insn {tot["i"] / tot["g"]:.2f} native insns, {tot["b"] / tot["g"]:.1f} code bytes'
    if before and tot['g0']:
        line += f' (before: {tot["i0"] / tot["g0"]:.2f} insns, {tot["b0"] / tot["g0"]:.1f} bytes over {tot["g0"]} guest insns)'
    print(line)
    return bad


def main():
    global FEXC
    args = sys.argv[1:]
    before = None
    if '--before' in args:
        k = args.index('--before'); before = args[k + 1]; del args[k:k + 2]
    scan_skips.FEXC = FEXC
    fixtures = json.loads((REPO / 'fixtures/slice1/programs.json').read_text())['programs']
    matrix = [{'name': n, 'code': h, 'rip': '0x102000'} for n, h in scan_skips.matrix().items()]
    corpora = [('fixtures', fixtures), ('matrix', matrix), ('adversarial', adversarial())]
    for a in args:
        corpora.append((Path(a).name, json.loads(Path(a).read_text())['programs']))
    bad = 0
    with tempfile.TemporaryDirectory() as t:
        for label, progs in corpora:
            bad += run_corpus(label, progs, Path(t), before if label != 'matrix' else None)
    print('PASS' if not bad else f'FAIL: {bad} programs with violations')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
