"""Structural checks of the fexc native compiler output.

Run: python3 -m unittest tools/fexc/test_fexc.py   (or python3 tools/fexc/test_fexc.py)
The binary is taken from $FEXC (default: the retained C1b build); the whole file
is skipped when it is missing. Only structure is asserted, except for one
old-vs-native identity check against a pinned I4-V producer body.
"""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

FEXC = os.environ.get('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series5/obj/fexc')
HERE = Path(__file__).resolve().parent

# (name, x86 hex, rip, bitness, decoded instructions, expected end reason)
INPUTS = [
    ('xor17', '660fefc14801d1488b030f94c2f30f7f07', 0x102000, 64, 5, 'end_of_input'),
    ('xor17-high', '660fefc14801d1488b030f94c2f30f7f07', 0xffff800000102000, 64, 5, 'end_of_input'),
    ('stack20', '68bbbcbdbe6668525166686261488b42f88b5af4', 0x102000, 64, 5, 'end_of_input'),
    ('moffs37-32', 'ba00001000b8420000008902b8ffffffffa10000100089c2b8ffffffff66a10000100089c1', 0x102000, 32, 9, 'end_of_input'),
    ('rip-relative', '488d0500000000', 0x102000, 64, 1, 'end_of_input'),
    ('rip-relative-high', '488d0500000000', 0xffff800000102000, 64, 1, 'end_of_input'),
    ('fence', '0f58c14801d10faee89f0f90c048890f66894708f30f7f47100fae5f20', 0x100000, 64, 9, 'end_of_input'),
    ('jmp', '90e9fbffffff', 0x102000, 64, 2, 'block_ending_instruction'),
]


def run(hexs, rip, bitness, *extra):
    r = subprocess.run([FEXC, '--hex', hexs, '--rip', hex(rip), '--bitness', str(bitness), *extra],
                       capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout), r.returncode


def body(d):
    code = d['code']
    raw = bytes.fromhex(code['hex'])
    entry = code['layout']['entries'][0]
    return raw[entry['body_native_offset']:code['layout']['end_marker_offset']]


