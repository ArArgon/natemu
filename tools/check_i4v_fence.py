#!/usr/bin/env python3
"""Finite LFENCE live-dependency oracle, independent of runtime/ABI helpers.

The literal ADDPS/ADD/LAHF/SETO/store effects determine expectations. Native
scratch roots are separately derived from retained LLVM disassembly. This is
qualification of the admitted capture/completion adapter, not raw DMB LD or a
universal proof for every floating-point input or guest instruction.
"""
import argparse
import copy
import json
from pathlib import Path

from check_i1 import resident_pc
from i4v_check import exact, require, uint, TCR
from check_i4v_ordering import native_seed, initial_tables, native_leaf, numeric_transport

PRIVATE = 0x800040000000
CODE = PRIVATE + 0x30000
DATA, RESULTS, RIP = 0x100000, 0x101000, 0x102000
TRIALS = 16
MASK = (1 << 64) - 1
SOURCE = bytes.fromhex('0f58c14801d10faee89f0f90c048890f66894708f30f7f4710')
SEMANTIC_SUFFIX = bytes.fromhex('0fae5f20')
# Actual pinned FEX output, disassembled independently in lfence-producer-01.
BODY = bytes.fromhex('10d6314efb0005cafa0005abe7031aaabf3d03d5f4379f9a75031ac'
                     'ab5120453941215aa40135a4a0008404a1504404ab5fa7fb294faf5aa'
                     '15423bd5b5066292946255aa94027fb2841e78b3f4779f9a841e40b3'
                     '1f2003d567fd9fc8742100911f2003d584fe9f4814028052bf3b03d5'
                     '70e9b43c1f4000d5')


def configuration(profile, n):
    uint(profile, 32, 'case'); uint(n, 32, 'participant count')
    require(profile < 5 and 1 <= n <= 4, 'finite fence fixture')
    return (n << 8) | (profile + 1)


def selected(profile, cpu, trial):
    return (profile + cpu + trial) % 5


def operands(case):
    require(type(case) is int and 0 <= case < 5, 'finite arithmetic case')
    return ((MASK, 1), ((1 << 63) - 1, 1), (15, 1), (1, 2), (1, 2))[case]


def fp_add_positive_normal(a, b):
    """Exact integer binary32 RNE reference for this finite positive-normal set."""
    for value in (a, b):
        require(type(value) is int and value >> 31 == 0 and 0 < (value >> 23) < 255,
                'positive finite normal binary32 reference domain')
    ea, eb = (a >> 23) - 150, (b >> 23) - 150
    exponent = min(ea, eb)
    total = (((a & 0x7fffff) | 0x800000) << (ea - exponent)) + (((b & 0x7fffff) | 0x800000) << (eb - exponent))
    shift = total.bit_length() - 24
    if shift > 0:
        significand, remainder = divmod(total, 1 << shift)
        half = 1 << (shift - 1)
        significand += int(remainder > half or (remainder == half and significand & 1))
    else:
        significand, remainder = total << -shift, 0
    exponent += shift
    if significand == 1 << 24:
        significand >>= 1
        exponent += 1
    biased = exponent + 150
    require(0 < biased < 255, 'normal result in admitted reference domain')
    return (biased << 23) | (significand & 0x7fffff), bool(remainder)


def vector_bytes(words):
    return list(b''.join(w.to_bytes(4, 'little') for w in words))


def vectors(case):
    return ([0x3f800000, 0x40000000, 0x40400000, 0x40800000],
            [0x34000000 if case == 4 else 0x33800000, 0x40000000, 0x3f800000, 0x3f800000])


def mxcsr_before(case):
    return 0x1f80 | (1 if case == 2 else 0x10 if case == 3 else 0)


def owner_initial(cpu):
    frame = native_seed(cpu)
    frame['x'][11] = RESULTS + cpu * 128
    frame['fpcr'] = frame['fpsr'] = 0
    return frame


def trial_initial(cpu, case):
    frame = owner_initial(cpu)
    frame['x'][7], frame['x'][5] = operands(case)
    frame['q'][16], frame['q'][17] = map(vector_bytes, vectors(case))
    return frame


