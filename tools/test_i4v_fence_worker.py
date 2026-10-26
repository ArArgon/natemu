import struct
import unittest
from i4v_fence_worker import CASES,CASE,request,parse_request,make_replies

class FenceWorkerTests(unittest.TestCase):
    def test_source_owner_and_all_four_body_chunks_bound(self):
        body=bytes.fromhex(CASES[CASE]['body'])
        for n in range(1,5):
            for case in range(5):
                for owner in range(n):
                    p=request(37,n,case,owner,0)
                    replies=make_replies(p,body)
                    self.assertEqual(b''.join(raw[72:104] for raw in replies),body+b'\0'*8)
                    for chunk in range(4):
                        q=request(37,n,case,owner,chunk)
                        self.assertEqual(parse_request(struct.pack('<16Q',*q)),q)
                        for i in range(16):
                            bad=list(q);bad[i]^=1
                            try:actual=parse_request(struct.pack('<16Q',*bad))
                            except ValueError:continue
                            self.assertNotEqual(actual,q)
                    for i in range(120):
                        bad=bytearray(body);bad[i]^=1
                        with self.assertRaises(ValueError):make_replies(p,bytes(bad))
    def test_nonzero_initial_chunk_and_padding_rejected(self):
        body=bytes.fromhex(CASES[CASE]['body'])
        with self.assertRaises(ValueError):make_replies(request(37,4,2,1,1),body)
        p=list(request(37,4,2,1,0));p[12]^=1<<8
        with self.assertRaises(ValueError):parse_request(struct.pack('<16Q',*p))
if __name__=='__main__':unittest.main()
