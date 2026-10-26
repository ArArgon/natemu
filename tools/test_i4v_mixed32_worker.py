import struct
import unittest
import i4v_mixed32_worker as worker

class MixedWorkerChecks(unittest.TestCase):
    def test_reply_retains_owner_mode_source_and_physical_fetch(self):
        for owner in range(4):
            for mode in (0,1):
                for generation in (1,2):
                    p=worker.request(37,owner,7,generation,mode+1,mode,3)
                    body=bytes.fromhex(worker.CASES[worker.VARIANTS[generation]]['body'])
                    reply=struct.unpack('<16Q',worker.make_reply(p,body))
                    self.assertEqual(reply[:13],(worker.DELIVER,*p[1:13]))
                    self.assertEqual(struct.pack('<3Q',*reply[13:]),body+bytes(4))

    def test_bad_source_mode_address_padding_and_body_rejected(self):
        p=list(worker.request(37,1,1,1,2,1,3))
        for index in [0,1,5,7,9,10,12,13,14,15]:
            bad=p.copy();bad[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        for index,value in [(2,0),(3,4),(4,0),(5,3),(6,0),(8,2),(11,0)]:
            bad=p.copy();bad[index]=value
            with self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        with self.assertRaises(ValueError):worker.make_reply(p,bytes(20))

if __name__=='__main__':unittest.main()
