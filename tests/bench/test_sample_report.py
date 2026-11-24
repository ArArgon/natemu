"""sample_report on synthetic NAME.samples and NAME.objects written here in the layout of crates/abi/src/sample.rs (whose
constants the first test reads from the source): decoding, places and kinds, phases from markers and marks, the counters'
checks, the objects' lookup and a sample's word, and the command it runs first."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json, re, shutil, struct, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sample_report as sr

ROOT = pathlib.Path(__file__).resolve().parents[2]
F = 24_000_000          # ticks per second


# --- a writer, as crates/abi/src/sample.rs encodes ----------------------------------------------------------------------
def el0(time, rip, place, context=0, at=None, frame=0, flags=0, flushes=0, since=0, kind=sr.EL0, raw_at=0):
    """A resident record; `at` a block sample's native offset (recorded + 4, natemu_abi::sample::at), `raw_at` the field."""
    if at is not None: raw_at = at + 4
    return (time, rip & ((1 << 48) - 1) | (flushes & 0xfff) << 48 | (flags & 0xf) << 60,
            context & ((1 << 40) - 1) | (raw_at & 0xffff) << 40 | (place & 0xf) << 56 | (kind & 0xf) << 60, frame | since << 32)


def hostrec(kind, time=0, value1=0, value2=0, value3=0, value4=0):
    return (time, value1, value2 & sr.M56 | kind << 60, value3 | value4 << 32)


def header(vcpus, interval=250):
    return (sr.FILE_MAGIC, sr.VERSION | 32 << 32, F, vcpus | interval << 32)


def write_samples(path, records):
    path.write_bytes(b''.join(struct.pack('<4Q', *r) for r in records))


def code_object(guest_rip, code, guest_insns=3):
    """A code object (crates/abi/src/code_object.rs Header, then the code) with `code` at code_offset 128."""
    h = struct.pack('<8sIIIIQIIIIIII', sr.CODE_MAGIC, 4, 128, 128 + len(code), 64, guest_rip, 12, guest_insns, 128, len(code), 8,
                    0xffff_ffff, len(code))
    return h + bytes(128 - len(h)) + code


def obj_record(rip, frame0, context, obj, guest, source=1, vcpu=0):
    length = (56 + len(obj) + len(guest) + 7) // 8 * 8
    h = struct.pack('<IBBHQQQQQII', length, source, vcpu, 0, rip, frame0, 2 ** 64 - 1, context, 0x1234, len(obj), len(guest))
    return (h + obj + guest).ljust(length, b'\0')


def write_objects(path, records):
    path.write_bytes(struct.pack('<4Q', sr.OBJECTS_MAGIC, sr.VERSION | 56 << 32, 0, 0) + b''.join(records))


USER64 = 3 | 1 << 2           # CPL 3, 64-bit mode
KERNEL64 = 0 | 1 << 2
USER32 = 3 | 2 << 2
S = F                         # one second in ticks
T0 = 100 * S                  # the run's first clock


def counts(v, rings, written, drained, halted=0, bells=None, in_wait=0, lost=0):
    names = dict(rings=rings, halted=halted, written=written, lost=lost, bells=written if bells is None else bells,
                 bells_in_wait=in_wait, unknown_calls=0, drained=drained)
    return [hostrec(sr.COUNT, T0 + 9 * S, value1=n, value3=v, value4=sr.COUNTERS.index(k)) for k, n in names.items()]


def run_counts(accesses=0, ignored=0, not_sampling=0):
    return [hostrec(sr.COUNT, T0 + 9 * S, value1=n, value3=sr.RUN, value4=sr.COUNTERS.index(k))
            for k, n in (('port_accesses', accesses), ('port_ignored', ignored), ('not_sampling', not_sampling))]


def standard_run():
    """Two vCPUs over 8 s. vCPU 0: in run1 (2-5 s) 4 samples of block A (user 64-bit, two at word 1, one hot), 1 probe charged to
    A, 3 dispatch samples (a return, not a call, unknown), 1 resident; in run2 (6-8 s) 2 samples of kernel block K and one
    of user 32-bit block B. vCPU 1: two halted samples and one EL0 head sample. Marks 7 at 3 s and 8 at 4 s."""
    A, K, B = 0x40_1000, 0xffff_ffff_8100_0040, 0x0804_8100
    t = lambda s: T0 + int(s * S)
    v0 = [el0(t(2.1), A, sr.BLOCK, USER64, at=4, frame=0x55, since=24), el0(t(2.2), A, sr.BLOCK, USER64, at=4, frame=0x55),
          el0(t(2.3), A, sr.BLOCK, USER64, at=8, frame=0x55, flags=sr.HOT), el0(t(3.5), A, sr.BLOCK, USER64, at=0, frame=0x55),
          el0(t(3.6), A, sr.PROBE, USER64, at=12, frame=0x55),
          el0(t(4.1), 0x40_2000, sr.DISPATCH, USER64, frame=0x56, flags=sr.CALL_YES << sr.CALL_SHIFT),
          el0(t(4.2), 0x40_2010, sr.DISPATCH, USER64, frame=0x56, flags=sr.CALL_NO << sr.CALL_SHIFT),
          el0(t(4.3), 0xffff_ffff_8100_0000, sr.DISPATCH, 0, flags=sr.CALL_UNKNOWN << sr.CALL_SHIFT),
          el0(t(4.4), 0, 0, since=5, kind=sr.RESIDENT),
          el0(t(6.5), K, sr.BLOCK, KERNEL64, at=-4, frame=0x1000), el0(t(6.6), K, sr.BLOCK, KERNEL64, at=4, frame=0x1000),
          el0(t(7.0), B, sr.STEP, USER32, at=0, frame=0x77)]
    v1 = [el0(t(2.5), 0, sr.HEAD, 0, frame=0x40)]
    recs = [header(2), hostrec(sr.CLOCK, T0, value1=1_700_000_000 * 10 ** 9),
            hostrec(sr.TOGGLE, T0, value1=0, value2=1, value3=0, value4=sr.RUN),
            hostrec(sr.CLOCK, t(4.5), value1=1, value2=1),
            hostrec(sr.VCPU, t(4.5), value1=0, value2=9, value3=0, value4=9), *v0[:9],
            hostrec(sr.VCPU, t(4.5), value1=0, value2=1, value3=1, value4=1), *v1,
            hostrec(sr.HALTED, t(2.6), value1=0, value3=1), hostrec(sr.MARK, t(3), value1=7, value3=0),
            hostrec(sr.MARK, t(4), value1=8, value3=1), hostrec(sr.HALTED, t(6.1), value1=0, value3=1),
            hostrec(sr.CLOCK, t(8), value1=2, value2=2),
            hostrec(sr.VCPU, t(8), value1=0, value2=12, value3=0, value4=3), *v0[9:],
            *counts(0, rings=14, written=12, drained=12), *counts(1, rings=1, written=1, drained=1, halted=2), *run_counts()]
    markers = [(2, 'NATEMU-BENCH-START bench=x jobs=1 cpus=1'), (3, 'NATEMU-BENCH-PHASE bench=x jobs=1 name=one rc=0 guest_s=1'),
               (5, 'NATEMU-BENCH-END bench=x jobs=1 rc=0 guest_s=3'), (5.1, 'NATEMU-BENCH-VERIFY ok'),
               (6, 'NATEMU-BENCH-START2 bench=x'), (8, 'NATEMU-BENCH-END2 bench=x rc=0 guest_s=2')]
    objects = [obj_record(A, 0x55_000, USER64, code_object(A, bytes(range(16))), b'\x90' * 12),
               obj_record(0x40_5000 | (K & 0xfff), 0x1000_000, KERNEL64, code_object(K, b'\xaa' * 8 + b'\xbb' * 4), b'\xcc' * 12)]
    return recs, markers, objects