@unittest.skipUnless(os.access(FEXC, os.X_OK), f'fexc binary not built or not executable: {FEXC} (set $FEXC or run tools/fexc/build.sh)')
class FexcStructure(unittest.TestCase):
    def check(self, name, hexs, rip, bitness, count, reason):
        d, rc = run(hexs, rip, bitness)
        self.assertEqual(rc, 0, name)
        self.assertEqual(d['format'], 'natemu-fexc-v2')
        self.assertEqual(d['status'], 'ok', name)
        code, layout = d['code'], d['code']['layout']
        raw = bytes.fromhex(code['hex'])
        self.assertTrue(raw, name)
        self.assertEqual(len(raw), code['size'])
        self.assertEqual(code['size'] % code['alignment'], 0)
        # layout offsets lie inside the block and in order
        begin, end = layout['code_region_begin'], layout['code_region_end']
        self.assertLessEqual(layout['header']['offset'] + layout['header']['size'], begin)
        self.assertLessEqual(begin, end)
        self.assertLessEqual(end, code['size'])
        entries = layout['entries']
        self.assertTrue(entries)
        for e in entries:
            self.assertTrue(begin <= e['entry_native_offset'] < e['body_native_offset'] <= end, name)
        self.assertTrue(begin <= layout['end_marker_offset'] < end)
        self.assertGreaterEqual(layout['end_marker_offset'], entries[0]['body_native_offset'])
        self.assertEqual(raw[layout['end_marker_offset']:layout['end_marker_offset'] + 4], bytes.fromhex('0f420000'))  # udf #0x420f
        tail = layout['tail']
        self.assertEqual(tail['offset'], end)
        self.assertLessEqual(tail['offset'] + tail['size'], code['size'])
        # relocations lie inside the block, do not overlap, and their slots are zeroed
        spans = []
        for r in code['relocations']:
            size = r['size'] or 4  # guest_rip_move has no fixed size; at least one instruction
            self.assertTrue(0 <= r['offset'] and r['offset'] + size <= code['size'], (name, r))
            spans.append((r['offset'], r['offset'] + size))
            if r['size']:
                self.assertEqual(raw[r['offset']:r['offset'] + r['size']], bytes(r['size']), (name, r))
                self.assertIn('emitted_value', r)
            if r['kind'] == 'guest_rip_literal':
                self.assertEqual(r['size'], 8)
        spans.sort()
        for a, b in zip(spans, spans[1:]):
            self.assertLessEqual(a[1], b[0], (name, 'overlapping relocations'))
        # PC table: native offsets monotone and inside the code region, guest offsets inside the input
        pcs = d['pc_table']['entries']
        self.assertTrue(pcs)
        native = [p['native_offset'] for p in pcs]
        self.assertEqual(native, sorted(native))
        for p in pcs:
            self.assertTrue(begin <= p['native_offset'] < end, (name, p))
            self.assertTrue(0 <= p['guest_offset'] <= len(hexs) // 2, (name, p))  # == length: trailing exit entry
        # guest side
        g = d['guest']
        self.assertEqual(g['instructions_translated'], count, name)
        translated = [i for i in g['instructions'] if i['translated']]
        self.assertEqual(len(translated), count)
        self.assertEqual(sum(i['size'] for i in translated), g['bytes_translated'])
        self.assertEqual(g['end']['reason'], reason, name)
        self.assertEqual(int(g['end']['rip'], 16), rip + g['bytes_translated'])
        if reason == 'end_of_input':
            self.assertEqual(g['bytes_translated'], len(hexs) // 2)
        return d

    def test_structure(self):
        for case in INPUTS:
            with self.subTest(case[0]):
                self.check(*case)

    def test_max_instructions(self):
        d, rc = run(INPUTS[0][1], 0x102000, 64, '--max-insts', '2')
        self.assertEqual(rc, 0)
        self.assertEqual(d['guest']['instructions_translated'], 2)
        self.assertEqual(d['guest']['end']['reason'], 'max_instructions')

    def test_rip_moves_with_input(self):
        low = self.check(*INPUTS[0])
        high = self.check(*INPUTS[1])
        lit = lambda d: sorted(r['guest_rip'] for r in d['code']['relocations'] if r['kind'] == 'guest_rip_literal')
        self.assertEqual(lit(low), sorted(['0x102000', '0x102011']))
        self.assertEqual(lit(high), sorted(['0xffff800000102000', '0xffff800000102011']))

    def test_partial_decode_reports_failure(self):
        d, rc = run('48', 0x102000, 64)  # REX prefix alone
        self.assertEqual(rc, 1)
        self.assertEqual(d['status'], 'no_instructions_translated')
        self.assertEqual(d['guest']['instructions_translated'], 0)
        self.assertEqual(d['guest']['end']['reason'], 'partial_decode')

    def test_bad_arguments(self):
        r = subprocess.run([FEXC, '--hex', 'zz', '--rip', '0x1000'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)

    def test_old_producer_identity(self):
        """One pinned I4-V body (same FEX commit, TSO, no SVE/LRCPC, LSE on) must be reproduced exactly."""
        sys.path.insert(0, str(HERE.parent / 'i4v_producer'))
        try:
            from export import CASES
        finally:
            sys.path.pop(0)
        self.assertEqual(CASES['fex_order_sb']['bitness'], 64)
        # Site recording, the dense PC table and the feature profile must not change emitted code: every pinned body
        # with a recorded bitness, at two RIPs for the 64-bit ones.
        for name, case in CASES.items():
            if 'bitness' not in case:
                continue
            for rip in ((0x102000, 0xffff800000102000) if case['bitness'] == 64 else (0x1000,)):  # bodies are RIP independent
                with self.subTest(name, rip=hex(rip)):
                    d, rc = run(case['source'], rip, case['bitness'], '--max-insts', str(case['instructions']))
                    self.assertEqual(rc, 0)
                    self.assertEqual(body(d).hex(), case['body'])

    def test_sites(self):
        """Exact sites for hand-checked single instructions (x86 bytes assembled by hand; registers per the SRA map)."""
        def sites(hexs, n=1):
            d, rc = run(hexs, 0x102000, 64, '--max-insts', str(n))
            self.assertEqual(rc, 0)
            return d['sites']
        [s] = sites('8b06')  # mov eax, [rsi]
        self.assertEqual((s['kind'], s['width'], s['intent'], s['rmw'], s['segment'], s['base_reg'], s['index_reg'], s['writeback'],
                          s['guest_offset'], s['ordering'], s['nzcv'], s['completed_effects']),
                         ('load', 4, 'read', 'none', 'ds', 10, None, 'none', 0, 'acquire', 'pstate', 'unverified'))
        [s] = sites('8906')  # mov [rsi], eax
        self.assertEqual((s['kind'], s['width'], s['intent'], s['rmw'], s['data_regs'], s['ordering']), ('store', 4, 'write', 'none', [4], 'release'))
        ld, st = sites('0106')  # add [rsi], eax: read-modify-write
        self.assertEqual((ld['kind'], ld['rmw'], ld['intent'], st['kind'], st['rmw'], st['intent']), ('load', 'load', 'write', 'store', 'store', 'write'))
        self.assertEqual(ld['guest_offset'], st['guest_offset'])
        self.assertLess(ld['native_offset'], st['native_offset'])
        [s] = sites('50')  # push rax
        self.assertEqual((s['kind'], s['width'], s['segment'], s['base_reg'], s['writeback'], s['writeback_delta']), ('push', 8, 'ss', 8, 'pre', -8))
        [s] = sites('5b')  # pop rbx
        self.assertEqual((s['kind'], s['width'], s['segment'], s['base_reg'], s['writeback'], s['writeback_delta'], s['data_regs']), ('pop', 8, 'ss', 8, 'post', 8, [6]))
        [s] = sites('488b4508')  # mov rax, [rbp+8]: RBP-based operand uses SS
        self.assertEqual((s['kind'], s['width'], s['segment']), ('load', 8, 'ss'))
        self.assertEqual(sites('4801d8'), [])  # add rax, rbx: no memory

    def test_sites_in_structure(self):
        for name, hexs, rip, bitness, count, reason in INPUTS:
            with self.subTest(name):
                d = self.check(name, hexs, rip, bitness, count, reason)
                begin, end = d['code']['layout']['code_region_begin'], d['code']['layout']['code_region_end']
                offsets = [s['native_offset'] for s in d['sites']]
                self.assertEqual(offsets, sorted(set(offsets)))
                insn_offsets = {i['offset'] for i in d['guest']['instructions'] if i['translated']}
                for s in d['sites']:
                    self.assertTrue(begin <= s['native_offset'] < end and s['native_offset'] % 4 == 0)
                    self.assertIn(s['guest_offset'], insn_offsets)
                    self.assertTrue(s['located'])
                for e in d['pc_table']['entries']:
                    self.assertIn(e['cf_inverted'], (True, False, None))

    def test_config_option(self):
        # --config sets any FEX config option: X87ReducedPrecision changes the x87 code (fld/fadd), is echoed, and bad names are refused
        base, _ = run('d9c0d8c1', 0x102000, 64)
        red, rc = run('d9c0d8c1', 0x102000, 64, '--config', 'FEX_X87REDUCEDPRECISION=1')
        self.assertEqual(rc, 0)
        self.assertNotEqual(base['code']['hex'], red['code']['hex'])
        self.assertEqual(red['settings']['config'], {'FEX_X87REDUCEDPRECISION': '1'})
        self.assertNotIn('config', base['settings'])
        r = subprocess.run([FEXC, '--hex', '90', '--rip', '0x1000', '--config', 'NOSUCHOPTION=1'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_object_file(self):
        import struct, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            o = Path(tmp) / 'o.bin'
            d, rc = run('0106', 0x102000, 64, '--object', str(o))
            self.assertEqual(rc, 0)
            f = o.read_bytes()
            magic, version, hsize, total, bitness, rip = struct.unpack_from('<8sIIIIQ', f, 0)
            self.assertEqual((magic, version, hsize, total, bitness, rip), (b'NFXOBJ\0\x01', 3, 128, len(f), 64, 0x102000))
            sys.path.insert(0, str(HERE))
            import check_sites
            obj = check_sites.read_object(o)
            self.assertEqual(check_sites.object_vs_json(obj, d), [])   # profile, settings, end reason, feature profile, counts
            self.assertEqual((obj['profile'], obj['settings']), (0, 1 | 4 | 8))   # default profile: TSO, dense PC, end marker
            self.assertEqual(obj['pre_nzcv'], 0xFFFFFFFF)   # default profile: no frame-NZCV sites
            self.assertTrue(all(s[24] == 1 for s in obj['sites']))
            code_off, code_size, body, marker, region, spill = struct.unpack_from('<6I', f, 40)
            soff, scount = struct.unpack_from('<II', f, 64)
            self.assertEqual(f[code_off:code_off + code_size].hex(), d['code']['hex'])
            self.assertEqual(scount, len(d['sites']))
            self.assertEqual(struct.unpack_from('<I', f, soff)[0], d['sites'][0]['native_offset'])


if __name__ == '__main__':
    unittest.main()
