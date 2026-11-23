#!/usr/bin/env python3
"""Compile ordinary compatibility-mode addressing and stack forms. Inspect the native operands without executing them."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402
import tempfile

sys.path.insert(0, str(testenv.ROOT / 'tools' / 'fexc'))
import check_sites
import verify_precision


# Guest bytes, followed by each expected access kind in instruction order. For POP [ESP+k] the explicit store follows
# the implicit stack read and uses the incremented ESP. verify_precision checks the split branch and pre-fault state.
CASES = {
    'mov eax,[ebp-12]': ('8b45f4', ['load']),
    'mov [ebx+ecx*4+16],eax': ('89448b10', ['store']),
    'mov eax,gs:[eax-8]': ('658b40f8', ['load']),
    'fs xlat': ('64d7', ['load']),
    'gs xlat': ('65d7', ['load']),
    'vmovups ymm0,[ebx+32]': ('c5fc104320', ['load_pair']),
    'push [ebx+8]': ('ff7308', ['load', 'push']),
    'push esp': ('54', ['push']),
    'pop ax': ('6658', ['pop']),
    'pop [esp+8]': ('8f442408', ['pop', 'store']),
}


# fexc reports the fault class ("ss" or "ds", the latter stands for every other segment).
# CMPS reads ES:EDI first and the overridable source second. MOVS reads its source first.
SEGMENTS = {
    'ss lodsd': ('36ad', [('load', 'ss')]),
    'ss rep movsd': ('36f3a5', [('load', 'ss'), ('store', 'ds')]),
    'ss repe cmpsd': ('36f3a7', [('load', 'ds'), ('load', 'ss')]),
    'ds cmpsd': ('3ea7', [('load', 'ds'), ('load', 'ds')]),
    'ss stosd': ('36ab', [('store', 'ds')]),
    'ss scasd': ('36af', [('load', 'ds')]),
    'ss xlat': ('36d7', [('load', 'ss')]),
    'ss mov eax,[moffs32]': ('36a178563412', [('load', 'ss')]),
    'ss mov [moffs32],eax': ('36a378563412', [('store', 'ss')]),
    'ds push [ebp]': ('3eff7500', [('load', 'ds'), ('push', 'ss')]),
}


def check_forms(compiler, options):
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, (code, kinds) in CASES.items():
            d = verify_precision.compile_(compiler, code, '0x401000', ['--bitness', '32', '--push-run', *options])
            if d['status'] != 'ok' or d['guest']['instructions_translated'] != 1:
                errors.append(f'{name}: failed to translate the instruction')
                continue
            sites = [s for s in d['sites'] if s['kind'] not in verify_precision.STUBS]
            if [s['kind'] for s in sites] != kinds:
                errors.append(f'{name}: expected {kinds}, got {[s["kind"] for s in sites]}')
                continue
            region = d['code']['layout']['code_region_end']
            dis = check_sites.disassemble(bytes.fromhex(d['code']['hex']), region, pathlib.Path(tmp))
            for s in sites:
                pc = s['native_offset']
                if f'[x{s["base_reg"]}]' not in dis[pc] or (
                    s['imm'], s['index_reg'], s['writeback']
                ) != (0, None, 'none'):
                    errors.append(f'{name}: {s["kind"]} must use its computed address: {dis[pc]}')
                base = s['base_reg']
                if s['kind'] != 'pop':
                    writes = [insn for off, insn in dis.items() if off < pc
                              and ('x', base) in verify_precision.writes(insn)]
                    if not writes or not writes[-1].partition(' ')[2].startswith(f'w{base},'):
                        errors.append(f'{name}: expected a 32-bit definition of the {s["kind"]} address, got {writes[-1:]}')
                else:
                    if dis[pc + 4] != f'add w{base}, w{base}, #{s["width"]:#x}':
                        errors.append(f'{name}: expected a 32-bit stack increment after the read, got {dis[pc + 4]}')
    for name, (code, expected) in SEGMENTS.items():
        d = verify_precision.compile_(compiler, code, '0x401000', ['--bitness', '32', '--push-run', *options])
        sites = d.get('sites', [])
        actual = [(s['kind'], s['segment']) for s in sites if s['kind'] not in verify_precision.STUBS]
        if actual != expected:
            errors.append(f'{name}: expected {expected}, got {actual}')
    return errors


def main(compiler):
    errors = []
    for size in ([], ['--max-insts', '1']):
        label = 'single' if size else 'default'
        errors.extend(f'{label}: {message}' for message in check_forms(compiler, size))
    if errors:
        sys.exit('\n'.join(errors))
    print(f'{len(CASES)} address and stack forms and {len(SEGMENTS)} segment cases passed at both block sizes')


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main(sys.argv[1])