class Base(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d, True)
        self.name = self.d / 'case'

    def write(self, recs, markers=None, objects=None):
        write_samples(self.d / 'case.samples', recs)
        if objects is not None: write_objects(self.d / 'case.objects', objects)
        if markers is not None:
            (self.d / 'case.markers.tsv').write_text(''.join(f'{int((T0 / F + s) * 1e9)}\t{m}\n' for s, m in markers))
        return self.d / 'case.markers.tsv'

    def report(self, **kw):
        problems = sr.report_one(self.name, **kw)
        return problems, json.loads((self.d / 'case.report.json').read_text()) if (self.d / 'case.report.json').exists() else None


class Format(unittest.TestCase):
    def test_constants_are_the_abi_s(self):
        text = (ROOT / 'crates/abi/src/sample.rs').read_text()
        const = {m[1]: m[2] for m in re.finditer(r'pub const (\w+): (?:u8|u16|u32|u64|i64|usize) = ([^;]+);', text)}
        num = lambda n: int(const[n].replace('_', ''), 0)
        for n in ('FILE', 'EL0', 'RESIDENT', 'EL0_UNKNOWN', 'OLD', 'HALTED', 'MARK', 'CLOCK', 'VCPU', 'COUNT', 'TOGGLE', 'PASS', 'BLOCK', 'PROBE',
                  'DISPATCH', 'STEP', 'HEAD', 'NO_BLOCK', 'OUTSIDE', 'HOT', 'DEAD', 'CALL_SHIFT', 'CALL_YES', 'CALL_NO',
                  'CALL_UNKNOWN', 'RECORD_BYTES', 'VERSION', 'CONTEXT_BITS', 'FAR', 'AT_BIAS', 'TAIL', 'TAIL_CALL', 'TAIL_POINT'):
            self.assertEqual(getattr(sr, n), num(n), n)
        self.assertEqual(sr.COUNTERS, re.findall(r'"(\w+)"', re.search(r'pub const COUNTERS: \[&str; \d+\] = \[(.*?)\];', text, re.S)[1]))
        for n in sr.COUNTERS: self.assertEqual(sr.COUNTERS.index(n), num('C_' + n.upper()), n)
        self.assertIn('u64::from_le_bytes(*b"NATEMUSA")', const['FILE_MAGIC'])
        self.assertIn('u64::from_le_bytes(*b"NATEMUOB")', const['OBJECTS_MAGIC'])
        self.assertIn('assert!(OBJECT_HEADER_BYTES == 56)', text)
        self.assertEqual(sr.OBJ.size, sr.OBJECT_HEADER_BYTES)
        self.assertEqual(sr.CODE_MAGIC, b'NFXOBJ\0\x01')
        co = (ROOT / 'crates/abi/src/code_object.rs').read_text()
        self.assertIn(r'pub const MAGIC: [u8; 8] = *b"NFXOBJ\0\x01";', co)
        fields = re.findall(r'pub (\w+): ', re.search(r'pub struct Header \{(.*?)\n\}', co, re.S)[1])
        self.assertEqual(fields[:13], ['magic', 'version', 'header_size', 'total_size', 'guest_bitness', 'guest_rip', 'guest_bytes',
                                       'guest_insns', 'code_offset', 'code_size', 'body_offset', 'end_marker_offset', 'code_region_end'])

    def test_records_decode_at_their_limits(self):
        w = el0(2 ** 64 - 6, 0xffff_ffff_8100_1234, sr.DISPATCH, context=(1 << 39) | 0x3_0000_0005, raw_at=0xfffc, frame=0x89ab_cdef,
                flags=sr.HOT | sr.CALL_UNKNOWN << sr.CALL_SHIFT, flushes=0xabc, since=77)
        self.assertEqual(sr.sample(w), dict(time=2 ** 64 - 6, rip=0xffff_ffff_8100_1234, flushes=0xabc, flags=sr.HOT | 3 << 2,
                                            context=(1 << 39) | 0x3_0000_0005, at=0xfffc, place=sr.DISPATCH, kind=sr.EL0,
                                            frame=0x89ab_cdef, since=77))
        self.assertEqual(sr.sample(el0(0, 0x7fff_ffff_f000, sr.BLOCK))['rip'], 0x7fff_ffff_f000)   # a user RIP stays positive
        self.assertEqual(sr.offset(sr.sample(el0(0, 1, sr.BLOCK, at=-4))), -4)            # a PIC block's full entry word
        self.assertEqual(sr.offset(sr.sample(el0(0, 1, sr.BLOCK, raw_at=0xffff, flags=sr.FAR))), None)   # saturated
        h = sr.host(hostrec(sr.MARK, 9, 255, (1 << 56) - 1, 3, 0xffff_ffff))
        self.assertEqual(h, dict(kind=sr.MARK, time=9, value1=255, value2=(1 << 56) - 1, value3=3, value4=0xffff_ffff))

    def test_slot_forms_and_tail_roles_come_from_at(self):
        # (Indirect plan step 1: a slot's three forms at 0, 64 and 128 of its 256 bytes; a tail word's role in `at`.)
        execution = (ROOT / 'crates/abi/src/execution.rs').read_text()
        for n, v in (('DISPATCH_SLOT_BYTES', sr.SLOT_BYTES), ('SLOT_LOOKUP', sr.SLOT_LOOKUP), ('SLOT_RETURN', sr.SLOT_RETURN)):
            self.assertIn(f'pub const {n}: usize = {v};', execution)
        self.assertEqual([sr.slot_form(a) for a in (4, 2 * 256 + 60, 256 + 64, 3 * 256 + 200)], ['ordinary', 'ordinary', 'lookup', 'return'])
        self.assertEqual([sr.tail_role(a) for a in (0, 1 | sr.TAIL_CALL, 2 | sr.TAIL_CALL | sr.TAIL_POINT, 1 | sr.TAIL_POINT)],
                         ['jump-bl', 'call-branch', 'link-call-pad', 'link-jump-branch'])

    def test_privilege_and_mode_come_from_the_context(self):
        self.assertEqual(sr.privilege(USER64, 0, True), 'user')
        self.assertEqual(sr.privilege(KERNEL64, 0x40_0000, True), 'kernel')
        self.assertEqual(sr.privilege(0, 0xffff_ffff_8100_0000, False), 'kernel')
        self.assertEqual(sr.privilege(0, 0x40_0000, False), 'user')
        self.assertEqual((sr.mode(USER64), sr.mode(USER32)), ('64', '32'))


