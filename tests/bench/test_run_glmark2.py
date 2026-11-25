"""run_glmark2's console parsing, its verdict on a boot, the summary and the appended cpio."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import shlex, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run_glmark2 as g

FPS = [383, 21, 197, 161, 7, 616, 1, 3, 2, 530]


def console(fps=FPS, score=191, caps='native', width='256 bits', validate=None, end=True, rc=0):
    lines = [f'GLMARK2-BEGIN cpus=4 caps={caps}',
             f'GLMARK2-INFO OpenGL renderer string: llvmpipe (LLVM 19.1.7, {width})']
    for (name, opts), v in zip(g.SCENES, validate or []):
        lines.append(f'GLMARK2-VALIDATE [{name}] {opts}: Validation: {v}')
    if validate: lines.append('GLMARK2-VALIDATE rc=0')
    lines.append('GLMARK2 ** Failed to set swap interval. Results may be bounded above by refresh rate.')
    for (name, opts), f in zip(g.SCENES, fps):
        lines.append(f'GLMARK2 [{name}] duration=5:{opts}: FPS: {f} FrameTime: {1000 / max(f, 1):.3f} ms')
    if score is not None: lines.append(f'GLMARK2                                   glmark2 Score: {score} ')
    lines.append(f'GLMARK2 rc={rc}')
    if end: lines.append('GLMARK2-END uptime=63.10')
    return '\n'.join(lines) + '\n'


class Parse(unittest.TestCase):
    def test_every_scene_its_fps_frame_time_and_the_score(self):
        r = g.parse(console())
        self.assertEqual([r['scenes'][n]['fps'] for n, _ in g.SCENES], FPS)
        self.assertEqual(r['scenes']['texture']['frame_ms'], 47.619)
        self.assertEqual((r['score'], r['cpus'], r['caps'], r['rc'], r['ended']), (191, 4, 'native', [0], True))
        self.assertEqual(r['renderer'], 'llvmpipe (LLVM 19.1.7, 256 bits)')
        self.assertEqual(g.problems(r, 'native', False, 4), [])

    def test_validation_is_read_and_a_failure_fails_the_boot(self):
        r = g.parse(console(validate=['Success'] * 9 + ['Failure']))
        self.assertEqual(r['validation']['conditionals'], 'Failure')
        self.assertEqual(g.problems(r, 'native', True, 4), ['validation failed: conditionals Failure'])
        ok = g.parse(console(validate=['Success', 'Unknown'] * 5))
        self.assertEqual(g.problems(ok, 'native', True, 4), [])
        self.assertEqual(g.problems(g.parse(console()), 'native', True, 4), ['validation ran no scene'])

    def test_a_boot_that_did_not_finish_or_ran_other_code_does_not_count(self):
        self.assertIn('scenes missing: conditionals', g.problems(g.parse(console(fps=FPS[:9], score=None, end=False)), 'native', False, 4))
        self.assertIn('no GLMARK2-END line', g.problems(g.parse(console(end=False)), 'native', False, 4))
        p = g.problems(g.parse(console(caps='native', width='256 bits')), 'sse4.1', False, 4)
        self.assertEqual(p, ['guest caps native, not sse4.1', "renderer 'llvmpipe (LLVM 19.1.7, 256 bits)': not llvmpipe at 128 bits"])
        self.assertEqual(g.problems(g.parse(console(caps='sse4.1', width='128 bits')), 'sse4.1', False, 4), [])
        self.assertEqual(g.problems(g.parse(console(rc=1)), 'native', False, 4), ['glmark2 exit status [1]'])
        self.assertEqual(g.problems(g.parse(console()), 'native', False, 2), ['guest saw 4 CPUs, not 2'])


class Summary(unittest.TestCase):
    def test_median_and_spread_per_scene_and_of_the_score(self):
        boots = [g.parse(console(fps=[f + d for f in FPS], score=191 + d)) for d in (0, 4, 2)]
        s = g.summary(boots)
        self.assertEqual(s['score'], {'median': 193, 'min': 191, 'max': 195, 'all': [191, 195, 193]})
        self.assertEqual(s['scenes']['texture']['fps']['median'], 23)
        self.assertEqual((s['scenes']['texture']['fps']['min'], s['scenes']['texture']['fps']['max']), (21, 25))


class Guest(unittest.TestCase):
    def test_the_scenes_reach_glmark2_as_ten_words(self):
        words = shlex.split(g.bench_args())
        self.assertEqual(words[::2], ['-b'] * 10)
        self.assertEqual(words[9], 'effect2d:kernel=0,1,0;1,-4,1;0,1,0;:duration=5')
        self.assertEqual(words[11], 'pulsar:duration=5')
        self.assertNotIn('@', g.guest_script())

    def test_the_tail_cpio_has_the_init_and_the_console(self):
        b = g.tail_cpio()
        names, modes, at = [], {}, 0
        while True:
            h = b[at:at + 110]; self.assertEqual(h[:6], b'070701')
            f = [int(h[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
            mode, size, rmaj, rmin, nlen = f[1], f[6], f[9], f[10], f[11]
            name = b[at + 110:at + 110 + nlen - 1].decode()
            at += 110 + nlen; at += -at % 4
            data = b[at:at + size]; at += size; at += -at % 4
            if name == 'TRAILER!!!': break
            names.append(name); modes[name] = (mode, rmaj, rmin, data)
        self.assertEqual(at, len(b)); self.assertEqual(len(b) % 4, 0)
        self.assertEqual(modes['dev/console'][:3], (0o20600, 5, 1))
        self.assertEqual(modes[g.INIT][0], 0o100755)
        self.assertTrue(modes[g.INIT][3].startswith(b'#!/bin/sh\n'))
        self.assertLess(names.index('dev'), names.index('dev/console'))
        self.assertIn(f'rdinit=/{g.INIT}', g.CMDLINE)


if __name__ == '__main__':
    unittest.main()
