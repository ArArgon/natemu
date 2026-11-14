#!/usr/bin/env python3
"""Page-currency plan, chunk 0 (docs/restructure/page-currency-plan.md section 8): sums the per-vCPU `page_study` counters of a
profile run and prints the chunk-0 gate model ("chunk 0 decides").

    python3 tools/page_study_an.py PROFILE_DIR [STEP ...]        (default steps: configure make)

PROFILE_DIR is a `tools/run_io5.py profile` output with a `page-study` resident (`--runtime`; the host needs no feature: the
record lies in the lookup area). Reads PROFILE_DIR/perf-STEP.log: its `vcpu {...}` lines carry k1.page_study
(`natemu_abi::page_study::Counts`, per caller [revalidation, placement]) and resident.buckets. The counts are exact; the times are
CNTVCT (24 MHz) sums around each fetch walk and each probe, so a mean over many events is unbiased but one event is not, and the
study build's own loads and stores cost time (decision 16): the busy time and every bucket of this run are for comparison with
the baseline's counts, never judged as a performance result.

The model (plan sections 5, 6, 8): the record would save the walks it would have answered (their measured total time: a
revalidation's whole walk; a placement's walk only, `mark` stays in the fetch bucket) and costs one probe per fetch walk. Gate:
modelled net saving >= the plan's 1.5 s of vCPU time on configure (planning range 1.3-1.9) or 3.5 vCPU-s on make, and disagreements
zero in single-vCPU runs / at most 10 per million hits at four vCPUs."""
import json
import pathlib
import sys

CALLERS = ('revalidation', 'placement')
CLASSES = ('global', 'large', 'user')
BLOCKS = ('all', 'global', 'large', 'user', 'crossing')
GATE = {'configure': (1.5, 1.3, 1.9), 'make': (3.5, None, None)}   # (gate, planning low, planning high) in vCPU-s
PROBE_NS = 15          # the plan's cost of one probe (hash, 32-byte load, compare)
STORE_NS = 5           # and of the store that records a walk the record missed (an estimate, below one cache miss)
DISAGREE_PER_MILLION = 10


def add(a, b):
    return [add(x, y) for x, y in zip(a, b)] if isinstance(a, list) else a + b


def load(path):
    """(vcpus, summed page_study counters, summed bucket (ns, events), busy ns, the number of vCPUs without the counters)."""
    total, buckets, busy, missing, vcpus = {}, {}, 0, 0, 0
    for line in pathlib.Path(path).read_text(errors='replace').splitlines():
        if not line.startswith('vcpu {'):
            continue
        v = json.loads(line[5:])
        vcpus += 1
        p = v['k1'].get('page_study')
        if p is None:
            missing += 1
            continue
        for k, x in p.items():
            total[k] = (max(total[k], x) if k == 'live_max' else add(total[k], x)) if k in total else x
        for b, (ns, n) in v['resident']['buckets'].items():
            old = buckets.get(b, [0, 0])
            buckets[b] = [old[0] + ns, old[1] + n]
        busy += sum(ns for b, (ns, n) in v['resident']['buckets'].items() if b not in ('halted', 'mailbox', 'start'))
    return vcpus, total, buckets, busy, missing


def pct(a, b):
    return f'{100 * a / b:5.1f}%' if b else '  n/a '


