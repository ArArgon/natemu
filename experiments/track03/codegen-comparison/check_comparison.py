#!/usr/bin/env python3
"""Independent finite guest oracle for the consolidated branch/FP/DIV probe."""
import argparse
from fractions import Fraction
import importlib.util
import json
import math
import hashlib
import random
import statistics
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('comparison_execution_oracle', HERE / 'check_execution.py')
execution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(execution)
MASK, DATA, BASE = execution.MASK, execution.DATA, execution.BASE
require = execution.require
NAMES = ['nop', 'branch_zero', 'branch_carry', 'fp_addsub', 'fp_muldiv', 'fp_sqrt', 'integer_div']


def guest_result(name, variant, n, initial):
    """Guest-level result, independent of native allocation and wrapper branches."""
    require(name in NAMES and variant in (0, 1) and n > 0, 'guest case domain')
    result = dict(initial)
    taken = 0
    flags, flag_mask = None, 0
    entry = initial.get('entry', 0x401000)
    final_rip = entry + dict(nop=1, branch_zero=16, branch_carry=15, fp_addsub=8,
                             fp_muldiv=8, fp_sqrt=4, integer_div=8)[name]
    if name.startswith('branch_'):
        result['rax'] = DATA + 128
        result['xmm0'] = bytes(x ^ y for x, y in zip(initial['xmm0'], initial['xmm1'])) if n % 2 else initial['xmm0']
        if name == 'branch_zero':
            require(initial['rcx'] == variant, 'branch-zero initial RCX')
            result['rcx'] = variant ^ (n & 1)
            taken = n // 2 + int(variant == 1 and n % 2 == 1)
            last_taken = result['rcx'] == 0
            flags = (int(last_taken) << 6) | (int(bin(result['rcx'] & 255).count('1') % 2 == 0) << 2)
            flag_mask = 0x8c5  # AF is undefined after TEST.
            final_rip = entry + (32 if last_taken else 16)
        else:
            require(initial['rcx'] == (MASK if variant == 0 else MASK >> 1) and initial['rdx'] == 1, 'branch-carry initial operands')
            a, b, s, d = execution.last_add(variant, n)
            result['rcx'], result['rdx'] = s, d
            flags, flag_mask = execution.add_flags(a, b, s), 0x8d5
            taken = int(variant == 0)
            final_rip = entry + (31 if flags & 1 else 15)
    elif name.startswith('fp_'):
        a = Fraction.from_float(struct.unpack('<d', initial['xmm0'][:8])[0])
        b = Fraction.from_float(struct.unpack('<d', initial['xmm1'][:8])[0])
        require(a == (Fraction(3, 2) if variant == 0 else Fraction(3)), 'FP initial destination')
        if name == 'fp_addsub':
            require(b == Fraction(1, 2), 'exact add/sub domain')
            value = a + b - b
        elif name == 'fp_muldiv':
            require(b == 2, 'exact mul/div domain')
            value = a * b / b
        else:
            require(b == (4 if variant == 0 else 9), 'exact square-root domain')
            value = Fraction(math.isqrt(b.numerator))
            require(value * value == b, 'square-root control')
        result['xmm0'] = struct.pack('<d', float(value)) + initial['xmm0'][8:]
    elif name == 'integer_div':
        require(initial['rcx'] == (100 if variant == 0 else MASK) and initial['rbx'] == (3 if variant == 0 else 7), 'nonzero restricted divide domain')
        result['rax'], result['rdx'] = divmod(initial['rcx'], initial['rbx'])
        # XOR EDX before every DIV makes the high dividend zero each time.
        # DIV arithmetic flags are undefined; they are not compared across engines.
    return result, dict(taken=taken, total=n,
                        final_rip=final_rip, flags=flags, flag_mask=flag_mask)


def unpack_input(blob, state_bytes):
    require(len(blob) == 760 + state_bytes + 4096, 'input extent')
    return (list(struct.unpack('<31Q', blob[:248])), bytearray(blob[248:760]),
            bytearray(blob[760:760 + state_bytes]), bytearray(blob[760 + state_bytes:]))


