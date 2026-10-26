import json
from pathlib import Path
import struct
import unittest

try:
    from . import check_i4v_fence_gate as gate
except ImportError:
    import check_i4v_fence_gate as gate


def fixture():
    """Wrap retained actual emitted snippets in a minimal ELF for corruption tests."""
    record = json.loads(Path(__file__).with_name('testdata').joinpath('i4v_fence_gate.json').read_text())
    segments = record['segments']
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
