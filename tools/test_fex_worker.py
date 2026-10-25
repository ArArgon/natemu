import json
from pathlib import Path
import struct
import tempfile
import unittest
import fex_worker as worker


def packet():
    p = [11,2,1,1,1,9,17,*struct.unpack('<3Q', worker.XOR+bytes(7)),1,0,0x100200,1,0,1]
    return p


class WorkerProtocolTests(unittest.TestCase):
    def test_request_rejects_profile_source_and_padding(self):
        p = packet()
        self.assertEqual(worker.parse_request(struct.pack('<16Q', *p))[1], worker.XOR)
        for index, value in ((0,0),(1,1),(2,0),(5,0),(6,16),(7,0),(9,0x100),(10,2),(11,1),(12,0)):
            bad = p.copy();bad[index] = value
            with self.assertRaises(ValueError):
                worker.parse_request(struct.pack('<16Q', *bad))
        for length in (0,127,129):
            with self.assertRaises(ValueError):
                worker.parse_request(bytes(length))

    def test_export_cannot_relabel_xor_as_or(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);(root/'outputs').mkdir()
            body = bytes.fromhex('101e316e')+worker.TAIL
            raw = bytes.fromhex('70000000e0ffff10800300f9')+body+bytes.fromhex('0f420000')+bytes(100)
            (root/'outputs/001.native').write_bytes(raw)
            (root/'outputs/001.relocations').write_bytes(bytes(144))
            entry = {'index':1,'bytes':160,'code_offset':4,'code_bytes':108,'guest_instructions':5,'relocation_bytes':144}
            log = 'NATEMU_FEATURES sve128=0 sve256=0 rcpc=0 imm9=0\nNATEMU_COMPLETED tests=1 result=0\n'
            log += 'NATEMU_OBJECT '+json.dumps({'index':0})+'\nNATEMU_OBJECT '+json.dumps(entry)
            self.assertEqual(worker.extract(root,log,worker.XOR)[0],body)
            with self.assertRaises(ValueError):
                worker.extract(root,log,worker.OR)
            (root/'outputs/001.relocations').write_bytes(bytes(143))
            with self.assertRaises(ValueError):
                worker.extract(root,log,worker.XOR)


if __name__ == '__main__':
    unittest.main()