def read_guest(candidate, regs, vectors, state, entry):
    if candidate == 'box64':
        mapping = dict(rax=10, rcx=11, rdx=12, rbx=13, rdi=17)
        guest = {key: regs[index] for key, index in mapping.items()}
        guest.update(xmm0=bytes(state[0x90:0xa0]), xmm1=bytes(state[0xa0:0xb0]))
        require(regs[0] == execution.STATE, 'Box64 state base')
    elif candidate == 'fex':
        mapping = dict(rax=4, rcx=7, rdx=5, rbx=6, rdi=11)
        guest = {key: regs[index] for key, index in mapping.items()}
        guest.update(xmm0=bytes(vectors[256:272]), xmm1=bytes(vectors[272:288]))
    else:
        require(candidate == 'tcg' and regs[19] == execution.STATE + 16, 'TCG environment base')
        guest = {key: struct.unpack_from('<Q', state, 16 + off)[0] for key, off in dict(rax=0, rcx=8, rdx=16, rbx=24, rdi=56).items()}
        guest.update(xmm0=bytes(state[976:992]), xmm1=bytes(state[1040:1056]))
    guest['entry'] = entry
    return guest


def sub_nzcv(a, b):
    s = (a - b) & MASK
    return (s >> 63) << 31 | int(s == 0) << 30 | int(a >= b) << 29 | (((a ^ b) & (a ^ s)) >> 63 & 1) << 28


def expected(candidate, name, variant, n, blob, state_bytes, entry, guarded_fp=False, fault=0):
    regs, vectors, state, data = unpack_input(blob, state_bytes)
    initial = read_guest(candidate, regs, vectors, state, entry)
    require(fault == 0 or guarded_fp and candidate in ('box64','fex') and name.startswith('fp_') and fault in (2,3,4) and n == 1, 'guard rejection domain')
    if fault:
        guest, result = dict(initial), dict(taken=0,total=0,final_rip=0,flags=None,flag_mask=0)
    else:
        guest, result = guest_result(name, variant, n, initial)
    if name.startswith('branch_'):
        require(initial['rbx'] == DATA and struct.unpack_from('<Q', data)[0] == DATA + 128, 'branch memory input')
        require(initial['xmm0'] == bytes(range(16)) and initial['xmm1'] == bytes(i ^ 0xa5 for i in range(16)), 'branch vector input')
    if name.startswith('fp_'):
        require(initial['xmm0'][8:] == struct.pack('<Q', 0x8877665544332211) and initial['xmm1'][8:] == struct.pack('<Q', 0x1020304050607080), 'FP high-lane sentinels')
    scratch, vector_scratch, masked_registers = {30}, set(), {}
    native_flags = 0x20000000 if candidate == 'fex' else 0
    if candidate == 'box64':
        for key, r in dict(rax=10, rcx=11, rdx=12, rbx=13, rdi=17).items():
            regs[r] = guest[key]
        state[0x90:0xa0] = guest['xmm0']
        if name.startswith(('branch_', 'fp_')) and fault != 2:
            vectors[:32] = guest['xmm0'] + guest['xmm1']
        if name == 'branch_zero':
            execution.put(state, 0x44c, 0x3f, '<I'); execution.put(state, 0x460, guest['rcx'])
            scratch.add(3); native_flags = int(guest['rcx'] == 0) << 30
        elif name == 'branch_carry':
            a, b, s, _ = execution.last_add(variant, n)
            execution.put(state, 0x44c, 7, '<I')
            for off, value in ((0x450, a), (0x458, b), (0x460, s)):
                execution.put(state, off, value)
            regs[26] = (regs[26] & ~0x41) | (result['flags'] & 0x41)
            scratch |= {1, 3}; native_flags = execution.nzcv(result['flags'])
        elif name == 'integer_div':
            execution.put(state, 0x44c, 0, '<I')
            scratch.add(2); masked_registers[26] = MASK ^ 0x8d5
            native_flags = None
        elif name.startswith('fp_'):
            vector_scratch.add(24)
    elif candidate == 'fex':
        for key, r in dict(rax=4, rcx=7, rdx=5, rbx=6, rdi=11).items():
            regs[r] = guest[key]
        vectors[256:272] = guest['xmm0']
        if name == 'branch_zero':
            regs[26] = guest['rcx']; native_flags = sub_nzcv(guest['rcx'], 0)
        elif name == 'branch_carry':
            a, b, s, _ = execution.last_add(variant, n)
            regs[26], regs[27] = s, a ^ b
            scratch.add(20); native_flags = execution.nzcv(result['flags']) ^ (1 << 29)
        elif name == 'integer_div':
            scratch |= {20, 22, 26}; native_flags = None
    else:
        for key, off in dict(rax=0, rcx=8, rdx=16, rbx=24, rdi=56).items():
            execution.put(state, 16 + off, guest[key])
        state[976:992] = guest['xmm0']
        if name.startswith('branch_'):
            vectors[:32] = guest['xmm0'] + guest['xmm1']
            state[4] = 1; execution.put(state, 16 + 256, result['final_rip'])
            execution.put(state, 16 + 272, guest['rcx'])
            execution.put(state, 16 + 296, 27 if name == 'branch_zero' else 11, '<I')
            scratch |= {20, 21}
            if name == 'branch_carry':
                _, b, s, _ = execution.last_add(variant, n)
                execution.put(state, 16 + 280, b)
                scratch |= {22, 23}; native_flags = sub_nzcv(s, b)
        elif name != 'nop':
            # C helper ABI applies only after separately reviewed actual helper binding.
            scratch |= set(range(19)) | {20}
            vector_scratch |= set(range(8)) | set(range(16, 32))
            native_flags = None
            state[4] = 1
            execution.put(state, 16 + 256, result['final_rip'])
            if name == 'integer_div':
                execution.put(state, 16 + 280, 0x44)
                execution.put(state, 16 + 296, 0, '<I')
    if guarded_fp and name.startswith('fp_'):
        if candidate == 'box64': scratch.add(3)
        elif candidate == 'fex': scratch.add(0); vector_scratch.add(0)
    return regs, vectors, state, data[:512], native_flags, scratch, vector_scratch, masked_registers, result


