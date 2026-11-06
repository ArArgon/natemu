#!/usr/bin/env python3
"""Independent finite checks for guarded bodies and resident helper boundaries."""
import argparse
import importlib.util
import json
from pathlib import Path
import random
import re
import statistics
import struct
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('boundary_execution_oracle', HERE / 'check_execution.py')
execution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(execution)
MASK, DATA, BASE = execution.MASK, execution.DATA, execution.BASE
require = execution.require


def admitted(address, size):
    """Integer interval oracle, independent of the emitted XOR/shift predicate."""
    end = address + size - 1
    return (0 <= address <= end < (1 << 47) or
            (1 << 64) - (1 << 47) <= address <= end <= MASK)


def words_at(base_index, variant, n, blob):
    regs, native_vectors, state, _, _, _ = execution.expected(base_index, variant, n, 0, blob)
    candidate = (base_index % 21) // 7
    if candidate == 0:
        words = [regs[r] for r in (10, 11, 12, 13, 17)]
        vectors = state[0x90:0xb0]
    elif candidate == 1:
        words = [regs[r] for r in (4, 7, 5, 6, 11)]
        vectors = native_vectors[256:288]
    else:
        words = [struct.unpack_from('<Q', state, 16 + off)[0] for off in (0, 8, 16, 24, 56)]
        vectors = state[976:992] + state[1040:1056]
    a, b, s, _ = execution.last_add(variant, n)
    words += [execution.add_flags(a, b, s)]
    return words + list(struct.unpack('<4Q', vectors))


def digest(words):
    value = 0
    for word in words:
        value = (value >> 7 | value << 57) & MASK
        value ^= word
    return value


def boundary_effects(base_index, variant, n, k, blob):
    """Exact count/sum/last snapshot; steady state has only two parity classes."""
    count = n // k if k else 0
    if not count:
        return 0, 0, [0] * 10
    first = min(count, 3)
    total = sum(digest(words_at(base_index, variant, j * k, blob)) for j in range(1, first + 1))
    remaining = count - first
    if remaining:
        a = digest(words_at(base_index, variant, (first + 1) * k, blob))
        b = digest(words_at(base_index, variant, (first + 2) * k, blob))
        total += (remaining + 1) // 2 * a + remaining // 2 * b
    return count, total & MASK, words_at(base_index, variant, count * k, blob)


def cases():
    result = []
    configs = [(0, 0, 0), (1, 0, 0)] + [(1, mode, k) for mode in (1, 2) for k in (1, 4, 16, 64)]
    for c, candidate in enumerate(execution.CANDIDATES):
        for f in (2, 4, 6):
            for guarded, mode, k in configs:
                scalar = (36, 16, 48 if f == 4 else 44)[c]
                vector = (48, 28, 96)[c]
                result.append(dict(index=len(result), base_index=21 + c * 7 + f,
                                   candidate=candidate, fixture=execution.NAMES[f],
                                   guarded=guarded, mode=mode, k=k,
                                   scalar_offset=scalar + 24 * guarded,
                                   vector_offset=vector + 48 * guarded,
                                   scalar_reg=(('x10' if f == 6 else 'x13'), ('x4' if f == 6 else 'x6'), ('x23' if f == 4 else 'x22'))[c],
                                   vector_reg=('x17', 'x11', 'x22')[c]))
    return result


def inputs(case, variant, fault):
    base = case['base_index']
    blob = bytearray(execution.fixture_input(base, variant, int(fault == 1)))
    if fault >= 2:
        address = {2: 1 << 47, 3: (1 << 47) - 4, 4: 1 << 47,
                   5: (1 << 47) - 8, 6: (1 << 64) - 4, 7: (1 << 64) - 8}[fault]
        vector = fault in (4, 5, 7)
        c, alias = (base % 21) // 7, base % 7 == 6
        if c == 2:
            off = 760 + 16 + (56 if vector else 0 if alias else 24)
        else:
            off = ((17, 11)[c] if vector else (10, 4)[c] if alias else (13, 6)[c]) * 8
        struct.pack_into('<Q', blob, off, address)
        require(not admitted(address, 16 if vector else 8), 'bad-address control unexpectedly admissible')
    return blob


