#!/usr/bin/env python3
"""Independent finite oracle for the retained extracted-body experiment.

Checks the declared cached-state/body boundary, not complete x86 recovery or an
adapted translator. Timing remains exploratory and separate from correctness.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import struct

BASE, STATE, DATA, BAD = 0x40000000, 0x40018000, 0x40020000, 0x50000000
MASK = (1 << 64) - 1
NAMES = ['nop', 'bare_load', 'live_load', 'bare_store', 'live_store',
         'bare_alias_load', 'live_alias_load']
CANDIDATES = ['box64', 'fex', 'tcg']
# Unchanged slices independently inspected in the three sealed exporter runs.
HASHES = [
['e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '32fce663959bdcf612c8f957df4c5687d3fa437f57aba7a978f12570309ee868', '5d3d7471c9434c0e02f770e23819c28d416b4cc58a13926f44717983c261dbe1', '4085e5cb7f22fa5e430304fa8c5c40d34bd0b1de5f43d7fe0ebe3e63243ad840', '93c690a1464f1bb408c6cf0b3141c821d99c9662a1ee5f95a6ac3f7bf2db3283', '3c14db928e7a9e2a82053196aaa7d714803f902c1090c248a7d13c14d2febc2a', '5d645c89d21a71af97bb1d92246224abbcc4d62702a3a52b2ff1c90994ed9a44'],
['e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '5ca671962621681001f6c3fd97f1b6334878b2a36a66cc134cd93f497d0e4e44', 'edb5702cefee10d86ea7e0d76295e4b4b86095b3871913966bf0a91bc5051b32', 'e9521b7329622674e48055f28d426dbf652d2eda1dc1bffd49f03a7700445fe1', 'b64246c18a303ff179b22e0d76b10afa3ac1f1e893c0a7bfe0f506ccbfe9890d', 'e367bc5d6ceaad1075f602b54e3fe01696bc549063b110104453302f90ac5a44', '0978ef398d1f394be55a95b2ac3c225cf1acf71615bd283ce4e8f5d30e268be7'],
['e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '9625e66058fb8e3310419f4e1868a099e70a0f19dca049d35f94c22df14b0e60', '3d2d031ac8ba896a9d5c0642ee8e99bdf8271135d17409ae7c1af1b7ebabcede', '3a67dfb5e44d2e1a3610fa9c35e45f9aec7f48c6cd090232535f00882fecfe2c', '16c9a1a673875e64be89ae166df85858c18b8f600df642dfc663587638aa1d85', '9645be0ffdb6e044ecea282efa49b77fc6a59fae1909fcfe862320c6a157e657', '3ec04f3b5f374793bd71b6352af7964ae6bdc9941ccd3c42a14d573d45f7b925'],
]
# /root/comparison_sources inspected all three sealed *-hardware-01 outputs.
# Box64/TCG remove only barriers; FEX additionally removes NOP padding and uses
# LDR/STR instead of LDAR/STLR. State and flag bindings are unchanged.
HARDWARE_HASHES = [
['e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '32fce663959bdcf612c8f957df4c5687d3fa437f57aba7a978f12570309ee868', '754fc9870cc364ad0b3c1f682d0eff1f08112c38e4205a267316dbd51841fb87', '1a51e28605d825d3a598d0782764eae76eafc75ab96c4876da7e62ea3ae9a10f', 'd4528d0d4890f08b6d495f062dc569d2a6ab81c611dcc387b6cbe155dc613a52', '3c14db928e7a9e2a82053196aaa7d714803f902c1090c248a7d13c14d2febc2a', '2605bebd409fb319507f9ce537fe03dac2f16701486547ac3fb4f2aabeb1a331'],
['e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '37a5369033392330a348b8f00aefa4d7a42e6fe14e2c94552685c4a4408cb399', 'd71abda89271c0f33c024e473dc7812668e4b689a49c61c5136918fbd1bb0081', '05075b813f4766f6282a5ee9f3dcbe77b257b8315f70292cdbce759059d44d35', '241e4f184838b4f9d8845e9e913f2a5b4e38ce12ec0a8abd92adbfa38416803c', '7c5f8a782713986d1a746f08a8dbc628ef56133dcc58e5999237b95732d71fbc', 'b6239a1b423717f3c82665f77dd7862ea02da88a5e5ee3cf4abf6dc500863a1f'],
['e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'be11ad50980fe7d640958cb22414a30b47dc316f9b35fb14601308467faa0ec9', '4db3b51bf03f59a52c65e1ce753f726e257abdd4bddba9809274f3528bb42e8c', '94260085b044608d80bac9b33f1cdf2ca9aef02277e32cb843874b3c91984c23', '45f91e893816ad16c2ea74da1406be9ffade1f866065250b49de79598e289231', 'aca8e01464445fb092f82fc69f8f32f1036089121455d3545a951637714d5446', '5e20f292753576c9cb0d5ca5ec00534771359ac985e140593fb74b054eee3a1a'],
]
HARDWARE_FAULT_OFFSETS = [[0, 0, 36, 0, 36, 0, 36],
                          [0, 0, 16, 0, 16, 0, 16],
                          [0, 4, 44, 8, 48, 4, 44]]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def put(buf, offset, value, fmt='<Q'):
    struct.pack_into(fmt, buf, offset, value)


def unpack_input(blob):
    require(len(blob) == 8952, 'input length')
    return (list(struct.unpack('<31Q', blob[:248])), bytearray(blob[248:760]),
            bytearray(blob[760:4856]), bytearray(blob[4856:]))


def fixture_input(index, variant, fault):
    """Independent binding of the fixed inputs specified in the method."""
    candidate, fixture = (index % 21) // 7, index % 7
    regs, vectors, state, data = [0] * 31, bytearray(512), bytearray(4096), bytearray(4096)
    a = BAD if fault and fixture in (5, 6) else DATA
    b = BAD if fault else DATA
    c = MASK if variant == 0 else MASK >> 1
    v0, v1 = bytes(range(16)), bytes(i ^ 0xa5 for i in range(16))
    put(data, 0, DATA + 128); put(data, 128, DATA)
    if candidate == 0:
        for r, value in ((0, STATE), (10, a), (11, c), (12, 1), (13, b), (17, DATA + 256), (26, 0x202)):
            regs[r] = value
        state[0x90:0xb0] = v0 + v1
    elif candidate == 1:
        for r, value in ((4, a), (7, c), (5, 1), (6, b), (11, DATA + 256), (26, 1)):
            regs[r] = value
        vectors[256:288] = v0 + v1
    else:
        regs[19] = STATE + 16
        for off, value in ((0, a), (8, c), (16, 1), (24, b), (56, DATA + 256)):
            put(state, 16 + off, value)
        state[976:992], state[1040:1056] = v0, v1
    return struct.pack('<31Q', *regs) + vectors + state + data


def last_add(variant, n):
    # SETZ makes D zero after at most two additions for these exact inputs.
    # Remaining iterations then preserve C,D; only vector/alias parity changes.
    c, d = (MASK if variant == 0 else MASK >> 1), 1
    for _ in range(min(n, 3)):
        a, b = c, d
        c = (a + b) & MASK
        d = (d & ~255) | int(c == 0)
    return a, b, c, d


def add_flags(a, b, s):
    cf, zf, sf = int(s < a), int(s == 0), s >> 63
    of = ((~(a ^ b) & (a ^ s)) >> 63) & 1
    af, pf = ((a ^ b ^ s) >> 4) & 1, int(bin(s & 255).count('1') % 2 == 0)
    return cf | pf << 2 | af << 4 | zf << 6 | sf << 7 | of << 11


def nzcv(flags):
    return ((flags >> 7 & 1) << 31 | (flags >> 6 & 1) << 30 |
            (flags & 1) << 29 | (flags >> 11 & 1) << 28)


def expected(index, variant, n, fault, blob):
    regs, vectors, state, data = unpack_input(blob)
    candidate, fixture = (index % 21) // 7, index % 7
    live, store, alias = fixture in (2, 4, 6), fixture in (3, 4), fixture in (5, 6)
    flags = 0x20000000 if candidate == 1 else 0
    scratch = {30}  # The common wrapper owns x30.
    if candidate == 0:
        ar, cr, dr = 10, 11, 12
    elif candidate == 1:
        ar, cr, dr = 4, 7, 5
    else:
        ar = cr = dr = None
        scratch |= ({20, 21, 22, 16} | ({23} if store else set())) if live else ({20, 21} if store else {20}) if fixture else set()
    if live:
        a, b, s, d = last_add(variant, n)
        f = add_flags(a, b, s)
        v0 = bytes(range(16))
        v1 = bytes(i ^ 0xa5 for i in range(16))
        v = bytes(x ^ y for x, y in zip(v0, v1)) if n % 2 else v0
        if candidate == 0:
            regs[cr], regs[dr] = s, b if fault else d
            scratch.add(3)
            put(state, 0x44c, 7, '<I')
            for off, value in ((0x450, a), (0x458, b), (0x460, s)):
                put(state, off, value)
            vectors[:32] = v + v1
            if not fault:
                state[0x90:0xa0] = v
            flags = nzcv(f)
        elif candidate == 1:
            regs[cr], regs[dr], regs[26], regs[27] = s, b if fault else d, s, a ^ b
            scratch.add(20)
            vectors[256:272] = v
            flags = nzcv(f) ^ (0 if fault else 1 << 29)
        else:
            for off, value in ((8, s), (16, b if fault else d), (0x110, s), (0x118, b)):
                put(state, 16 + off, value)
            state[976:992] = v
            vectors[:32] = v + v1
            if not fault:
                put(state, 16 + 0x128, 11, '<I'); state[4] = 1
                # CMP S,0 supplies native NZCV; guest flags reside in cc_*.
                flags = (s >> 63) << 31 | int(s == 0) << 30 | 1 << 29
        if not fault:
            data[256:272] = v
    if fixture and not fault:
        if store:
            put(data, 0, DATA)
        else:
            result = DATA + (128 if not alias or n % 2 else 0)
            if candidate == 2:
                put(state, 16, result)
            else:
                regs[ar] = result
    return regs, vectors, state, data[:512], flags, scratch


def fault_offset(index):
    if index >= 21:
        require(HARDWARE_FAULT_OFFSETS is not None, 'hardware fault sites not inspected')
        return HARDWARE_FAULT_OFFSETS[(index % 21) // 7][index % 7]
    c, f = index // 7, index % 7
    live, store = f in (2, 4, 6), f in (3, 4)
    if c == 0:
        return (40 if store else 36) if live else (4 if store else 0)
    if c == 1:
        return (20 if store else 16) if live else (4 if store else 0)
    return (52 if store else 48) if live else (12 if store else 8)


def verify_sample(row, blob):
    index, variant, n, fault = (row[k] for k in ('index', 'variant', 'n', 'fault'))
    require(blob == fixture_input(index, variant, fault), 'retained input differs from method')
    want = expected(index, variant, n, fault, blob)
    regs = list(struct.unpack('<31Q', bytes.fromhex(row['registers'])))
    for i, value in enumerate(want[0]):
        if i not in want[5]:
            require(regs[i] == value, f'x{i} live/preserved mismatch')
    for key, value in zip(('vectors', 'state', 'data'), want[1:4]):
        require(bytes.fromhex(row[key]) == value, key + ' mismatch')
    # Fault entry can alter non-NZCV PSTATE bits; the handler has no flag writer.
    require(row['cpsr'] & 0xf0000000 == want[4], 'native flags mismatch')
    require(row['cpsr'] & 0xf == 5, 'wrong EL/SP mode')
    if fault:
        esr = row['esr']
        require(esr >> 26 == 0x25 and esr & 0x3f == 6, 'not same-EL L2 translation data abort')
        require(esr & (1 << 10) == 0, 'FAR invalid')
        require((esr >> 6 & 1) == int(index % 7 in (3, 4)), 'fault access direction')
        require(row['far'] == BAD, 'wrong fault address')
        require(row['elr'] == row['body_address'] + fault_offset(index), 'wrong native fault PC')
        require(regs[30] == n, 'fault loop counter changed')
    else:
        require(regs[30] == row['end'], 'end timestamp/register mismatch')


def schedule_for(phase, count=21):
    require(count in (21, 42), 'unsupported body count')
    if phase == 'admission':
        return [(i, v, n, 0) for i in range(count) for v in range(2) for n in (1, 2, 3)] + [(c * 7 + f, 0, 1, 1) for c in range(count // 7) for f in range(1, 7)]
    require(phase == 'timing', 'unknown phase')
    rng, rows = random.Random(20260920), []
    for repeat in range(9):
        batch = [(i, repeat % 2, n, 0) for i in range(count) for n in (262144, 1048576)]
        rng.shuffle(batch); rows.extend(batch)
    return rows


def check_inputs(source):
    """Fail closed on provenance, byte binding, profile, inputs and schedules."""
    source = Path(source)
    metadata = json.loads((source / 'extraction.json').read_text())
    count = len(metadata)
    require(count in (21, 42), 'extraction count')
    for phase in ('admission', 'timing'):
        retained = [tuple(map(int, line.split())) for line in (source / (phase + '.schedule')).read_text().splitlines()]
        require(retained == schedule_for(phase, count), 'retained schedule differs from fixed method')
    for i, entry in enumerate(metadata):
        base = i % 21
        hardware = i >= 21
        hashes = HARDWARE_HASHES if hardware else HASHES
        require(hashes is not None, 'hardware body bytes not independently inspected')
        body = (source / f'{i}.body').read_bytes()
        native = (source / f'{i}.native').read_bytes()
        require(entry['index'] == i and entry['candidate'] == CANDIDATES[base // 7] and entry['fixture'] == NAMES[i % 7], 'extraction identity')
        profile = 'hardware-tso-dependent' if hardware else 'software-ordering'
        require(entry.get('ordering_profile', 'software-ordering' if count == 21 else None) == profile, 'ordering profile identity')
        require(hashlib.sha256(body).hexdigest() == hashes[base // 7][i % 7], 'body differs from inspected slice')
        c, f = base // 7, i % 7
        live = f in (2, 4, 6)
        if c == 0:
            span = (0, (56 if live else 0 if f == 0 else 4) if hardware else [0, 4, 64, 12, 68, 4, 64][f])
        elif c == 1:
            span = (12, 12 if f == 0 else (48 if hardware else 56) if live else (16 if hardware else 20))
        else:
            span = (12, 120 if hardware else 128) if live else (16, 16 if f == 0 else 28 if hardware else 32)
        require((entry['start'], entry['end']) == span, 'range differs from inspected binding')
        require(0 <= entry['start'] <= entry['end'] <= len(native) and entry['start'] % 4 == entry['end'] % 4 == 0, 'invalid slice range')
        require(body == native[entry['start']:entry['end']] == bytes.fromhex(entry['body_hex']), 'slice provenance mismatch')
        require(hashlib.sha256(native).hexdigest() == entry['sha256'], 'full native hash mismatch')
        for variant in (0, 1):
            for fault in (0, 1):
                blob = (source / f'{i}-{variant}-{fault}.input').read_bytes()
                require(blob == fixture_input(i, variant, fault), 'retained input differs from method')
    return dict(body_count=count, profiles=['software-ordering', 'hardware-tso-dependent'] if count == 42 else ['software-ordering'], status='inputs_checked')


def check(rows, source, phase):
    source = Path(source)
    count = check_inputs(source)['body_count']
    schedule = schedule_for(phase, count)
    require(len(rows) == len(schedule) + 2, 'missing/extra output records')
    boot, end = rows[0], rows[-1]
    require(boot['kind'] == 'boot' and end['kind'] == 'teardown', 'boot/teardown ordering')
    for key, value in dict(actlr=2, mair=255, tcr=16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32, sctlr=0x30d01985).items():
        require(boot[key] == value, 'boot ' + key)
    require(0 < boot['code_bytes'] <= 0x4000, 'code extent')
    require((boot['isar0'] >> 52 & 15) >= 1, 'FEX requires FlagM')
    # Current producer: 21 boot APIs, 137 per sample, three teardown APIs.
    require(end['samples'] == len(schedule) and end['api_calls'] == 24 + 137 * len(schedule), 'teardown accounting')
    hz = boot['hz']
    num, den = boot['timebase']
    timing_ok = hz > 0 and hz == boot['guest_hz'] and num > 0 and den > 0
    ratios, groups, body_addresses = [], {}, {}
    for serial, (row, case) in enumerate(zip(rows[1:-1], schedule)):
        require(row['kind'] == 'sample' and row['serial'] == serial, 'sample order')
        require(tuple(row[k] for k in ('index', 'variant', 'n', 'fault')) == case, 'sample schedule mismatch')
        i, v, n, fault = case
        address = row['body_address']
        require(BASE <= address < BASE + boot['code_bytes'] and address % 4 == 0, 'body address outside code')
        require(body_addresses.setdefault(i, address) == address, 'body address changed')
        blob = (source / f'{i}-{v}-{fault}.input').read_bytes()
        try:
            verify_sample(row, blob)
        except (ValueError, struct.error) as error:
            raise ValueError(f'sample {serial} {CANDIDATES[(i % 21) // 7]}/{NAMES[i % 7]}: {error}') from error
        if not fault:
            ticks = row['end'] - row['start']
            valid = row['start'] > 0 and 0 < ticks <= row['host_ticks'] and row['host_mach'] > 0
            ratio = row['host_ticks'] * 1e9 * den / (row['host_mach'] * num * hz) if min(row['host_mach'], num, hz) > 0 else 0
            # Broad bracketing tolerance: diagnostic sanity, not qualified clock evidence.
            timing_ok &= valid and 0.9 <= ratio <= 1.1
            ratios.append(ratio)
            groups.setdefault((i, n), []).append(ticks / n)
    stats, paired_profiles = [], []
    if phase == 'timing':
        for i in range(count):
            small, large = groups[i, 262144], groups[i, 1048576]
            require(len(small) == len(large) == 9, 'timing repeat coverage')
            stats.append(dict(candidate=CANDIDATES[(i % 21) // 7], fixture=NAMES[i % 7],
                              ordering_profile='hardware-tso-dependent' if i >= 21 else 'software-ordering',
                              ticks_per_body_n262144=statistics.median(small),
                              ticks_per_body_n1048576=statistics.median(large),
                              paired_total_tick_ratios=[4 * b / a if a else None for a, b in zip(small, large)]))
        if count == 42:
            for i in range(21):
                for n in (262144, 1048576):
                    paired_profiles.append(dict(candidate=CANDIDATES[i // 7], fixture=NAMES[i % 7], n=n,
                                                software_over_hardware=[a / b if b else None for a, b in zip(groups[i, n], groups[i + 21, n])]))
    return dict(correctness='pass', samples=len(schedule), timing_sanity=bool(timing_ok),
                clock_ratio_range=[min(ratios), max(ratios)], exploratory_body_timings=stats,
                paired_profile_ratios=paired_profiles,
                limits='Cached-state unchanged slices; no complete guest recovery, canonicalization, translation, dispatch, or generator ranking. Hardware profile depends on ACTLR readback; this is not general TSO validation. No NOP subtraction. Clock sanity is not timing qualification.')


def self_check():
    checked = 0
    for variant in (0, 1):
        c, d = (MASK if variant == 0 else MASK >> 1), 1
        for n in range(1, 201):
            a, b = c, d; c = (c + d) & MASK; d = d & ~255 | int(c == 0)
            require(last_add(variant, n) == (a, b, c, d), 'recurrence oracle')
        require(last_add(variant, 1048576) == (c, d, c, d), 'stable recurrence')
    require(add_flags(MASK, 1, 0) == 0x55, 'carry/zero/parity/aux flags')
    require(add_flags(MASK >> 1, 1, 1 << 63) == 0x894, 'overflow/sign/aux flags')
    for index, variant, n, fault in schedule_for('admission', 42) + [(i, v, n, 0) for i in range(42) for v in (0, 1) for n in (262144, 1048576)]:
        blob = fixture_input(index, variant, fault)
        regs, vectors, state, data, flags, _ = expected(index, variant, n, fault, blob)
        regs[30] = n if fault else 100
        row = dict(index=index, variant=variant, n=n, fault=fault,
                   registers=struct.pack('<31Q', *regs).hex(), vectors=vectors.hex(),
                   state=state.hex(), data=data.hex(), cpsr=0x3c5 | flags, end=100,
                   esr=0x25 << 26 | 6 | (int(index % 7 in (3, 4)) << 6),
                   far=BAD, elr=BASE + fault_offset(index), body_address=BASE)
        verify_sample(row, blob)
        candidate = (index % 21) // 7
        for key, offset in (('registers', 19 * 8 if candidate == 2 else 6 * 8 if candidate == 1 else 13 * 8), ('vectors', 17 * 16 if candidate == 1 else 16), ('state', 2000), ('data', 0)):
            bad = dict(row); corrupted = bytearray.fromhex(bad[key]); corrupted[offset] ^= 1; bad[key] = corrupted.hex()
            try:
                verify_sample(bad, blob)
            except ValueError:
                checked += 1
            else:
                raise AssertionError('accepted mutation ' + key)
        bad = dict(row); bad['cpsr'] ^= 1 << 30
        try:
            verify_sample(bad, blob)
        except ValueError:
            checked += 1
        else:
            raise AssertionError('accepted native flags mutation')
        if fault:
            for key in ('esr', 'far', 'elr'):
                bad = dict(row); bad[key] ^= 4
                try:
                    verify_sample(bad, blob)
                except ValueError:
                    checked += 1
                else:
                    raise AssertionError('accepted fault mutation ' + key)
    print(json.dumps(dict(self_check='pass', rejected_mutants=checked)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    if not args.self_check:
        parser.error('use --self-check; runner calls check(rows, source, phase)')
    self_check()
