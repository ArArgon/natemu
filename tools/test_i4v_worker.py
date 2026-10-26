import struct
import unittest
import i4v_worker as worker

REQUEST=[worker.MISS,1,123,0,1,1,1,0xffff800000102000,64,3,0x038b48,0,0,1,0,0]

class WorkerBindingChecks(unittest.TestCase):
    def test_identity_echo_and_actual_body(self):
        request=worker.parse_request(struct.pack('<16Q',*REQUEST))
        reply=list(struct.unpack('<16Q',worker.make_reply(request,worker.BODY)))
        expected=REQUEST.copy();expected[0]=worker.COMPILED;expected[11]=8;expected[12]=0xd503201fc8dffcc4
        self.assertEqual(reply,expected)

    def test_every_fixed_request_field_rejected(self):
        for index in set(range(16))-{2,7}:
            request=REQUEST.copy();request[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):
                worker.parse_request(struct.pack('<16Q',*request))

    def test_zero_vm_noncanonical_rip_and_bad_body_rejected(self):
        for index,value in [(2,0),(7,0x800000102000),(7,0x10000)]:
            request=REQUEST.copy();request[index]=value
            with self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*request))
        with self.assertRaises(ValueError):worker.make_reply(REQUEST,bytes(8))

    def test_truncated_and_extended_packets_rejected(self):
        raw=struct.pack('<16Q',*REQUEST)
        for bad in [raw[:-1],raw+b'\0']:
            with self.assertRaises(ValueError):worker.parse_request(bad)

if __name__=='__main__':unittest.main()
