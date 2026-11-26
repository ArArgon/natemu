"""run_desktop's analysis on a made-up boot: the driver's windows, the guest-to-host clock, the guest's counters, the
translation requests begun in a window (first-seen code), the frames, the processes' CPU time, the sampler's markers
and the harness appended to the initramfs."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import io, shutil, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run_desktop as rd
import sample_symbols

OFF = 5_000_000_000            # host ns at guest uptime 0


def stamps(lines, late=0.01):
    """(host ns, text) as the runner stamps the driver's lines: each arrives `late` s after its t=."""
    out = []
    for text in lines:
        t = rd.uptime(text)
        out.append((OFF + int((t + late) * 1e9) if t is not None else OFF + int(10.5e9), text))
    return out


def stat(label, t, user, idle, pgfault, rsect, ioms):
    return (f'DE-STAT {label} user={user} nice=0 system=10 idle={idle} iowait=5 irq=0 softirq=0 steal=0 pgfault={pgfault} '
            f'pgmajfault=3 rd=10 rsect={rsect} rms=40 wr=1 wsect=8 wms=1 ioms={ioms} memavail=100 t={t}')


CONSOLE = ['DE-DRIVER phases=,idle,ui,apps, t=10.0', 'DE-KTEXT ffffffff81000000', 'DE-SESSION t=20.0 rt=1.0',
           'DE-SHELL-MAPPED at=50.0 t=50.3', 'DE-KWIN-SCRIPT loaded t=50.4', 'DE-IDLE-READY quiet=ok t=63.0',
           'DE-BEGIN idle t=64.0', stat('idle-begin', 64.0, 100, 1000, 1000, 0, 0), stat('idle-end', 84.0, 300, 8000, 1100, 0, 0), 'DE-END idle t=84.0',
           'DE-PS-BEGIN idle t=84.1', 'DE-PS 100 kwin_wayland cpu=150 minflt=10 majflt=0 rss=1', 'DE-PS 200 plasmashell cpu=60 minflt=5 majflt=0 rss=1',
           'DE-PS-END idle t=84.2',
           'DE-BEGIN app-konsole-1 t=90.0', stat('app-konsole-1-begin', 90.0, 300, 8000, 1100, 1000, 10),
           'DE-MAPPED app-konsole-1 at=95.5 started=90.1 pid=300 t=95.9', stat('app-konsole-1-mapped', 96.0, 600, 8100, 9100, 9192, 400),
           stat('app-konsole-1-settled', 99.0, 700, 8800, 9300, 9192, 410), 'DE-SETTLED app-konsole-1 quiet=ok t=99.0', 'DE-END app-konsole-1 t=99.0',
           'DE-PS-BEGIN apps t=100.0', 'DE-PS 100 kwin_wayland cpu=250 minflt=12 majflt=0 rss=1', 'DE-PS 300 konsole cpu=90 minflt=900 majflt=40 rss=1',
           'DE-PS-END apps t=100.1', 'DE-END-ALL t=101.0']


