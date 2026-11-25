"""run_speedometer's console parsing, its verdict on a boot, the summary over boots and the appended cpio."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run_speedometer as sp

UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0'


def console(score=2.28, iters=5, suites=sp.SUITES, done=True, error=None, cpus=4, hook=True, exited=False, end=True, asked=5):
    lines = ([] if hook else ['SP-NO-HOOK']) + [f'SP-BEGIN cpus={cpus} iter={asked} MemTotal: 4000212 kB', 'SP /log {"loaded":906}',
                                                 'SP /log {"suite":"TodoMVC-JavaScript-ES5","step":0,"of":290,"t":4176}',
                                                 'SP /log {"error":"x is undefined","at":"a.js:1"}']
    if exited: lines.append('SP-FF-EXITED')
    if done:
        body = {'error': error} if error else {
            'score': score, 'scoreText': f'{score:.2f} ± 0.16 (6.9%)', 'perIteration': [score] * iters, 'geomeanMs': 1000 / score / 0.99,
            'suites': {n: {'mean': 100.0 + i, 'values': [100 + i] * iters} for i, n in enumerate(suites)}, 'ua': UA, 'wallMs': 120000}
        lines.append('SP /done ' + json.dumps(body))
    if end: lines += ['SP-END uptime=140.5', 'SP-FF ** (firefox-esr:167): WARNING **: no UPower']
    return '\n'.join(lines) + '\n'


class Parse(unittest.TestCase):
    def test_the_score_every_iteration_and_every_suite(self):
        r = sp.parse(console())
        self.assertEqual((r['score'], r['score_text'], r['per_iteration'], r['cpus'], r['iterations']), (2.28, '2.28 ± 0.16 (6.9%)', [2.28] * 5, 4, 5))
        self.assertEqual(list(r['suites']), sp.SUITES)
        self.assertEqual((r['suites']['TodoMVC-JavaScript-ES5'], r['suites']['Perf-Dashboard']), (100.0, 119.0))
        self.assertEqual(r['page_errors'], [{'error': 'x is undefined', 'at': 'a.js:1'}])
        self.assertEqual(sp.problems(r, 5, 4), [])

    def test_a_boot_without_results_or_with_other_ones_does_not_count(self):
        self.assertEqual(sp.problems(sp.parse(console(done=False, exited=True)), 5, 4), ['Firefox exited before the results', 'no results (no /done)'])
        self.assertEqual(sp.problems(sp.parse(console(error='TypeError: boom')), 5, 4), ['Speedometer failed: TypeError: boom'])
        self.assertEqual(sp.problems(sp.parse(console(iters=4)), 5, 4), ['4 iterations measured, not 5'])
        self.assertEqual(sp.problems(sp.parse(console(suites=sp.SUITES[1:] + ['Other'])), 5, 4),
                         ['suites missing: TodoMVC-JavaScript-ES5', 'unknown suites: Other'])
        self.assertEqual(sp.problems(sp.parse(console(hook=False, end=False, cpus=2)), 5, 4),
                         ["the hook is not in Speedometer's index.html", 'no SP-END line', 'guest saw 2 CPUs, not 4'])
        self.assertEqual(sp.problems(sp.parse(console(asked=10, iters=10)), 5, 4),
                         ['the guest asked for 10 iterations, not 5', '10 iterations measured, not 5'])


class Summary(unittest.TestCase):
    def test_median_and_spread_of_the_scores_and_each_suites_median(self):
        boots = [sp.parse(console(score=s)) for s in (2.2, 2.4, 2.3)]
        s = sp.summary(boots)
        self.assertEqual((s['score']['median'], s['score']['min'], s['score']['max']), (2.3, 2.2, 2.4))
        self.assertEqual(s['suites']['Perf-Dashboard']['median'], 119.0)
        self.assertEqual(len(s['score_text']), 3)


def args(out, **kw):
    import argparse
    a = dict(emu='natemu', name='sp', kernel=out / 'k', inputs=out / 'in', out=out / 'o', boots=3, iterations=5, vcpus=4, ram=4,
             cap=900, fexc=out / 'fexc', natemu=out / 'natemu', runtime=out / 'rt', perf_log=False, compile_cache='off',
             compile_cache_mb=8192, natemu_set=[], qemu=None, sample_us=None)
    a.update(kw)
    return argparse.Namespace(**a)


class CompileCache(unittest.TestCase):
    def test_natemu_command_keeps_off_by_default_and_takes_a_folder_and_cap(self):
        a = args(pathlib.Path('/x'))
        off = sp.gu.natemu_command(a, 'initrd', 'cmd', 'serial')
        self.assertEqual(off[off.index('--compile-cache') + 1], 'off')
        self.assertNotIn('--compile-cache-mb', off)
        self.assertNotIn('--compile-cache-mb', sp.gu.natemu_command(a, 'initrd', 'cmd', 'serial', cache_mb=64))   # no cap for off
        on = sp.gu.natemu_command(a, 'initrd', 'cmd', 'serial', pathlib.Path('/x/cc'), cache='/x/cache', cache_mb=8192)
        self.assertEqual(on[on.index('--compile-cache') + 1:on.index('--compile-cache') + 4], ['/x/cache', '--compile-cache-mb', '8192'])
        self.assertEqual(on[on.index('--perf-log') + 1], pathlib.Path('/x/cc'))

    def run_with(self, compile_cache, perf_log=False, fill_ok=True, boots=3, sample_us=None):
        """run() with the boots faked: each writes a pack into the cache folder it was given and a console with a score."""
        import tempfile
        from unittest import mock
        tmp = pathlib.Path(tempfile.mkdtemp())
        for f in ('k', 'natemu', 'rt'): (tmp / f).write_bytes(b'x')
        a = args(tmp, compile_cache=compile_cache, perf_log=perf_log, boots=boots, sample_us=sample_us)
        seen, self.cmds = [], []

        def boot(out, i, cmd, cap, stamper=None):
            self.cmds.append([str(c) for c in cmd])
            cache = cmd[cmd.index('--compile-cache') + 1]
            seen.append((i, str(cache), '--perf-log' in cmd))
            if cache != 'off':                                   # natemu makes the folder, then writes packs into it
                pathlib.Path(cache).mkdir(parents=True, exist_ok=True); (pathlib.Path(cache) / f'{i}.pack').write_bytes(b'p')
            text = console(score=1.0 + i) if fill_ok or i else console(done=False)
            if stamper:                                          # the console as it arrives: a partial line waits
                k = 'SP-KTEXT ffffffff81000000\n'
                stamper.feed(k + 'SP-BEGIN cpus=4 iter=5', 1); stamper.feed(k + text, 2); stamper.close()
            return {'text': text, 'seconds': 1.0, 'capped': False, 'exit': 0, 'contended': [], 'command': [str(c) for c in cmd]}

        def initrd(inputs, dest, tail):
            dest.write_bytes(b'i'); return {}
        perf = 'vcpu ' + json.dumps({'vcpu': 0, 'cpu_ns': 10, 'resident': {'buckets': {'guest': [10, 1]}}})
        real_boot = boot
        def boot_perf(out, i, cmd, cap, stamper=None):
            if '--perf-log' in cmd: pathlib.Path(cmd[cmd.index('--perf-log') + 1]).write_text(perf + '\n')
            return real_boot(out, i, cmd, cap, stamper)
        with mock.patch.object(sp.gu, 'boot_local', boot_perf), mock.patch.object(sp.gu, 'make_initrd', initrd), \
                mock.patch('builtins.print'):
            rc = sp.run(a)
        return rc, seen, json.loads((a.out / 'result.json').read_text()), a.out

    def test_cold_boots_with_the_cache_off(self):
        rc, seen, r, _ = self.run_with('off')
        self.assertEqual((rc, seen), (0, [(1, 'off', False), (2, 'off', False), (3, 'off', False)]))
        self.assertEqual((r['score']['all'], r['compile_cache']), ([2.0, 3.0, 4.0], 'off'))
        self.assertNotIn('fill', r)

    def test_warm_fills_the_cache_untimed_then_the_timed_boots_reuse_it_and_it_is_removed(self):
        rc, seen, r, out = self.run_with('warm')
        cache = str(out / 'compile-cache')
        self.assertEqual((rc, seen), (0, [(0, cache, False), (1, cache, False), (2, cache, False), (3, cache, False)]))
        self.assertEqual(r['score']['all'], [2.0, 3.0, 4.0])                 # the fill (score 1.0) is not in the summary
        self.assertEqual((r['fill']['score'], r['fill']['compile_cache']), (1.0, {'packs': 1, 'bytes': 1}))
        self.assertFalse((out / 'compile-cache').exists())
        lines = [json.loads(l) for l in (out / 'bench.jsonl').read_text().splitlines()]
        self.assertEqual([(l['boot'], l['fill'], l['compile_cache']['packs']) for l in lines], [(0, True, 1), (1, False, 2), (2, False, 3), (3, False, 4)])

    def test_the_warm_profile_logs_only_its_timed_boot(self):
        rc, seen, r, _ = self.run_with('warm', perf_log=True, boots=1)
        self.assertEqual([(i, p) for i, _, p in seen], [(0, False), (1, True)])
        self.assertEqual((rc, len(r['perf']), r['perf'][0]['cpu_ns']), (0, 1, 10))

    def test_a_sampled_boot_gets_the_sampler_its_markers_and_the_elf_image(self):
        from unittest import mock
        made = []
        with mock.patch.object(sp.gu, 'elf_image', lambda src, dest: made.append((src.name, dest.name))):
            rc, seen, r, out = self.run_with('off', perf_log=True, boots=1, sample_us=250)
        self.assertEqual((rc, made), (0, [('userland.cpio.zst', 'userland.cpio')]))
        cmd = self.cmds[0]
        self.assertEqual(cmd[cmd.index('--profile'):cmd.index('--profile') + 4], ['--profile', str(out / 'sp'), '--profile-us', '250'])
        self.assertIn('--perf-log', cmd)
        marks = [l.split('\t')[1] for l in (out / 'sp.markers.tsv').read_text().splitlines()]
        self.assertEqual(marks, ['NATEMU-BENCH-KTEXT ffffffff81000000', 'NATEMU-BENCH-START', 'NATEMU-BENCH-END'])

    def test_a_failed_fill_fails_the_case_and_nothing_is_timed(self):
        rc, seen, r, _ = self.run_with('warm', fill_ok=False)
        self.assertEqual((rc, [i for i, *_ in seen], r['pass'], r['fill']['problems']), (1, [0], False, ['no results (no /done)']))


class Tail(unittest.TestCase):
    def test_the_appended_cpio_holds_the_init_hook_server_profile_and_policies(self):
        t = sp.tail()
        self.assertEqual(len(t) % 4, 0)
        for name in (b'sp-init', b'sp/hook.js', b'sp-harness/serve.py', b'sp-harness/user.js',
                     b'usr/lib/firefox-esr/distribution', b'usr/lib/firefox-esr/distribution/policies.json'):
            self.assertIn(name + b'\0', t)
        self.assertIn(b'iterationCount=$iter', t)
        self.assertIn(b'didFinishLastIteration', t)        # hook.js itself


if __name__ == '__main__':
    unittest.main()
