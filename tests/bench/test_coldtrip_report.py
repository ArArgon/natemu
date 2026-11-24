"""coldtrip_report on synthetic boots: a model of the program's phases (guest-bucket time per iteration that rises by a
known gate cost per trip, plus a known cold-state cost in the large footprint) turned into serial lines and 5 ms bucket
snapshots, and the same with each thing the judge checks broken."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import copy, json, shutil, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import coldtrip_report as C

NB = len(C.BUCKETS)
B = {n: i for i, n in enumerate(C.BUCKETS)}
OFFSET = 7_000_000_000           # host clock less the guest's TSC
GATE, COLD, SYSTEM, CTRL = 300.0, 500.0, 2500.0, 0.5   # ns per trip: guest gate, extra guest cold state, resident; control
WORK = {'small': 20.0, 'large': 15000.0}                # ns per iteration without trips
ITERS = {'small': 400000, 'large': 16000}
KS = {'small': (0, 64, 16, 4, 2, 1), 'large': (0, 16, 4, 2, 1)}
ROUNDS = 3


def program(run):
    """The program's phases, as coldtrip.c orders them."""
    out = []
    for rnd in range(ROUNDS + 1):
        for fp in ('small', 'large'):
            for k in KS[fp]:
                for var in ('trip', 'ctrl'):
                    out.append(dict(run=run, round=rnd, fp=fp, variant=var, k=k, iters=ITERS[fp], trips=ITERS[fp] // k if k else 0))
    for i, p in enumerate(out): p['phase'] = i
    return out


def boot(seed=1):
    """(serial text, perf log text, record): an init stretch, then two program runs with 100 ms halted gaps."""
    spans = []                    # (duration ns, {bucket: share})
    idle = {'halted': 0.998, 'guest': 0.001, 'irq': 0.001}
    spans.append((800e6, {'guest': 0.7, 'host': 0.3}))            # boot and init
    lines, t, trips = [], 800e6, 0
    for run in (1, 2):
        ph = program(run)
        lines.append(f'NATEMU-COLDTRIP begin run={run} phases={len(ph)} rounds={ROUNDS} gap_ms=100 funcs=512 steps=16 code_bytes=262144 vendor=GenuineIntel')
        for p in ph:
            if p['phase'] == 3:                                      # the host busy for 2 ms in a gap: no phase's
                spans += [(40e6, idle), (2e6, {'host': 0.7, 'halted': 0.3}), (58e6, idle)]
            elif p['phase'] == 9:                                    # and for 15 ms just before a phase: one stretch
                spans += [(85e6, idle), (15e6, {'host': 1.0})]
            else:
                spans.append((100e6, idle))
            t += 100e6
            x = p['trips'] / p['iters']
            per = GATE + (COLD if p['fp'] == 'large' else 0)
            if p['variant'] == 'trip':
                guest = p['iters'] * (WORK[p['fp']] + x * per); system = p['iters'] * x * SYSTEM; trips += p['trips']
            else:
                guest = p['iters'] * (WORK[p['fp']] + x * CTRL); system = 0.0
            dur = guest + system
            spans.append((dur, {'guest': guest / dur, 'system': system / dur}))
            spans.append((0.6e6, {'host': 0.8, 'guest': 0.2}))       # its line on the console, the sleep's entry
            p['tsc0'] = int(t); p['tsc_ns'] = int(dur); t += dur + 0.6e6
            lines.append(f'NATEMU-COLDTRIP phase={p["phase"]} run={run} round={p["round"]} fp={p["fp"]} variant={p["variant"]} k={p["k"]} '
                         f'iters={p["iters"]} trips={p["trips"]} tsc0={p["tsc0"]} tsc_ns={p["tsc_ns"]} sum={"aa" if p["fp"] == "small" else "bb"}')
        spans.append((100e6, idle)); t += 100e6
        lines += [f'NATEMU-COLDTRIP end run={run} ok=1', 'NATEMU-COLDTRIP sums small=aa large=bb']
        spans.append((300e6, {'guest': 0.5, 'host': 0.5})); t += 300e6     # init between the runs, poweroff
    # 5 ms snapshots (with a little jitter) of the cumulative buckets
    snaps, cum, now, i, s_t = [], [0.0] * NB, 0.0, 0, 0.0
    edges = []
    for d, share in spans:
        edges.append((now, now + d, share)); now += d
    tnext, j, rng = 0.0, 0, seed
    while tnext < now:
        while j < len(edges) and edges[j][1] <= tnext:
            a, b, share = edges[j]; lo = max(a, s_t)
            for n, v in share.items(): cum[B[n]] += v * (b - lo)
            s_t = b; j += 1
        partial = list(cum)
        if j < len(edges):
            a, b, share = edges[j]; lo = max(a, s_t)
            for n, v in share.items(): partial[B[n]] += v * (tnext - lo)
        snaps.append(f'snap {int(OFFSET + tnext)} ' + ' '.join(str(int(v)) for v in partial))
        rng = (rng * 1103515245 + 12345) % 2 ** 31
        tnext += 5.0e6 + (rng % 200) * 1e3
    record = dict(verified=True, system_counts=dict(stack=0, kinds={'7': trips + 5000}))
    return '\n'.join(lines) + '\n', 'mark run 1\n' + '\n'.join(snaps) + '\n', record


class Synthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serial, cls.perf, cls.record = boot()

    def outcome(self, serial=None, perf=None, record=None):
        runs = C.parse_serial(serial or self.serial)
        ts, vals, n = C.parse_snaps(perf or self.perf)
        rows, placing, _ = C.measure(runs, ts, vals)
        return C.judge(runs, self.record if record is None else record) + placing, rows

    def test_the_costs_come_back(self):
        problems, rows = self.outcome()
        self.assertEqual(problems, [])
        a = C.analyse(rows)
        self.assertEqual(a['gate_ns']['n'], 2 * ROUNDS)
        self.assertAlmostEqual(a['gate_ns']['median'], GATE - CTRL, delta=3)
        self.assertAlmostEqual(a['cold_ns']['median'], COLD, delta=5)
        self.assertAlmostEqual(a['trip_small_ns']['median'], GATE + SYSTEM - CTRL, delta=5)
        self.assertLess(a['gate_ns']['max'] - a['gate_ns']['min'], 5)
        self.assertAlmostEqual(a['pooled']['small_trip']['slope'], GATE, delta=3)

    def test_the_warm_up_round_is_left_out(self):
        _, rows = self.outcome()
        self.assertEqual({(r['run'], r['round']) for r in C.analyse(rows)['rounds']}, {(r, n) for r in (1, 2) for n in range(1, ROUNDS + 1)})

    def test_a_wrong_trip_count_fails(self):
        bad = self.serial.replace('k=64 iters=400000 trips=6250 ', 'k=64 iters=400000 trips=6249 ', 1)
        self.assertNotEqual(bad, self.serial)
        self.assertTrue(any('trips, K=64 gives 6250' in p for p in self.outcome(serial=bad)[0]))

    def test_a_different_sum_fails(self):
        lines = self.serial.splitlines()
        i = next(i for i, l in enumerate(lines) if 'phase=5 ' in l)
        lines[i] = lines[i].replace('sum=aa', 'sum=ab')
        self.assertTrue(any('sum ab, not aa' in p for p in self.outcome(serial='\n'.join(lines))[0]))

    def test_a_missing_phase_fails(self):
        lines = [l for l in self.serial.splitlines() if 'phase=7 run=1 ' not in l]
        problems = self.outcome(serial='\n'.join(lines))[0]
        self.assertTrue(any('phases, the program announced' in p for p in problems))

    def test_a_missing_run_fails(self):
        bad = '\n'.join(l for l in self.serial.splitlines() if 'run=2 ' not in l)
        self.assertIn('program runs [1], not [1, 2]', self.outcome(serial=bad)[0])

    def test_phases_out_of_order_fail(self):
        lines = self.serial.splitlines()
        i = next(i for i, l in enumerate(lines) if 'phase=5 run=1 ' in l)
        lines[i], lines[i + 1] = lines[i + 1], lines[i]
        self.assertIn('run 1: phases out of order', self.outcome(serial='\n'.join(lines))[0])
        self.assertNotIn('run 1: phases out of order', self.outcome()[0])

    def test_competing_placements_fail(self):
        # Phase 0 alone (a small k=0 phase) fits every small k=0 stretch at its own offset: many placements, none chosen.
        lines = self.serial.splitlines()
        one = '\n'.join(l for l in lines if 'NATEMU-COLDTRIP phase=' not in l or l.startswith('NATEMU-COLDTRIP phase=0 run=1 '))
        runs = {1: C.parse_serial(one)[1]}
        ts, vals, _ = C.parse_snaps(self.perf)
        where, tried = C.place_runs(runs, ts, C.stretches(ts, vals))
        self.assertEqual(where, {}); self.assertGreater(tried['placements'], 1)
        rows, problems, _ = C.measure(runs, ts, vals)
        self.assertEqual(rows, []); self.assertTrue(any(f'({tried["placements"]} placements found)' in p for p in problems), problems)
        where, tried = C.place_runs(C.parse_serial(self.serial), ts, C.stretches(ts, vals))   # all phases: one placement
        self.assertEqual(tried['placements'], 1); self.assertEqual(len(where), 2 * len(program(1)))

    def test_an_unfinished_run_fails(self):
        bad = self.serial.replace('NATEMU-COLDTRIP end run=2 ok=1', 'NATEMU-COLDTRIP end run=2 ok=0')
        self.assertIn('run 2: no end line with ok=1', self.outcome(serial=bad)[0])

    def test_phases_that_do_not_match_the_snapshots_fail(self):
        lines = self.serial.splitlines()
        i = next(i for i, l in enumerate(lines) if 'phase=3 run=2 ' in l)
        w = lines[i].split(); w = [f'tsc_ns={int(x[7:]) + 40_000_000}' if x.startswith('tsc_ns=') else x for x in w]; lines[i] = ' '.join(w)
        problems = self.outcome(serial='\n'.join(lines))[0]
        self.assertTrue(any('no one place' in p for p in problems), problems)
        lines = self.serial.splitlines()          # a start 30 ms off the others
        w = lines[i].split(); w = [f'tsc0={int(x[5:]) + 30_000_000}' if x.startswith('tsc0=') else x for x in w]; lines[i] = ' '.join(w)
        self.assertTrue(any('no one place' in p for p in self.outcome(serial='\n'.join(lines))[0]))

    def test_the_resident_cpuid_count_is_checked(self):
        for extra in (-1, C.CPUID_SLACK + 6000):
            r = copy.deepcopy(self.record); r['system_counts']['kinds']['7'] += extra - 5000
            self.assertTrue(any('CPUIDs' in p for p in self.outcome(record=r)[0]), extra)
        r = copy.deepcopy(self.record); r['verified'] = False
        self.assertTrue(any('not verified' in p for p in self.outcome(record=r)[0]))

    def test_report_writes_its_json_and_says_why_it_fails(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        (d / 'coldtrip.serial').write_text(self.serial); (d / 'coldtrip.perf').write_text(self.perf)
        (d / 'bench.jsonl').write_text(json.dumps(self.record) + '\n')
        self.assertEqual(C.report(d), 0)
        out = json.loads((d / 'coldtrip.report.json').read_text())
        self.assertEqual(out['problems'], []); self.assertAlmostEqual(out['cold_ns']['median'], COLD, delta=5)
        (d / 'coldtrip.perf').write_text('mark run 1\n')       # no snapshots: no `perf` resident
        self.assertEqual(C.report(d), 1)
        self.assertTrue(any('vCPUs in its snapshots' in p for p in json.loads((d / 'coldtrip.report.json').read_text())['problems']))

    def test_fit(self):
        f = C.fit([0, 1, 2, 3], [1, 3, 5, 7.0])
        self.assertAlmostEqual(f['slope'], 2); self.assertAlmostEqual(f['intercept'], 1); self.assertAlmostEqual(f['se'], 0)
        self.assertIsNone(C.fit([1, 1], [2, 3])['slope'])


if __name__ == '__main__':
    unittest.main()