CANDIDATES = ['box64', 'fex', 'tcg']
STATE_BYTES = 32768


def input_model(candidate, name, variant, helper_env=b'', guarded_fp=False, fault=0):
    c = CANDIDATES.index(candidate)
    regs, vectors, small, data = execution.unpack_input(execution.fixture_input(c * 7 + 2, variant, 0))
    state = bytearray(STATE_BYTES); state[:4096] = small
    if candidate == 'tcg' and helper_env:
        state[16:16 + len(helper_env)] = helper_env
    values = [0, variant, 1, DATA, DATA + 256]
    if name == 'branch_carry': values[1] = MASK if variant == 0 else MASK >> 1
    if name == 'integer_div': values[1], values[3] = (100, 3) if variant == 0 else (MASK, 7)
    if candidate == 'tcg':
        for off, value in zip((0, 8, 16, 24, 56), values): execution.put(state, 16 + off, value)
    else:
        for r, value in zip((10, 11, 12, 13, 17) if c == 0 else (4, 7, 5, 6, 11), values): regs[r] = value
    if c == 0: regs[27] = 0x401000
    v0, v1 = bytes(range(16)), bytes(i ^ 0xa5 for i in range(16))
    if name.startswith('fp_'):
        b = .5 if name == 'fp_addsub' else 2. if name == 'fp_muldiv' else 4. if variant == 0 else 9.
        v0 = struct.pack('<dQ', 1.5 if variant == 0 else 3., 0x8877665544332211)
        v1 = struct.pack('<dQ', b, 0x1020304050607080)
    if guarded_fp and name.startswith('fp_'):
        execution.put(state, 0x7f00, 0x1fa0 if fault != 2 else 0x1f80, '<I')
        if c == 2:
            execution.put(state, 16+936, 0x10, '<H'); execution.put(state, 16+944, 0x1fa0, '<I')
        if fault == 3: v1 = struct.pack('<Q',0x7ff8000000000001) + v1[8:]
        elif fault == 4:
            if name == 'fp_sqrt': v1 = struct.pack('<d',-4. if variant == 0 else -9.) + v1[8:]
            else:
                v0 = struct.pack('<Q',0x7fefffffffffffff) + v0[8:]
                if name == 'fp_addsub': v1 = struct.pack('<Q',0x7fefffffffffffff) + v1[8:]
    if c == 0: state[0x90:0xb0] = v0 + v1
    elif c == 1: vectors[256:288] = v0 + v1
    else: state[976:992], state[1040:1056] = v0, v1
    return struct.pack('<31Q', *regs) + vectors + state + data


