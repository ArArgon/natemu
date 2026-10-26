import json
from pathlib import Path
import struct
import unittest

try:
    from . import check_i4v_fence_gate as gate
except ImportError:
    import check_i4v_fence_gate as gate


def fixture(extra=()):
    """Wrap retained actual emitted snippets in a minimal ELF for corruption tests."""
    record = json.loads(Path(__file__).with_name('testdata').joinpath('i4v_fence_gate.json').read_text())
    segments = record['segments']+list(extra)
    data = bytearray(64 + 56 * len(segments))
    names = bytearray(b'\0')
    symbols = bytearray(24)
    for i, segment in enumerate(segments):
        code = bytes.fromhex(segment['hex'])
        offset = len(data)
        data += code
        struct.pack_into('<II6Q', data, 64 + 56 * i, 1, 5, offset, segment['address'],
                         segment['address'], len(code), len(code), 4)
        if segment['name']:
            name_at = len(names)
            names += segment['name'].encode() + b'\0'
            symbols += struct.pack('<IBBHQQ', name_at, 0x12, 0, 1,
                                   segment['address'], segment['symbol_size'])
    string_offset = len(data)
    data += names
    symbol_offset = len(data)
    data += symbols
    section_offset = len(data)
    data += bytes(64)
    data += struct.pack('<II4QII2Q', 0, 3, 0, 0, string_offset, len(names), 0, 0, 1, 0)
    data += struct.pack('<II4QII2Q', 0, 2, 0, 0, symbol_offset, len(symbols), 1, 0, 8, 24)
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, ident, 2, 183, 1, segments[0]['address'],
                     64, section_offset, 0, 64, 56, len(segments), 64, 3, 0)
    return data


