import json, subprocess, sys, os, tempfile
def compile_block(fexc, hexs, rip, profile='slice1', extra=()):
    with tempfile.TemporaryDirectory() as d:
        out = d + '/j.json'
        rc = subprocess.run([fexc, '--profile', profile, '--hex', hexs, '--rip', hex(rip), '--out', out, *extra], capture_output=True, text=True)
        j = json.load(open(out))
    return j
def words(j):
    code = bytes.fromhex(j['code']['hex']); lay = j['code']['layout']
    end = lay['tail']['offset'] if lay['tail'] else len(code)
    return code, end, lay
def disasm(code, end, lits=()):
    with tempfile.TemporaryDirectory() as d:
        lits = set(lits)
        lines = ['.text', '_b:']
        i = 0
        while i < end:
            if i in lits:
                lines.append('.quad 0x%x' % int.from_bytes(code[i:i+8], 'little')); i += 8
            else:
                lines.append('.long 0x%08x' % int.from_bytes(code[i:i+4], 'little')); i += 4
        open(d + '/b.s', 'w').write('\n'.join(lines) + '\n')
        subprocess.run(['clang', '-c', '-arch', 'arm64', d + '/b.s', '-o', d + '/b.o'], check=True)
        out = subprocess.run(['xcrun', 'objdump', '-d', '--no-show-raw-insn', d + '/b.o'], capture_output=True, text=True).stdout
    import re
    return [l for l in out.splitlines() if re.match(r'\s+[0-9a-f]+:\s', l)]
if __name__ == '__main__':
    fexc, bench, profile = sys.argv[1:4]
    b = [x for x in json.load(open(__import__('pathlib').Path(__file__).resolve().parent.parent / 'benchmarks.json'))['benchmarks'] if x['name'] == bench][0]
    j = compile_block(fexc, b['code'], 0x100000, profile)
    code, end, lay = words(j)
    lits = [r['offset'] for r in j['code']['relocations'] if r['kind'] == 'guest_rip_literal']
    print(profile, 'status', j['status'], 'code_region', lay['code_region_begin'], lay['code_region_end'], 'end', end)
    print('\n'.join(disasm(code, end, lits)))
