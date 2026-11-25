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