def schedule_for(phase):
    meta, rows = cases(), []
    if phase == 'admission':
        for e in meta:
            rows += [(e['index'], v, n, 0) for v in (0, 1) for n in (1, 3, 64)]
            if e['mode'] == 0:
                rows += [(e['index'], 0, 1, f) for f in range(1, 8 if e['guarded'] else 2)]
        return rows
    require(phase == 'timing', 'unknown phase')
    rng = random.Random(20260921)
    for repeat in range(7):
        batch = [(e['index'], repeat % 2, n, 0) for e in meta for n in (65536, 262144)]
        rng.shuffle(batch); rows += batch
    return rows


def check_inputs(source):
    source = Path(source)
    execution.check_inputs(source / 'emission')
    require(json.loads((source / 'boundary-cases.json').read_text()) == cases(), 'boundary case/binding identity')
    assembly = (source / 'execution-generated.S').read_text()
    for e in cases():
        i, base = e['index'], e['base_index']
        for v in (0, 1):
            for f in range(8):
                require((source / f'{i}-{v}-{f}.input').read_bytes() == inputs(e, v, f), 'input snapshot differs from method')
        # Check that each body uses the three unchanged, contiguous source spans.
        body = assembly.split(f'_body_{i}_code:\n', 1)[1].split(f'_body_{i}_resume:', 1)[0]
        scalar = e['scalar_offset'] - 24 * e['guarded']
        vector = e['vector_offset'] - 48 * e['guarded']
        size = len((source / 'emission' / f'{base}.body').read_bytes())
        spans = re.findall(r'\.incbin "emission/(\d+)\.body",(\d+),(\d+)', body)
        require(spans == [(str(base), str(a), str(b - a)) for a, b in ((0, scalar), (scalar, vector), (vector, size))], 'emitter body spans changed')
        require(body.count('sbfx ') == 2 * e['guarded'] and body.count('cbnz x1,_body_') >= 2 * e['guarded'], 'guard count')
        require(body.count(f'bl _boundary_{e["candidate"]}_{e["mode"]}') == int(e['mode'] != 0), 'helper call identity')
    for phase in ('admission', 'timing'):
        observed = [tuple(map(int, line.split())) for line in (source / (phase + '.schedule')).read_text().splitlines()]
        require(observed == schedule_for(phase), 'boundary schedule mismatch')
    return dict(status='inputs_checked', configurations=90)


def expected_sample(case, variant, n, fault, blob):
    scalar_fault = fault in (1, 2, 3, 6)
    values = execution.expected(case['base_index'], variant, n, int(scalar_fault), blob)
    regs, vectors, state, data, flags, scratch = values
    if fault == 1:
        regs[29] = case['k']  # Native landing precedes the wrapper's x29 restore.
    if fault and case['guarded']:
        scratch |= {1, 2}  # Guard temporaries are captured before normal restoration.
    if fault in (4, 5, 7):
        # ADD/scalar/SETZ completed, vector target store and later writes did not.
        data[256:272] = blob[4856 + 256:4856 + 272]
        c = (case['base_index'] % 21) // 7
        if c == 0:
            state[0x90:0xa0] = blob[760 + 0x90:760 + 0xa0]
        elif c == 1:
            flags ^= 1 << 29  # CFINV lies after the rejected vector store.
        else:
            state[16 + 0x128:16 + 0x12c] = blob[760 + 16 + 0x128:760 + 16 + 0x12c]
    return regs, vectors, state, data, flags, scratch


