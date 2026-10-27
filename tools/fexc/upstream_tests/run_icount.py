#!/usr/bin/env python3
"""Run FEX's own InstructionCountCI suite (unittests/InstructionCountCI/**/*.json) through fexc.

Upstream flow (Scripts/InstructionCountParser.py + Source/Tools/CodeSizeValidation/Main.cpp + ctest):
  nasm each test ("BITS n;" + x86Insts or the key) -> CodeSizeValidation compiles ONE block of x86InstCount guest
  instructions at RIP 0x10000, with the file's Bitness / Enabled|DisabledHostFeatures / Env, disassembles the block with
  vixl, drops the entry header and everything from the first "udf #0x420f".  ctest PASSES iff the number of remaining
  lines == ExpectedInstructionCount (text is NOT compared by ctest).  CI additionally regenerates ExpectedArm64ASM
  (vixl text, " (addr" suffix stripped, adr/adrp cut at " #") and fails on `git diff`, so text equality is the real gate.
We do both: count (ctest rule) and text (CI rule, with the vendored vixl disassembler built by build_vixl()).

usage: run_icount.py --src .../unittests/InstructionCountCI --fexc FEXC --work DIR [--variant upstream|fexc-default]
                     [--pad N] [--jobs N] [--only SUBSTR] [--limit N]
Variants (what the unspecified feature bits are):
  upstream     the x86-CI view FEX's HostFeatures.cpp gives under the VIXL simulator (CPUFeaturesAll minus AFP/RPRES), +
               multiblock on (FEX's config default, CodeSizeValidation never changes it)
  fexc-default fexc's own default feature table (Apple M4 view) and fexc's default multiblock off
The file's Enabled/Disabled lists, TSO and Bitness are applied on top in both.  --pad N appends N zero bytes after the
code (upstream's code window is 512 MiB of zero-filled anonymous memory; fexc gets exactly the bytes).
Writes DIR/<variant>-pad<N>.jsonl (one record per test) and prints per-file totals.
"""
import argparse, base64, concurrent.futures as cf, glob, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# upstream HostFeatures enum name -> fexc feature table names (fexc.cpp FeatureTable()).  CRYPTO fans out as in
# HostFeatures.cpp OverrideFeatures.
FEATURE_MAP = {
    'SVE128': ['sve128'], 'SVE256': ['sve256'], 'CLZERO': ['clzero'], 'RNG': ['rng'], 'FCMA': ['fcma'], 'CSSC': ['cssc'],
    'AFP': ['afp'], 'RPRES': ['rpres'], 'FLAGM': ['flagm'], 'FLAGM2': ['flagm2'],
    'CRYPTO': ['aes', 'crc32', 'sha', 'pmull128', 'x86_aes256'], 'AES256': ['x86_aes256'], 'SVEBITPERM': ['svebitperm'],
    'LRCPC': ['lrcpc'], 'LRCPC2': ['lrcpc2'], 'FRINTTS': ['frintts'], 'MOPS': ['mops'], 'I8MM': ['i8mm'], 'DOTPROD': ['dotprod'],
    # TSO is a config switch, handled separately
}
# The x86-CI (VIXL simulator) view: HostFeatures.cpp FetchHostFeatures with CPUFeaturesAll minus AFP and RPRES, then
# OverrideFeatures (atomics + preserve_all always forced on; SVE256 only via ForceSVEWidth).
UPSTREAM_BASE = dict(
    atomics_lse=1, lrcpc=1, lrcpc2=1, sve128=1, sve256=0, preserve_all_abi=1, rpres=0, wfxt=1, flagm=1, flagm2=1, fcma=1,
    crc32=1, aes=1, sha=1, pmull128=1, rng=1, cssc=1, afp=0, frintts=1, mops=1, i8mm=1, dotprod=1, ecv=1, clzero=1,
    svebitperm=1, float_exceptions=0, cache_maintenance_ops=1, prefer_zva_for_vzero=0, x86_avx=1, x86_aes256=1,
    x86_3dnow=1, x86_sse4a=0)
KNOWN_ENV = {}          # FEX_* environment options fexc can express (none: it only has multiblock/tso)


def settings_of(feat, variant):
    """feat = the file's Features dict -> (bitness, feature overrides list [(name,0/1)], tso, missing-knob list)"""
    bits = int(feat.get('Bitness', 64))
    en = [x.upper() for x in feat.get('EnabledHostFeatures', [])]
    dis = [x.upper() for x in feat.get('DisabledHostFeatures', [])]
    f = dict(UPSTREAM_BASE) if variant == 'upstream' else {}
    for x in en:
        for n in FEATURE_MAP.get(x, []):
            f[n] = 1
    for x in dis:
        for n in FEATURE_MAP.get(x, []):
            f[n] = 0
    # SVE256 is ForceSVEWidth>=256 (Main.cpp sets 256 only for an enabled SVE256; the disable bit has no effect on it)
    f['sve256'] = 1 if 'SVE256' in en else 0
    if variant == 'fexc-default':
        if 'SVE256' not in en:
            f.pop('sve256')          # keep fexc's default (off) -> same thing
    tso = 'TSO' in en and 'TSO' not in dis
    missing = [f'{k}={v}' for k, v in feat.get('Env', {}).items() if k not in KNOWN_ENV]
    return bits, sorted(f.items()), tso, missing


