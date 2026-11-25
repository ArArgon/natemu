"""run_web_tooling's console parsing and verdict, and guest_userland's appended cpio and timing-log summary."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import guest_userland as gu
import run_web_tooling as w


def console(tools=w.TOOLS, gm='2.35', rc=0, cpus=4, end=True):
    lines = [f'WTB-BEGIN cpus={cpus} node=v20.19.2 MemTotal: 4000212 kB', 'WTB Running Web Tooling Benchmark v0.5.3…',
             'WTB -------------------------------------']
    lines += [f'WTB {t:>14}: {1 + i / 10:5.2f} runs/s' for i, t in enumerate(tools)]
    lines += ['WTB -------------------------------------']
    if gm: lines.append(f'WTB Geometric mean: {gm:>5} runs/s')
    lines += ['WTB WTB-MAXRSS 696 MiB', f'WTB rc={rc}']
    if end: lines.append('WTB-END uptime=438.26')
    return '\n'.join(lines) + '\n'


class Parse(unittest.TestCase):
    def test_every_tool_the_geometric_mean_and_the_guest(self):
        r = w.parse(console())
        self.assertEqual(list(r['tools']), w.TOOLS)
        self.assertEqual((r['tools']['acorn'], r['tools']['uglify-js'], r['geomean']), (1.0, 2.7, 2.35))
        self.assertEqual((r['max_rss_mib'], r['rc'], r['cpus'], r['node'], r['uptime']), (696, 0, 4, 'v20.19.2', 438.26))
        self.assertEqual(w.problems(r, 4), [])

    def test_a_boot_that_did_not_finish_does_not_count(self):
        self.assertEqual(w.problems(w.parse(console(tools=w.TOOLS[:17], gm=None, end=False)), 4),
                         ['no WTB-END line', 'tools missing: uglify-js', 'no geometric mean'])
        self.assertEqual(w.problems(w.parse(console(rc=1)), 4), ['node exit status 1'])
        self.assertEqual(w.problems(w.parse(console(cpus=1)), 4), ['guest saw 1 CPUs, not 4'])
        self.assertEqual(w.problems(w.parse(console(tools=w.TOOLS + ['jquery'])), 4), ['unknown tools: jquery'])


def vcpu(i, cpu, shadow, abort, halted, resets, cr3, tlb):
    return 'vcpu ' + json.dumps({'vcpu': i, 'cpu_ns': cpu, 'host': {'translate': [1000 * (i + 1), 2], 'run': [5, 1]},
                                 'resident': {'buckets': {'guest': [cpu - shadow - abort - tlb, 10], 'shadow': [shadow, 4],
                                                          'abort': [abort, 3], 'tlb': [tlb, cr3], 'halted': [halted, 1]}},
                                 'k1': {'cr3_loads': cr3, 'global_flushes': 0, 'invlpgs': 7, 'page_faults': 9},
                                 'state': {'evictions': 5, 'shadow_resets': resets}})


class PerfSummary(unittest.TestCase):
    def test_vcpu_lines_summed_and_buckets_as_shares_of_cpu_ns(self):
        log = '\n'.join(['mark main 1', 'snap 2 0 0', vcpu(0, 1000, 250, 230, 9000, 100, 4, 20),
                         vcpu(1, 3000, 750, 690, 1000, 300, 6, 60), 'pool {"published":1}', 'mark destroyed 3']) + '\n'
        s = gu.perf_summary(log)
        self.assertEqual((s['vcpus'], s['cpu_ns'], s['halted_ns']), (2, 4000, 10000))
        self.assertEqual(list(s['buckets']), ['guest', 'shadow', 'abort', 'tlb'])           # largest first, no halted
        self.assertEqual((s['buckets']['shadow']['ns'], s['buckets']['shadow']['share'], s['buckets']['abort']['share']), (1000, 0.25, 0.23))
        self.assertEqual(s['translate'], {'ns': 3000, 'count': 4})
        self.assertEqual({k: s['counters'][k] for k in ('state.shadow_resets', 'state.evictions', 'k1.page_faults', 'k1.cr3_loads', 'k1.invlpgs')},
                         {'state.shadow_resets': 400, 'state.evictions': 10, 'k1.page_faults': 18, 'k1.cr3_loads': 10, 'k1.invlpgs': 14})
        self.assertEqual(s['cr3'], {'loads': 10, 'tlb_ns': 80, 'tlb_share': 0.02, 'ns_per_flush': 8})
        text = '\n'.join(gu.perf_lines(s))
        self.assertIn('shadow        25.0%', text)
        self.assertIn('CR3 loads 10', text)

    def test_a_log_without_vcpu_lines_has_no_summary(self):
        self.assertIsNone(gu.perf_summary('mark main 1\nsnap 2 0\n'))
        self.assertIn('no vcpu lines', gu.perf_lines(None)[0])


class Tail(unittest.TestCase):
    def test_the_appended_cpio_holds_the_init_and_the_preload(self):
        t = w.tail()
        self.assertEqual(len(t) % 4, 0)
        for name in (b'dev/console', b'wtb-init', b'wtb/rss.js', b'TRAILER!!!'): self.assertIn(name + b'\0', t)
        self.assertIn(b'node -r /wtb/rss.js /wtb/cli.js', t)


if __name__ == '__main__':
    unittest.main()
