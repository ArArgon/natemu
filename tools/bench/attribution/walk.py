import json, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from dis2 import *
F9 = str(__import__('pathlib').Path(__import__('os').environ['NATEMU_RAW_ROOT']) / 'fexc-native-20261026-01/series9/obj/fexc')  # series9 compiler (override: FEXC)
F9 = __import__('os').environ.get('FEXC', F9)
def blocks(b, fexc=F9, profile='slice1', extra=(), maxinsts=None):
    code = bytes.fromhex(b['code']); base = 0x100000; seen = {}; todo = [base] + ([0x100017] if b['name'] == 'call' else [])
    while todo:
        rip = todo.pop(0)
        if rip in seen or not (base <= rip < base + len(code)): continue
        j = compile_block(fexc, code[rip - base:].hex(), rip, profile, extra)
        seen[rip] = j
        for r in j['code']['relocations']:
            if r['kind'] == 'guest_rip_literal': todo.append(int(r['guest_rip'], 16))
    return seen
if __name__ == '__main__':
    name = sys.argv[1]; profile = sys.argv[2] if len(sys.argv) > 2 else 'slice1'; extra = sys.argv[3:]
    b = [x for x in json.load(open(__import__('pathlib').Path(__file__).resolve().parent.parent / 'benchmarks.json'))['benchmarks'] if x['name'] == name][0]
    for rip, j in sorted(blocks(b, F9, profile, extra).items()):
        code, end, lay = words(j)
        lits = [r['offset'] for r in j['code']['relocations'] if r['kind'] == 'guest_rip_literal']
        print(f"--- {name} block {rip:#x} guest_bytes {j['guest']['bytes_translated']} insns {j['guest']['instructions_translated']} end {j['guest']['end']}")
        print('\n'.join(disasm(code, end, lits)))
        print('sites', [(s['native_offset'], s['kind']) for s in j['sites']])