def schedule_for(phase, cases):
    ids = [c['index'] for c in cases if c['available']]
    if phase == 'admission':
        rows = [(i, v, n, 0) for i in ids for v in (0, 1) for n in (1, 2, 3, 64)]
        rows += [(c['index'],v,1,f) for c in cases if c.get('guarded_fp',False) and c['candidate'] in ('box64','fex') and c['fixture'].startswith('fp_') for v in (0,1) for f in (2,3,4)]
        return rows
    require(phase == 'timing', 'unknown phase')
    rng, rows = random.Random(20260922), []
    for repeat in range(7):
        batch = [(i, repeat % 2, n, 0) for i in ids for n in (65536, 262144)]
        rng.shuffle(batch); rows.extend(batch)
    return rows


def check_inputs(source):
    source = Path(source)
    fmt = json.loads((source / 'comparison-format.json').read_text())
    guarded_fp = fmt.pop('guarded_fp', False)
    require(isinstance(guarded_fp,bool), 'guarded profile type')
    require(fmt == dict(state_bytes=STATE_BYTES, fpcr=6, boundary_start=32, boundary_bytes=224,
                        counters=dict(completed=64, taken=72, rip=80, outcome=88), api_base=25, api_per_sample=140), 'format contract')
    cases = json.loads((source / 'comparison-cases.json').read_text())
    require(len(cases) == 21, 'case count')
    env = (source / 'helper-env.bin').read_bytes() if (source / 'helper-env.bin').exists() else b''
    if env:
        require(json.loads((source / 'helper-layout.json').read_text()) == dict(env_size=15312, sse_status=936, mxcsr=944, xmm0=960, xmm1=1024), 'helper environment layout')
        require(len(env) == 15312 and (source / 'helpers.bin').stat().st_size > 0, 'helper artifacts extent')
        symbols = json.loads((source / 'helper-symbols.json').read_text())
        for name in ('helper_addsd', 'helper_subsd', 'helper_mulsd', 'helper_divsd', 'helper_sqrtsd', 'helper_divq_EAX'):
            require(BASE + 0x8000 <= symbols[name] < BASE + 0x10000 and symbols[name] % 4 == 0, 'helper linked address')
    bindings = json.loads((source / 'consolidated-bindings.json').read_text())['candidates']
    for i, case in enumerate(cases):
        c, k = divmod(i, 7); candidate, name = CANDIDATES[c], NAMES[k]
        require((case['index'], case['base_index'], case['candidate'], case['fixture'], case['entry'], case['fpcr']) ==
                (i, c * 7, candidate, name, 0x10000 if c == 1 else 0x401000, 6), 'case identity')
        require(case['guest_bytes'] == (1,16,15,8,8,4,8)[k], 'guest instruction length')
        available = not (c == 2 and k >= 3 and not env)
        require(case['available'] == available and case['status'] == ('bound' if available else 'unbound-actual-helper'), 'availability')
        raw = (source / case['raw_path']).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == case['raw_sha256'], 'raw source integrity')
        if candidate in bindings and name in bindings[candidate]:
            require(case['raw_sha256'] == bindings[candidate][name]['raw_sha256'], 'independent inspected native identity')
        ranges = [] if k == 0 else ([[0,44 if k == 1 else 72]] if k in (1,2) else [[0,20 if k == 5 else 28]] if k < 6 else [[0,16],[92,132]]) if c == 0 else ([[12,32 if k == 1 else 48]] if k in (1,2) else [[12,16 if k == 5 else 20]] if k < 6 else [[12,28],[68,84]]) if c == 1 else [[12,108 if k == 1 else 140]] if k in (1,2) else [[8,56 if k == 5 else 76 if k == 6 else 84]] if available else []
        require(case['ranges'] == ranges and all(0 <= a <= b <= len(raw) and a % 4 == b % 4 == 0 for a,b in ranges), 'inspected body ranges')
        expected_relocations = []
        prefix = f'_body_{i}'
        if k in (1,2):
            if c == 0: expected_relocations = [(36 if k == 1 else 64, 'conditional', prefix+'_false')]
            elif c == 1: expected_relocations = [(28 if k == 1 else 44, 'conditional', prefix+'_true')]
            else: expected_relocations = [(88 if k == 1 else 120, 'branch', prefix+'_false'), (104 if k == 1 else 136, 'branch', prefix+'_true')]
            require(case['scalar_raw_offset'] == ((4 if k == 1 else 24) if c == 0 else (16 if k == 1 else 24) if c == 1 else (32 if k == 1 else 44)), 'scalar guard source site')
        elif k == 6 and c < 2:
            expected_relocations = [(12 if c == 0 else 24, 'conditional', prefix+'_divfast')]
        elif c == 2 and k >= 3 and available:
            if k in (3,4):
                helpers = ('helper_addsd','helper_subsd') if k == 3 else ('helper_mulsd','helper_divsd')
                expected_relocations = [(28,'literal',helpers[0]),(60,'literal',helpers[1]),(80,'branch',prefix+'_done')]
            elif k == 5: expected_relocations = [(32,'literal','helper_sqrtsd'),(52,'branch',prefix+'_done')]
            else: expected_relocations = [(48,'literal','helper_divq_EAX'),(72,'branch',prefix+'_done')]
        actual_relocations = [(r['offset'],r['kind'],r['symbol'] if r['kind']=='literal' else r['target']) for r in case['relocations']]
        require(actual_relocations == expected_relocations, 'exact admitted adapter relocations')
        for relocation in case['relocations']:
            require(struct.unpack_from('<I', raw, relocation['offset'])[0] == relocation['word'], 'relocation source word')
        require(case.get('guarded_fp',False) == guarded_fp, 'guarded profile identity')
        if guarded_fp and c < 2 and name.startswith('fp_'):
            require(case['reject_label'] == prefix+'_fp_reject_resume', 'registered rejection label')
        faults = (0,2,3,4) if guarded_fp and c < 2 and name.startswith('fp_') else (0,)
        for variant in (0,1):
            for fault in faults:
                require((source / f'{i}-{variant}-{fault}.input').read_bytes() == input_model(candidate, name, variant, env, guarded_fp, fault), 'retained input differs from independent input model')
    for phase in ('admission', 'timing'):
        schedule = [tuple(map(int, line.split())) for line in (source / (phase + '.schedule')).read_text().splitlines()]
        require(schedule == schedule_for(phase, cases), 'retained schedule')
    return dict(body_count=21, available=sum(c['available'] for c in cases), cases=cases)


