#!/usr/bin/env python3
"""Compile ordinary compatibility-mode addressing and stack forms. Inspect the native operands without executing them."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402
import tempfile

sys.path.insert(0, str(testenv.ROOT / 'tools' / 'fexc'))
import check_sites
import verify_precision


# Guest bytes, followed by each access in instruction order: its kind and its native instruction. 32-bit code runs under
# translation roots that map nothing above 4 GiB (docs/restructure/m2-checkpoint-c-plan.md, "Folded displacements behind a
# 4 GiB translation root"), so FEX folds a displacement under 16 KiB into the native operand, where it is added in 64 bits,
# and PUSH and POP step ESP's register with writeback; an address that should have wrapped aborts and the resident makes
# the access again. These are main's forms (035b243e, FEX 4eba851): tests/system `compat32-resident-limits` proves them
# exact (the mixed-user cases). An address with no folded part is a register of its own with a 32-bit definition. For
# POP [ESP+k] the explicit store follows the implicit stack read and uses the incremented ESP. verify_precision checks the
# split branch and pre-fault state.
CASES = {
    'mov eax,[ebp-12]': ('8b45f4', [('load', 'ldur w20, [x9, #-0xc]')]),
    'mov [ebx+ecx*4+16],eax': ('89448b10', [('store', 'str w4, [x20, #0x10]')]),
    'mov eax,gs:[eax-8]': ('658b40f8', [('load', 'ldur w20, [x20, #-0x8]')]),
    'fs xlat': ('64d7', [('load', 'ldrb w20, [x20]')]),
    'gs xlat': ('65d7', [('load', 'ldrb w20, [x20]')]),
    'vmovups ymm0,[ebx+32]': ('c5fc104320', [('load_pair', 'ldp q2, q3, [x6, #0x20]')]),
    'push [ebx+8]': ('ff7308', [('load', 'ldr w20, [x6, #0x8]'), ('push', 'str w20, [x8, #-0x4]!')]),
    'push esp': ('54', [('push', 'stur w8, [x8, #-0x4]')]),
    'pop ax': ('6658', [('pop', 'ldrh w20, [x8], #0x2')]),
    'pop [esp+8]': ('8f442408', [('pop', 'ldr w21, [x20], #0x4'), ('store', 'str w21, [x20, #0x8]')]),
    # Displacements of 4 KiB and more, below the 16 KiB at which FEX stops folding them.
    'mov eax,[ebx+0x2000]': ('8b8300200000', [('load', 'ldr w20, [x6, #0x2000]')]),
    'mov [ebx+0x2004],ecx': ('898b04200000', [('store', 'str w7, [x6, #0x2004]')]),
    'mov edx,[ebx+esi*4+0x2000]': ('8b94b300200000', [('load', 'ldr w20, [x20, #0x2000]')]),
    'mov eax,[ebp-0x2000]': ('8b8500e0ffff', [('load', 'ldr w20, [x9, x20, sxtx]')]),
    'mov al,[ebx+0x1001]': ('8a8301100000', [('load', 'ldrb w20, [x6, x20, sxtx]')]),
    'push [ebx+0x1000]': ('ffb300100000', [('load', 'ldr w20, [x6, #0x1000]'), ('push', 'str w20, [x8, #-0x4]!')]),
    'mov eax,[ebx+0x3ffc]': ('8b83fc3f0000', [('load', 'ldr w20, [x6, #0x3ffc]')]),
    'mov eax,[ebx-0x3fff]': ('8b8301c0ffff', [('load', 'ldr w20, [x6, x20, sxtx]')]),
    # Not folded: the address is a 32-bit sum (or constant) in a register of its own.
    'mov eax,[ebx+esi]': ('8b0433', [('load', 'ldr w20, [x20]')]),
    'mov eax,[ebx+0x4000]': ('8b8300400000', [('load', 'ldr w20, [x20]')]),
    'mov eax,[0xfffffff0]': ('a1f0ffffff', [('load', 'ldr w20, [x20]')]),
    'push eax': ('50', [('push', 'str w4, [x8, #-0x4]!')]),
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


# M2 checkpoint C: 32-bit code whose FS and GS are flat-limit segments (Linux's TLS: no --fs-limited, --gs-limited) has no
# operand check: its native code is what the compiler made before the checks existed. Guest bytes, then the first 16 hex
# digits of the SHA-256 of the unit's code (relocation slots zeroed) at 0x401000 with the arguments the host sends for
# such a unit (HOST below), as an ordinary unit and alone (--max-insts 1). The digests are this compiler's (FEX 13a1bfc)
# and were equal, form by form, to the code of main 035b243e's compiler (FEX 4eba851, built as the framework's
# `fexc_base`) when they were written. To regenerate after a deliberate change of that code: hash the same compiles.
HOST = ['--bitness', '32', '--push-run', '--narrow-cmp', '--ext-conds', '4']
UNCHECKED = {
    'mov eax,gs:[0x14]': ('65a114000000', '7777ff285dd55f9d', 'ba233807cbc4bb18'),
    'mov gs:[0x14],eax': ('65a314000000', '8501273a55d12c37', '9045089158622fbc'),
    'sub eax,gs:[0x14]': ('652b0514000000', 'da882028fe68a277', 'd06016d74b723983'),
    'xor ecx,gs:[0x14]': ('65330d14000000', '2566a9511d0e15d2', '12ca559807c3da8d'),
    'cmp eax,gs:[0x14]': ('653b0514000000', 'b1fcfa18c0a75c36', 'd52112a189fa9725'),
    'cmp dword gs:[0xc],0': ('65833d0c00000000', '8afa65f828a04444', '7e03004433ee0182'),
    'call gs:[0x10]': ('65ff1510000000', '5f01761cedb17203', '8b003701637685f0'),
    'mov eax,gs:[eax]': ('658b00', '463f3aac1d784e7f', '7cfa64b1f3061c9d'),
    'mov eax,gs:[eax+ecx*4]': ('658b0488', '31324c7237a96939', '756c0cc507ccc786'),
    'mov gs:[4],ecx': ('65890d04000000', 'cff366434d2d855b', '9e61a688efda4479'),
    'mov edx,gs:[8]': ('658b1508000000', '9f8a42950614a906', '3c40a27f0f784dfd'),
    'mov dword gs:[4],1': ('65c7050400000001000000', '42e8c6dc5a6e566f', 'f05936ee48e3b6de'),
    'movzx eax,byte gs:[4]': ('650fb60504000000', '2f336cc2503158d5', '58bee6dc8f1a291d'),
    'add gs:[eax],ecx': ('650108', '87a16038a6996bed', 'afc6026266557dec'),
    'inc dword gs:[eax]': ('65ff00', '3356d29be1b595a8', 'a7aaa63b2985eb48'),
    'lock cmpxchg gs:[ebx],ecx': ('f0650fb10b', '0791f1b252f28e3e', 'fdacabd98f852f36'),
    'lock xadd gs:[ebx],eax': ('f0650fc103', 'b8876165355d6aa5', '855a9213136df6d1'),
    'xchg gs:[ebx],eax': ('658703', '3cffe365e7c91a13', '05aca3f1bc8423e3'),
    'movss xmm0,gs:[eax]': ('f3650f1000', '7816b6eb9374a207', '41cfaee00f95390b'),
    'movsd xmm0,gs:[0x10]': ('f2650f100510000000', 'e10f03af1f2a2941', '51278ff5ff927018'),
    'movups xmm0,gs:[eax]': ('650f1000', '2d1bc9b9985e7345', '2283221162f1e96b'),
    'fld qword gs:[0x10]': ('65dd0510000000', 'd22068a40ffb3fbb', '7d07284a5010622c'),
    'push dword gs:[0x10]': ('65ff3510000000', '31eae0b16c374a27', '1922cc71c42db3a9'),
    'pop dword gs:[0x10]': ('658f0510000000', 'b9c63c1adabd264a', 'e41f528345a69bfb'),
    'gs xlat': ('65d7', '1d2a5beb2bd1cba0', 'a017b2aaaf126ac0'),
    'bt gs:[0x10],ebx': ('650fa31d10000000', '3f97f650c1be60ec', '9849d5d9a8afdfa4'),
    'mov eax,fs:[0]': ('64a100000000', '6c83adfab9dd5554', 'e949f359bec934e1'),
    'mov fs:[eax],edx': ('648910', 'c5c0cb688e21e897', '7169a1eee24a2841'),
}


def check_unchecked(compiler):
    import hashlib
    errors = []
    for name, (code, *digests) in UNCHECKED.items():
        for digest, size in zip(digests, ([], ['--max-insts', '1'])):
            d = verify_precision.compile_(compiler, code, '0x401000', [*HOST, *size])
            if d['status'] != 'ok' or d['guest']['instructions_translated'] != 1:
                errors.append(f'{name}: failed to translate the instruction')
                continue
            if any(s['kind'] == 'segment' for s in d['sites']):
                errors.append(f'{name}: an operand check without a limited segment')
            through = [s for s in d['sites'] if s['kind'] in ('load', 'store', 'swap', 'atomic', 'load_pair', 'store_pair')]
            if not through or any(s['segment'] != ('fs' if code.startswith('64') or code[2:4] == '64' else 'gs') for s in through):
                errors.append(f'{name}: the access sites of the operand do not carry its segment')
            got = hashlib.sha256(bytes.fromhex(d['code']['hex'])).hexdigest()[:16]
            if got != digest:
                errors.append(f'{name}{" (alone)" if size else ""}: the code is not that of the unchecked compiler ({got}, expected {digest})')
    return errors


def check_forms(compiler, options):
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, (code, accesses) in CASES.items():
            d = verify_precision.compile_(compiler, code, '0x401000', ['--bitness', '32', '--push-run', *options])
            if d['status'] != 'ok' or d['guest']['instructions_translated'] != 1:
                errors.append(f'{name}: failed to translate the instruction')
                continue
            sites = [s for s in d['sites'] if s['kind'] not in verify_precision.STUBS]
            region = d['code']['layout']['code_region_end']
            dis = check_sites.disassemble(bytes.fromhex(d['code']['hex']), region, pathlib.Path(tmp))
            got = [(s['kind'], dis[s['native_offset']]) for s in sites]
            if got != accesses:
                errors.append(f'{name}: expected {accesses}, got {got}')
                continue
            for s in sites:
                pc, base = s['native_offset'], s['base_reg']
                if (s['imm'], s['index_reg'], s['writeback']) != (0, None, 'none'):
                    continue   # folded: a constant added to the base in 64 bits
                writes = [insn for off, insn in dis.items() if off < pc and ('x', base) in verify_precision.writes(insn)]
                if not writes or not writes[-1].partition(' ')[2].startswith(f'w{base},'):
                    errors.append(f'{name}: expected a 32-bit definition of the {s["kind"]} address, got {writes[-1:]}')
    for name, (code, expected) in SEGMENTS.items():
        d = verify_precision.compile_(compiler, code, '0x401000', ['--bitness', '32', '--push-run', *options])
        sites = d.get('sites', [])
        actual = [(s['kind'], s['segment']) for s in sites if s['kind'] not in verify_precision.STUBS]
        if actual != expected:
            errors.append(f'{name}: expected {expected}, got {actual}')
    return errors


def check_inline_registers(compiler):
    # Inspect the actual ARM transfers, including a guest register used as both data and address.
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for reg, root in enumerate((4, 7, 5, 6, 8, 9, 10, 11)):
            for opcode in ('648b', '6589'):
                hexs = opcode + f'{(reg << 3) | 7:02x}'
                d = verify_precision.compile_(compiler, hexs, '0x401000', ['--bitness', '32'])
                site, = (s for s in d['sites'] if s['kind'] in ('load', 'store'))
                code = bytes.fromhex(d['code']['hex'])
                dis = check_sites.disassemble(code, d['code']['layout']['code_region_end'], pathlib.Path(tmp))
                at = site['native_offset']
                rt, = site['data_regs']
                if site['kind'] == 'store':
                    if rt != root or not dis[at].startswith(f'str w{root},'):
                        errors.append(f'{hexs}: store source does not hold its ModRM GPR: {dis[at]}')
                elif not dis[at].startswith(f'ldr w{rt},') or dis.get(at + 4) != f'mov x{root}, x{rt}':
                    errors.append(f'{hexs}: missing load result for its ModRM GPR: {dis[at]}, {dis.get(at + 4)}')
    for hexs in ('64678b0f', '6765890f'):
        d = verify_precision.compile_(compiler, hexs, '0x401000', ['--bitness', '32'])
        if d['status'] != 'no_code':
            errors.append(f'{hexs}: compiled a 16-bit-address dword MOV')
    return errors


def main(compiler):
    errors = check_inline_registers(compiler) + check_unchecked(compiler)
    for size in ([], ['--max-insts', '1']):
        label = 'single' if size else 'default'
        errors.extend(f'{label}: {message}' for message in check_forms(compiler, size))
    if errors:
        sys.exit('\n'.join(errors))
    print(f'{len(CASES)} address and stack forms, {len(SEGMENTS)} segment cases and {len(UNCHECKED)} unchecked FS/GS forms passed at both block sizes')


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main(sys.argv[1])
