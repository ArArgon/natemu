#!/usr/bin/env python3
"""Guest memory must not access the generated-code state alias.

Run under the shared VM lock. Noncanonical final linear addresses require #GP(0)
(#SS(0) for SS operands), even when Arm maps those bytes at EL0. Faulting first
accesses must preserve the prefix state. Zero-count REP performs no access.
With a distant --guest-state-alias override, legacy operands test unmapped
holes; run_frame_randomization.py separately records the selected-page hole.
Public tests are unchanged; this runner uses architectural expectations, not the
old guest-state-page deviation. --max-block defaults to the normal block size.
"""
import argparse
import hashlib
import json
import pathlib
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/reference')]
import ref

CODE, DATA, HIGH = 0x100000, 0x200000, 0xffff800000200000
FRAME, MASK = 0xffff400000001000, (1 << 64) - 1
SEGMENT = 0xffff800000000000
VALUE = 0x123456789abcdef0
PREFIX = f'fninit\nfld qword ptr [{DATA + 0x180:#x}]\nvmovdqu ymm1, [{DATA + 0x100:#x}]\n'
STATE = ('gpr', 'rflags', 'xmm', 'ymmh', 'mxcsr', 'x87', 'memory')


def cases():
    out = []

    def add(name, insn, regs=None, fault=13, **kw):
        out.append(dict(name=name, insn=insn, regs=regs or {}, fault=fault, **kw))

    forms = [('byte-load', 'mov al, byte ptr [rdi]'), ('byte-store', 'mov byte ptr [rdi], cl'),
             ('word-load', 'mov ax, word ptr [rdi]'), ('dword-load', 'mov eax, dword ptr [rdi]'),
             ('qword-load', 'mov rax, qword ptr [rdi]'), ('qword-store', 'mov qword ptr [rdi], rax'),
             ('rmw-add', 'add qword ptr [rdi], 1'), ('rmw-neg', 'neg qword ptr [rdi]'),
             ('lock-add', 'lock add qword ptr [rdi], rax'), ('lock-xadd', 'lock xadd qword ptr [rdi], rax'),
             ('lock-add-byte', 'lock add byte ptr [rdi], al'), ('lock-add-word', 'lock add word ptr [rdi], ax'),
             ('lock-add-dword', 'lock add dword ptr [rdi], eax'),
             ('lock-cmpxchg', 'lock cmpxchg qword ptr [rdi], rbx'), ('xchg', 'xchg qword ptr [rdi], rax'),
             ('lock-neg', 'lock neg qword ptr [rdi]'), ('lock-not', 'lock not qword ptr [rdi]'),
             ('lock-bts', 'lock bts qword ptr [rdi], 0'),
             ('vector-load', 'movups xmm2, [rdi]'), ('vector-store', 'movups [rdi], xmm1'),
             ('pair-load', 'vmovdqu ymm2, [rdi]'), ('pair-store', 'vmovdqu [rdi], ymm1'),
             ('x87-load', 'fld qword ptr [rdi]'), ('x87-store', 'fstp dword ptr [rdi]'),
             ('x87-load80', 'fld tbyte ptr [rdi]'), ('x87-store80', 'fstp tbyte ptr [rdi]')]
    for name, insn in forms:
        add(name, insn, {7: FRAME + 0x800})
    add('cmpxchg16b-ds', 'lock cmpxchg16b [rdi]', {7: FRAME + 0x800})
    add('cmpxchg16b-ss', 'lock cmpxchg16b [rsp]', {4: FRAME + 0x800}, fault=12)
    add('cmpxchg16b-misaligned-ss', 'lock cmpxchg16b [rsp]', {4: FRAME + 0x801})
    # Effective-address formation, including bases which are not themselves final linear addresses.
    add('positive-displacement', 'mov rax, [rdi + 0x780]', {7: FRAME + 0x80})
    add('negative-displacement', 'mov rax, [rdi - 0x80]', {7: FRAME + 0x880})
    add('scaled-index', 'mov rax, [rdi + rcx*8 + 0x100]', {7: FRAME + 0x400, 1: 0x80})
    segment_offset = (FRAME + 0x800 - SEGMENT) & MASK
    add('fs-final-address', 'mov rax, fs:[rdi]', {7: segment_offset}, setup=[f'fsbase {SEGMENT:#x}'])
    add('gs-final-address', 'mov gs:[rdi], rax', {7: segment_offset}, setup=[f'gsbase {SEGMENT:#x}'])
    add('ss-rsp', 'mov rax, [rsp]', {4: FRAME + 0x800}, fault=12)
    add('ss-rbp', 'mov [rbp + 8], rax', {5: FRAME + 0x7f8}, fault=12)
    add('fs-overrides-ss', 'mov rax, fs:[rsp]', {4: segment_offset}, setup=[f'fsbase {SEGMENT:#x}'])
    # Address-size truncation happens before adding a canonical FS/GS base.
    # A zero-extended32 offset cannot reach FRAME from any canonical segment base.
    for segment in ('fs', 'gs'):
        add('address32-' + segment + '-noncanonical-final', f'mov rax, {segment}:[edi]',
            {7: FRAME + 0x800}, setup=[f'{segment}base 0x7ffffffff000'])
    # Inputs not wholly inside the exposed page still must fault without effects.
    for name, addr in [('before-page', FRAME - 4), ('end-page', FRAME + 0xffc),
                       ('next-page', FRAME + 0x1000), ('low-canonical-end', 0x7ffffffffffc)]:
        add(name + '-load', 'mov rax, [rdi]', {7: addr})
        add(name + '-store', 'mov [rdi], rax', {7: addr})
    add('vector-page-end', 'vmovdqu ymm2, [rdi]', {7: FRAME + 0xff0})
    add('lock-page-end', 'lock add qword ptr [rdi], rax', {7: FRAME + 0xffc})
    add('bt-index-final-address', 'bt qword ptr [rdi], rcx', {7: FRAME - 8, 1: 64})
    # REP zero-count ignores even noncanonical source/destination addresses.
    for op in ('movsb', 'movsq', 'stosb', 'stosq'):
        add('rep-zero-' + op, 'rep ' + op, {1: 0, 6: FRAME, 7: FRAME}, fault=None, unchanged=True)
        add('rep-destination-' + op, 'rep ' + op, {1: 64, 6: DATA, 7: FRAME + 0x800})
    for name, addr in [('inside', FRAME + 0x800), ('before', FRAME - 8), ('end', FRAME + 0xff8)]:
        add('rep-source-' + name, 'rep movsb', {1: 64, 6: addr, 7: DATA + 0x200})
    add('rep-backward-source', 'rep movsq', {1: 2, 6: FRAME + 0xff8, 7: DATA + 0x208}, flags=0xed7)
    # Real canonical memory on both sides of the sign-extension boundary.
    for name, address in [('low', DATA), ('high', HIGH)]:
        memory = [(address, VALUE.to_bytes(8, 'little'))] if address == HIGH else []
        add('canonical-' + name + '-load', 'mov rax, [rdi]', {7: address}, fault=None,
            memory=memory, wantregs={0: VALUE})
        add('canonical-' + name + '-store', 'mov [rdi], rax', {7: address}, fault=None,
            memory=memory, wantmem=[(address, 8, 0x1122334455667788)])
    add('noncanonical-base-valid-displacement', 'mov rax, [rdi + rcx]',
        {7: FRAME, 1: (DATA - FRAME) & MASK}, fault=None, wantregs={0: VALUE})
    add('wrapped-ea-valid', 'mov rax, [rdi + 0x200020]', {7: MASK - 0x1f}, fault=None, wantregs={0: VALUE})
    add('fs-cancellation-valid', 'mov rax, fs:[rdi]', {7: (DATA - SEGMENT) & MASK}, fault=None,
        setup=[f'fsbase {SEGMENT:#x}'], wantregs={0: VALUE})
    add('address32-fs-high-valid', 'mov rax, fs:[edi]', {7: (FRAME & ~0xffffffff) | DATA}, fault=None,
        setup=[f'fsbase {SEGMENT:#x}'], memory=[(HIGH, VALUE.to_bytes(8, 'little'))], wantregs={0: VALUE})
    add('address32-gs-store-valid', 'mov gs:[edi], rax', {7: FRAME + 0x800}, fault=None,
        setup=[f'gsbase {DATA - 0x1800:#x}'], wantmem=[(DATA, 8, 0x1122334455667788)])
    add('address32-wrap-valid', 'mov rax, fs:[edi + 0x10]', {7: 0xdeadbeeffffffff8}, fault=None,
        setup=[f'fsbase {DATA - 8:#x}'], wantregs={0: VALUE})
    add('canonical-vector-roundtrip', f'vmovdqu [{DATA + 0x240:#x}], ymm1', fault=None,
        wantbytes=[(DATA + 0x240, bytes(range(32)))])
    add('canonical-rep-copy', 'rep movsb', {1: 32, 6: DATA + 0x100, 7: DATA + 0x240}, fault=None,
        wantregs={1: 0, 6: DATA + 0x120, 7: DATA + 0x260}, wantbytes=[(DATA + 0x240, bytes(range(32)))])
    add('legitimate-frame-fields', f'fnstcw word ptr [{DATA + 0x240:#x}]\nstmxcsr dword ptr [{DATA + 0x244:#x}]',
        fault=None, wantmem=[(DATA + 0x240, 2, 0x37f), (DATA + 0x244, 4, 0x1f80)])
    # Review additions remain separately selectable from the initial75-case run.
    for name, mnemonic, register, width in [('movaps', 'movaps', 'xmm2', 16), ('vmovaps', 'vmovaps', 'ymm2', 32)]:
        for store in (False, True):
            insn = f'{mnemonic} [rsp], {register}' if store else f'{mnemonic} {register}, [rsp]'
            tag = f'extra-{name}-{"store" if store else "load"}'
            add(tag + '-aligned-ss', insn, {4: FRAME + 0x800}, fault=12)
            # MOVAPS/VMOVAPS alignment faults are #GP even on an SS-selected address.
            add(tag + '-misaligned-ss', insn, {4: FRAME + 0x800 + width // 2})
    add('extra-push-stack', 'push rax', {4: FRAME + 0x808}, fault=12)
    add('extra-pop-stack', 'pop rax', {4: FRAME + 0x800}, fault=12)
    add('extra-call-stack', 'call rax', {0: CODE, 4: FRAME + 0x808}, fault=12)
    add('extra-ret-stack', 'ret', {4: FRAME + 0x800}, fault=12)
    # MISC_ENABLE resets to0x1801 (fast strings enabled). TF/AC are clear;
    # the live host requests --bulk, so these stores cover both sides of a page.
    for op, count, pattern in [('stosb', 128, b'\x88'), ('stosq', 64, (0x1122334455667788).to_bytes(8, 'little'))]:
        add('extra-valid-bulk-' + op, 'rep ' + op, {1: count, 7: DATA + 0xff0}, fault=None,
            wantregs={1: 0, 7: DATA + 0xff0 + count * len(pattern)}, wantbytes=[(DATA + 0xff0, pattern * count)])
    return out


def description(c, cpl, prefix_only=False):
    regs = [0x1122334455667788, 0xaa, 0x5566778899aabbcc, VALUE, DATA + 0x1f00, DATA,
            DATA + 0x100, DATA, *range(8, 16)]
    for r, value in c['regs'].items():
        regs[r] = value
    raw = bytearray(0x2000)
    raw[:8] = VALUE.to_bytes(8, 'little')
    raw[0x100:0x120] = bytes(range(32))
    raw[0x180:0x188] = struct.pack('<d', 1e300)  # a faulting f32 store must not publish overflow/inexact
    head = ref.assemble(PREFIX)
    code = head + (b'' if prefix_only else ref.assemble(c['insn']))
    lines = [f'rip {CODE:#x}', f'cpl {cpl}', f'rflags {c.get("flags", 0xad7):#x}', 'sse 1', 'xcr0 7', 'ne 1',
             'gpr ' + ' '.join(hex(r) for r in regs), f'code {code.hex()}', f'memory {DATA:#x} {raw.hex()}']
    lines += [f'memory {a:#x} {b.hex()}' for a, b in c.get('memory', [])]
    lines += c.get('setup', [])
    return '\n'.join(lines) + '\n', CODE + len(head), CODE + len(code)


def memory(got, address, size):
    for m in got['memory']:
        start = ref.num(m['addr'])
        data = bytes.fromhex(m['data'])
        if start <= address and address + size <= start + len(data):
            return data[address - start:address - start + size]
    raise ValueError(f'missing output memory at {address:#x}')


def judge(c, before, got, fault_rip, end):
    errors = []
    if c['fault'] is not None:
        want = [c['fault'], 0, 0, fault_rip]
        if got['stop'] != 'guest_fault' or list(map(ref.num, got.get('fault', []))) != want:
            errors.append(f'fault {got["stop"]} {got.get("fault")}, expected {want}')
    elif got['stop'] != 'reached' or ref.num(got['rip']) != end:
        errors.append(f'completion {got["stop"]} at {got["rip"]}, expected {end:#x}')
    if not got.get('frame_intact', False):
        errors.append('frame sentinel changed')
    if c['fault'] is not None or c.get('unchanged'):
        errors += [f'{key} changed' for key in STATE if got.get(key) != before.get(key)]
    else:
        # All successful controls are moves/REP moves or state reads: only the
        # explicitly named destination registers/bytes may change, never flags.
        errors += [f'{key} changed' for key in STATE if key not in ('gpr', 'memory') and got.get(key) != before.get(key)]
        regs = list(map(ref.num, before['gpr']))
        for r, value in c.get('wantregs', {}).items():
            regs[r] = value
        if list(map(ref.num, got['gpr'])) != regs:
            errors.append('unexpected register effect')
        changes = [(a, v.to_bytes(n, 'little')) for a, n, v in c.get('wantmem', [])] + c.get('wantbytes', [])
        expected = [dict(m) for m in before['memory']]
        for address, value in changes:
            for m in expected:
                base = ref.num(m['addr'])
                raw = bytearray.fromhex(m['data'])
                if base <= address and address + len(value) <= base + len(raw):
                    raw[address - base:address - base + len(value)] = value
                    m['data'] = raw.hex()
                    break
            else:
                errors.append(f'expected destination {address:#x} is not captured')
        if got['memory'] != expected:
            errors.append('unexpected memory effect')
    for r, value in c.get('wantregs', {}).items():
        if ref.num(got['gpr'][r]) != value:
            errors.append(f'register {ref.REGS[r]} != {value:#x}')
    for address, size, value in c.get('wantmem', []):
        if memory(got, address, size) != value.to_bytes(size, 'little'):
            errors.append(f'memory {address:#x} != {value:#x}')
    for address, value in c.get('wantbytes', []):
        if memory(got, address, len(value)) != value:
            errors.append(f'memory {address:#x} differs')
    return errors


def execute(a, text, work):
    work.mkdir(parents=True)
    (work / 'program.txt').write_text(text)
    cmd = [str(a.host), str(a.runtime), 'run-program', str(a.fexc), str(work / 'program.txt'), str(work),
           '--watchdog-ms', '5000', '--code-shape'] + ([str(a.max_block)] if a.max_block else [])
    if a.guest_state_alias is not None:
        cmd += ['--guest-state-alias', hex(a.guest_state_alias)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        (work / 'stdout').write_text(run.stdout)
        (work / 'stderr').write_text(run.stderr)
        return json.loads(run.stdout) if run.returncode == 0 else None, run.returncode
    except subprocess.TimeoutExpired as e:
        (work / 'stderr').write_text(f'timeout after30 seconds: {e.stderr!r}')
        return None, 'timeout'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--runtime', type=pathlib.Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--max-block', type=int)
    ap.add_argument('--guest-state-alias', type=lambda s: int(s, 0), help='test-driver override; tested addresses and strict architectural expectations stay unchanged')
    ap.add_argument('--only', default='')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)  # never overwrite an earlier attempted run
    inputs = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in (a.host, a.runtime, a.fexc)}
    inputs['guest_state_alias_override'] = a.guest_state_alias
    (a.out / 'inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
    rows = []
    for c in cases():
        if a.only not in c['name']:
            continue
        for cpl in (0, 3):
            name = f'{c["name"]}-cpl{cpl}'
            text, at, end = description(c, cpl)
            before, rc0 = execute(a, description(c, cpl, True)[0], a.out / name / 'prefix')
            got, rc = execute(a, text, a.out / name / 'probe')
            errors = ([f'prefix failed: {rc0}'] if not before or before['stop'] != 'reached' else
                      [f'probe failed: {rc}'] if not got else judge(c, before, got, at, end))
            rows.append(dict(name=name, max_block=a.max_block, errors=errors))
            print(name, 'FAIL' if errors else 'PASS', errors, flush=True)
    (a.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    print(f'{len(rows)} cases, {sum(bool(r["errors"]) for r in rows)} failed', flush=True)
    return int(not rows or any(r['errors'] for r in rows))


if __name__ == '__main__':
    sys.exit(main())
