import struct
import unittest
import i4v_off_steps_worker as worker

class InstructionWorkerChecks(unittest.TestCase):
    def test_actual_instruction_eip_and_profile_routes_survive_reply(self):
        for profile in range(10):
            for step in range(9):
                if profile==5 or (profile in (3,4) and step>=4):
                    with self.assertRaises(ValueError):worker.request(73,profile,step)
                    continue
                packet=worker.request(73,profile,step)
                self.assertEqual(worker.parse_request(struct.pack('<16Q',*packet)),packet)
                body=bytes.fromhex(worker.CASES[worker.STEP_CASES[step]]['body'])
                reply=struct.unpack('<16Q',worker.make_reply(packet,body))
                self.assertEqual(reply[:9],(worker.DELIVER,*packet[1:9]))
                self.assertEqual(reply[11:],packet[11:])
                self.assertEqual(struct.pack('<2Q',*reply[9:11]),body.ljust(16,b'\0'))
                self.assertEqual(packet[7],0x2000+worker.OFFSETS[step])
                self.assertEqual(packet[12],(0x202000 if profile==1 else 0x302000)+worker.OFFSETS[step])

    def test_inconsistent_identity_source_address_or_body_is_rejected(self):
        packet=list(worker.request(73,0,7))
        for index in set(range(16))-{2,6}:
            bad=packet.copy();bad[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        for index,value in [(2,0),(6,0),(6,11),(7,0x10201d),(8,64),(9,packet[9]|(1<<63)),(15,9)]:
            bad=packet.copy();bad[index]=value
            with self.assertRaises(ValueError):worker.parse_request(struct.pack('<16Q',*bad))
        with self.assertRaises(ValueError):worker.make_reply(packet,bytes(16))

if __name__=='__main__':unittest.main()