class Windows(unittest.TestCase):
    def setUp(self):
        self.lines = rd.guest_lines(stamps(CONSOLE))
        self.wins, self.first = rd.windows(self.lines)
        self.by = {w['name']: w for w in self.wins}

    def test_the_windows_and_their_spans(self):
        self.assertEqual(set(self.by), {'idle', 'app-konsole-1', 'app-konsole-1:start', 'app-konsole-1:settle',
                                        'session:shell', 'session:idle-ready'})
        s = self.by['app-konsole-1:start']
        self.assertEqual((s['t0'], s['t1'], s['pid']), (90.1, 95.5, '300'))      # from the command to KWin's own time of the window
        self.assertEqual((self.by['app-konsole-1:settle']['t0'], self.by['app-konsole-1:settle']['t1']), (95.5, 99.0))
        self.assertEqual((self.by['session:shell']['t0'], self.by['session:shell']['t1']), (20.0, 50.0))
        self.assertEqual(self.by['session:idle-ready']['t1'], 60.0)               # the 3 quiet seconds before the line

    def test_a_timed_out_start_has_no_window(self):
        lines = rd.guest_lines(stamps(['DE-BEGIN app-x-1 t=1.0', 'DE-MAPPED app-x-1 timeout started=1.0 t=241.0', 'DE-END app-x-1 t=250.0']))
        names = {w['name'] for w in rd.windows(lines)[0]}
        self.assertEqual(names, {'app-x-1'})

    def test_a_click_start_is_timed_from_the_enter_key_with_its_marks(self):
        lines = rd.guest_lines(stamps(['DE-INPUT key MetaLeft t=9.0', 'DE-BEGIN click-konsole-1 t=10.0', 'DE-INPUT key Enter t=10.1',
                                       'DE-MAPPED click-konsole-1 at=14.0 started=10.4 pid=7 t=14.3', 'DE-SETTLED click-konsole-1 quiet=ok t=16.0',
                                       'DE-END click-konsole-1 t=16.0',
                                       'DE-CLICKMARKS click-konsole-1 dbus=11.5 unit=12.0 exec=12.25 at=14.0 unitline=app-x t=16.5']))
        by = {w['name']: (w['kind'], w['t0'], w['t1']) for w in rd.windows(lines)[0]}
        self.assertEqual(by['click-konsole-1:start'], ('start', 10.1, 14.0))
        self.assertEqual(by['click-konsole-1:click-to-dbus'], ('mark', 10.1, 11.5))
        self.assertEqual(by['click-konsole-1:dbus-to-unit'], ('mark', 11.5, 12.0))
        self.assertEqual(by['click-konsole-1:exec-to-at'], ('mark', 12.25, 14.0))
        self.assertEqual(by['click-konsole-1:click-to-exec'], ('mark', 10.1, 12.25))
        self.assertEqual(by['click-konsole-1'][0], 'click')

    def test_the_guest_counters_over_a_window(self):
        g = rd.guest_delta(rd.stats_lines(self.lines), self.by['app-konsole-1:start'])
        self.assertEqual((g['user_s'], g['pgfault'], g['disk_read_mib'], g['disk_busy_ms']), (3.0, 8000, 4.0, 390))
        self.assertEqual(rd.guest_delta(rd.stats_lines(self.lines), self.by['idle'])['cpu_busy_s'], 2.0)

    def test_the_clock_maps_uptime_to_the_host_by_the_earliest_arrival(self):
        c = rd.Clock(rd.guest_lines(stamps(CONSOLE, late=0.01) + [(OFF + int(70.5e9), 'DE-X t=70.0')]))   # one line 0.5 s late
        self.assertAlmostEqual(c.ns(64.0), OFF + int(64.01e9), delta=10)
        self.assertIsNone(rd.Clock([]).ns(1.0))

    def test_cpu_time_by_process_between_ps_blocks(self):
        p = rd.ps_phases(rd.ps_blocks(self.lines))
        self.assertEqual(p[0]['cpu_s'], {'kwin_wayland': 1.5, 'plasmashell': 0.6})
        self.assertEqual(p[1]['cpu_s'], {'kwin_wayland': 1.0, 'konsole': 0.9})       # plasmashell had no time since