def boundary_model(blob, result, name):
    boundary = bytearray(224)
    outcome = int(result['final_rip'] in (0x401020, 0x40101f, 0x10020, 0x1001f)) if name.startswith('branch_') else 0
    for off, value in ((64,result['total']), (72,result['taken']), (80,result['final_rip']), (88,outcome)):
        execution.put(boundary, off-32, value)
    for reg, off in ((1,128),(2,136),(8,144),(9,152),(29,160)):
        execution.put(boundary, off-32, struct.unpack_from('<Q', blob, reg*8)[0])
    return boundary


def verify_sample(row, blob, case):
    want = expected(case['candidate'], case['fixture'], row['variant'], row['n'], blob, STATE_BYTES, case['entry'], case.get('guarded_fp',False), row['fault'])
    regs, vectors, state, data, flags, scratch, vscratch, masks, result = want
    actual = struct.unpack('<31Q', bytes.fromhex(row['registers']))
    for r, value in enumerate(regs):
        if r not in scratch:
            mask = masks.get(r, MASK)
            require(actual[r] & mask == value & mask, f'x{r} guest/preserved mismatch')
    for r in (1,2,8,9,29): require(actual[r] == struct.unpack_from('<Q',blob,r*8)[0], 'wrapper saved-register mismatch')
    require(actual[30] == row['end'], 'timestamp register')
    av = bytes.fromhex(row['vectors']); require(len(av) == 512, 'vector extent')
    for q in range(32):
        if q not in vscratch:
            # AAPCS preserves only the low half of v8..v15 across actual C helpers.
            width = 8 if case['candidate'] == 'tcg' and case['fixture'] not in ('nop','branch_zero','branch_carry') and 8 <= q < 16 else 16
            require(av[q*16:q*16+width] == vectors[q*16:q*16+width], f'q{q} owned/preserved mismatch')
    require(bytes.fromhex(row['state']) == state, 'whole guest state mismatch')
    require(bytes.fromhex(row['data']) == data, 'data mismatch')
    require(bytes.fromhex(row['boundary']) == boundary_model(blob,result,case['fixture']), 'actual branch/counter/saved-frame mismatch')
    if flags is not None: require(row['cpsr'] & 0xf0000000 == flags, 'native NZCV mismatch')
    require(row['cpsr'] & 15 == 5 and row['hvc'] == (0x47 if row['fault'] else 0x44), 'completion identity')
    require(row['fpcr'] == 6 and row['fpsr'] == 0, 'FP control/status changed')
    if row['fault']:
        require(row['pc'] == row['reject_address'] and BASE <= row['pc'] < BASE + 0x10000 and row['pc'] % 4 == 0, 'registered rejection PC')