class Report(Base):
    def test_a_run_is_counted_by_kind_place_privilege_and_call_test(self):
        recs, markers, objects = standard_run()
        problems, r = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, [])
        whole = r['phases'][0]
        self.assertEqual(whole['name'], 'all')
        s = whole['summed']
        self.assertEqual(s['kinds'], dict(el0=12, el0_unknown=0, resident=1, halted=2))
        self.assertEqual(s['el0_places'], dict(block=6, probe=1, dispatch=3, step=1, head=1, no_block=0, outside=0, tail=0))
        self.assertEqual((s['dispatch_forms'], s['tail_roles']), (dict(ordinary=3, lookup=0, ** {'return': 0}), {}))
        self.assertEqual(s['dispatch_calls'], {'return': 1, 'not_call': 1, 'unknown': 1})
        # Blocks and the dispatcher with a context by it; the context-less dispatcher sample by its RIP; the head sample unknown.
        self.assertEqual(s['privilege'], dict(user=8, kernel=3, unknown=1))
        self.assertEqual(s['modes'], {'64': 9, '32': 1})
        self.assertEqual(whole['vcpus']['1']['kinds'], dict(el0=1, el0_unknown=0, resident=0, halted=2))
        self.assertEqual(s['hot'], 1)
        self.assertEqual(r['vcpus'], 2)
        self.assertEqual(r['counters']['1']['halted'], 2)
        self.assertEqual(r['counters']['0']['merged'], 2)              # 14 rings, 12 bells
        self.assertEqual(r['duration_s'], 8.0)
        self.assertEqual(r['on_s'], 8.0)
        md = (self.d / 'case.report.md').read_text()
        self.assertIn('| all |', md); self.assertIn('| run1/one |', md); self.assertIn('`0x401000`', md)

    def test_el0_with_no_place_counts_as_el0_but_has_no_place(self):
        """Version 2: a ring in an earlier stretch of generated code than the one the bell interrupted
        (natemu_abi::sample::EL0_UNKNOWN) is EL0 time in `el0_all_pct` and in its own kind, but in no place, privilege or
        EL0 share below it; `at` is how many stretches back."""
        recs, markers, objects = standard_run()
        resident = [i for i, r in enumerate(recs) if r[2] >> 60 == sr.RESIDENT]
        self.assertEqual(len(resident), 1)
        r = recs[resident[0]]
        recs[resident[0]] = (r[0], r[1], r[2] & ~(0xf << 60) | 3 << 40 | sr.EL0_UNKNOWN << 60, r[3])   # three stretches back
        problems, rep = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, [])
        s = rep['phases'][0]['summed']
        self.assertEqual(s['kinds'], dict(el0=12, el0_unknown=1, resident=0, halted=2))
        self.assertEqual((s['el0_unknown_back'], s['resident_old']), ({'3': 1}, 0))
        self.assertEqual(s['el0_all_pct'], round(100 * 13 / 15, 2))
        self.assertEqual(s['kind_pct']['el0'], round(100 * 12 / 15, 2))
        self.assertEqual(sum(s['el0_places'].values()), 12)
        self.assertEqual(sum(s['privilege'].values()), 12)
        md = (self.d / 'case.report.md').read_text()
        row = next(l for l in md.splitlines() if l.startswith('| all |'))
        self.assertEqual(row.split(' | ')[3:6], ['86.7', '80.0', '6.7'])
        head = next(l for l in md.splitlines() if l.startswith('| phase |'))
        sep = md.splitlines()[md.splitlines().index(head) + 1]
        self.assertEqual(head.count('|'), sep.count('|'))
        self.assertEqual(head.count('|'), row.count('|'))

    def test_a_resident_sample_older_than_every_stretch_is_counted(self):
        recs, markers, objects = standard_run()
        i = next(i for i, r in enumerate(recs) if r[2] >> 60 == sr.RESIDENT)
        recs[i] = (recs[i][0], recs[i][1] | sr.OLD << 60, recs[i][2], recs[i][3])
        problems, rep = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, [])
        s = rep['phases'][0]['summed']
        self.assertEqual((s['kinds']['resident'], s['resident_old']), (1, 1))
        self.assertIn('kept (counted resident): 1.', (self.d / 'case.report.md').read_text())

    def test_a_version_1_file_still_reads(self):
        recs, markers, objects = standard_run()
        recs[0] = (sr.FILE_MAGIC, 1 | 32 << 32, F, recs[0][3])
        problems, rep = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, [])
        self.assertEqual(rep['phases'][0]['summed']['el0_all_pct'], round(100 * 12 / 15, 2))

    def test_phases_come_from_markers_and_marks(self):
        recs, markers, objects = standard_run()
        _, r = self.report(markers=self.write(recs, markers, objects))
        ph = {p['name']: p for p in r['phases']}
        self.assertEqual(list(ph), ['all', 'boot', 'run1', 'run1/one', 'between', 'run2', 'mark/7', 'mark/8'])
        self.assertEqual((ph['run1']['start_s'], ph['run1']['end_s']), (2.0, 5.0))
        self.assertEqual(ph['run1']['summed']['kinds'], dict(el0=9, el0_unknown=0, resident=1, halted=1))
        self.assertEqual(ph['run1/one']['summed']['el0_places']['block'], 3)        # 2.1, 2.2, 2.3 s
        self.assertEqual(ph['run2']['summed']['el0_places'], dict(block=2, probe=0, dispatch=0, step=1, head=0, no_block=0, outside=0, tail=0))
        self.assertEqual(ph['boot']['summed']['samples'], 0)
        self.assertEqual(ph['mark/7']['summed']['el0_places']['block'], 1)          # 3.5 s, until mark 8 at 4 s
        self.assertEqual(ph['mark/8']['end_s'], 8.0)
        self.assertEqual(ph['run2']['vcpus']['1']['kinds']['halted'], 1)
        # Rates: vCPU 0 has 11 EL0 samples and 1 resident in 8 s on.
        self.assertEqual(ph['all']['samples_per_s']['0'], 1.5)
        self.assertEqual([x['mark'] for x in r['marks']], [7, 8])
        # The fixed windows: second 2 has three block samples, vCPU 1's head sample and a halted one.
        sec2 = next(x for x in r['series'] if x['t_s'] == 2.0)
        self.assertEqual((sec2['block'], sec2['head'], sec2['halted'], sec2['el0']), (3, 1, 1, 4))

    def test_top_blocks_find_their_objects(self):
        recs, markers, objects = standard_run()
        _, r = self.report(markers=self.write(recs, markers, objects))
        b = r['phases'][0]['blocks']
        self.assertEqual((b['distinct'], b['samples']), (3, 8))
        top = b['top']
        self.assertEqual([(x['rip'], x['samples'], x['object']) for x in top],
                         [('0x401000', 5, 'exact'), ('0xffffffff81000040', 2, 'page'), ('0x8048100', 1, None)])
        self.assertEqual(top[0]['cum_pct'], 62.5)
        self.assertEqual(top[0]['guest_bytes'], '90' * 12)
        self.assertEqual((top[0]['privilege'], top[1]['privilege'], top[2]['mode']), ('user', 'kernel', '32'))
        self.assertEqual(b['cum_pct_at'], {'1': 62.5, '2': 87.5})
        self.assertEqual(b['blocks_for_pct'], {'50': 1, '80': 2, '90': 3, '95': 3, '99': 3})

    def test_locate_gives_the_object_and_word(self):
        recs, markers, objects = standard_run()
        self.write(recs, markers, objects)
        o = sr.Objects(self.d / 'case.objects')
        self.assertEqual(o.count, 2)
        a = sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER64, at=8, frame=0x55)))
        self.assertEqual((a['how'], a['offset'], a['word_index'], a['word']), ('exact', 8, 2, bytes([8, 9, 10, 11])))
        self.assertEqual(a['record']['code']['guest_insns'], 3)
        # A probe sample is charged to its store's block, at the word its probe returns to.
        self.assertEqual(sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.PROBE, USER64, at=12, frame=0x55)))['word'], bytes([12, 13, 14, 15]))
        # Another RIP at the same page offset, frame and context: the template's object.
        k = sr.locate(o, sr.sample(el0(0, 0xffff_ffff_8100_0040, sr.BLOCK, KERNEL64, at=8, frame=0x1000)))
        self.assertEqual((k['how'], k['word']), ('page', b'\xbb' * 4))
        # A PIC block's full entry (-4) and past the code: no word.
        self.assertIsNone(sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER64, at=-4, frame=0x55)))['word'])
        far = sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER64, raw_at=0xffff, flags=sr.FAR, frame=0x55)))
        self.assertEqual((far['how'], far['offset'], far['word']), ('exact', None, None))
        past = sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER64, at=16, frame=0x55)))
        self.assertEqual((past['word'], past['beyond']), (None, True))
        self.assertFalse(sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER64, at=12, frame=0x55)))['beyond'])
        # Not found: another context, another frame; and places with no block.
        self.assertIsNone(sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER32, at=8, frame=0x55))))
        self.assertIsNone(sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.BLOCK, USER64, at=8, frame=0x56))))
        self.assertIsNone(sr.locate(o, sr.sample(el0(0, 0x40_1000, sr.DISPATCH, USER64, frame=0x55))))

    def test_the_whole_run_counts_words_per_block(self):
        recs, markers, objects = standard_run()
        self.write(recs, markers, objects)
        p = sr.Profile(self.name, self.d / 'case.markers.tsv')
        key = (0x40_1000, 0x55, USER64)
        self.assertEqual({(at, probe): n for (k, at, probe), n in p.sum[0].words.items() if k == key},
                         {(4, False): 2, (8, False): 1, (0, False): 1, (12, True): 1})
        # Every phase counts its words (run1 holds all of A's).
        run1 = [n for n, _, _ in p.phases.windows].index('run1')
        self.assertEqual(sum(n for (k, _, _), n in p.sum[run1].words.items() if k == key), 5)


