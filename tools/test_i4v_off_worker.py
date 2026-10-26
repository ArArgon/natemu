import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
import i4v_off_worker as worker

REQUEST=[worker.MISS,1,123,0,1,1,1,0x102000,32,*worker.SOURCE_WORDS,1,0]

class PagingOffWorkerChecks(unittest.TestCase):
    def test_all_source_bytes_and_reply_chunks_are_bound(self):
        for rip in (0x102000,0x102080):
            first=REQUEST.copy();first[7]=rip;body=b''
            for chunk,request in enumerate(worker.requests_for(first)):
                reply=struct.unpack('<16Q',worker.make_reply(request,worker.BODY))
                self.assertEqual(reply[:9],(worker.COMPILED,*request[1:9]))
                self.assertEqual(reply[12:],(chunk,(24,24,16)[chunk],1,64))
                part=struct.pack('<3Q',*reply[9:12]);self.assertEqual(part[reply[13]:],bytes(24-reply[13]))
                body+=part[:reply[13]]
            self.assertEqual(body,worker.BODY)

    def test_bad_identity_source_mode_padding_and_body_rejected(self):
        for index in set(range(16))-{2,15}:
            changed=REQUEST.copy();changed[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):
                worker.parse_request(struct.pack('<16Q',*changed))
        for index,value in [(2,0),(7,0x100000000),(8,64),(13,REQUEST[13]|(1<<63)),(15,3)]:
            changed=REQUEST.copy();changed[index]=value
            with self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*changed))
        with self.assertRaises(ValueError):worker.make_reply(REQUEST,bytes(64))

    def test_retained_compile_and_delivered_packets_are_independently_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copytree(Path(__file__).parent/'i4v_producer/testdata/load32',root/'compile')
            (root/'request.bin').write_bytes(struct.pack('<16Q',*REQUEST))
            program=worker.verify(root/'compile')
            requests=worker.requests_for(REQUEST);replies=[]
            for chunk,request in enumerate(requests):
                raw=worker.make_reply(request,worker.BODY);replies.append(list(struct.unpack('<16Q',raw)))
                (root/f'reply-{chunk}.bin').write_bytes(raw)
                (root/f'request-{chunk}.bin').write_bytes(struct.pack('<16Q',*request))
                (root/f'reply-delivered-{chunk}.bin').write_bytes(raw)
            record=dict(schema='natemu-i4v-paging-off-worker-v1',requests=[list(p) for p in requests],replies=replies,
                        producer_sha256=program['producer_sha256'],body_sha256=program['body_sha256'])
            (root/'worker.json').write_text(json.dumps(record))
            worker.verify_transfers(root,requests,replies)
            bad=REQUEST.copy();bad[7]=0x102080
            with self.assertRaises(ValueError):worker.verify_request(root,bad)
            (root/'reply-delivered-1.bin').write_bytes(bytes(128))
            with self.assertRaises(ValueError):worker.verify_transfers(root,requests,replies)

if __name__=='__main__':unittest.main()