def results(cpu, case):
    """Full initial, completed-prefix and generated-suffix states plus outputs."""
    initial = trial_initial(cpu, case)
    sink = copy.deepcopy(initial)
    a, b = operands(case)
    value = (a + b) & MASK
    n, z, c = value >> 63, int(value == 0), (a + b) >> 64
    v = ((~(a ^ b) & (a ^ value)) >> 63) & 1
    af = ((a ^ b ^ value) >> 4) & 1
    parity = int(bin(value & 255).count('1') % 2 == 0)
    sink['x'][7] = sink['x'][26] = value
    sink['x'][27] = a ^ b
    sink['nzcv'] = (n << 31) | (z << 30) | (c << 29) | (v << 28)
    additions = [fp_add_positive_normal(x, y) for x, y in zip(*vectors(case))]
    sink['q'][16] = vector_bytes([x for x, _ in additions])
    sink['fpsr'] = 16 if any(inexact for _, inexact in additions) else 0
    final = copy.deepcopy(sink)
    lahf = (n << 7) | (z << 6) | (af << 4) | (parity << 2) | 2 | c
    final['x'][4] = (initial['x'][4] & ~0xffff) | (lahf << 8) | v
    scratch = (value & 0xffffffff) ^ ((value & 0xffffffff) >> 4)
    final['x'][0] = scratch ^ (scratch >> 2)
    final['x'][20] = 16
    final['x'][21] = sink['nzcv'] & 0xc0000000
    final['nzcv'] ^= 1 << 29  # Actual generated CFINV after all flag consumers.
    mxcsr = mxcsr_before(case) | ((sink['fpsr'] >> 4) << 5)
    output = [0xa5] * 40
    output[:8] = value.to_bytes(8, 'little')
    output[8:10] = (final['x'][4] & 0xffff).to_bytes(2, 'little')
    output[16:32] = sink['q'][16]
    output[32:36] = mxcsr.to_bytes(4, 'little')
    return initial, sink, final, output, mxcsr


def packet(vm, profile, n, cpu, chunk):
    context = configuration(profile, n)
    require(type(cpu) is int and 0 <= cpu < n and type(chunk) is int and 0 <= chunk < 4, 'owner/chunk identity')
    require(uint(vm, 64, 'VM nonce') != 0, 'nonzero compiler VM identity')
    source = SOURCE.ljust(32, b'\0')
    return [0x49345691, 1, vm, cpu, 1, 1, context, RIP + cpu * 256, 64] + [int.from_bytes(source[i:i + 8], 'little') for i in range(0, 32, 8)] + [1, 120, chunk]


def reply(vm, profile, n, cpu, chunk):
    p = packet(vm, profile, n, cpu, chunk)
    p[0] = 0x49345692
    body = BODY[chunk * 32:(chunk + 1) * 32].ljust(32, b'\0')
    p[9:13] = [int.from_bytes(body[i:i + 8], 'little') for i in range(0, 32, 8)]
    return p