class Checks(Base):
    """Each check of the report, on a run broken in its one way."""

    def test_records_written_after_the_last_drain_fail(self):
        recs, markers, objects = standard_run()
        recs = [r for r in recs if not (r[2] >> 60 == sr.COUNT and r[3] == 0 | sr.COUNTERS.index('written') << 32)]
        recs += [hostrec(sr.COUNT, 0, value1=14, value3=0, value4=sr.COUNTERS.index('written'))]
        problems, _ = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, ['vCPU 0: 2 records written after the last drain'])

    def test_records_the_host_did_not_drain_fail(self):
        recs, markers, objects = standard_run()
        recs = [r for r in recs if not (r[2] >> 60 == sr.COUNT and r[3] == 0 | sr.COUNTERS.index('drained') << 32)]
        recs += [hostrec(sr.COUNT, 0, value1=11, value3=0, value4=sr.COUNTERS.index('drained'))]
        problems, _ = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, ['vCPU 0: 12 ring records in the file, the host drained 11',
                                    'vCPU 0: 1 records written after the last drain'])

    def test_a_run_that_did_not_end_fails(self):
        recs, markers, objects = standard_run()
        problems, _ = self.report(markers=self.write([r for r in recs if r[2] >> 60 != sr.COUNT], markers, objects))
        self.assertEqual(problems, ['no counters: the run did not end (natemu killed?)'])

    def test_a_stray_port_access_fails(self):
        recs, markers, objects = standard_run()
        recs = [r for r in recs if not (r[2] >> 60 == sr.COUNT and r[3] >> 32 >= sr.COUNTERS.index('port_accesses'))] + run_counts(3, 1)
        problems, r = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, ['the control port ignored 1 accesses (a stray port access)'])
        self.assertEqual(r['counters']['run']['port_accesses'], 3)

    def test_no_el0_sample_fails(self):
        recs = [header(1), hostrec(sr.CLOCK, T0), hostrec(sr.TOGGLE, T0, value2=1, value4=sr.RUN),
                hostrec(sr.HALTED, T0 + 1, value3=0), *counts(0, rings=0, written=0, drained=0, halted=1), *run_counts()]
        self.write(recs, objects=[])
        problems, r = self.report()
        self.assertEqual(problems, ['no EL0 sample'])
        self.assertEqual(r['phases'][0]['summed']['kinds'], dict(el0=0, el0_unknown=0, resident=0, halted=1))

    def test_a_required_place_without_samples_fails(self):
        recs, markers, objects = standard_run()
        recs = [r for r in recs if not (r[2] >> 60 == sr.EL0 and r[2] >> 56 & 0xf == sr.PROBE)]
        recs = [hostrec(sr.VCPU, r[0], value1=0, value2=8, value3=0, value4=8) if r[2] >> 60 == sr.VCPU and r[3] == 9 << 32 else r for r in recs]
        recs = [r for r in recs if r[2] >> 60 != sr.COUNT] + counts(0, rings=14, written=11, drained=11) + \
            counts(1, rings=1, written=1, drained=1, halted=2) + run_counts()
        problems, _ = self.report(markers=self.write(recs, markers, objects), require=['probe'])
        self.assertEqual(problems, ['no probe sample (--require-place probe)'])
        self.assertEqual(sr.report_one(self.name, require=['block']), [])

    def test_a_batch_cut_short_fails(self):
        recs, markers, objects = standard_run()
        cut = recs[:recs.index(next(r for r in recs if r[2] >> 60 == sr.HALTED)) - 1]    # vCPU 1's batch loses its record
        problems, _ = self.report(markers=self.write(cut, markers, objects))
        self.assertIn("the file ends 1 records into vCPU 1's batch", problems)

    def test_missing_objects_and_bad_files_fail(self):
        recs, markers, _ = standard_run()
        problems, r = self.report(markers=self.write(recs, markers))
        self.assertEqual(problems, ['case.objects: missing'])
        self.assertNotIn('object', r['phases'][0]['blocks']['top'][0])
        (self.d / 'case.samples').write_bytes(struct.pack('<4Q', 1, 2, 3, 4))
        self.assertEqual(sr.report_one(self.name), [f'{self.d / "case.samples"}: not a version 1 or 2 samples file'])
        (self.d / 'case.samples').unlink()
        self.assertEqual(sr.report_one(self.name), [f'{self.d / "case.samples"}: missing'])
        write_samples(self.d / 'case.samples', recs)
        (self.d / 'case.objects').write_bytes(struct.pack('<4Q', sr.OBJECTS_MAGIC, 1 | 56 << 32, 0, 0) + struct.pack('<I', 99))
        self.assertIn('cut short', sr.report_one(self.name)[0])

    def test_a_missing_markers_file_is_a_note(self):
        recs, markers, objects = standard_run()
        self.write(recs, None, objects)
        problems, r = self.report(markers=self.d / 'none.tsv')
        self.assertEqual(problems, [])
        self.assertEqual([p['name'] for p in r['phases']], ['all', 'mark/7', 'mark/8'])
        self.assertIn('no markers file', r['notes'][0])