def verify_sample(row, case, blob):
    n, variant, fault = row['n'], row['variant'], row['fault']
    require(blob == inputs(case, variant, fault), 'input differs from method')
    want = expected_sample(case, variant, n, fault, blob)
    regs = struct.unpack('<31Q', bytes.fromhex(row['registers']))
    for i, value in enumerate(want[0]):
        if i not in want[5]:
            require(regs[i] == value, f'guest/preserved register x{i}')
    for key, value in zip(('vectors', 'state', 'data'), want[1:4]):
        require(bytes.fromhex(row[key]) == value, key + ' mismatch')
    require(row['cpsr'] & 0xf0000000 == want[4] and row['cpsr'] & 15 == 5, 'native flags/EL mismatch')
    stack = bytes.fromhex(row['boundary'])
    require(len(stack) == 224, 'boundary capture length')
    count, total, last = boundary_effects(case['base_index'], variant, n if not fault else 0, case['k'], blob)
    word = lambda offset: struct.unpack_from('<Q', stack, offset - 32)[0]
    require(word(104) == count and word(112) == total, 'helper count/digest mismatch')
    require(word(224) == (last[5] if count else 0), 'decoded arithmetic flags mismatch')
    record = struct.unpack_from('<10Q', stack, 128 - 32)
    require(record == tuple(last if count and case['mode'] == 2 else [0] * 10), 'canonical snapshot mismatch')
    reject = 2 if fault in (4, 5, 7) else 1 if fault >= 2 else 0
    require(word(232) == reject, 'wrong guard rejection site')
    require(word(240) == struct.unpack_from('<Q', blob, 29 * 8)[0], 'saved wrapper x29')
    require(row['hvc'] == (0x47 if fault >= 2 else 0x46 if fault else 0x44), 'wrong HVC result')
    if fault == 1:
        require(row['esr'] >> 26 == 0x25 and row['esr'] & 0x3f == 6, 'native data-abort type')
        require(row['esr'] & (1 << 10) == 0 and row['far'] == execution.BAD, 'native FAR')
        require((row['esr'] >> 6 & 1) == int(case['base_index'] % 7 == 4), 'native access direction')
        require(row['elr'] == row['body_address'] + case['scalar_offset'], 'native fault site')
    if fault:
        require(regs[30] == n, 'fault changed loop count')
    else:
        require(regs[30] == row['end'], 'end counter register mismatch')


def check(rows, source, phase):
    source = Path(source)
    check_inputs(source)
    schedule, meta = schedule_for(phase), cases()
    require(len(rows) == len(schedule) + 2, 'record count')
    boot, end = rows[0], rows[-1]
    require(boot['kind'] == 'boot' and end['kind'] == 'teardown', 'record envelope')
    for key, value in dict(actlr=2, mair=255, tcr=16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32, sctlr=0x30d01985).items():
        require(boot[key] == value, 'boot ' + key)
    require(0 < boot['code_bytes'] <= 0x10000 and (boot['isar0'] >> 52 & 15) >= 1, 'code extent/FlagM')
    require(end['samples'] == len(schedule) and end['api_calls'] == 24 + 137 * len(schedule), 'teardown accounting')
    hz, (num, den) = boot['hz'], boot['timebase']
    timing_ok = hz > 0 and hz == boot['guest_hz'] and num > 0 and den > 0
    groups, ratios, addresses = {}, [], {}
    for serial, (row, entry) in enumerate(zip(rows[1:-1], schedule)):
        require(row['kind'] == 'sample' and row['serial'] == serial, 'row identity')
        require(tuple(row[k] for k in ('index', 'variant', 'n', 'fault')) == entry, 'row schedule')
        i, v, n, f = entry
        require(BASE <= row['body_address'] < BASE + boot['code_bytes'] and row['body_address'] % 4 == 0, 'body address')
        require(addresses.setdefault(i, row['body_address']) == row['body_address'], 'unstable body address')
        require(BASE <= row['pc'] < BASE + boot['code_bytes'] and row['pc'] % 4 == 0, 'checkpoint address')
        try:
            verify_sample(row, meta[i], (source / f'{i}-{v}-{f}.input').read_bytes())
        except ValueError as error:
            raise ValueError(f'sample {serial} case {i}: {error}') from error
        if not f:
            ticks = row['end'] - row['start']
            ratio = row['host_ticks'] * 1e9 * den / (row['host_mach'] * num * hz) if min(row['host_mach'], num, hz) > 0 else 0
            timing_ok &= row['start'] > 0 and 0 < ticks <= row['host_ticks'] and 0.9 <= ratio <= 1.1
            ratios.append(ratio); groups.setdefault((i, n), []).append(ticks / n)
    stats = []
    if phase == 'timing':
        for e in meta:
            i = e['index']; a, b = groups[i, 65536], groups[i, 262144]
            require(len(a) == len(b) == 7, 'repeat coverage')
            stats.append(dict(e, ticks_per_body_n65536=statistics.median(a), ticks_per_body_n262144=statistics.median(b),
                              paired_total_tick_ratios=[4 * y / x if x else None for x, y in zip(a, b)]))
    return dict(correctness='pass', samples=len(schedule), timing_sanity=bool(timing_ok),
                clock_ratio_range=[min(ratios), max(ratios)], exploratory_boundary_timings=stats,
                limits='Synthetic guard/helper ABI with unchanged emitted bodies; no general recovery, workload mix, full state image, or generator ranking. Clock sanity is not timing qualification.')