def code_image(sink, returned):
    require(resident_pc(sink) and resident_pc(returned) and sink != returned, 'distinct resident sink/return callbacks')
    def branch(at, target):
        delta = target - at
        require(delta % 4 == 0 and -(1 << 27) <= delta < 1 << 27, 'direct branch representable')
        return (0x14000000 | ((delta // 4) & 0x3ffffff)).to_bytes(4, 'little')
    return list(BODY[:20] + branch(CODE + 20, sink) + BODY[20:] + branch(CODE + 124, returned))


def source_image(n):
    image = [0xcc] * 4096
    for cpu in range(n):
        image[cpu * 256:cpu * 256 + 29] = SOURCE + SEMANTIC_SUFFIX
    return image


def expected_image(profile, n, trial):
    image = [0xa5] * 8192
    for cpu in range(n):
        at = 4096 + cpu * 128
        image[at:at + 40] = results(cpu, selected(profile, cpu, trial))[3]
    return image


def audit_development(record, producer_directory=None):
    require(type(record) is dict, 'fence report object')
    numeric_transport(record)
    exact(set(record), {'case', 'profile', 'n', 'configured_vcpus', 'tested_vcpus', 'trial_count',
                        'vm_nonce', 'owners', 'images', 'source_before', 'source_after',
                        'tables_before', 'tables_after', 'common'}, 'full fence report schema')
    p, n = record['profile'], record['n']
    configuration(p, n)
    exact((record['case'], record['configured_vcpus'], record['tested_vcpus'], record['trial_count']),
          ('fence', n, n, TRIALS), 'declared finite execution')
    for name in ('source_before', 'source_after'):
        exact(record[name], source_image(n), 'full immutable source with semantic STMXCSR')
    for name in ('tables_before', 'tables_after'):
        exact(record[name], initial_tables(), 'preaccessed guest tables unchanged')
    exact(record['common'], dict(compiled=(1 << n) - 1, stopped=(1 << n) - 1, epoch=TRIALS,
                                ready=[TRIALS] * n + [0] * (4 - n), done=[TRIALS] * n + [0] * (4 - n)), 'trial gates')
    require(type(record['owners']) is list and len(record['owners']) == n, 'all owners')
    require(type(record['images']) is list and len(record['images']) == TRIALS, 'all full RAM snapshots')
    for trial, image in enumerate(record['images']):
        exact(image, bytes(expected_image(p, n, trial)).hex(), 'full final RAM and untouched canaries')
    for cpu, cell in enumerate(record['owners']):
        exact(set(cell), {'id', 'trial', 'completed', 'expected', 'requests', 'replies', 'transfers', 'walks',
                          'table_reads', 'table_writes', 'routes', 'code', 'code_length', 'sink_pc', 'return_pc',
                          'status', 'detail', 'current_el', 'sctlr', 'actlr', 'tcr', 'capture_pc', 'service_pc',
                          'initial', 'current', 'captured', 'bank_before', 'bank_after', 'published_code',
                          'final_native_leaf', 'compiler_requests', 'trials'}, 'complete owner schema')
        requests = [packet(record['vm_nonce'], p, n, cpu, c) for c in range(4)]
        replies = [reply(record['vm_nonce'], p, n, cpu, c) for c in range(4)]
        code = code_image(cell['sink_pc'], cell['return_pc'])
        bank = [((i * 37 + 19) & 255) ^ cpu for i in range(512)]
        expected = dict(id=cpu, trial=TRIALS, completed=TRIALS, expected=requests[-1], requests=requests,
                        replies=replies, transfers=4, walks=3, table_reads=12, table_writes=0,
                        routes=[DATA, RESULTS, RIP + cpu * 256], code=code, code_length=128, published_code=code,
                        status=1, detail=0, current_el=4, tcr=TCR, capture_pc=cell['return_pc'],
                        initial=owner_initial(cpu), bank_before=bank, bank_after=bank,
                        final_native_leaf=native_leaf(), compiler_requests=requests)
        for key, value in expected.items():
            exact(cell[key], value, f'owner{cpu}.{key}')
        require(resident_pc(cell['service_pc']), 'resident compiler service')
        require(cell['sctlr'] & 0x1005 == 0x1005 and cell['sctlr'] & 2 == 0, 'native MMU/caches and admitted alignment')
        require(cell['actlr'] & 2 != 0, 'hardware TSO active')
        require(type(cell['trials']) is list and len(cell['trials']) == TRIALS, 'all live dependency trials')
        for trial, t in enumerate(cell['trials']):
            case = selected(p, cpu, trial)
            initial, sink, final, _, mxcsr = results(cpu, case)
            expected = dict(epoch=trial + 1, case=case, completed=1, fence_completed=1, sink_count=1,
                            initial=initial, sink=sink, captured=final, sink_output=[0xa5] * 40,
                            mxcsr_before=mxcsr_before(case), mxcsr=mxcsr, continuation=CODE + 24,
                            actlr=t['actlr'])
            exact(t, expected, f'owner{cpu}.trial{trial} complete prefix/suffix and preserved roots')
            require(t['actlr'] & 2 != 0, 'TSO enabled at every actual return')
        final = results(cpu, selected(p, cpu, TRIALS - 1))[2]
        exact(cell['current'], final, 'resident final full frame')
        exact(cell['captured'], final, 'actual final shared capture')
        if producer_directory is not None:
            from i4v_fence_worker import verify_compilations
            verify_compilations(Path(producer_directory) / f'cpu-{cpu}', requests, replies)
    return dict(profile=p, owners=n, trials=TRIALS, native_entries=n * TRIALS,
                completion_sinks=n * TRIALS, continuations=n * TRIALS,
                native_faults=0, compilations=n, transfers=4 * n)


def check(record, expected_profile=None, expected_vcpus=None, producer_directory=None):
    require(type(record) is dict and all(k in record for k in ('profile', 'n')), 'declared fence identity')
    configuration(record['profile'], record['n'])
    for key, value in (('profile', expected_profile), ('n', expected_vcpus)):
        if value is not None:
            uint(value, 32, 'requested ' + key)
            exact(record[key], value, 'requested fence ' + key)
    return audit_development(record, producer_directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--profile', type=int)
    parser.add_argument('--vcpus', type=int)
    parser.add_argument('--producer-directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(check(json.loads(args.report.read_text()), args.profile, args.vcpus, args.producer_directory)))