def partial_fixture():
    """Synthetic generic gates plus unchanged retained allocated gate snippets."""
    record = json.loads(Path(__file__).with_name('testdata').joinpath('i4v_fence_gate.json').read_text())
    finish = next(s['address'] for s in record['segments'] if s['name'] == 'finish')
    start, capture, vectors, main, store = (gate.PRIVATE + offset for offset in (0,0xc0000,0xc0800,0xc2000,0xc3000))
    def packed(words):return struct.pack('<'+'I'*len(words),*words).hex()
    literals = [start+60,start+0x11008,start+0x20000,start+0x64000,start+0x54000]
    loads = {index:0x58000009|(((128+j*8-index*4)//4)<<5) for j,index in enumerate((13,15,23,25,27))}
    boot = [0xd5034fdf,0xd5182000,0x91401c09,0xd5182029,0xd5182041,0xd518a202,
            0xd5033fdf,0xd5381009,0xb2400129,0xb27e0129,0xb2740129,0xd5181009,0xd5033fdf,
            loads[13],0xd61f0120,loads[15],0xf900013f,0xd5033a9f,0xd508871f,0xd5033b9f,0xd5033fdf,
            gate.adr(start+84,vectors),0xd518c009,loads[23],0xd518d089,loads[25],0x9100013f,
            loads[27],0xd5184109,0xd50040bf,gate.branch(start+120,main,True),0x14000000]
    # Literal instruction encodings copied from disassembly of save_native,
    # independently enumerating every register-pair save and status operation.
    save = [0xd10c83ff] + [0xa90007e0+i*0x10802 for i in range(15)] + [0xf9007bfe]
    save += [0xad0807e0+i*0x10802 for i in range(16)]
    save += [0xd53b4409,0xf90187e9,0xd53b4429,0xf9018be9,0xf9018fff,
             0xd5384109,0xf9007fe9,0xd5384009,0x92640d29,0xf90183e9,0x910003e0,0xd2800021]
    save += [gate.branch(capture+len(save)*4,finish,True),0x14000000]
    vector_words=[gate.NOP]*512
    for slot in range(16):vector_words[slot*32]=gate.branch(vectors+slot*128,capture)
    data=fixture([
        dict(name='_start',address=start,symbol_size=0,hex=packed(boot)+struct.pack('<5Q',*literals).hex()),
        dict(name='private_start',address=start+60,symbol_size=0,hex=''),
        dict(name='exception_capture',address=capture,symbol_size=0,hex=packed(save)),
        dict(name='vectors',address=vectors,symbol_size=0,hex=packed(vector_words)),
        dict(name='runtime_main',address=main,symbol_size=4,hex='00000014'),
        dict(name='partial_store',address=store,symbol_size=8,hex='01000039c0035fd6')])
    struct.pack_into('<Q',data,24,start)
    return data


class GateTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.elf = gate.Elf(self.data)

    def mutate(self, address, word):
        data = self.data.copy()
        struct.pack_into('<I', data, self.elf.offset(address, 4), word)
        with self.assertRaises(ValueError):
            gate.validate(data)

    def test_actual_retained_gate_and_full_restore_pass(self):
        result = gate.validate(self.data)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['stored_gprs'], list(range(31)))
        self.assertEqual(result['stored_q'], list(range(32)))
        self.assertEqual(result['frame_bytes'], 800)

    def test_partial_store_requires_generic_exception_path(self):
        pc=gate.PRIVATE+0xf0000
        incomplete=fixture([dict(name='partial_store',address=pc,symbol_size=8,hex='01000039c0035fd6')])
        with self.assertRaises(ValueError):gate.validate_partial(incomplete)
        data=partial_fixture();result=gate.validate_partial(data)
        self.assertEqual(result['exception_capture_bytes'],188)
        self.assertEqual(result['vector_slots'],16)
        elf=gate.Elf(data);pc=elf.symbol('partial_store')[0]
        for at in (pc,pc+4,elf.symbol('resume_exception')[0]+4):
            bad=data.copy();struct.pack_into('<I',bad,elf.offset(at,4),gate.NOP)
            with self.assertRaises(ValueError):gate.validate_partial(bad)

    def test_generic_capture_every_instruction_and_fault_dispatch(self):
        data=partial_fixture();elf=gate.Elf(data);pc=elf.symbol('exception_capture')[0]
        for i in range(47):
            bad=data.copy();struct.pack_into('<I',bad,elf.offset(pc+i*4,4),gate.NOP)
            with self.subTest(word=i),self.assertRaises(ValueError):gate.validate_partial(bad)
        finish=elf.symbol('finish')[0]
        for word in (gate.branch(pc+45*4,finish+4,True),gate.branch(pc+45*4,finish)):
            bad=data.copy();struct.pack_into('<I',bad,elf.offset(pc+45*4,4),word)
            with self.assertRaises(ValueError):gate.validate_partial(bad)

    def test_all_vectors_must_target_first_capture_instruction(self):
        data=partial_fixture();elf=gate.Elf(data)
        vectors=elf.symbol('vectors')[0];capture=elf.symbol('exception_capture')[0]
        for slot in range(16):
            pc=vectors+slot*128
            for word in (gate.NOP,gate.branch(pc,capture+4),gate.branch(pc,capture,True)):
                bad=data.copy();struct.pack_into('<I',bad,elf.offset(pc,4),word)
                with self.subTest(slot=slot,word=word),self.assertRaises(ValueError):gate.validate_partial(bad)

    def test_boot_vbar_reachability_stack_literals_and_finish_binding(self):
        data=partial_fixture();elf=gate.Elf(data);start=gate.PRIVATE
        for i in range(32):
            bad=data.copy();struct.pack_into('<I',bad,elf.offset(start+i*4,4),gate.NOP)
            with self.subTest(word=i),self.assertRaises(ValueError):gate.validate_partial(bad)
        for i in range(5):
            bad=data.copy();struct.pack_into('<Q',bad,elf.offset(start+128+i*8,8),0)
            with self.subTest(literal=i),self.assertRaises(ValueError):gate.validate_partial(bad)
        bad=data.copy();struct.pack_into('<Q',bad,24,start+60)
        with self.assertRaises(ValueError):gate.validate_partial(bad)
        bad=data.copy();vectors=elf.symbol('vectors')[0]
        struct.pack_into('<I',bad,elf.offset(start+84,4),gate.adr(start+84,vectors+128))
        with self.assertRaises(ValueError):gate.validate_partial(bad)

    def test_every_gpr_vector_and_status_sink_store_is_required(self):
        sink = self.elf.symbol('fence_sink_gate')[0]
        # Every saved root, including X16/X30 and deferred X26/X27, is load-bearing.
        for i in [*range(2, 34), 35, 37, 38, 40, 42]:
            with self.subTest(word=i):
                self.mutate(sink + i * 4, gate.NOP)
        for i in (34, 36, 39, 41):
            self.mutate(sink + i * 4, gate.NOP)

    def test_completion_cannot_be_bypassed_or_redirected(self):
        sink = self.elf.symbol('fence_sink_gate')[0]
        handler = self.elf.symbol('fence_sink_handler')[0]
        for i in (43, 44):
            self.mutate(sink + i * 4, gate.NOP)
            self.mutate(sink + i * 4, gate.branch(sink + i * 4, handler))
        self.mutate(sink + 46 * 4, gate.branch(sink + 46 * 4, handler + 4, True))
        self.mutate(sink, gate.branch(sink, handler))

    def test_allocated_return_captures_every_root_and_binds_finish(self):
        returned = self.elf.symbol('returned_allocated')[0]
        for i in range(43):
            self.mutate(returned + i * 4, gate.NOP)
        finish = self.elf.symbol('finish')[0]
        self.mutate(returned + 45 * 4, gate.branch(returned + 45 * 4, finish + 4, True))
        self.assertEqual(gate.validate(self.data)['return_pc'], returned)

    def test_full_eret_restoration_and_exception_stack_are_required(self):
        resume = self.elf.symbol('resume_exception')[0]
        for i in [0, 1, 3, 4, 7, 8, 9, 10, *range(11, 59)]:
            with self.subTest(word=i):
                self.mutate(resume + i * 4, gate.NOP)
        word = self.elf.words(resume, 6)[5]
        literal = resume + 20 + gate.signed((word >> 5) & 0x7ffff, 19) * 4
        data = self.data.copy()
        struct.pack_into('<Q', data, self.elf.offset(literal, 8), gate.PRIVATE + 0x54000)
        with self.assertRaises(ValueError):
            gate.validate(data)
        enter = self.elf.symbol('enter_allocated')[0]
        self.mutate(enter + 4, gate.branch(enter + 4, resume + 4))

    def test_handler_reuses_saved_status_frame_and_continuation(self):
        report = gate.validate(self.data)
        call = report['handler_resume_call']
        for delta in range(-40, 1, 4):
            self.mutate(call + delta, gate.NOP)
        # A conditional failure branch retargeted past the completion barrier is rejected.
        handler, size = self.elf.symbol('fence_sink_handler')
        words = self.elf.words(handler, size // 4)
        i = next(i for i, word in enumerate(words) if word & 0xff000010 == 0x54000000)
        pc = handler + 4 * i
        word = (words[i] & ~(0x7ffff << 5)) | (((call - pc) // 4 & 0x7ffff) << 5)
        self.mutate(pc, word)
        for opcode in (0x36000000, 0x37000000):
            self.mutate(pc, opcode | (((call - pc) // 4 & 0x3fff) << 5))
        self.mutate(pc, 0xd69f03e0)  # ERET cannot bypass the admitted restore gate.

    def test_malformed_elf_or_non_executable_mapping_rejected(self):
        for data in (self.data[:63], self.data[:-1]):
            with self.assertRaises(ValueError):
                gate.validate(data)
        data = self.data.copy()
        struct.pack_into('<I', data, 64 + 4, 6)
        with self.assertRaises(ValueError):
            gate.validate(data)


if __name__ == '__main__':
    unittest.main()
