"""run_paper's marker parsing (starts, markers) and the guest script's substitutions."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, shutil, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run_paper


class Starts(unittest.TestCase):
    def test_times_come_from_the_host_stamps(self):
        rows = [(10.0, ['start', '1']), (42.5, ['done', '1', '29.889']), (44.0, ['exit', '1', '137']),
                (50.0, ['step', 'restore-2', 'ok']), (51.0, ['start', '2']), (86.25, ['done', '2', '32.661']),
                (87.0, ['exit', '2', '137'])]
        self.assertEqual(run_paper.starts(rows), [
            {'start': 1, 'wall_to_done': 32.5, 'paper_done': 29.889, 'to_exit': 1.5, 'exit': 137},
            {'start': 2, 'wall_to_done': 35.25, 'paper_done': 32.661, 'to_exit': 0.75, 'exit': 137}])

    def test_a_start_without_done_is_not_counted(self):
        self.assertEqual(run_paper.starts([(1.0, ['start', '1']), (9.0, ['failed', '1'])]), [])

    def test_markers_read_the_launch_stamps(self):
        t = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, t, True)
        (t / 'markers-bench-1.tsv').write_text('1500000000\tPAPER start 1\n4000000000\tPAPER done 1 2.1\n')
        self.assertEqual(run_paper.markers(t, 'bench-1'), [(1.5, ['start', '1']), (4.0, ['done', '1', '2.1'])])
        self.assertEqual(run_paper.markers(t, 'bench-2'), [])


class Options(unittest.TestCase):
    def args(self, perf_log):
        import types
        return types.SimpleNamespace(natemu='natemu', runtime='rt', fexc='fexc', inputs=pathlib.Path('in'), ram=2, vcpus=4,
                                     speculation_barriers=True, perf_log=perf_log, out=pathlib.Path('out'))

    def test_the_timing_log_only_when_asked(self):
        self.assertEqual(run_paper.phase_command(self.args(True), 'disk', 'bench-1')[-2:], ['--perf-log', pathlib.Path('out/perf-bench-1.log')])
        self.assertNotIn('--perf-log', run_paper.phase_command(self.args(False), 'disk', 'bench-1'))

    def test_a_hidden_afp_must_show_in_the_startup_line(self):
        t = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, t, True)
        (t / 'stderr-bench-1.log').write_text('natemu: cpu: x86-64-v3\nnatemu: fp: fast mode\n')
        (t / 'stderr-bench-2.log').write_text('natemu: fp: fast mode, degraded (host lacks FEAT_AFP)\n')
        hidden = {'NATEMU_FEXC_FEATURE_OFF': 'flagm2,afp'}
        self.assertEqual(run_paper.fp_line(t, 'bench-1'), 'natemu: fp: fast mode')
        self.assertFalse(run_paper.afp_as_asked(run_paper.fp_line(t, 'bench-1'), hidden))
        self.assertTrue(run_paper.afp_as_asked(run_paper.fp_line(t, 'bench-2'), hidden))
        self.assertFalse(run_paper.afp_as_asked(run_paper.fp_line(t, 'setup'), hidden))   # (no stderr: no line)
        self.assertTrue(run_paper.afp_as_asked(run_paper.fp_line(t, 'bench-1'), {}))


class Guest(unittest.TestCase):
    def test_every_placeholder_is_substituted(self):
        filled = run_paper.guest_script(2)
        self.assertNotIn('@', filled.replace('$@', ''))
        for v in (run_paper.JDK_VERSION, run_paper.JAR_SHA, run_paper.MOJANG_SHA, run_paper.SEED, run_paper.HEAP): self.assertIn(v, filled)

    def test_the_script_is_valid_bash(self):
        import subprocess
        subprocess.run(['bash', '-n'], input=run_paper.guest_script(2), text=True, check=True)

    def test_the_jar_pin_is_the_resource_pin(self):
        import tomllib
        res = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / 'resources.toml').read_text())['resource']['paper-jar']
        self.assertEqual(res['sha256'], run_paper.JAR_SHA)
        self.assertTrue(res['urls'][0].endswith(run_paper.JAR))
        mojang = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / 'resources.toml').read_text())['resource'][f'minecraft-server-{run_paper.MC_VERSION}']
        self.assertEqual(mojang['sha256'], run_paper.MOJANG_SHA)

    def test_the_jdk_package_is_the_pinned_version(self):
        import tomllib, urllib.parse
        res = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / 'resources.toml').read_text())['resource']
        jdk = urllib.parse.unquote(res['paper-deb-openjdk-25-jre-headless']['urls'][0].rsplit('/', 1)[1])
        self.assertEqual(jdk, f'openjdk-25-jre-headless_{run_paper.JDK_VERSION}_amd64.deb')


if __name__ == '__main__':
    unittest.main()
