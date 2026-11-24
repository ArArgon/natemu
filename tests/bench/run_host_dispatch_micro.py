#!/usr/bin/env python3
"""host-dispatch-micro: the indirect plan's host kill-test (docs/restructure/indirect-cache-plan.md section 5), mechanism
only. Host-native Arm code shaped like natemu's exits, timed on this Mac; never a guest number, never on the benchmarks page.

    python3 tests/bench/run_host_dispatch_micro.py --out DIR [--rounds 5] [--exits 16777216]

The generator writes one assembly file with 64 "blocks" per variant and a small C driver (built with the Xcode command-line
tools' `cc`). Each block loads the next guest RIP from a trace (x0), counts the exit, and leaves through its variant's exit:

  V0   today: `b slot`; the slot probes the data table (the 13-word probe of cache::write_slot) and ends in `br x1`
  S1J  step 1, jumps: `b tail`; tail `bl lookup; br x1`; the lookup form is the same probe ending in `ret`
  S1C  step 1, calls: tail `bl lookup; blr x1; b miss` (the pad); nothing pops what `blr` pushes
  S2J  step 2, jumps: a one-entry inline cache filled with the site's main target before S1J's words
  S2C  step 2, calls: the cache's hit is `bl <entry>; b miss`, its miss S1C's words

Patterns: each of the 64 sites has 4 targets, the main one at a share p (0.55, 0.63, 0.75, 0.97, 1.0; the census's main-target
shares) and the rest evenly; the trace is drawn from that Markov chain, with a short period (64) or a long one (65536), and
repeated. `interp`: 64 handlers, each with 4 successors at 0.5, 0.25, 0.15, 0.1, as a bytecode loop of 4096 ops repeated.
Rounds: every pattern and variant in each round, variants alternating (forward in odd rounds, reversed in even ones), each
timing preceded by a warm-up of the same variant and trace. ns per exit is the median over rounds, the noise the spread
(max - min) across rounds.

Verdict (the plan's): step 1 is killed if it saves less than 1 ns per exit over V0 at p = 0.63 and 0.75 in the long period
(jumps), or loses more than 0.5 ns at p = 1.0 (either period); step 2 is considered only if it saves 1 ns or more over step 1
at 0.75 (long period). Writes DIR/result.json and DIR/summary.md. Exit 0 when measured (whatever the verdict), 1 on an error."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import platform
import random
import statistics
import struct
import subprocess

SITES = 64
TARGETS = 4
SHARES = (0.55, 0.63, 0.75, 0.97, 1.0)
PERIODS = {'short': 64, 'long': 65536}
INTERP_SHARES = (0.5, 0.25, 0.15, 0.1)
INTERP_PERIOD = 4096
VARIANTS = ('V0', 'S1J', 'S1C', 'S2J', 'S2C')
RIP_BASE = 0x5555_5000_0000         # a 64-bit user address: 3 words to build (movz, movk, movk); low 6 bits zero
SEED = 20261123
KILL_SAVE_NS = 1.0
KILL_LOSS_NS = 0.5
STEP2_SAVE_NS = 1.0


def successors(seed=SEED):
    """T[s]: the 4 targets of site s; T[s][0] is the main one, a single 64-cycle (so p = 1.0 visits every site)."""
    rng = random.Random(seed)
    order = list(range(SITES))
    rng.shuffle(order)
    main = {order[i]: order[(i + 1) % SITES] for i in range(SITES)}
    t = []
    for s in range(SITES):
        rest = rng.sample([x for x in range(SITES) if x != main[s]], TARGETS - 1)
        t.append([main[s], *rest])
    return t


def chain(t, shares, n, seed):
    """n sites drawn from the Markov chain: from s, t[s][k] with probability shares[k]."""
    rng = random.Random(seed)
    s, out = t[0][0], []
    for _ in range(n):
        s = rng.choices(t[s], weights=shares)[0]
        out.append(s)
    return out


def patterns(t):
    """(name, list of sites) for every pattern."""
    out = []
    for p in SHARES:
        rest = (1.0 - p) / (TARGETS - 1)
        for pname, n in PERIODS.items():
            out.append((f'p{p:.2f}-{pname}', chain(t, (p, rest, rest, rest), n, SEED + int(p * 100) + n)))
    out.append(('interp', chain(t, INTERP_SHARES, INTERP_PERIOD, SEED + 7)))
    return out


PROBE = '''    and x1, x0, #63
    add x2, x28, #1, lsl #12
    add x2, x2, x1, lsl #5
    ldr x1, [x2]
    eor x1, x1, x0
    cbnz x1, 1f
    ldr x1, [x2, #8]
    {end}
1:  ldr x1, [x2, #16]
    eor x1, x1, x0
    cbnz x1, {miss}
    ldr x1, [x2, #24]
    {end}
'''


def assembly(t) -> str:
    """Every variant's 64 blocks, slot, lookup form and miss; and the address tables the driver reads."""
    a = ['    .text', '    .p2align 7', '    .globl _hdm_run',
         # hdm_run(entry, trace, mask, exits, table): x19 trace, x20 exits left, x21 index, x22 mask, x28 table base
         '_hdm_run:', '    stp x29, x30, [sp, #-64]!', '    stp x19, x20, [sp, #16]', '    stp x21, x22, [sp, #32]',
         '    stp x27, x28, [sp, #48]', '    mov x29, sp', '    mov x19, x1', '    mov x20, x3', '    mov x21, #0', '    mov x22, x2',
         '    mov x28, x4', '    br x0',
         '_hdm_done:', '    mov sp, x29', '    ldp x27, x28, [sp, #48]', '    ldp x21, x22, [sp, #32]', '    ldp x19, x20, [sp, #16]',
         '    ldp x29, x30, [sp], #64', '    ret']
    for v in VARIANTS:
        a += ['    .p2align 7', f'{v}_slot:', PROBE.format(end='br x1', miss=f'{v}_miss'),
              '    .p2align 7', f'{v}_lookup:', PROBE.format(end='ret', miss=f'{v}_lmiss'),
              f'{v}_lmiss:', f'    adr x1, {v}_miss', '    ret',
              f'{v}_miss:', '    brk #1']
        tails = []                  # the appended words, after the variant's blocks (the cold side)
        for s in range(SITES):
            a += ['    .p2align 7', f'{v}_b{s}:',
                  '    ldr x0, [x19, x21, lsl #3]', '    add x21, x21, #1', '    and x21, x21, x22',
                  '    subs x20, x20, #1', '    b.eq _hdm_done']
            if v == 'V0':
                a.append(f'    b {v}_slot')
                continue
            a.append(f'    b {v}_t{s}')
            tails += ['    .p2align 4', f'{v}_t{s}:']
            if v.startswith('S2'):
                c = RIP_BASE + t[s][0]
                tails += [f'    movz x1, #{c & 0xffff:#x}', f'    movk x1, #{c >> 16 & 0xffff:#x}, lsl #16',
                          f'    movk x1, #{c >> 32 & 0xffff:#x}, lsl #32', '    eor x1, x1, x0', f'    cbnz x1, {v}_l{s}']
                tails += ([f'    b {v}_b{t[s][0]}'] if v == 'S2J' else [f'    bl {v}_b{t[s][0]}', f'    b {v}_miss'])
                tails.append(f'{v}_l{s}:')
            tails += [f'    bl {v}_lookup', '    br x1' if v.endswith('J') else '    blr x1']
            if v.endswith('C'):
                tails.append(f'    b {v}_miss')
        a += tails
    a += ['    .section __DATA,__data', '    .p2align 3', '    .globl _hdm_blocks', '_hdm_blocks:']
    for v in VARIANTS:
        a += [f'    .quad {v}_b{s}' for s in range(SITES)]
    return '\n'.join(a) + '\n'


