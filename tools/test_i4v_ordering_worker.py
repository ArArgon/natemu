import struct
import unittest
from i4v_ordering_worker import CASES, NAMES, make_reply, parse_request, request

class OrderingWorkerTests(unittest.TestCase):
    def test_all_admitted_packets_and_mutations(self):
        for profile in range(13):
            for n in range(1,5):
                if profile<7 and n!=(2,2,2,3,4,2,2)[profile]: continue
                for rotation in range(n):
                    for owner in range(n):
                        packet=request(37,n,profile,rotation,19,owner)
                        self.assertEqual(parse_request(struct.pack('<16Q',*packet)),packet)
                        body=bytes.fromhex(CASES['fex_order_'+NAMES[packet[15]]]['body'])
                        expected=make_reply(packet,body)
                        for i in range(16):
                            bad=list(packet);bad[i]^=1
                            try: changed=parse_request(struct.pack('<16Q',*bad))
                            except (ValueError,AssertionError): continue
                            self.assertNotEqual(make_reply(changed,bytes.fromhex(CASES['fex_order_'+NAMES[changed[15]]]['body'])),expected)
                        corrupted=bytes([body[0]^1])+body[1:]
                        with self.assertRaises(ValueError):make_reply(packet,corrupted)
    def test_both_source_words_are_bound(self):
        packet=request(37,2,1,0,19,0)
        self.assertNotEqual(packet[10],0)
        for i in (9,10,11):
            bad=list(packet);bad[i]^=1
            with self.assertRaises(ValueError):parse_request(struct.pack('<16Q',*bad))
    def test_participant_count_and_seed_rejected(self):
        for values in [(37,1,0,0,1,0),(37,2,0,0,0,0),(37,2,0,2,1,0),(37,2,0,0,1,2)]:
            with self.assertRaises(ValueError):request(*values)

if __name__=='__main__':unittest.main()