def check(rows, source, phase):
    source = Path(source); cases = check_inputs(source)['cases']; schedule = schedule_for(phase,cases)
    require(len(rows) == len(schedule)+2, 'output record count')
    boot,end = rows[0],rows[-1]
    require(boot['kind'] == 'boot' and end['kind'] == 'teardown', 'envelope order')
    for key,value in dict(actlr=2,mair=255,tcr=16|1<<8|1<<10|3<<12|1<<23|2<<32,sctlr=0x30d01985).items(): require(boot[key] == value, 'boot '+key)
    require(0 < boot['code_bytes'] <= 0x10000 and (boot['isar0']>>52&15)>=1, 'code/FlagM admission')
    require((boot['mmfr1']>>44&15)>=1, 'AFP admission')
    require(end['samples'] == len(schedule) and end['api_calls'] == 25+140*len(schedule), 'teardown accounting')
    hz=boot['hz'];num,den=boot['timebase']; sane=hz>0 and hz==boot['guest_hz'] and num>0 and den>0
    ratios,groups,addresses=[],{},{}
    for serial,(row,scheduled) in enumerate(zip(rows[1:-1],schedule)):
        require(row['kind']=='sample' and row['serial']==serial and tuple(row[k] for k in ('index','variant','n','fault'))==scheduled, 'sample schedule/order')
        i,v,n,fault=scheduled; address=row['body_address']
        require(BASE<=address<BASE+boot['code_bytes'] and address%4==0 and addresses.setdefault(i,address)==address, 'body address')
        require(BASE<=row['pc']<BASE+boot['code_bytes'] and row['pc']%4==0, 'completion PC')
        try: verify_sample(row,(source/f'{i}-{v}-{fault}.input').read_bytes(),cases[i])
        except ValueError as error: raise ValueError(f'sample {serial} {cases[i]["candidate"]}/{cases[i]["fixture"]}: {error}') from error
        ticks=row['end']-row['start']; ratio=row['host_ticks']*1e9*den/(row['host_mach']*num*hz) if min(row['host_mach'],num,hz)>0 else 0
        sane &= row['start']>0 and 0<ticks<=row['host_ticks'] and .9<=ratio<=1.1
        ratios.append(ratio);groups.setdefault((i,n),[]).append(ticks/n)
    stats=[]
    if phase=='timing':
        for case in cases:
            if not case['available']: continue
            small,large=(groups[case['index'],n] for n in (65536,262144))
            require(len(small)==len(large)==7,'timing replication')
            stats.append(dict(candidate=case['candidate'],fixture=case['fixture'],ticks_per_body_n65536=statistics.median(small),ticks_per_body_n262144=statistics.median(large),paired_total_tick_ratios=[4*b/a if a else None for a,b in zip(small,large)]))
    return dict(correctness='pass',samples=len(schedule),timing_sanity=bool(sane),clock_ratio_range=[min(ratios),max(ratios)],exploratory_body_timings=stats,limits=('Finite inputs under the sticky-inexact owner profile, with Box64/FEX pre-commit guard rejection controls. No cold completion or general MXCSR/FP conformance. ' if cases[0].get('guarded_fp',False) else '') + 'Finite normal-path inputs, actual branch veneers and linked helpers. Raw relocation and helper implementation require separate source review. No exception recovery, whole-translator performance, NOP subtraction, or generator ranking; clock sanity is not qualification.')