def load_tests(src, only=None):
    """Mirror InstructionCountParser.parse_json_data: skip Skip==YES, x86Insts or the key."""
    out = []
    for path in sorted(glob.glob(os.path.join(src, '**', '*.json'), recursive=True)):
        rel = os.path.relpath(path, src)
        if only and only not in rel:
            continue
        d = json.load(open(path))
        feat = d.get('Features', {})
        for key, it in d['Instructions'].items():
            if it.get('Skip', '').upper() == 'YES':
                continue
            insts = it.get('x86Insts', [key])
            out.append(dict(file=rel, name=key, feat=feat, insts=insts, exp_count=int(it.get('ExpectedInstructionCount', 0)),
                            exp_asm=it.get('ExpectedArm64ASM')))
    return out


HEADER = {}


def nop_header(a, bits):
    d = json.loads(subprocess.run([a.fexc, '--hex', '90', '--rip', '0x10000', '--bitness', str(bits), '--max-insts', '1'],
                                  capture_output=True, text=True).stdout)
    lay = d['code']['layout']
    return (lay['end_marker_offset'] - lay['code_region_begin']) // 4


def run_one(t, a):
    r = dict(file=t['file'], name=t['name'], exp_count=t['exp_count'], exp_asm=t['exp_asm'], x86=t['insts'])
    bits, feats, tso, missing = settings_of(t['feat'], a.variant)
    r['missing_knobs'] = missing
    r['bitness'] = bits
    tag = base64.urlsafe_b64encode(f"{t['file']}|{t['name']}".encode()).decode()[:60] + str(abs(hash((t['file'], t['name']))))
    asm = os.path.join(a.tmp, tag + '.asm'); binf = os.path.join(a.tmp, tag + '.bin')
    with open(asm, 'w') as f:
        f.write(f'BITS {bits};\n' + ''.join(i + '\n' for i in t['insts']))
    p = subprocess.run(['nasm', asm, '-o', binf], capture_output=True, text=True)
    if p.returncode:
        r['error'] = 'nasm: ' + p.stderr.strip()[:300]
        return r
    code = open(binf, 'rb').read()
    os.remove(asm); os.remove(binf)
    r['code'] = code.hex()
    cmd = [a.fexc, '--hex', (code + b'\0' * a.pad).hex(), '--rip', '0x10000', '--bitness', str(bits), '--max-insts', str(len(t['insts']))]
    if a.variant == 'upstream' and not a.no_multiblock:
        cmd.append('--multiblock')
    if not tso:
        cmd.append('--no-tso')
    for n, v in feats:
        cmd += ['--feature', f'{n}={v}']
    p = subprocess.run(cmd, capture_output=True, text=True)
    r['rc'] = p.returncode
    try:
        d = json.loads(p.stdout)
    except Exception:
        r['error'] = 'fexc: rc=%d %s' % (p.returncode, (p.stderr or p.stdout)[:300]); return r
    r['status'] = d.get('status'); r['diag'] = d.get('diagnostics', [])
    # 'no_instructions_translated' still carries code: FEX's invalid-instruction block (SIGILL raise), which upstream counts too
    if not d.get('code', {}).get('layout'):
        r['error'] = 'fexc status ' + str(d.get('status')); return r
    raw = bytearray.fromhex(d['code']['hex'])
    for rel in d['code']['relocations']:       # put the literals back (fexc zeroes them in `hex`)
        if not rel.get('size') or 'emitted_value' not in rel:
            continue
        raw[rel['offset']:rel['offset'] + rel['size']] = int(rel['emitted_value'], 16).to_bytes(rel['size'], 'little')
    lay = d['code']['layout']
    # upstream drops HeaderSize lines, HeaderSize = the length of the disassembly of a compiled NOP (CalculateBaseStats): the
    # fixed entry prologue.  fexc's body_native_offset also skips the spill-slot `sub sp` that FEX emits right after it,
    # which upstream counts as part of the body.
    begin = lay['code_region_begin']; body = begin + 4 * HEADER[bits]
    end = lay['end_marker_offset'] if lay.get('end_marker_offset') is not None else lay['tail']['offset']
    r['header_words'] = (body - begin) // 4
    r['words'] = [raw[o:o + 4][::-1].hex() for o in range(body, end, 4)]     # little-endian word -> hex value
    r['actual_count'] = len(r['words'])
    r['guest'] = dict(n=d['guest']['instructions_translated'], end=d['guest']['end']['reason'])
    return r