DRIVER = r'''
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/qos.h>

#define SITES %(sites)d
#define NV %(nv)d
#define RIP_BASE %(rip)dULL
extern void hdm_run(void *entry, const uint64_t *trace, uint64_t mask, uint64_t exits, void *table);
extern void *hdm_blocks[NV * SITES];

static uint64_t table[NV][(4096 + 64 * 32) / 8] __attribute__((aligned(4096)));

int main(int argc, char **argv) {
    if (argc < 5) { fprintf(stderr, "usage: driver ROUNDS EXITS TRACEFILE NAME...\n"); return 2; }
    int rounds = atoi(argv[1]);
    uint64_t exits = strtoull(argv[2], 0, 0);
    FILE *f = fopen(argv[3], "rb");
    if (!f) { perror(argv[3]); return 1; }
    int np = argc - 4;
    uint64_t **traces = calloc(np, sizeof *traces), *len = calloc(np, sizeof *len);
    for (int p = 0; p < np; p++) {
        if (fread(&len[p], 8, 1, f) != 1) return 1;
        traces[p] = malloc(len[p] * 8);
        if (fread(traces[p], 8, len[p], f) != len[p]) return 1;
    }
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    for (int v = 0; v < NV; v++)
        for (int s = 0; s < SITES; s++) {
            uint64_t *e = &table[v][(4096 + s * 32) / 8];
            e[0] = RIP_BASE + s; e[1] = (uint64_t)hdm_blocks[v * SITES + s]; e[2] = ~0ULL; e[3] = 0;
        }
    mach_timebase_info_data_t tb; mach_timebase_info(&tb);
    for (int r = 0; r < rounds; r++)
        for (int p = 0; p < np; p++)
            for (int i = 0; i < NV; i++) {
                int v = r %% 2 ? NV - 1 - i : i;
                uint64_t first = traces[p][len[p] - 1] - RIP_BASE;   /* the block whose successor is trace[0] */
                void *entry = hdm_blocks[v * SITES + first];
                hdm_run(entry, traces[p], len[p] - 1, exits / 8, table[v]);
                uint64_t t0 = mach_absolute_time();
                hdm_run(entry, traces[p], len[p] - 1, exits, table[v]);
                uint64_t t1 = mach_absolute_time();
                double ns = (double)(t1 - t0) * tb.numer / tb.denom / (double)exits;
                printf("%%d %%s %%d %%.4f\n", r, argv[4 + p], v, ns);
                fflush(stdout);
            }
    return 0;
}
'''


