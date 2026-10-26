#!/usr/bin/env python3
"""Finite emitted-ELF admission for the LFENCE dependency sink and ERET gates."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

PRIVATE = 0x800040000000
FRAME = 800
DSB = 0xd5033f9f
ISB = 0xd5033fdf
NOP = 0xd503201f


def require(ok, message):
    if not ok:
        raise ValueError(message)


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def branch(pc, target, link=False):
    delta = target - pc
    require(delta % 4 == 0 and -(1 << 27) <= delta < 1 << 27, 'branch range')
    return (0x94000000 if link else 0x14000000) | ((delta // 4) & 0x3ffffff)


def pair(base, first, address, offset, scale):
    return base | ((offset // scale) << 15) | ((first + 1) << 10) | (address << 5) | first


def scalar(base, register, address, offset):
    return base | ((offset // 8) << 10) | (address << 5) | register


def sink_words(pc, handler):
    # SP_EL1 receives every GPR/Q root before any scratch register is overwritten.
    words = [0xd50041bf, 0xd10003ff | (FRAME << 10)]
    words += [pair(0xa9000000, i, 31, i * 8, 8) for i in range(0, 30, 2)]
    words += [scalar(0xf9000000, 30, 31, 240)]
    words += [pair(0xad000000, i, 31, 256 + i * 16, 16) for i in range(0, 32, 2)]
    words += [0xd53b4409, scalar(0xf9000000, 9, 31, 776),  # FPCR
              0xd53b4429, scalar(0xf9000000, 9, 31, 784),  # FPSR
              scalar(0xf9000000, 31, 31, 792),
              0xd5384109, scalar(0xf9000000, 9, 31, 248),  # SP_EL0
              0xd53b4209, scalar(0xf9000000, 9, 31, 768),  # NZCV
              DSB, ISB, 0x910003e0]
    words += [branch(pc + len(words) * 4, handler, True), 0x14000000]
    return words


def resume_words(literal):
    words = [0xd5184021, 0xd5184002, 0xaa0003f0,  # ELR, SPSR, frame in x16
             scalar(0xf9400000, 9, 16, 248), 0xd5184109,
             literal, 0x9100013f,
             scalar(0xf9400000, 9, 16, 776), 0xd51b4409,
             scalar(0xf9400000, 9, 16, 784), 0xd51b4429]
    words += [pair(0xad400000, i, 16, 256 + i * 16, 16) for i in range(0, 32, 2)]
    words += [scalar(0xf9400000, i, 16, i * 8) for i in range(31) if i != 16]
    words += [scalar(0xf9400000, 16, 16, 128), 0xd69f03e0]
    return words


class Elf:
    def __init__(self, data):
        self.data = bytes(data)
        require(len(data) >= 64, 'short ELF')
        h = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        require(h[0][:7] == b'\x7fELF\x02\x01\x01' and h[1:4] == (2, 183, 1), 'not AArch64 LE executable ELF')
        require(h[8] == 64 and h[9] == 56 and h[11] == 64 and 0 < h[10] < 64 and 0 < h[12] < 1024, 'ELF header geometry')
        self.loads = []
        for i in range(h[10]):
            at = h[5] + i * 56
            require(at + 56 <= len(data), 'short program header')
            p = struct.unpack_from('<II6Q', data, at)
            if p[0] == 1:
                require(p[5] <= p[6] and p[2] + p[5] <= len(data), 'bad load extent')
                self.loads.append((p[3], p[2], p[5], p[1]))
        sections = []
        for i in range(h[12]):
            at = h[6] + i * 64
            require(at + 64 <= len(data), 'short section header')
            sections.append(struct.unpack_from('<II4QII2Q', data, at))
        self.symbols = {}
        for s in sections:
            if s[1] != 2:
                continue
            require(s[9] == 24 and s[5] % 24 == 0 and s[6] < len(sections), 'bad symbol table')
            strings = sections[s[6]]
            require(strings[1] == 3, 'symbol string table type')
            names = self.slice(strings[4], strings[5])
            symbols = self.slice(s[4], s[5])
            for at in range(0, len(symbols), 24):
                name, info, other, section, value, size = struct.unpack_from('<IBBHQQ', symbols, at)
                if not section or not name:
                    continue
                require(name < len(names) and b'\0' in names[name:], 'unterminated symbol name')
                text = names[name:names.index(0, name)].decode('ascii')
                if text not in ('fence_sink_gate','fence_sink_handler','enter_allocated','resume_exception','returned_allocated','finish'):
                    continue
                if text in self.symbols:
                    require(self.symbols[text] == (value, size), 'ambiguous symbol')
                self.symbols[text] = (value, size)

    def slice(self, offset, count):
        require(0 <= offset <= len(self.data) and 0 <= count <= len(self.data) - offset, 'ELF extent')
        return self.data[offset:offset + count]

    def offset(self, address, count):
        matches = [(offset + address - base, flags) for base, offset, length, flags in self.loads
                   if base <= address and count <= length - (address - base)]
        require(len(matches) == 1 and matches[0][1] == 5, 'gate must occupy one RX segment')
        return matches[0][0]

    def read(self, address, count):
        return self.slice(self.offset(address, count), count)

    def words(self, address, count):
        return list(struct.unpack('<' + 'I' * count, self.read(address, count * 4)))

    def symbol(self, name):
        require(name in self.symbols, 'missing symbol: ' + name)
        return self.symbols[name]


def validate(data):
    elf = Elf(data)
    sink, _ = elf.symbol('fence_sink_gate')
    handler, handler_size = elf.symbol('fence_sink_handler')
    enter, _ = elf.symbol('enter_allocated')
    returned, _ = elf.symbol('returned_allocated')
    finish, _ = elf.symbol('finish')
    elf.read(finish, 4)
    resume, _ = elf.symbol('resume_exception')
    expected_sink = sink_words(sink, handler)
    require(elf.words(sink, len(expected_sink)) == expected_sink, 'sink capture/completion/handler branch mismatch')
    expected_return = expected_sink[:43] + [0x910003e0, 0xd2800001, branch(returned + 45 * 4, finish, True), 0x14000000]
    require(elf.words(returned, len(expected_return)) == expected_return, 'allocated return capture/finish branch mismatch')
    require(elf.words(enter, 2) == [0xd50041bf, branch(enter + 4, resume)], 'allocated entry does not directly enter restore gate')
    actual_resume = elf.words(resume, 59)
    literal = actual_resume[5]
    require(literal & 0xff00001f == 0x58000009, 'restore stack literal instruction')
    literal_address = resume + 20 + signed((literal >> 5) & 0x7ffff, 19) * 4
    require(struct.unpack('<Q', elf.read(literal_address, 8))[0] == PRIVATE + 0x64000, 'restore exception stack target')
    require(actual_resume == resume_words(literal), 'restore loses native roots/status or ERET')
    require(0 < handler_size <= 4096 and handler_size % 4 == 0, 'handler extent')
    words = elf.words(handler, handler_size // 4)
    calls = [(i, handler + i * 4 + signed(w & 0x3ffffff, 26) * 4)
             for i, w in enumerate(words) if w >> 26 == 0x25]
    returns = [i for i, target in calls if target == enter]
    require(len(returns) == 1, 'handler must have one allocated resume')
    end = returns[0]
    # Finite current Trial layout: sink +0x350, NZCV +0x650, continuation +0x9d0.
    tail = [0x52800028, 0x910d4260, 0xf9001268, DSB, ISB, 0xf9000e68,
            0x52807888, 0xf9432a69, 0xf944ea61, 0xaa080122,
            branch(handler + end * 4, enter, True)]
    require(end >= 10 and words[end - 10:end + 1] == tail, 'handler completion/full-frame/NZCV/continuation suffix mismatch')
    # Conditional failure edges may only leave for the two terminal stop arms;
    # none may enter the middle of the completion or restore argument sequence.
    failure_targets = {handler + (end + 1) * 4, handler + (end + 4) * 4}
    for i, word in enumerate(words):
        require(word & 0x7e000000 != 0x36000000, 'unsupported handler test-bit branch')
        require(word & 0xff000010 != 0x54000010, 'unsupported handler conditional branch variant')
        if word & 0xff000010 == 0x54000000 or word & 0x7e000000 == 0x34000000:
            target = handler + i * 4 + signed((word >> 5) & 0x7ffff, 19) * 4
            require(target in failure_targets, 'handler conditional edge bypasses completion')
        require(word & 0xfe000000 != 0xd6000000, 'unexpected register/exception handler transfer')
        require(word >> 26 != 5, 'unexpected direct handler jump')
    return dict(status='PASS', schema='natemu-i4v-fence-gate-v1', elf_sha256=hashlib.sha256(data).hexdigest(),
                sink_pc=sink, handler_pc=handler, enter_pc=enter, resume_pc=resume, return_pc=returned, finish_pc=finish,
                sink_bytes=len(expected_sink) * 4, return_bytes=len(expected_return) * 4, frame_bytes=FRAME,
                stored_gprs=list(range(31)), stored_q=list(range(32)),
                stored_status=['SP_EL0', 'NZCV', 'FPCR', 'FPSR'],
                dsb_pc=sink + 43 * 4, isb_pc=sink + 44 * 4,
                handler_resume_call=handler + end * 4,
                sink_sha256=hashlib.sha256(elf.read(sink, len(expected_sink) * 4)).hexdigest(),
                restore_sha256=hashlib.sha256(elf.read(resume, 59 * 4)).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('elf', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.elf.read_bytes()), indent=2))
    except (ValueError, struct.error, UnicodeError) as error:
        print(json.dumps(dict(status='FAIL', error=str(error))))
        raise SystemExit(1)
