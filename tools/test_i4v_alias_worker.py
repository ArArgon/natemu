import struct
import unittest
import i4v_alias_worker as worker

def request(rip,source):
    return [11,3,1,1,1,37,17,*struct.unpack('<3Q',source.ljust(24,b'\0')),3,0,rip,1,0,1]

class AliasWorkerChecks(unittest.TestCase):
    def test_actual_rip_mode_and_profile_echo(self):
        for source,case in worker.SOURCES.items():
            for rip in (0x102000,0xffff800000102000):
                p=request(rip,source);body=bytes.fromhex(worker.CASES[case]['body'])
                q=struct.unpack('<16Q',worker.make_reply(p,body))
                self.assertEqual(q[:7],(2,3,*p[2:6],44))
                self.assertEqual(q[13:],(rip,64,3))
                self.assertEqual(struct.pack('<6Q',*q[7:13]),body+bytes(4))

    def test_legacy_noncanonical_wrongsource_padding_rejected(self):
        source=next(iter(worker.SOURCES));good=request(0xffff800000102000,source)
        for index,value in [(0,12),(1,4),(2,0),(3,0),(4,0),(5,0),(6,16),(10,1),(11,1),(12,0x800000102000),(13,0),(15,0),(9,1<<8)]:
            p=good.copy();p[index]=value
            with self.subTest(index=index),self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*p))
        with self.assertRaises(ValueError):worker.make_reply(good,bytes(44))

if __name__=='__main__':unittest.main()
