#!/usr/bin/env python3
"""Time hot, aligned accesses in a 32-bit compatibility-mode user loop: through flat DS, through a limited FS or GS (each
instruction's operand is checked inline) and through a flat-limit FS (Linux's TLS shape: no check), as dword MOV loads and
stores and as a mix of other forms (read-modify-write, compare with an immediate, a byte load, a scalar SSE load)."""

import argparse
import json
import pathlib
import statistics
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
import testenv
from run_slice3_vm import body_end, driver, smp_description, vm_state

import compat
from smp import num

# 800,000 accesses per sample leaves time for the per-access resident check on both segments.
ITERATIONS = 50_000
WARMUP = 4096
SAMPLES = 3
WORDS = 8
TSC_HZ = 1_000_000_000  # natemu's virtual TSC rate
FIRST_WORD = 0x40  # offset from TLS, with a byte-granular segment limit of 0xfff


# The mixed loop's instructions per word: each adds 2 to the word in all.
MIXED = ('inc dword [{s}:esi+offset]', 'add [{s}:esi+offset], ebx', 'cmp dword [{s}:esi+offset], 0',
         'movzx eax, byte [{s}:esi+offset]', 'movss xmm0, [{s}:esi+offset]')


def program(segment, flat_limit=False, mixed=False):
    # Use the same linear addresses, operand encodings and loop for all modes.
    # DS is flat. Load the nonzero-base descriptor only into the segment being timed.
    selector = 0x1b if segment == 'ds' else 0x2b
    base = compat.TLS if segment == 'ds' else 0
    work = ('\n'.join('    ' + i.format(s=segment) for i in MIXED) if mixed else
            f'    mov eax, [{segment}:esi+offset]\n    add eax, offset\n    mov [{segment}:esi+offset], eax')
    user = f'''    mov eax, {selector:#x}
    mov {segment}, ax
    mov esi, {base:#x}
    mov edi, {compat.W:#x}
    mov ebp, {SAMPLES + 1}
    mov ebx, 1
    mov ecx, {WARMUP}
.sample:
    lfence
    rdtsc
    lfence
    mov [edi], eax
    mov [edi+4], edx
    call .work
    mfence
    lfence
    rdtsc
    lfence
    mov [edi+8], eax
    mov [edi+12], edx
    add edi, 16
    mov ecx, {ITERATIONS}
    dec ebp
    jnz .sample
    int 0x80
.work:
    %assign offset {FIRST_WORD}
    %rep {WORDS}
{work}
    %assign offset offset+4
    %endrep
    dec ecx
    jnz .work
    ret
'''
    descriptor = compat.F['g'] + 0x28
    # A byte-granular limit of 0xfff (a limited segment), or 4 GiB (flat-limit: page-granular, limit 0xfffff).
    limit = (f'    mov word [{descriptor:#x}], 0xffff\n    or byte [{descriptor + 6:#x}], 0x8f\n' if flat_limit else
             f'    mov word [{descriptor:#x}], 0xfff\n    and byte [{descriptor + 6:#x}], 0x70\n')
    kernel = limit + compat.ENTER() + compat.handlers()
    return compat.program(f'compat-tls-{segment}{"-flat-limit" if flat_limit else ""}{"-mixed" if mixed else ""}', kernel, user)


def measure(label, segment, flat_limit, mixed, host, runtime, fexc, out):
    prog = program(segment, flat_limit, mixed)
    rc, record, err = driver(host, runtime, fexc, smp_description(prog), out / label,
                             ['--watchdog-ms', '10000'], timeout=15, shape=False)
    if rc:
        raise RuntimeError(f'{segment}: VM exit {rc}: {err}')
    cpu = record['cpus'][0]
    end = body_end(prog, num(prog['cpus'][0]['rip']))
    if cpu['stop'] != 'reached' or num(cpu['rip']) != end:
        raise RuntimeError(f'{segment}: {cpu["stop"]} {cpu["reason"]} at {cpu["rip"]}')
    state = vm_state(prog, record)
    memory = {r['addr']: r['data'] for r in state['memory']}
    log = memory[compat.LOG]
    events, = struct.unpack_from('<Q', log)
    vector, _, _, cs = struct.unpack_from('<4Q', log, 0x100)
    if state['cpus'][0]['fault'] or (events, vector, cs) != (1, 0x80, 0xdb):
        raise RuntimeError(f'{segment}: bad completion: events={events}, vector={vector:#x}, CS={cs:#x}, '
                           f'fault={state["cpus"][0]["fault"]}')
    data = memory[compat.W]
    words = struct.unpack_from(f'<{WORDS}I', data, compat.TLS - compat.W + FIRST_WORD)
    total = WARMUP + SAMPLES * ITERATIONS
    want = tuple((total * (2 if mixed else FIRST_WORD + 4 * i)) & 0xffffffff for i in range(WORDS))
    if words != want:
        raise RuntimeError(f'{segment}: final words {words} != {want}')
    times = list(struct.iter_unpack('<QQ', data[:16 * (SAMPLES + 1)]))
    endpoints = [tick for pair in times for tick in pair]
    if any(x >= y for x, y in zip(endpoints, endpoints[1:])):
        raise RuntimeError(f'{segment}: non-increasing TSC samples {times}')
    # The shorter first sample warms the timing path and the work loop at the same guest RIPs.
    return [end - start for start, end in times[1:]]


def main():
    testenv.require_framework(__file__)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=pathlib.Path, required=True)
    parser.add_argument('--natemu', nargs=3, metavar=('HOST', 'RUNTIME', 'FEXC'), required=True)
    args = parser.parse_args()
    variants = {}
    # flat, fs, gs: the dword MOV loop through flat DS and through a limited FS, GS. The others: the same loop through a
    # flat-limit FS (no check in its code), and the mixed loop through each kind of segment.
    for label, segment, flat_limit, mixed in (('flat', 'ds', False, False), ('fs', 'fs', False, False), ('gs', 'gs', False, False),
                                              ('fs-flat-limit', 'fs', True, False), ('mixed-flat', 'ds', False, True),
                                              ('mixed-fs', 'fs', False, True), ('mixed-fs-flat-limit', 'fs', True, True)):
        accesses = (len(MIXED) if mixed else 2) * WORDS * ITERATIONS
        ticks = measure(label, segment, flat_limit, mixed, *args.natemu, args.out)
        costs = [dt * 1e9 / TSC_HZ / accesses for dt in ticks]
        variants[label] = dict(tsc_ticks=ticks, accesses_per_sample=accesses, ns_per_access=costs, median_ns_per_access=statistics.median(costs))
    result = dict(tsc_hz=TSC_HZ, warmup_iterations=WARMUP, iterations_per_sample=ITERATIONS, variants=variants)
    text = json.dumps(result, indent=2)
    (args.out / 'compat-tls.json').write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