class Attribution(Base):
    """Roles and symbols (chunk 1) in the report: a real admitted object (the host's admission fixture push-run.obj, which
    fexc compiled) mapped from sample offsets to its words, and the report's tables on the standard run."""
    FIXTURE = ROOT / 'crates/host/tests/admission/push-run.obj'

    def fixture_objects(self):
        obj = self.FIXTURE.read_bytes()
        guest = self.FIXTURE.with_suffix('.src').read_bytes()
        c = sr.code_header(obj)
        write_objects(self.d / 'case.objects', [obj_record(c['guest_rip'], 0x55_000, USER64, obj, guest)])
        return sr.Objects(self.d / 'case.objects'), obj, c

    def test_a_sample_offset_is_the_admitted_objects_word(self):
        import sample_roles
        o, obj, c = self.fixture_objects()
        code = sample_roles.Code(obj)
        self.assertLess(c['code_region_end'], c['code_size'])          # FEX's tail follows the region in the object
        for s in code.sites:
            at = sr.locate(o, sr.sample(el0(0, c['guest_rip'], sr.BLOCK, USER64, at=s[0], frame=0x55)))
            self.assertEqual(at['word'], obj[c['code_offset'] + s[0]:][:4])
            if s[5] in sample_roles.ACCESS_KINDS: self.assertTrue(sample_roles.is_memory(int.from_bytes(at['word'], 'little')))
        # Past the code region (the tail admission strips, still inside code_size): no word, a fault to count.
        for off in (c['code_region_end'], c['code_size'] - 4):
            at = sr.locate(o, sr.sample(el0(0, c['guest_rip'], sr.BLOCK, USER64, at=off, frame=0x55)))
            self.assertEqual((at['word'], at['beyond']), (None, True), off)
        a = sr.Attribution(o)
        roles = sample_roles.roles(code)
        info = a.info((c['guest_rip'], 0x55, USER64))
        self.assertEqual(info['roles'], roles)
        self.assertEqual(sample_roles.role_at(info['roles'], c['code_region_end']), 'beyond')

    def test_a_sample_past_the_code_region_is_a_problem(self):
        o, obj, c = self.fixture_objects()
        rip = c['guest_rip']
        recs = [header(1), hostrec(sr.CLOCK, T0, value1=1), hostrec(sr.TOGGLE, T0, value1=0, value2=1, value3=0, value4=sr.RUN),
                hostrec(sr.CLOCK, T0 + S, value1=1, value2=1), hostrec(sr.VCPU, T0 + S, value1=0, value2=3, value3=0, value4=3),
                el0(T0 + 1, rip, sr.BLOCK, USER64, at=4, frame=0x55), el0(T0 + 2, rip, sr.BLOCK, USER64, at=-4, frame=0x55),
                el0(T0 + 3, rip, sr.BLOCK, USER64, at=c['code_region_end'], frame=0x55),
                *counts(0, rings=3, written=3, drained=3), *run_counts()]
        write_samples(self.d / 'case.samples', recs)
        problems, r = self.report()
        self.assertEqual(problems, ['1 block samples past their object\'s code region'])
        a = r['phases'][0]['attribution']
        self.assertEqual((a['beyond'], a['all_roles']['entry'], a['block_samples']), (1, 1, 3))
        self.assertEqual(a['all_roles'].get('stack_check'), 1)           # push-run's first word: its stack check

    def test_the_report_gives_roles_per_class_and_the_top_blocks_words(self):
        recs, markers, objects = standard_run()
        problems, r = self.report(markers=self.write(recs, markers, objects))
        self.assertEqual(problems, [])
        whole = r['phases'][0]
        a = whole['attribution']
        self.assertEqual(a['block_samples'], 8)
        self.assertEqual(set(a['roles']), {'user/64', 'kernel/64', 'user/32'})
        self.assertEqual(a['roles']['user/32'], {'none': 1})                # B has no object
        self.assertEqual(a['roles']['kernel/64']['entry'], 1)              # K's -4 sample
        self.assertEqual(a['all_roles']['probe'], 1)
        top = whole['blocks']['top'][0]
        self.assertEqual([w[0] for w in top['words']], [0, 4, 8, 12])
        self.assertEqual(top['words'][-1][1:], ['probe', 1])
        self.assertEqual(top['symbol'], 'user:?')                          # no image
        md = (self.d / 'case.report.md').read_text()
        for heading in ('## Roles and symbols by phase', '## Block samples by role (whole run)', '## Top blocks\' words',
                        '## Top 30 symbols: all'):
            self.assertIn(heading, md)
        self.assertTrue(any('no --image' in n for n in r['notes']))

    def test_rep_strings_are_split_by_form_mode_and_privilege(self):
        """The bulk REP plan's task 0 (a): a phase's REP share by form, as % of its EL0 samples."""
        class T: words = {}
        info = lambda forms: dict(roles=['fex_mem', 'fex_alu', 'fex_mem'], insns=[0, 0, 2], exits={}, delta=0, guest=None,
                                  wide=False, rep={g: f.split()[1][:4] for g, f in forms.items()}, rep_form=forms)
        a = sr.Attribution(None)
        U, K, B = (0x40_1000, 0x55, USER64), (0xffff_ffff_8100_0000, 0x66, KERNEL64), (0x0804_8000, 0x77, USER32)
        a.info_of = {U: info({0: 'rep movsb', 2: 'rep movsq'}), K: info({0: 'rep movsq'}), B: info({0: 'rep movsd'})}
        T.words = {(U, 0, False): 3, (U, 4, False): 2, (U, 8, False): 1, (K, 0, False): 2, (B, 4, False): 2}
        s = a.phase(T, 20, set())['shares']
        self.assertEqual(s['rep_forms'], {'rep movsb 64-bit user': 25.0, 'rep movsq 64-bit kernel': 10.0,
                                          'rep movsd 32-bit user': 10.0, 'rep movsq 64-bit user': 5.0})
        self.assertEqual((s['rep_movs'], s['rep_string']), (50.0, 50.0))

    def test_the_skid_correction_charges_the_next_word_executed(self):
        import test_sample_roles as tr
        import sample_roles
        A, B = 0x40_1000, 0x40_2000
        a = tr.code_object([tr.LDR_X20_X11_8, tr.MOV_X6_X20, tr.MOVZ_W0, tr.MOVK_W0, tr.SVC_LINK], sites=[tr.site(4, sample_roles.LOAD)],
                           relocs=[tr.reloc(12, sample_roles.RELOC_GUEST_RIP_MOVE, 0, value=B)], rip=A)
        b = tr.code_object([0x8b14_016b, tr.LDR_X20_X11_8], rip=B)          # B's first body word: an add
        objects = [obj_record(A, 0x55_000, USER64, a, b'\x90' * 16), obj_record(B, 0x77_000, USER64, b, b'\x90' * 16)]
        samples = [el0(T0 + i, A, sr.BLOCK, USER64, at=at, frame=0x55) for i, at in enumerate((4, 4, 8, 12, 16))]
        recs = [header(1), hostrec(sr.CLOCK, T0, value1=1), hostrec(sr.TOGGLE, T0, value1=0, value2=1, value3=0, value4=sr.RUN),
                hostrec(sr.CLOCK, T0 + S, value1=1, value2=1), hostrec(sr.VCPU, T0 + S, value1=0, value2=5, value3=0, value4=5),
                *samples, *counts(0, rings=5, written=5, drained=5), *run_counts()]
        self.write(recs, None, objects)
        problems, r = self.report()
        self.assertEqual(problems, [])
        at = r['phases'][0]['attribution']
        self.assertEqual(at['all_roles'], {'fex_mem': 2, 'exit_literal': 2, 'load_move': 1})
        # The load's samples go to its move, the move's to the exit, the exit's (both words) to B's first body word.
        self.assertEqual(at['skid_roles'], {'load_move': 2, 'fex_alu': 2, 'exit_literal': 1})
        self.assertEqual((at['shares']['fex_mem'], at['shares']['skid']['fex_mem']), (40.0, 0.0))
        self.assertEqual(at['shares']['skid']['exit_samples_unresolved'], 0)
        # Without the target's object the exit's samples stay, counted unresolved.
        self.write(recs, None, objects[:1])
        _, r = self.report()
        self.assertEqual(r['phases'][0]['attribution']['shares']['skid']['exit_samples_unresolved'], 2)
        self.assertIn('### One-word skid correction', (self.d / 'case.report.md').read_text())

    def test_user_blocks_found_ambiguous_short_and_twice(self):
        import test_sample_symbols as ts
        import sample_symbols
        body = ts.Image.BLOCK
        pad = lambda b, n: b + b'\xc3' * (n - len(b))
        text = pad(pad(pad(b'\xcc' * 0x80 + body, 0x200) + body, 0x1080) + body, 0x1100)   # at 0x80 twice (pages 0, 1), at 0x200
        files = [('usr/bin/prog', ts.elf64(0x40_1000, text, [(0x40_1080, 0x20, 'work')]), 0o100755),
                 ('usr/bin/other', ts.elf64(0x40_1000, b'\xcc' * 0x200 + body, []), 0o100755),
                 ('usr/bin/lib', ts.elf64(0x40_1000, b'\xcc' * 0x300 + body[:12] + b'\x90\x90', []), 0o100755)]
        (self.d / 'i.cpio').write_bytes(ts.cpio(files))
        image = sample_symbols.Image(self.d / 'i.cpio')
        obj = code_object(0, b'\0' * 8)
        U = [(0x5555_0080, 0x60), (0x5555_0200, 0x61), (0x5555_0300, 0x62), (0x5555_0400, 0x63)]
        guests = [body, body, body[:12] + b'\x90\x90', body[:5]]
        objects = [obj_record(rip, frame << 12, USER64, obj, g) for (rip, frame), g in zip(U, guests)]
        write_objects(self.d / 'case.objects', objects)
        a = sr.Attribution(sr.Objects(self.d / 'case.objects'), image=image)
        keys = [(rip, frame, USER64) for rip, frame in U]
        a.match_user(keys)
        names = [a.symbol(k, a.info(k), 4) for k in keys]
        self.assertEqual(names, ['prog:work', 'ambiguous:other|prog', 'lib:?', 'user:short'])
        self.assertEqual((a.looked_up, a.matched, a.ambiguous, a.multi_in_file, a.short), (3, 2, 1, 1, 1))

    def test_the_booted_kernel_must_be_the_symbols_kernel(self):
        import test_sample_symbols as ts
        import sample_symbols
        k = sample_symbols.Kallsyms(ts.kallsyms_blob(ts.SYMS))
        (self.d / 'bz').write_bytes(b'kernel one')
        k.bzimage_sha256 = sample_symbols.sha256(self.d / 'bz')
        k.write_table(self.d / 'k.txt')
        recs, markers, objects = standard_run()
        self.write(recs, markers, objects)
        argv = ['--profile', str(self.name), '--kernel-symbols', str(self.d / 'k.txt'), '--boots', str(self.d / 'bz')]
        self.assertEqual(sr.main(argv), 0)
        (self.d / 'bz').write_bytes(b'kernel two')
        self.assertEqual(sr.main(argv), 1)

    def test_kernel_symbols_take_the_slide_from_the_markers(self):
        import test_sample_symbols as ts
        import sample_symbols
        k = sample_symbols.Kallsyms(ts.kallsyms_blob(ts.SYMS))
        recs, markers, objects = standard_run()
        slide = 0x40_0000
        K = 0xffff_ffff_8100_0040
        # Move K to clear_page_erms + 0x10 under a 4 MiB slide.
        new_k = ts.TEXT + 0x110 + slide
        recs = [(r[0], new_k & ((1 << 48) - 1) | r[1] & ~((1 << 48) - 1), r[2], r[3])
                if r[2] >> 60 == sr.EL0 and r[1] & ((1 << 48) - 1) == K & ((1 << 48) - 1) else r for r in recs]
        objects[1] = obj_record(0x40_5000 | (new_k & 0xfff), 0x1000_000, KERNEL64,
                                code_object(new_k, b'\xaa' * 8 + b'\xbb' * 4), b'\xcc' * 12)
        mk = self.write(recs, markers + [(1, f'NATEMU-BENCH-KTEXT {ts.TEXT + slide:x}')], objects)
        problems = sr.report_one(self.name, markers=mk, kernel=k)
        r = json.loads((self.d / 'case.report.json').read_text())
        self.assertEqual(problems, [])
        syms = {x['symbol']: x['samples'] for x in r['phases'][0]['attribution']['symbols']}
        self.assertEqual(syms.get('kernel:clear_page_erms'), 2)
        self.assertEqual(r['phases'][0]['attribution']['kernel_copy_clear'], {'clear_page_erms': 2})
        # Without the slide the same samples name nothing.
        mk.write_text(mk.read_text().replace(f'NATEMU-BENCH-KTEXT {ts.TEXT + slide:x}', f'NATEMU-BENCH-KTEXT {ts.TEXT:x}'))
        sr.report_one(self.name, markers=mk, kernel=k)
        r = json.loads((self.d / 'case.report.json').read_text())
        self.assertNotIn('kernel:clear_page_erms', {x['symbol'] for x in r['phases'][0]['attribution']['symbols']})