def self_check():
    for size in (8, 16):
        require(admitted(DATA, size), 'ordinary mapped interval')
        require(admitted((1 << 47) - size, size), 'last low interval')
        require(not admitted((1 << 47) - size + 1, size), 'crossing low interval')
        require(admitted((1 << 64) - (1 << 47), size), 'first high interval')
        require(admitted((1 << 64) - size, size), 'last high interval')
        require(not admitted((1 << 64) - size + 1, size), 'wrapping interval')
        require(not admitted(1 << 47, size), 'low noncanonical')
        require(not admitted((1 << 64) - (1 << 47) - 1, size), 'high noncanonical')
    checked = 0
    for base in (23, 25, 27, 30, 32, 34, 37, 39, 41):
        for variant in (0, 1):
            blob = execution.fixture_input(base, variant, 0)
            for k in (1, 4, 16, 64):
                for n in (1, 2, 3, 4, 7, 64, 65, 257):
                    count, total, last = boundary_effects(base, variant, n, k, blob)
                    snapshots = [words_at(base, variant, j, blob) for j in range(k, n + 1, k)]
                    require(count == len(snapshots), 'helper count')
                    require(total == sum(map(digest, snapshots)) & MASK, 'helper sum recurrence')
                    require(last == (snapshots[-1] if snapshots else [0] * 10), 'last snapshot')
                    checked += 1
    mutants = 0
    for index, variant, n, fault in schedule_for('admission'):
        case = cases()[index]; blob = inputs(case, variant, fault)
        regs, vectors, state, data, flags, _ = expected_sample(case, variant, n, fault, blob)
        regs[30] = n if fault else 100
        stack = bytearray(224)
        count, total, last = boundary_effects(case['base_index'], variant, n if not fault else 0, case['k'], blob)
        for off, val in ((104, count), (112, total), (224, last[5] if count else 0), (232, 2 if fault in (4, 5, 7) else 1 if fault >= 2 else 0)):
            struct.pack_into('<Q', stack, off - 32, val)
        if count and case['mode'] == 2:
            struct.pack_into('<10Q', stack, 96, *last)
        row = dict(n=n, variant=variant, fault=fault, registers=struct.pack('<31Q', *regs).hex(),
                   vectors=vectors.hex(), state=state.hex(), data=data.hex(), boundary=stack.hex(),
                   cpsr=0x3c5 | flags, hvc=0x47 if fault >= 2 else 0x46 if fault else 0x44,
                   esr=0x25 << 26 | 6 | int(case['base_index'] % 7 == 4) << 6,
                   far=execution.BAD, elr=BASE + case['scalar_offset'], body_address=BASE, end=100)
        verify_sample(row, case, blob)
        for offset in (104, 112, 128, 224, 232):
            bad = dict(row); changed = bytearray(stack); changed[offset - 32] ^= 1; bad['boundary'] = changed.hex()
            try:
                verify_sample(bad, case, blob)
            except ValueError:
                mutants += 1
            else:
                raise AssertionError('accepted boundary mutation')
    print(json.dumps(dict(self_check='pass', interval_controls=16, digest_recurrences=checked, rejected_mutants=mutants)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    if not args.self_check:
        parser.error('use --self-check; runner calls the checker')
    self_check()
