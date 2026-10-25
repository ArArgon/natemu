"""Worker protocol controls; real compiler bytes are retained parser testdata."""
import copy
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import i3_worker as worker

DATA = Path(__file__).with_name('testdata')


def packet(source=worker.ALIAS):
    return [11, 3, 1, 2, 3, 9, len(source), *struct.unpack('<3Q', source.ljust(24, b'\0')),
            2, 0, 0x102000, 1, 0, 1]


def maps(name='alias'):
    source = (DATA / f'{name}.x86').read_bytes()
    return worker.parse((DATA / f'{name}.txt').read_text(), (DATA / f'{name}.native').read_bytes(), 1, source), source


class WorkerTests(unittest.TestCase):
    def test_request_admission(self):
        for source in (worker.ALIAS, worker.FP_ALIAS):
            p = packet(source)
            self.assertEqual(worker.parse_request(struct.pack('<16Q', *p)), (tuple(p), source))
            for index, value in ((0, 0), (1, 2), (2, 0), (3, 0), (4, 0), (5, 0),
                                 (6, 15), (7, 0), (9, 1 << 63), (10, 1), (11, 1), (12, 0)):
                with self.subTest(source=source.hex(), index=index):
                    bad = p.copy()
                    bad[index] = value
                    with self.assertRaises(ValueError):
                        worker.parse_request(struct.pack('<16Q', *bad))
        for length in (0, 127, 129):
            with self.assertRaises(ValueError):
                worker.parse_request(bytes(length))

    def test_exact_wire_layout(self):
        for name in ('alias', 'fp_alias'):
            mapped, source = maps(name)
            raw = worker.serialize_export(mapped, source)
            self.assertEqual(len(raw), 1168)
            self.assertEqual(struct.unpack_from('<3Q', raw), (308, len(source), 768))
            self.assertEqual(raw[24:48], source.ljust(24, b'\0'))
            self.assertEqual(raw[48:560], mapped['body'].ljust(512, b'\0'))
            for index, site in enumerate(mapped['sites']):
                offset = 560 + index * 304
                self.assertEqual(struct.unpack_from('<3Q', raw, offset),
                                 (site['native_offset'], site['guest_offset'], index))
                roots = struct.unpack_from('<35Q', raw, offset + 24)
                self.assertEqual(roots[0], 3 | (384 * index) << 8)  # old/new RAX, actual spills
                self.assertEqual(roots[1], 1 | 21 << 8)
                self.assertEqual(roots[16], 2 | 16 << 8)
                self.assertEqual(roots[34], 1 | 20 << 8)

    def test_unrepresentable_maps_rejected(self):
        mapped, source = maps()
        mutations = [lambda m: m.update(source_hex=worker.FP_ALIAS.hex()),
                     lambda m: m.update(body=bytes(516)),
                     lambda m: m.update(spill_bytes=0),
                     lambda m: m['sites'][0].update(carry_inverted=True),
                     lambda m: m['sites'][0].update(bytes=16),
                     lambda m: m['sites'][0].update(fpsr_authority='stale-bank'),
                     lambda m: m['sites'][0].update(native_continuation=0),
                     lambda m: m['sites'][0].update(guest_offset=8),
                     lambda m: m['sites'][0]['roots'][0].update(offset=800),
                     lambda m: m['sites'][0]['roots'][0].update(initializing_native_offset=148),
                     lambda m: m['sites'][0]['roots'][1].update(register=31),
                     lambda m: m['sites'][0]['roots'][16].update(kind='gpr')]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                bad = copy.deepcopy(mapped)
                mutate(bad)
                with self.assertRaises(ValueError):
                    worker.serialize_export(bad, source)

    def test_timeout_removes_daemon_container_and_retains_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'request.bin').write_bytes(struct.pack('<16Q', *packet()))
            producer = directory / 'producer'
            producer.write_bytes(b'test-only producer')
            calls = []

            def run(argv, **kwargs):
                calls.append(argv)
                if argv[1] == 'start':
                    raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
                return subprocess.CompletedProcess(argv, 0, 'container\n', '')

            with patch.object(worker, 'PRODUCER_SHA', worker.sha(producer.read_bytes())), \
                 patch.object(worker.subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    worker.compile_request(directory, producer)
            self.assertEqual([argv[1] for argv in calls], ['create', 'start', 'rm'])
            self.assertEqual(calls[-1][2:4], ['--force', '--volumes'])
            record = worker.json.loads((directory / 'compiler.json').read_text())
            self.assertEqual(record['status'], 'failed')
            self.assertIn('TimeoutExpired', record['error'])
            self.assertFalse((directory / 'export.bin').exists())

    def test_verifier_binds_artifacts_and_command_lifecycle(self):
        # Synthetic transport only: the requested native object/log are actual
        # retained output; opaque calibration/relocation bytes are test doubles.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'request-000'
            directory.mkdir()
            p = packet()
            (directory / 'request.bin').write_bytes(struct.pack('<16Q', *p))
            producer = root / 'producer'
            producer.write_bytes(b'test-only producer')
            state = {'Running': False, 'OOMKilled': False, 'ExitCode': 0}

            def run(argv, **kwargs):
                stdout = 'container\n'
                if argv[1] == 'start':
                    log = (DATA / 'alias.txt').read_bytes()
                    kwargs['stdout'].write(log)
                    objects = [worker.json.loads(line.removeprefix('NATEMU_OBJECT '))
                               for line in log.decode().splitlines() if line.startswith('NATEMU_OBJECT ')]
                    for obj in objects:
                        index = obj['index']
                        raw = (DATA / 'alias.native').read_bytes() if index else bytes(obj['bytes'])
                        (directory / f'outputs/{index:03}.native').write_bytes(raw)
                        (directory / f'outputs/{index:03}.relocations').write_bytes(bytes(obj['relocation_bytes']))
                    (directory / 'normalized.json').write_text('{}')
                if argv[1] == 'inspect':
                    stdout = worker.json.dumps([{'State': state}])
                return subprocess.CompletedProcess(argv, 0, stdout, '')

            with patch.object(worker, 'PRODUCER_SHA', worker.sha(producer.read_bytes())), \
                 patch.object(worker.subprocess, 'run', side_effect=run):
                worker.compile_request(directory, producer)
                worker.verify_compilations(root, [p])
                for name in ('request.bin', 'input.x86', 'input.bin', 'reply-request.bin',
                             'export.bin', 'body.arm64', 'maps.json', 'producer.log',
                             'outputs/001.native', 'outputs/001.relocations', 'normalized.json'):
                    with self.subTest(artifact=name):
                        path = directory / name
                        original = path.read_bytes()
                        path.write_bytes(original + b'\0')
                        with self.assertRaises((ValueError, UnicodeError)):
                            worker.verify_compilations(root, [p])
                        path.write_bytes(original)
                path = directory / 'compiler.json'
                original = path.read_text()
                for change in ('isolation', 'cleanup', 'state'):
                    with self.subTest(record=change):
                        record = worker.json.loads(original)
                        if change == 'isolation':
                            record['commands'][0]['argv'].remove('--network=none')
                        elif change == 'cleanup':
                            record['commands'].pop()
                        else:
                            record['container_state']['Running'] = True
                        path.write_text(worker.json.dumps(record))
                        with self.assertRaises(ValueError):
                            worker.verify_compilations(root, [p])
                        path.write_text(original)
                changed = p.copy()
                changed[2] += 1
                with self.assertRaises(ValueError):
                    worker.verify_compilations(root, [changed])
                worker.verify_compilations(root, [p])


if __name__ == '__main__':
    unittest.main()