class Markers(unittest.TestCase):
    def test_io5_steps_end_at_their_line(self):
        w = sr.marker_windows([(100 * 10 ** 9, 'IO5 step configure ok 60.5 start=1.0'), (200 * 10 ** 9, 'IO5 step make ok 99 start=2'),
                               (210 * 10 ** 9, 'IO5 value system running')], 300 * 10 ** 9)
        self.assertEqual(w, [('step/configure', int(39.5 * 10 ** 9), 100 * 10 ** 9), ('step/make', 101 * 10 ** 9, 200 * 10 ** 9)])

    def test_phases_overlap(self):
        p = sr.Phases([('all', 0, 100), ('a', 10, 20), ('b', 15, 30), ('empty', 5, 5)])
        self.assertEqual([p.at(t) for t in (0, 10, 15, 20, 29, 30, 99, 100)], [(0,), (0, 1), (0, 1, 2), (0, 2), (0, 2), (0,), (0,), ()])

    def test_buckets_between_snapshots(self):
        names = ['start', 'guest', 'halted']
        snap = [(0, [0, 0, 0, 0, 0, 0]), (10, [1, 5, 0, 0, 2, 7]), (20, [1, 9, 4, 0, 6, 7])]
        self.assertEqual(sr.buckets_between(snap, 10, 25, names), dict(start=0, guest=8e-9, halted=4e-9))
        self.assertIsNone(sr.buckets_between(snap, 20, 25, names))
        self.assertEqual(sr.buckets_between(snap, -5, 10, names), dict(start=1e-9, guest=7e-9, halted=7e-9))   # from 0 before the first
        self.assertIsNone(sr.buckets_between(snap, -9, -5, names))
        self.assertEqual(sr.bucket_names()[:4], ['start', 'guest', 'host', 'halted'])


