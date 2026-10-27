#!/usr/bin/env python3
"""C1b smoke check: compile a handful of inputs with fexc, then disassemble the
native block with Apple's llvm-based objdump. Usage: smoke.py FEXC_BINARY OUT_DIR"""
import json, subprocess, sys
from pathlib import Path

here = Path(__file__).resolve().parent
repo = here.parent.parent
sys.path.insert(0, str(repo / 'tools/i4v_producer'))
from export import CASES  # existing pinned bodies, used only as sample inputs

fexc, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
programs = {p['name']: p for p in json.loads((repo / 'fixtures/slice1/programs.json').read_text())['programs']}
inputs = [  # (tag, hex, rip, bitness, max_insts)
    ('case-memory_data64', CASES['fex_memory_data64']['source'], 0xffff800000102000, 64, 1),
    ('case-address_store_ecx', CASES['fex_address_store_ecx']['source'], 0x1000, 32, 1),
    ('prog-edge-carry-chain', programs['edge-carry-chain']['code'], 0x100000, 64, 0),
    ('prog-edge-rip-relative', programs['edge-rip-relative']['code'], 0x100000, 64, 0),
    ('prog-random-04-high', programs['random-04']['code'], 0xffff800000100000, 64, 0),
]
for tag, hexs, rip, bits, mx in inputs:
    cmd = [fexc, '--hex', hexs, '--rip', hex(rip), '--bitness', str(bits), '--out', str(out / f'{tag}.json')]
    if mx: cmd += ['--max-insts', str(mx)]
    rc = subprocess.run(cmd).returncode
    d = json.loads((out / f'{tag}.json').read_text())
    code = bytes.fromhex(d['code']['hex'])
    lay = d['code']['layout']
    end = lay['tail']['offset'] if lay['tail'] else len(code)
    # Wrap the code region in a Mach-O object so objdump will disassemble it.
    asm = ['.text', '.globl _block', '_block:'] + [
        '.long 0x%08x' % int.from_bytes(code[o:o + 4], 'little') for o in range(0, end, 4)]
    (out / f'{tag}.s').write_text('\n'.join(asm) + '\n')
    subprocess.run(['clang', '-c', '-arch', 'arm64', str(out / f'{tag}.s'), '-o', str(out / f'{tag}.o')], check=True)
    dis = subprocess.run(['xcrun', 'objdump', '-d', '--no-show-raw-insn', str(out / f'{tag}.o')], capture_output=True, text=True, check=True).stdout
    (out / f'{tag}.dis').write_text(dis)
    g = d['guest']
    print(f"{tag}: rc={rc} status={d['status']} guest={g['instructions_translated']} insts / {g['bytes_translated']} B, "
          f"native={d['code']['size']} B, relocs={len(d['code']['relocations'])}, pc_entries={len(d['pc_table']['entries'])}, "
          f"end={g['end']['reason']}")
