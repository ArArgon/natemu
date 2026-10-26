import struct
import unittest
import i4v_controls_worker as worker

class ControlWorkerChecks(unittest.TestCase):
    def test_all_admitted_sources_keep_complete_control_context(self):
        for source,case in worker.SOURCE_CASES.items():
            for mode in (0,1):
                p=worker.request(37,7,mode,0x102037,source,9)
                self.assertEqual(worker.parse_request(struct.pack('<16Q',*p)),p)
                body=bytes.fromhex(worker.CASES[case]['body'])
                reply=struct.unpack('<16Q',worker.make_reply(p,body))
                self.assertEqual(reply[:14],(worker.DELIVER,*p[1:14]))
                self.assertEqual(struct.pack('<2Q',*reply[14:]),body.ljust(16,b'\0'))

    def test_malformed_identity_source_padding_and_body_fail(self):
        p=list(worker.request(37,1,0,0x102000,b'\xb8\x11\x00\x01\x00',1))
        for index in [0,1,3,4,5,9,10,11,13,14,15]:
            bad=p.copy();bad[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        for index,value in [(2,0),(6,0),(8,2),(12,0),(7,0x101fff),(7,0x102ffc),(10,9)]:
            bad=p.copy();bad[index]=value
            with self.subTest(index=index,value=value),self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        bad=p.copy();bad[9]|=1<<63
        with self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        with self.assertRaises(ValueError):worker.make_reply(p,bytes(8))
        for size in (0,127,129):
            with self.assertRaises(ValueError):worker.parse_request(bytes(size))

    def test_numeric_mode_return_does_not_erase_generation(self):
        source=b'\xb8\x11\x00\x01\x00'
        old=worker.request(37,1,0,0x102000,source,2)
        new=worker.request(37,7,0,0x102000,source,11)
        body=bytes.fromhex(worker.CASES[worker.SOURCE_CASES[source]]['body'])
        self.assertNotEqual(worker.make_reply(old,body),worker.make_reply(new,body))
        self.assertEqual(old[7:12],new[7:12])

if __name__=='__main__':unittest.main()
