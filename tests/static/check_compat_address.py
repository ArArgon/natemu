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
    # Displacements of 4 KiB and more, below the 16 KiB at which FEX stopped folding them into a 64-bit native operand.
    'mov eax,[ebx+0x2000]': ('8b8300200000', ['load']),
    'mov [ebx+0x2004],ecx': ('898b04200000', ['store']),
    'mov edx,[ebx+esi*4+0x2000]': ('8b94b300200000', ['load']),
    'mov eax,[ebp-0x2000]': ('8b8500e0ffff', ['load']),
    'mov al,[ebx+0x1001]': ('8a8301100000', ['load']),
    'push [ebx+0x1000]': ('ffb300100000', ['load', 'push']),
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
# such a unit (HOST below), as an ordinary unit and alone (--max-insts 1), from the fexc of FEX fc4270d ("Compute compat effective addresses and stack
# updates at 32-bit width", the fork commit before the first check) with natemu 30793c6b's tools/fexc. To regenerate after a
# deliberate change of that code: build that pair (tools/fexc/build.sh with FEXC_PIN and FEXC_TOOLS, or the two worktrees and
# cmake as build.sh does) and hash the same compiles.
HOST = ['--bitness', '32', '--push-run', '--narrow-cmp', '--ext-conds', '4']
UNCHECKED = {
    'mov eax,gs:[0x14]': ('65a114000000', 'd9a153c9a98f8fe2', '719da5a5c89bc73f'),
    'mov gs:[0x14],eax': ('65a314000000', 'a3492b7f878a2147', 'fffa026083902e07'),
    'sub eax,gs:[0x14]': ('652b0514000000', 'f171ae82bc431791', '8b99c1c546025a5c'),
    'xor ecx,gs:[0x14]': ('65330d14000000', 'f04092a0c4a42cfb', '9c72046e0fcc3a1d'),
    'cmp eax,gs:[0x14]': ('653b0514000000', '5e64cf53c220e487', '981bc44f6711b008'),
    'cmp dword gs:[0xc],0': ('65833d0c00000000', 'd16b197f2afc7139', '1257541eed5ae0b2'),
    'call gs:[0x10]': ('65ff1510000000', 'f12d3ebb83b64ad2', '00dd21aaf121a431'),
    'mov eax,gs:[eax]': ('658b00', '463f3aac1d784e7f', '7cfa64b1f3061c9d'),
    'mov eax,gs:[eax+ecx*4]': ('658b0488', '31324c7237a96939', '756c0cc507ccc786'),
    'mov gs:[4],ecx': ('65890d04000000', '3542321ac515ddb5', '79f0dfccc5ce710f'),
    'mov edx,gs:[8]': ('658b1508000000', '0b5add951389fdf2', 'f9d9c55968f58b39'),
    'mov dword gs:[4],1': ('65c7050400000001000000', 'f9535a6aac80a847', 'ab8d2121b98c2ac3'),
    'movzx eax,byte gs:[4]': ('650fb60504000000', '91c6584d45e8cedf', '9b3612a7aeb7c502'),
    'add gs:[eax],ecx': ('650108', '87a16038a6996bed', 'afc6026266557dec'),
    'inc dword gs:[eax]': ('65ff00', '3356d29be1b595a8', 'a7aaa63b2985eb48'),
    'lock cmpxchg gs:[ebx],ecx': ('f0650fb10b', '0791f1b252f28e3e', 'fdacabd98f852f36'),
    'lock xadd gs:[ebx],eax': ('f0650fc103', 'b8876165355d6aa5', '855a9213136df6d1'),
    'xchg gs:[ebx],eax': ('658703', '3cffe365e7c91a13', '05aca3f1bc8423e3'),
    'movss xmm0,gs:[eax]': ('f3650f1000', '7816b6eb9374a207', '41cfaee00f95390b'),
    'movsd xmm0,gs:[0x10]': ('f2650f100510000000', '3074a1d33d7b8ab5', '10d9bd3d58e674e7'),
    'movups xmm0,gs:[eax]': ('650f1000', '2d1bc9b9985e7345', '2283221162f1e96b'),
    'fld qword gs:[0x10]': ('65dd0510000000', '28f38504563fda5a', '5c72280f60095982'),
    'push dword gs:[0x10]': ('65ff3510000000', '4cef0316ad6fb72f', '4c04c2f5ff87f5e2'),
    'pop dword gs:[0x10]': ('658f0510000000', 'de0f816327db88e0', 'bcdcab7ba24f1295'),
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