def report(directory, step):
    path = directory / f'perf-{step}.log'
    vcpus, c, buckets, busy, missing = load(path)
    print(f'===== {step}: {vcpus} vCPUs ({path})')
    if missing or not c:
        print(f'  {missing} vCPUs have no page_study counters (not a page-study resident): nothing to report')
        return
    s = 1e9
    print(f'  busy {busy / s:.1f} vCPU-s; buckets revalidate {buckets["revalidate"][0] / s:.2f} s / {buckets["revalidate"][1]:,} events, '
          f'fetch {buckets["fetch"][0] / s:.2f} s / {buckets["fetch"][1]:,}')
    hits = [c['hit_4k'][k] + c['hit_2m'][k] for k in (0, 1)]
    for k, who in enumerate(CALLERS):
        n = c['calls'][k]
        print(f'  {who}: {n:,} fetch walks; the record would have answered {hits[k]:,} ({pct(hits[k], n)}; 4 KiB {c["hit_4k"][k]:,}, '
              f'2 MiB {c["hit_2m"][k]:,}); walks that faulted {c["faulted"][k]:,} (not RAM {c["not_ram"][k]:,}); disagreements {c["disagree"][k]:,}')
        classes = ', '.join(f'{name} {c["class_hits"][k][j]:,}/{c["class_calls"][k][j]:,} ({pct(c["class_hits"][k][j], c["class_calls"][k][j])})'
                            for j, name in enumerate(CLASSES))
        print(f'      by class (would-hit / walks): {classes}')
    print('  revalidated blocks (all pages answered by the record: no walk needed): '
          + ', '.join(f'{name} {c["blocks_hit"][j]:,}/{c["blocks"][j]:,} ({pct(c["blocks_hit"][j], c["blocks"][j])})' for j, name in enumerate(BLOCKS)))
    print(f'  records {c["records"]:,}; live records evicted {c["evictions"]:,}; most records made in one epoch {c["live_max"]:,} (per vCPU, max; table 8,192); '
          f'2 MiB leaves not recorded (split) {c["split_2m"]:,}; INVLPG/#PF clears {c["clear_events"]:,} ({c["cleared"]:,} records)')
    walk_ns, walk_n = c['walk_ns'], c['walk_count']
    mean = lambda k, h: walk_ns[k][h] / walk_n[k][h] if walk_n[k][h] else 0.0
    for k, who in enumerate(CALLERS):
        print(f'  {who} walk time: would-miss {walk_n[k][0]:,} walks {walk_ns[k][0] / s:.2f} s (mean {mean(k, 0):.0f} ns); '
              f'would-hit {walk_n[k][1]:,} walks {walk_ns[k][1] / s:.2f} s (mean {mean(k, 1):.0f} ns)')
    fetch_walk = walk_ns[1][0] + walk_ns[1][1]
    fetch = buckets['fetch'][0]
    print(f'  fetch bucket {fetch / s:.2f} s: walks {fetch_walk / s:.2f} s, the rest (mark, bucket switches) {(fetch - fetch_walk) / s:.2f} s; '
          f'revalidate bucket {buckets["revalidate"][0] / s:.2f} s: walks {(walk_ns[0][0] + walk_ns[0][1]) / s:.2f} s; '
          f'probes measured {c["probe_ns"] / s:.2f} s over {sum(c["calls"]):,} ({c["probe_ns"] / max(sum(c["calls"]), 1):.0f} ns each); '
          f'two counter reads {c["tick_pair_milli_ns"] / 1000:.2f} ns')

    saved = [walk_ns[0][1], walk_ns[1][1]]
    probes = sum(c['calls']) * PROBE_NS
    stores = sum(walk_n[k][0] for k in (0, 1)) * STORE_NS
    net = sum(saved) - probes - stores
    print(f'  model: walks saved {saved[0] / s:.2f} s (revalidation) + {saved[1] / s:.2f} s (placement, walk part only) '
          f'- probes {sum(c["calls"]):,} x {PROBE_NS} ns = {probes / s:.2f} s - stores {STORE_NS} ns x {sum(walk_n[k][0] for k in (0, 1)):,} = {stores / s:.2f} s '
          f'= net {net / s:.2f} vCPU-s ({pct(net, busy)} of busy)')
    gate, low, high = GATE.get(step, (None, None, None))
    if gate is not None:
        plan = f' (planning range {low}-{high} s)' if low else ''
        print(f'  gate: net >= {gate} vCPU-s{plan}: {"PASS" if net / s >= gate else "FAIL"}')
    total_hits, dis = sum(hits), sum(c['disagree'])
    rate = 1e6 * dis / total_hits if total_hits else 0.0
    print(f'  disagreements {dis:,} of {total_hits:,} would-be hits = {rate:.2f} per million (frame {c["disagree_frame"]:,}, flags {c["disagree_flags"]:,}, '
          f'walk faulted {c["disagree_fault"]:,}); gate: 0 at one vCPU, at most {DISAGREE_PER_MILLION} per million at four: '
          f'{"PASS" if (vcpus > 1 and rate <= DISAGREE_PER_MILLION) or dis == 0 else "FAIL (trace the address before judging, plan 3.5)"}')
    print('  if the gate fails the record is not dropped: it folds into the fast miss path as its revalidation source (plan, sequencing).')


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    directory, steps = pathlib.Path(a[0]), a[1:] or ['configure', 'make']
    for step in steps:
        report(directory, step)


if __name__ == '__main__':
    main()