class Requests(unittest.TestCase):
    def test_requests_begun_in_a_window_with_their_sources_and_first_seen_code(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        def line(began, src, h, fexc_ns):
            s = {'m': (1, 0, 0), 'd': (0, 1, 0), 'f': (0, 0, 1)}[src]
            parts = [10, 20, 30, fexc_ns, 0, 40, 0, 50, 60]
            return ' '.join(map(str, [0, 'abc000', '401000', '1000', 'B', *s, 64, h, *parts, sum(parts), 0, '3', 0, 'ffffffffffffffff',
                                      began, 0, 64, 0, '-']))
        (d / 'boot-1.perf.v0').write_text('\n'.join([line(100, 'f', 'aa', 1000), line(200, 'm', 'aa', 0), line(300, 'f', 'bb', 2_000_000),
                                                     line(400, 'd', 'cc', 0)]) + '\n')
        (d / 'boot-1.perf.v1').write_text(line(250, 'f', 'dd', 3_000_000) + '\n' + 'garbage\n')
        r = rd.Requests(d / 'boot-1.perf')
        w = r.between(150, 401)
        self.assertEqual((w['count'], w['answers'], w['first_seen']), (4, {'memory': 1, 'disk': 1, 'fexc': 2, 'none': 0}, 3))
        self.assertEqual(w['parts_s'], {'fexc': 0.005})
        self.assertEqual(r.between(0, 150)['count'], 1)


class Frames(unittest.TestCase):
    def test_flips_in_a_window(self):
        ev = [(t, {'enabled': True, 'offset': t}) for t in (0, 16_000_000, 32_000_000, 132_000_000)] + [(200_000_000, {'enabled': False})]
        f = rd.frames(ev, 0, 1_000_000_000)
        self.assertEqual((f['count'], f['per_s'], f['gap_ms_median'], f['gap_ms_max']), (4, 4.0, 16.0, 100.0))
        self.assertEqual(rd.frames(ev, 500_000_000, 1_000_000_000)['count'], 0)


class Switches(unittest.TestCase):
    NAMES = ['guest', 'halted', 'tlb', 'link']

    def test_stretches_class_each_interval_by_its_busy_vcpus(self):
        # two vCPUs, 4 buckets each; 10 ms intervals (u = 1 ms): one busy, two busy, none busy
        ms, u = 10_000_000, 1_000_000
        sl = [(0, [0] * 8),
              (ms, [6 * u, 0, 4 * u, 0, 0, 10 * u, 0, 0]),                     # vCPU 0 busy (guest + tlb), vCPU 1 halted
              (2 * ms, [14 * u, 0, 6 * u, 0, 10 * u, 10 * u, 0, 0]),            # both busy
              (3 * ms, [14 * u, 10 * u, 6 * u, 0, 10 * u, 20 * u, 0, 0])]       # both halted
        st = rd.stretches(sl, 0, 3 * ms, self.NAMES)
        self.assertEqual((st['one']['wall_s'], st['many']['wall_s'], st['idle']['wall_s']), (0.01, 0.01, 0.01))
        self.assertEqual((st['one']['busy_s'], st['one']['switch_s']['tlb'], st['one']['switch_share']), (0.01, 0.004, 0.4))
        self.assertEqual(st['many']['switch_share'], 0.1)

    def test_counters_between_two_snapc_lines(self):
        per = len(rd.SNAP_COUNTERS) + 2
        sc = [(0, [0] * per * 2), (100, [5, 0, 0, 1, 2, 3, 7, 8] + [1, 0, 0, 0, 0, 0, 0, 0]), (200, [9, 0, 0, 1, 2, 4, 7, 8] * 2)]
        c, entries = rd.counts_between(sc, 0, 100, 2)
        self.assertEqual((c['cr3_loads'], c['relinked'], entries), (6, 3, [7, 8]))
        c, _ = rd.counts_between(sc, 100, 200, 2)
        self.assertEqual(c['cr3_loads'], 12)
        self.assertIsNone(rd.counts_between(sc, 150, 160, 2))


class Markers(unittest.TestCase):
    def test_the_sampler_markers_name_each_window(self):
        d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        b = {'stamps': stamps(CONSOLE), 'display': []}
        a = rd.analyse_boot(b, None)
        rd.markers_for_sampler(d / 'm.tsv', b, a)
        texts = [l.split('\t')[1] for l in (d / 'm.tsv').read_text().splitlines()]
        self.assertEqual(texts[:2], ['NATEMU-BENCH-KTEXT ffffffff81000000', 'NATEMU-BENCH-START'])
        self.assertEqual(texts[-1], 'NATEMU-BENCH-END')
        self.assertIn('NATEMU-BENCH-PHASE name=session/idle-ready', texts)
        self.assertIn('NATEMU-BENCH-PHASE name=app-konsole-1/start', texts)
        self.assertLess(texts.index('NATEMU-BENCH-PHASE name=idle'), texts.index('NATEMU-BENCH-PHASE name=app-konsole-1/start'))


class Harness(unittest.TestCase):
    def test_the_cases_sets_replace_the_harnesss_for_the_same_key(self):
        import types
        sets = rd.natemu_sets(types.SimpleNamespace(natemu_set=['machine.pvpanic=false', 'machine.ps2=false']))
        self.assertEqual([kv for kv in sets if kv.startswith('machine.')], ['machine.pvpanic=false', 'machine.ps2=true'])

    def test_the_appended_cpio_holds_the_harness_and_speedometers_files(self):
        data = rd.tail()
        names = sample_symbols.cpio_files(data)
        for n in ('de-harness/driver.sh', 'de-harness/setup.sh', 'de-harness/session.sh', 'de-harness/serve.py', 'de-harness/hook.js',
                  'de-harness/de-gpu.js', 'de-harness/kwin-demarker/contents/code/main.js', 'de-harness/home-config/kwinrc'):
            self.assertIn(n, names)


if __name__ == '__main__':
    unittest.main()