def build_vixl(work):
    exe = os.path.join(work, 'vixl_disasm')
    if os.path.exists(exe):
        return exe
    v = os.path.join(a_src_root, 'External/vixl/src')
    os.makedirs(os.path.join(work, 'vx'), exist_ok=True)
    srcs = ['compiler-intrinsics-vixl', 'cpu-features', 'utils-vixl'] + ['aarch64/' + n for n in (
        'decoder-aarch64', 'disasm-aarch64', 'instructions-aarch64', 'registers-aarch64', 'operands-aarch64', 'cpu-aarch64',
        'cpu-features-auditor-aarch64', 'pointer-auth-aarch64')]
    flags = ['-std=c++17', '-O1', '-w', '-include', 'map', '-include', 'string', '-DVIXL_INCLUDE_TARGET_AARCH64=1', '-I' + v]
    objs = []
    for s in srcs + [None]:
        o = os.path.join(work, 'vx', (os.path.basename(s) if s else 'main') + '.o'); objs.append(o)
        subprocess.run(['clang++', *flags, '-c', os.path.join(HERE, 'vixl_disasm.cc') if s is None else f'{v}/{s}.cc', '-o', o], check=True)
    subprocess.run(['clang++', *objs, '-o', exe], check=True)
    return exe


def sanitize(line):
    # Main.cpp SanitizeDisassembly
    i = line.find(' (addr')
    if i >= 0:
        line = line[:i]
    if 'adrp ' in line or 'adr ' in line:
        j = line.find(' #')
        if j >= 0:
            line = line[:j]
    return line


def main():
    global a_src_root
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True); ap.add_argument('--fexc', required=True); ap.add_argument('--work', required=True)
    ap.add_argument('--variant', choices=['upstream', 'fexc-default'], default='upstream')
    ap.add_argument('--pad', type=int, default=0); ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--only'); ap.add_argument('--limit', type=int); ap.add_argument('--no-multiblock', action='store_true')
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    a_src_root = os.path.abspath(os.path.join(a.src, '..', '..'))
    os.makedirs(a.work, exist_ok=True)
    a.tmp = tempfile.mkdtemp(prefix='icount', dir=a.work)
    tests = load_tests(a.src, a.only)[:a.limit]
    for b in (32, 64):
        HEADER[b] = nop_header(a, b)
    print('NOP header words (HeaderSize):', HEADER)
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(lambda t: run_one(t, a), tests))
    os.rmdir(a.tmp)
    # text
    vx = build_vixl(a.work)
    stdin = ''.join(''.join(w + '\n' for w in r.get('words', [])) + '#\n' for r in res)
    out = subprocess.run([vx], input=stdin, capture_output=True, text=True, check=True).stdout.split('\n')
    groups, cur = [], []
    for l in out[:-1]:
        if l == '#':
            groups.append(cur); cur = []
        else:
            cur.append(sanitize(l))
    assert len(groups) == len(res)
    for r, g in zip(res, groups):
        if 'words' not in r:
            r['result'] = 'error'; continue
        r['actual_asm'] = g
        cnt_ok = r['actual_count'] == r['exp_count']
        # upstream's regenerated json omits ExpectedArm64ASM for zero-instruction tests
        txt_ok = g == r['exp_asm'] if r['exp_asm'] is not None else (g == [] and r['exp_count'] == 0)
        r['count_ok'] = cnt_ok; r['text_ok'] = txt_ok
        r['result'] = 'pass' if cnt_ok and txt_ok else 'count_ok_text_differs' if cnt_ok else 'count_mismatch'
        del r['words']
    name = f'{a.variant}-pad{a.pad}{a.tag}.jsonl'
    with open(os.path.join(a.work, name), 'w') as f:
        for r in res:
            f.write(json.dumps(r) + '\n')
    per = {}
    for r in res:
        per.setdefault(r['file'], {}).setdefault(r['result'], 0)
        per[r['file']][r['result']] += 1
    tot = {}
    for f_, c in sorted(per.items()):
        n = sum(c.values())
        print(f"{f_:50s} n={n:4d} pass={c.get('pass', 0):4d} count_ok_text_differs={c.get('count_ok_text_differs', 0):4d} "
              f"count_mismatch={c.get('count_mismatch', 0):4d} error={c.get('error', 0):3d}")
        for k, v in c.items():
            tot[k] = tot.get(k, 0) + v
    print('TOTAL', len(res), tot)


if __name__ == '__main__':
    main()
