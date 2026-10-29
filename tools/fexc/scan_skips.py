"""Scan for conditional control flow inside ONE guest instruction's native range that can skip a recorded access site.

A site is skippable if, in the same guest instruction (PC-table entry range), a forward branch before the site targets
an offset after the site. The check is on the emitted code, so it holds for whatever FEX generated.

usage: scan_skips.py [FEXC]   -- scans a matrix of memory-operand forms and the 64 fixture programs under slice1.
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_sites  # noqa: E402

FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series18/obj/fexc')
BR = [re.compile(r'b\.\w+\s+0x([0-9a-f]+)$'), re.compile(r'(?:cbz|cbnz)\s+\w+,\s+0x([0-9a-f]+)$'),
      re.compile(r'(?:tbz|tbnz)\s+\w+,\s+#\d+,\s+0x([0-9a-f]+)$'), re.compile(r'b\s+0x([0-9a-f]+)$')]


def skips(d, tmp):
    """[(guest_offset, branch_native_offset, target, site_native_offset)] for one compile result."""
    code = bytes.fromhex(d['code']['hex'])
    end = d['code']['layout']['code_region_end']
    lits = [(r['offset'], r['size']) for r in d['code']['relocations'] if r['size']]
    dis = check_sites.disassemble(code, end, tmp, lits)
    starts = sorted({(e['native_offset'], e['guest_offset']) for e in d['pc_table']['entries']})
    bounds = [s[0] for s in starts] + [end]
    found = []
    stubs = {s['native_offset'] for s in d['sites'] if s['kind'] in check_sites.STUBS}   # leave to the runtime, not skips
    for s in d['sites']:
        if s['kind'] in check_sites.STUBS:
            continue
        i = max(k for k, (n, _) in enumerate(starts) if n <= s['insn_native_offset'])
        lo = starts[i][0]
        for off, text in dis.items():
            if not (lo <= off < s['native_offset']):
                continue
            for pat in BR:
                m = pat.match(text)
                if m and int(m.group(1), 16) > s['native_offset'] and int(m.group(1), 16) not in stubs:
                    found.append((starts[i][1], off, int(m.group(1), 16), s['native_offset'], text))
    return found


def run(hexs, rip=0x102000, *extra):
    r = subprocess.run([FEXC, '--profile', 'slice1', '--hex', hexs, '--rip', hex(rip), *extra], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout)


def modrm(reg, rm=0, mod=0):
    return '%02x' % ((mod << 6) | (reg << 3) | rm)


def matrix():
    """name -> hex of single instructions with a memory operand [rax] (plus the register form for contrast)."""
    out = {}
    pre = {8: '', 16: '66', 32: '', 64: '48'}
    for size in (8, 16, 32, 64):
        for mn, reg in (('rol', 0), ('ror', 1), ('rcl', 2), ('rcr', 3), ('shl', 4), ('shr', 5), ('sar', 7)):
            b = size == 8
            for form, op, tail in (('1', 'd0' if b else 'd1', ''), ('cl', 'd2' if b else 'd3', ''), ('0', 'c0' if b else 'c1', '00'),
                                   ('5', 'c0' if b else 'c1', '05'), ('31', 'c0' if b else 'c1', '1f'), ('32', 'c0' if b else 'c1', '20'),
                                   ('255', 'c0' if b else 'c1', 'ff')):
                for mem in (True, False):
                    out[f'{mn}{size} {"[rax]" if mem else "rcx"},{form}'] = pre[size] + op + (modrm(reg) if mem else modrm(reg, 1, 3)) + tail
        # I1: shld/shrd (16-bit cut), cmpxchg/xadd without LOCK, popcnt/tzcnt/lzcnt, BMI (32/64)
        if size in (32, 64):
            for mn, op2 in (('shld', 'a4'), ('shrd', 'ac')):
                for c in (0, 1, 5, 31, 32, 63, 255):
                    out[f'{mn}{size} [rax],rcx,{c}'] = pre[size] + '0f' + op2 + modrm(1) + '%02x' % c
                out[f'{mn}{size} [rax],rcx,cl'] = pre[size] + '0f' + '%02x' % (int(op2, 16) + 1) + modrm(1)
            for mn, op2 in (('popcnt', 'b8'), ('tzcnt', 'bc'), ('lzcnt', 'bd')):
                out[f'{mn}{size} rcx,[rax]'] = 'f3' + pre[size] + '0f' + op2 + modrm(1)
            w = int(size == 64)
            vex = lambda m, pp, op, reg=1, vvvv=2: 'c4%02x%02x%s' % (0xe0 | m, w << 7 | (~vvvv & 15) << 3 | pp, op) + modrm(reg)
            for mn, h in (('andn', vex(2, 0, 'f2')), ('bextr', vex(2, 0, 'f7')), ('bzhi', vex(2, 0, 'f5')), ('shlx', vex(2, 1, 'f7')),
                          ('sarx', vex(2, 2, 'f7')), ('shrx', vex(2, 3, 'f7')), ('mulx', vex(2, 3, 'f6')), ('blsr', vex(2, 0, 'f3', 1)),
                          ('blsmsk', vex(2, 0, 'f3', 2)), ('blsi', vex(2, 0, 'f3', 3)), ('rorx', vex(3, 3, 'f0', 1, 0) + '07')):
                out[f'{mn}{size} [rax]'] = h
        if size == 16:
            out['popcnt16 cx,[rax]'] = '66f30fb8' + modrm(1)
        for mn, op2 in (('cmpxchg', 'b0' if size == 8 else 'b1'), ('xadd', 'c0' if size == 8 else 'c1')):
            out[f'{mn}{size} [rax],rcx'] = pre[size] + '0f' + op2 + modrm(1)
        # bit tests
        for mn, op2 in (('bt', 'a3'), ('bts', 'ab'), ('btr', 'b3'), ('btc', 'bb')):
            if size != 8:
                out[f'{mn}{size} [rax],rcx'] = pre[size] + '0f' + op2 + modrm(1)
        for name, reg in (('bt', 4), ('bts', 5), ('btr', 6), ('btc', 7)):
            if size != 8:
                out[f'{name}{size} [rax],imm'] = pre[size] + '0fba' + modrm(reg) + '05'
        if size != 8:
            for cc in (0x4, 0x5, 0xc, 0xf):
                out[f'cmov{cc:x}{size} rcx,[rax]'] = pre[size] + '0f4%x' % cc + modrm(1)
            out[f'bsf{size} rcx,[rax]'] = pre[size] + '0fbc' + modrm(1)
            out[f'bsr{size} rcx,[rax]'] = pre[size] + '0fbd' + modrm(1)
            out[f'imul{size} rcx,[rax]'] = pre[size] + '0faf' + modrm(1)
            out[f'imul{size} rcx,[rax],7'] = pre[size] + '6b' + modrm(1) + '07'
            out[f'movzx{size} rcx,byte[rax]'] = pre[size] + '0fb6' + modrm(1)
            out[f'movsx{size} rcx,byte[rax]'] = pre[size] + '0fbe' + modrm(1)
        for mn, op in (('add', 0), ('or', 1), ('adc', 2), ('sbb', 3), ('and', 4), ('sub', 5), ('xor', 6), ('cmp', 7)):
            b = size == 8
            out[f'{mn}{size} [rax],rcx'] = pre[size] + '%02x' % (op * 8 + (0 if b else 1)) + modrm(1)
            out[f'{mn}{size} rcx,[rax]'] = pre[size] + '%02x' % (op * 8 + (2 if b else 3)) + modrm(1)
            out[f'{mn}{size} [rax],imm'] = pre[size] + ('80' if b else '83') + modrm(op) + '05'
        b = size == 8
        for mn, reg in (('test', 0), ('not', 2), ('neg', 3), ('mul', 4), ('imul', 5), ('div', 6), ('idiv', 7)):
            out[f'{mn}{size} [rax]'] = pre[size] + ('f6' if b else 'f7') + modrm(reg) + ('01' if reg == 0 and b else '01000000' if reg == 0 and size == 32 else '0100' if reg == 0 and size == 16 else '01000000' if reg == 0 else '')
        for mn, reg in (('div', 6), ('idiv', 7)):
            out[f'{mn}{size} rcx'] = pre[size] + ('f6' if b else 'f7') + modrm(reg, 1, 3)
        out[f'inc{size} [rax]'] = pre[size] + ('fe' if b else 'ff') + modrm(0)
        out[f'dec{size} [rax]'] = pre[size] + ('fe' if b else 'ff') + modrm(1)
        out[f'mov{size} [rax],rcx'] = pre[size] + ('88' if b else '89') + modrm(1)
        out[f'mov{size} rcx,[rax]'] = pre[size] + ('8a' if b else '8b') + modrm(1)
        out[f'test{size} [rax],rcx'] = pre[size] + ('84' if b else '85') + modrm(1)
    for cc in range(16):
        out[f'set{cc:x} [rax]'] = '0f9%x' % cc + modrm(0)
    out.update({'push [rax]': 'ff30', 'pop [rax]': '8f00', 'call [rax]': 'ff10', 'jmp [rax]': 'ff20', 'push rax': '50', 'pop rax': '58',
                'push r9': '4151', 'pop r9': '4159', 'push ax': '6650', 'pop ax': '6658', 'movsxd rcx,[rax]': '486308',
                'xchg rax,rcx': '4891', 'lea': '488d0408', 'leave [rbp]': 'c9', 'enter [rsp]': 'c8100000', 'pushf [rsp]': '9c',
                'movnti [rax],rcx': '480fc308', 'prefetcht0 [rax]': '0f1808', 'prefetchnta [rax]': '0f1800', 'movabs al,[moffs]': 'a00102030405060708', 'movabs [moffs],rax': '48a30102030405060708'})
    return out


def scan():
    """(cut {name: end reason}, problems {name: first finding}, fixtures scanned)."""
    check_sites.FEXC = FEXC
    bad, cut = {}, {}
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, h in matrix().items():
            d = run(h)
            if d['status'] != 'ok' or d['guest']['instructions_translated'] != 1:
                cut[name] = d['guest']['end']['reason']
                continue
            s = skips(d, tmp)
            if s:
                bad[name] = s[0]
            elif '[' in name and 'lea' not in name and not name.startswith('prefetch') and not d['sites']:   # prefetch: a hint, no access
                bad[name] = ('no site at all', 'the memory access is not emitted')
        programs = check_sites.fixture_programs()
        fixtures = 0
        for p in programs:
            d = run(p['code'], int(p['rip'], 16))
            if d['status'] != 'ok':
                continue
            s = skips(d, tmp)
            fixtures += 1
            if s:
                bad['fixture ' + p['name']] = s[0]
    return cut, bad, fixtures


def main():
    global FEXC
    if len(sys.argv) > 1:
        FEXC = sys.argv[1]
    cut, bad, fixtures = scan()
    n = len(matrix())
    print(f'{n} single-instruction forms ({len(cut)} cut by the profile) and {fixtures} fixture programs scanned; {len(bad)} with a skippable or missing site')
    for k, v in bad.items():
        print(' ', k, v)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
