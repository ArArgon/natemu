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
    'vmovups ymm0,[ebx+32]': ('c5fc104320', ['load_pair']),
    'push [ebx+8]': ('ff7308', ['load', 'push']),
    'push esp': ('54', ['push']),
    'pop ax': ('6658', ['pop']),
    'pop [esp+8]': ('8f442408', ['pop', 'store']),
}


def main(compiler):
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, (code, kinds) in CASES.items():
            d = verify_precision.compile_(compiler, code, '0x401000', ['--bitness', '32'])
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
    if errors:
        sys.exit('\n'.join(errors))
    print(f'{len(CASES)} compatibility-mode addressing and stack forms passed')


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main(sys.argv[1])