def self_check():
    tested = 0
    for variant in (0, 1):
        for name in NAMES:
            initial = dict(rax=DATA, rbx=DATA, rcx=variant, rdx=1,
                           xmm0=bytes(range(16)), xmm1=bytes(i ^ 0xa5 for i in range(16)))
            if name == 'branch_carry':
                initial['rcx'] = MASK if variant == 0 else MASK >> 1
            if name == 'integer_div':
                initial.update(rcx=100 if variant == 0 else MASK, rbx=3 if variant == 0 else 7)
            if name.startswith('fp_'):
                initial['xmm0'] = struct.pack('<d', 1.5 if variant == 0 else 3.0) + initial['xmm0'][8:]
                b = .5 if name == 'fp_addsub' else 2.0 if name == 'fp_muldiv' else 4.0 if variant == 0 else 9.0
                initial['xmm1'] = struct.pack('<d', b) + initial['xmm1'][8:]
            rcx, rdx, taken = initial['rcx'], initial['rdx'], 0
            for n in range(1, 201):
                result, meta = guest_result(name, variant, n, initial)
                if name == 'branch_zero':
                    rcx ^= 1; taken += int(rcx == 0)
                    require(result['rcx'] == rcx and meta['taken'] == taken, 'zero-branch recurrence')
                elif name == 'branch_carry':
                    s = (rcx + rdx) & MASK; taken += int(s < rcx)
                    rdx = rdx & ~255 | int(s == 0); rcx = s
                    require(result['rcx'] == rcx and result['rdx'] == rdx and meta['taken'] == taken, 'carry-branch recurrence')
                if name.startswith('fp_'):
                    require(result['xmm0'][8:] == initial['xmm0'][8:] and result['xmm1'] == initial['xmm1'], 'scalar lane preservation')
                tested += 1
    mutants = 0
    for candidate in CANDIDATES:
        for name in NAMES:
            for variant in (0,1):
                blob = input_model(candidate,name,variant)
                case = dict(candidate=candidate,fixture=name,entry=0x10000 if candidate=='fex' else 0x401000)
                for n in (1,2,3,64,65536,262144):
                    want = expected(candidate,name,variant,n,blob,STATE_BYTES,case['entry'])
                    regs,vectors,state,data,flags,scratch,vscratch,masks,result=want
                    regs[30]=100
                    # Wrapper restores its own scratch before the snapshot.
                    for r in (1,2,8,9,29): regs[r]=struct.unpack_from('<Q',blob,r*8)[0]
                    row=dict(index=CANDIDATES.index(candidate)*7+NAMES.index(name),variant=variant,n=n,fault=0,end=100,
                             registers=struct.pack('<31Q',*regs).hex(),vectors=vectors.hex(),state=state.hex(),data=data.hex(),
                             boundary=boundary_model(blob,result,name).hex(),cpsr=0x3c5|(flags or 0),hvc=0x44,fpcr=6,fpsr=0)
                    verify_sample(row,blob,case)
                    preserved_reg = 19 if candidate=='tcg' else 17 if candidate=='box64' else 11
                    preserved_q = 17 if candidate=='fex' else 8
                    for key,off in (('registers',preserved_reg*8),('vectors',preserved_q*16),('state',2000),('data',0),('boundary',64-32),('boundary',72-32),('boundary',80-32),('boundary',88-32)):
                        bad=dict(row); value=bytearray.fromhex(bad[key]);value[off]^=1;bad[key]=value.hex()
                        try: verify_sample(bad,blob,case)
                        except ValueError: mutants+=1
                        else: raise AssertionError('accepted '+key+' mutant')
                    for key in ('fpcr','fpsr','hvc'):
                        bad=dict(row);bad[key]^=1
                        try: verify_sample(bad,blob,case)
                        except ValueError: mutants+=1
                        else: raise AssertionError('accepted '+key+' mutant')
    guarded_cases = 0
    for candidate in CANDIDATES:
        for name in ('fp_addsub','fp_muldiv','fp_sqrt'):
            for variant in (0,1):
                scenarios = [(0,n) for n in (1,2,3,64,65536,262144)] + ([(f,1) for f in (2,3,4)] if candidate != 'tcg' else [])
                for fault,n in scenarios:
                    blob = input_model(candidate,name,variant,guarded_fp=True,fault=fault)
                    case = dict(candidate=candidate,fixture=name,entry=0x10000 if candidate=='fex' else 0x401000,guarded_fp=True)
                    regs,vectors,state,data,flags,_,_,_,result = expected(candidate,name,variant,n,blob,STATE_BYTES,case['entry'],True,fault)
                    regs[30]=100
                    for r in (1,2,8,9,29): regs[r]=struct.unpack_from('<Q',blob,r*8)[0]
                    row=dict(variant=variant,n=n,fault=fault,end=100,registers=struct.pack('<31Q',*regs).hex(),vectors=vectors.hex(),state=state.hex(),data=data.hex(),boundary=boundary_model(blob,result,name).hex(),cpsr=0x3c5|(flags or 0),hvc=0x47 if fault else 0x44,fpcr=6,fpsr=0,pc=BASE+256,reject_address=BASE+256)
                    verify_sample(row,blob,case);guarded_cases+=1
                    changes = [('state',0x7f00),('state',16+936),('state',16+944),('boundary',64-32),('boundary',80-32)]
                    changes += [('vectors',256),('vectors',264)] if candidate=='fex' else [('state',144 if candidate=='box64' else 976),('state',152 if candidate=='box64' else 984)]
                    for key,off in changes:
                        bad=dict(row);value=bytearray.fromhex(row[key]);value[off]^=1;bad[key]=value.hex()
                        try: verify_sample(bad,blob,case)
                        except ValueError: mutants+=1
                        else: raise AssertionError('accepted guarded '+key+' mutation')
                    if fault:
                        bad=dict(row);bad['pc']+=4
                        try: verify_sample(bad,blob,case)
                        except ValueError: mutants+=1
                        else: raise AssertionError('accepted wrong rejection PC')
                    bad=dict(row);bad['hvc']=0x44 if fault else 0x47
                    try: verify_sample(bad,blob,case)
                    except ValueError: mutants+=1
                    else: raise AssertionError('accepted wrong guarded completion')
    print(json.dumps(dict(self_check='pass', semantic_cases=tested, guarded_cases=guarded_cases, rejected_mutants=mutants)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    if not args.self_check:
        parser.error('use --self-check; runner calls the checker')
    self_check()