def build(d: pathlib.Path, t):
    (d / 'hdm.s').write_text(assembly(t))
    (d / 'driver.c').write_text(DRIVER % {'sites': SITES, 'nv': len(VARIANTS), 'rip': RIP_BASE})
    exe = d / 'hdm'
    subprocess.run(['cc', '-O2', '-o', str(exe), str(d / 'driver.c'), str(d / 'hdm.s')], check=True)
    return exe


def spread(xs):
    return max(xs) - min(xs)


def verdict(med, spr):
    """The plan's rules over the medians (ns per exit) and spreads: dict of the deltas and the decision."""
    def save(base, fix, pat):
        return med[pat][base] - med[pat][fix]
    s063, s075 = save('V0', 'S1J', 'p0.63-long'), save('V0', 'S1J', 'p0.75-long')
    loss1 = max(-save('V0', 'S1J', 'p1.00-long'), -save('V0', 'S1J', 'p1.00-short'))
    c063, c075 = save('V0', 'S1C', 'p0.63-long'), save('V0', 'S1C', 'p0.75-long')
    closs1 = max(-save('V0', 'S1C', 'p1.00-long'), -save('V0', 'S1C', 'p1.00-short'))
    s2 = save('S1J', 'S2J', 'p0.75-long')
    s2c = save('S1C', 'S2C', 'p0.75-long')
    kill = s063 < KILL_SAVE_NS or s075 < KILL_SAVE_NS or loss1 > KILL_LOSS_NS
    return {'step1_save_063_long': s063, 'step1_save_075_long': s075, 'step1_loss_100': loss1,
            'step1_call_save_063_long': c063, 'step1_call_save_075_long': c075, 'step1_call_loss_100': closs1,
            'step2_save_over_step1_075_long': s2, 'step2_call_save_over_step1_075_long': s2c,
            'step1': 'kill' if kill else 'build', 'step2': 'consider' if s2 >= STEP2_SAVE_NS else 'drop'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--exits', type=int, default=1 << 24)
    a = ap.parse_args()
    d = pathlib.Path(a.out); d.mkdir(parents=True, exist_ok=True)
    t = successors()
    pats = patterns(t)
    with open(d / 'traces.bin', 'wb') as f:
        for _, sites in pats:
            f.write(struct.pack('<Q', len(sites)))
            f.write(struct.pack(f'<{len(sites)}Q', *(RIP_BASE + s for s in sites)))
    exe = build(d, t)
    names = [n for n, _ in pats]
    print(' '.join([str(exe), str(a.rounds), str(a.exits), 'traces.bin', *names]), flush=True)
    r = subprocess.run([str(exe), str(a.rounds), str(a.exits), str(d / 'traces.bin'), *names], capture_output=True, text=True)
    (d / 'raw.txt').write_text(r.stdout)
    if r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        return 1
    per = {n: {v: [] for v in VARIANTS} for n in names}
    for line in r.stdout.split('\n'):
        if line.strip():
            _, n, v, ns = line.split()
            per[n][VARIANTS[int(v)]].append(float(ns))
    med = {n: {v: statistics.median(xs) for v, xs in vs.items()} for n, vs in per.items()}
    spr = {n: {v: spread(xs) for v, xs in vs.items()} for n, vs in per.items()}
    ver = verdict(med, spr)
    chip = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True).stdout.strip()
    res = {'case': 'host-dispatch-micro', 'machine': chip or platform.machine(), 'rounds': a.rounds, 'exits': a.exits,
           'ns_per_exit': per, 'median': med, 'spread': spr, 'verdict': ver}
    (d / 'result.json').write_text(json.dumps(res, indent=1))
    rows = [f'# host-dispatch-micro on {res["machine"]} (host-native, mechanism only; not a guest number)', '',
            f'{a.rounds} rounds of {a.exits} exits per variant and pattern; ns per exit, median over rounds (spread).', '',
            '| Pattern | ' + ' | '.join(VARIANTS) + ' |', '| --- |' + ' --- |' * len(VARIANTS)]
    for n in names:
        rows.append(f'| {n} | ' + ' | '.join(f'{med[n][v]:.2f} ({spr[n][v]:.2f})' for v in VARIANTS) + ' |')
    rows += ['', '| Rule | ns |', '| --- | --- |']
    rows += [f'| {k} | {v:+.2f} |' if isinstance(v, float) else f'| {k} | {v} |' for k, v in ver.items()]
    (d / 'summary.md').write_text('\n'.join(rows) + '\n')
    print('\n'.join(rows))
    return 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