class Command(Base):
    def test_the_command_runs_first_and_its_failure_is_the_exit_status(self):
        recs, markers, objects = standard_run()
        m = self.write(recs, markers, objects)
        self.assertEqual(sr.main(['--profile', str(self.name), '--markers', str(m), '--', sys.executable, '-c', 'exit(3)']), 3)
        self.assertTrue((self.d / 'case.report.json').exists())
        (self.d / 'case.report.json').unlink()
        self.assertEqual(sr.main(['--profile', str(self.name), '--', sys.executable, '-c', 'exit(0)']), 0)
        self.assertTrue((self.d / 'case.report.md').exists())
        write_samples(self.d / 'case.samples', [r for r in recs if r[2] >> 60 != sr.COUNT])
        self.assertEqual(sr.main(['--profile', str(self.name), '--', sys.executable, '-c', 'exit(0)']), 1)



class Cost(unittest.TestCase):
    """run_sampler_cost's verdict: within the rounds' spread plus 2%, and the guest share within 2 points."""

    def rows(self, off, on, share_off=(50, 50, 50), share_on=(50, 50, 50)):
        out = []
        for k in range(3):
            for m, w, g in (('off', off[k], share_off[k]), ('on', on[k], share_on[k])):
                out.append(dict(config='build-1', mode=m, round=k + 1, wall_s=w, wall2_s=w, guest_share=dict(run1=g, run2=g)))
        return out

    def test_within_the_spread_plus_two_percent(self):
        import run_sampler_cost as rc
        ok, c = rc.compare(self.rows([10.0, 10.2, 10.1], [10.4, 10.3, 10.5]))   # spread 0.2, bound 0.402, diff 0.3
        self.assertTrue(ok, c)
        self.assertEqual((c['build-1/run1']['wall_bound'], c['build-1/run1']['wall_diff']), (0.402, 0.3))

    def test_a_wall_beyond_it_fails(self):
        import run_sampler_cost as rc
        ok, c = rc.compare(self.rows([10.0, 10.2, 10.1], [10.6, 10.55, 10.5]))  # diff 0.45
        self.assertFalse(ok)
        self.assertIn('wall differs by 0.450 s > 0.402 s', c['build-1/run2']['why'])

    def test_a_guest_share_moved_by_more_than_two_points_fails(self):
        import run_sampler_cost as rc
        self.assertTrue(rc.compare(self.rows([10] * 3, [10] * 3, share_on=(51, 50.9, 51)))[0])
        self.assertTrue(rc.compare(self.rows([10] * 3, [10] * 3, share_on=(48.9, 48.8, 49)))[0])   # 1.1: inside 2
        ok, c = rc.compare(self.rows([10] * 3, [10] * 3, share_on=(47.9, 47.8, 48)))
        self.assertFalse(ok)
        self.assertEqual(c['build-1/run1']['why'], 'guest share differs by 2.10 points > 2.0')

    def test_a_missing_value_fails(self):
        import run_sampler_cost as rc
        rows = self.rows([10] * 3, [10] * 3); rows[1]['wall_s'] = None; rows[2]['guest_share'] = {}
        ok, c = rc.compare(rows)
        self.assertFalse(ok)
        self.assertEqual((c['build-1/run1']['why'], c['build-1/run2']['why']), ('a wall time is missing', 'a guest share is missing'))


if __name__ == '__main__':
    unittest.main()
