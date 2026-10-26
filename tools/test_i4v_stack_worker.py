import struct
import unittest
import i4v_stack_worker as worker

REQUEST=[worker.MISS,1,123,0,1,1,1,0xffff800000102000,64,20,*worker.SOURCE_WORDS,1,52,0]

class StackWorkerChecks(unittest.TestCase):
    def test_three_chunks_reconstruct_actual_body(self):
        body=b''
        for chunk,request in enumerate(worker.requests_for(REQUEST)):
            reply=struct.unpack('<16Q',worker.make_reply(request,worker.BODY))
            self.assertEqual(reply[:9],(worker.COMPILED,*request[1:9]))
            self.assertEqual((reply[9],reply[13],reply[14],reply[15]),(52,1,chunk,(24,24,4)[chunk]))
            part=struct.pack('<3Q',*reply[10:13]);self.assertEqual(part[reply[15]:],bytes(24-reply[15]))
            body+=part[:reply[15]]
        self.assertEqual(body,worker.BODY)

    def test_bad_request_identity_and_body_rejected(self):
        for index in set(range(16))-{2,3,4,5,6,7,13,15}:
            changed=REQUEST.copy();changed[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*changed))
        for index,value in [(2,0),(3,4),(4,0),(5,0),(6,0),(7,0x800000102000),(13,0),(15,3)]:
            changed=REQUEST.copy();changed[index]=value
            with self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*changed))
        with self.assertRaises(ValueError):worker.make_reply(REQUEST,bytes(52))

    def test_each_context_field_and_replacement_source_survive_reply(self):
        for source,case in worker.SOURCES.items():
            request=REQUEST.copy();request[3:7]=[3,7,2,11];request[13]=9
            request[10:13]=struct.unpack('<3Q',source.ljust(24,b'\0'))
            reply=struct.unpack('<16Q',worker.make_reply(request,bytes.fromhex(worker.CASES[case]['body'])))
            self.assertEqual(reply[2:9],tuple(request[2:9]))
            self.assertEqual(reply[13],9)

if __name__=='__main__':unittest.main()
