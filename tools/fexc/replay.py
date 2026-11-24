#!/usr/bin/env python3
"""The replay gate (tools/fexc/README.md, "Replay gate") as one check: a change to fexc, the shim, the FEX fork or the
feature tables that must not change 64-bit output is shown by sending the requests a boot made to the compiler before the
change and to the one after it, and comparing every reply whole (status, report, object, extent, args_read).

    replay.py --fexc NEW --old OLD --stub NATEMU --runtime RESIDENT --kernel BZIMAGE --initrd FILE --out DIR
    replay.py --fexc NEW --old OLD --stream FILE              (a stream recorded earlier, NATEMU_FEXC_RECORD's format)

Without --stream it records one first: the Debian kernel and installer initrd booted at 1 vCPU to a busybox `poweroff -f`
(run_boot_smoke.py's boot) with the persistent cache off, so every request reaches the compiler, under
NATEMU_FEXC_RECORD (the host appends each request body as it posts it: u32 length, then the body). Both compilers then
answer the stream as `fexc --serve` over the pipe (the channel transport has its own check, test_channel.py).
Exit 1 unless the stream holds requests and no reply differs; OLD and NEW the same binary is the self-check. The stream is
several hundred MiB: it stays in DIR, which the framework deletes after a pass.

--program32 FILE.asm (M2): the recording boot also runs that static 32-bit program (assembled with nasm, appended to
the initrd as run_boot_smoke.py's i386 case does), so the stream mixes 64-bit and 32-bit requests. NEW answers them all,
in the recorded order, from one process; OLD answers only the 64-bit ones, since the compiler on `main` refuses 32-bit
code until M2 merges (then OLD can answer them too: drop the filter). So the check also shows that a 64-bit reply does
not depend on 32-bit requests answered before it by the same compiler process. The program must print its `natemu-i386-ok`, and a stream without a 32-bit request fails.

--old32 FEXC: that compiler answers the 32-bit requests too, and each of NEW's 32-bit replies must be its reply or differ
only as only_moves allows (32-bit code's one guest-RIP move form, `movz w; movk w, lsl #16; nop`); each such reply changed
in one place (mutations) must then be reported, and the code bytes of both are summed (the form's growth). A stream of
32-bit requests alone: the host's `pic32_survey` (crates/host/src/execution.rs) records one offline."""
import argparse, os, pathlib, struct, subprocess, sys

MARKER = 'natemu-replay-recorded'
CMDLINE = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh -c "echo {MARKER}; poweroff -f"'


def newc(name, mode, data):
    """An uncompressed initramfs (cpio newc) holding one file."""
    out = b''
    for k, (n, m, d) in enumerate([(name, mode, data), ('TRAILER!!!', 0, b'')]):
        head = b'070701' + b''.join(b'%08X' % f for f in [k + 1, m, 0, 0, 1, 0, len(d), 0, 0, 0, 0, len(n) + 1, 0]) + n.encode() + b'\0'
        out += head + bytes(-len(head) % 4) + d + bytes(-len(d) % 4)
    return out


def record(a):
    stream = a.out / 'requests.stream'
    stream.unlink(missing_ok=True)
    cmdline = CMDLINE
    if a.program32:
        subprocess.run(['nasm', '-f', 'bin', '-o', a.out / 'program32', a.program32], check=True)
        base = pathlib.Path(a.initrd).read_bytes()
        a.initrd = a.out / 'initrd-32'
        a.initrd.write_bytes(base + bytes(-len(base) % 4) + newc('program32', 0o100755, (a.out / 'program32').read_bytes()))
        cmdline = CMDLINE.replace('echo ', '/program32; echo ')
    env = dict(os.environ, NATEMU_FEXC_RECORD=str(stream))
    r = subprocess.run([a.stub, '--runtime', a.runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd, '--cmdline', cmdline,
                        '--vcpus', '1', '--serial', a.out / 'record.serial', '--compile-cache', 'off'],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=a.timeout)
    serial = (a.out / 'record.serial').read_text(errors='replace') if (a.out / 'record.serial').exists() else ''
    if r.returncode or not any(line.strip() == MARKER for line in serial.splitlines()):
        sys.exit(f'recording boot failed: exit {r.returncode}, {r.stderr.strip()[-400:]}')
    if a.program32 and not any(line.strip() == 'natemu-i386-ok' for line in serial.splitlines()):
        sys.exit('recording boot: the 32-bit program did not print natemu-i386-ok')
    return stream


def serve(fexc):
    return subprocess.Popen([fexc, '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def answer(p, record):
    p.stdin.write(record); p.stdin.flush()
    head = p.stdout.read(4)
    if len(head) < 4: sys.exit('a compiler ended while answering')
    return p.stdout.read(struct.unpack('<I', head)[0])


MAGIC, STUB_KINDS, SPLIT = b'NFXOBJ\0\x01', {6, 7, 11, 14, 15, 16, 17, 19, 20, 21, 22, 23, 25}, 7
BRK2, PLACEHOLDER, PROBE_BRANCH = 0xd4200040, 0x14000000, 5


def probe_index(bits32, reg, width):
    """The shared probe of a store of `width` bytes at x`reg` (natemu_abi::execution::probe::index), or None."""
    if width not in (2, 4, 8, 16, 32) or reg in (0, 28) or reg >= 31: return None
    return (160 if bits32 else 0) + 32 * (2, 4, 8, 16, 32).index(width) + reg


def only_probes(old, new):
    """M2 task 4, page-crossing stores without the trap: an intended change of 64-bit output, bounded here. None when the
    two replies differ only as that change allows, else what else differs. Allowed: split stubs that probe through their
    shared probe (two words, `adr x0, <the word after the check>; b .`, the `b` at a probe_branch relocation naming the
    store's probe: fexc_sites.inc FexcEmitStubs) where OLD has `brk #2`, hence later stubs and the words after them at
    other offsets, one probe_branch relocation per such stub after OLD's, and the header's sizes and table offsets.
    Everything else must be byte-identical: the bytes around the object, the header's other fields, the code before the
    first stub (every block's main line), each stub that is no split stub, the words after the stubs, every site record but
    a stub's native offset, the PC table and the other relocations."""
    u32 = lambda b, o: struct.unpack_from('<I', b, o)[0]
    # A reply's payload: code, json_len, object_len, extent, args_read, json, object (fexc.cpp, --serve).
    if len(old) < 20 or len(new) < 20 or old[:8] != new[:8] or old[12:20] != new[12:20] or u32(old, 0) != 0: return 'the status, report, extent or args_read differ'
    at = 20 + u32(old, 4)
    if old[20:at] != new[20:at] or old[at:at + 8] != MAGIC or new[at:at + 8] != MAGIC: return 'the report differs, or no object'
    ho, hn = old[at:at + 128], new[at:at + 128]
    # (The header's sizes and offsets that lie behind the stubs move: total_size, code_size, end_marker_offset,
    # code_region_end and the three tables' offsets, and the relocation count grows; what they locate is compared below.)
    for o in range(0, 128, 4):
        if o not in (16, 44, 52, 56, 64, 72, 80, 84) and u32(hn, o) != u32(ho, o): return f'header word +{o}'
    if len(old) != at + u32(old, 8) or len(new) != at + u32(new, 8) or u32(old, 8) != u32(ho, 16) or u32(new, 8) != u32(hn, 16): return 'the object lengths'
    oo, on = old[at:], new[at:]
    table = lambda b, h, o, size: [b[u32(h, o) + size * k:u32(h, o) + size * (k + 1)] for k in range(u32(h, o + 4))]
    so, sn = table(oo, ho, 64, 40), table(on, hn, 64, 40)
    if table(oo, ho, 72, 12) != table(on, hn, 72, 12): return 'the PC table differs'
    if len(so) != len(sn): return 'the number of sites differs'
    co, cn = oo[u32(ho, 40):][:u32(ho, 44)], on[u32(hn, 40):][:u32(hn, 44)]
    stubs = [(u32(x, 0), u32(y, 0), y) for x, y in zip(so, sn) if x[20] in STUB_KINDS]
    for x, y in zip(so, sn):
        if x[4:] != y[4:] or (x[20] not in STUB_KINDS and x[:4] != y[:4]): return 'a site record differs'
    if not stubs: return 'the code differs in a block with no stub'
    # (The code's first word is FEX's block header, `udf` of the code's size: it moves with the stubs.)
    if stubs[0][0] != stubs[0][1] or u32(co, 0) >> 16 or u32(cn, 0) >> 16: return 'the main line differs'
    moved = {o: n for o, n, _ in stubs}
    for at in range(4, stubs[0][0], 4):
        x, y = u32(co, at), u32(cn, at)
        if x == y: continue
        # A branch to a stub behind a split stub: the same branch, to that stub where it is now.
        for mask, value, lo, bits in ((0x7e000000, 0x36000000, 5, 14), (0x7e000000, 0x34000000, 5, 19), (0xff000010, 0x54000000, 5, 19), (0xfc000000, 0x14000000, 0, 26)):
            field = ((1 << bits) - 1) << lo
            if x & mask == value and x & ~field == y & ~field:
                to = lambda w: at + 4 * (((w & field) >> lo) - (1 << bits if w >> (lo + bits - 1) & 1 else 0))
                if moved.get(to(x)) == to(y): break
        else:
            return f'the main line differs at +{at:#x}: {x:08x} | {y:08x}'
    rn_all = table(on, hn, 80, 16)
    probes = {u32(r, 0): struct.unpack_from('<Q', r, 8)[0] for r in rn_all if r[4] == PROBE_BRANCH}
    seen = set()
    for k, (o, n, site) in enumerate(stubs):
        word = u32(cn, n)
        if site[20] == SPLIT and word & 0x9f00001f == 0x10000000:
            imm = ((word >> 5 & 0x7ffff) << 2 | word >> 29 & 3)
            back = n + (imm - (1 << 21) if imm >> 20 else imm)
            # (64-bit code's probe; back: the word after the split check, a `tbnz` to this stub)
            check = u32(cn, back - 4) if back >= 8 else 0
            imm = check >> 5 & 0x3fff
            to = back - 4 + 4 * (imm - (1 << 14) if imm >> 13 else imm)
            if u32(co, o) != BRK2 or u32(cn, n + 4) != PLACEHOLDER or not 4 <= back <= u32(site, 8) or check & 0x7f000000 != 0x37000000 or \
                    to != n or probes.get(n + 4) != probe_index(False, site[25], site[21]):
                return 'a probing stub of another form'
            seen.add(n + 4)
            size = 8
        else:
            if u32(co, o) != word: return 'a stub that is no probing split stub differs'
            size = 4
        if (stubs[k + 1][1] if k + 1 < len(stubs) else n + size) != n + size or (stubs[k + 1][0] if k + 1 < len(stubs) else o + 4) != o + 4:
            return 'the stubs are not one after the other'
    if seen != set(probes): return 'a probe branch at no probing stub'
    # Behind the stubs: zero words up to an alignment, then FEX's tail (a literal, with its relocation): the same bytes at
    # another offset. Relocations: OLD's, that one moved as far as the tail did, then the probe branches.
    tail = [stubs[-1][0] + 4, stubs[-1][1] + (8 if stubs[-1][1] + 4 in probes else 4)]
    for k, c in enumerate((co, cn)):
        while tail[k] + 4 <= len(c) and u32(c, tail[k]) == 0: tail[k] += 4
    # (FEX's JITCodeTail starts with the code's size, which grows with the stubs; the rest is the same.)
    # (and zero bytes pad the code to its alignment)
    if co[tail[0] + 4:].rstrip(b'\0') != cn[tail[1] + 4:].rstrip(b'\0') or u32(cn, tail[1]) < u32(co, tail[0]):
        return 'the words after the stubs differ: ' + ' '.join(f'{u32(co, o):x}' for o in range(tail[0], len(co) - 3, 4)) + ' | ' + ' '.join(f'{u32(cn, o):x}' for o in range(tail[1], len(cn) - 3, 4))
    ro, rn = table(oo, ho, 80, 16), [r for r in rn_all if r[4] != PROBE_BRANCH]
    if len(ro) != len(rn) or rn_all[:len(rn)] != rn or u32(hn, 84) != u32(ho, 84) + len(probes) or \
            any(x[4:] != y[4:] or u32(y, 0) - u32(x, 0) != (tail[1] - tail[0] if u32(x, 0) >= stubs[0][0] else 0) for x, y in zip(ro, rn)):
        return 'the relocations differ'
    return None


NOP, NONE32, X87_INT64 = 0xd503201f, 0xffffffff, 0xc0000001
GUEST_RIP_LITERAL, GUEST_RIP_MOVE = 1, 3
STUB = {6, 7, 11, 14, 15, 16, 17, 19, 20, 21, 22, 23, 25}   # natemu_abi::code_object::kind::is_stub_kind


def bitmask(w, sf):
    """The value of A64's logical immediate in `w` (N:immr:imms), at 64 or 32 bits."""
    n, immr, imms = w >> 22 & 1, w >> 16 & 63, w >> 10 & 63
    x = (n << 6 | (~imms & 63))
    size = 1 << (x.bit_length() - 1)
    ones = (imms & (size - 1)) + 1
    r = immr & (size - 1)
    elem = (1 << ones) - 1
    elem = (elem >> r | elem << (size - r)) & ((1 << size) - 1)
    width = 64 if sf else 32
    return sum(elem << (size * k) for k in range(width // size))


def move_value(code, at, reg):
    """FEX's move into `reg` at `at` (movz/movn/orr #bitmask, then movk; natemu_abi::execution::move_value): (bytes, value)."""
    u = lambda o: struct.unpack_from('<I', code, o)[0]
    w = u(at)
    sf = w >> 31
    mask = (1 << 64) - 1 if sf else 0xffffffff
    imm, sh = w >> 5 & 0xffff, 16 * (w >> 21 & 3)
    if w & 31 != reg: return None
    if w & 0x7f800000 == 0x12800000: v = ~(imm << sh) & mask
    elif w & 0x7f800000 == 0x52800000: v = imm << sh
    elif w & 0x7f800000 == 0x32000000 and w >> 5 & 31 == 31: v = bitmask(w, sf) & mask
    else: return None
    end = at + 4
    while end + 4 <= len(code) and u(end) & 0xff80001f == (0x72800000 | sf << 31 | reg):
        k = u(end); sh = 16 * (k >> 21 & 3)
        v = (v & ~(0xffff << sh) | (k >> 5 & 0xffff) << sh) & mask
        end += 4
    return end - at, v


def move32(code, at, reg):
    """32-bit code's one guest-RIP move (natemu_abi::execution::move32_value): `movz w; movk w, lsl #16; nop`, or None."""
    if at + 12 > len(code): return None
    z, k, n = struct.unpack_from('<III', code, at)
    if z & 0xffe0001f != 0x52800000 | reg or k & 0xffe0001f != 0x72a00000 | reg or n != NOP: return None
    return z >> 5 & 0xffff | (k >> 5 & 0xffff) << 16


# A64's PC-relative words: (mask, value, field's low bit, field bits; ADR's split field below).
PCREL = ((0x7c000000, 0x14000000, 0, 26), (0xff000010, 0x54000000, 5, 19), (0x7e000000, 0x34000000, 5, 19),
         (0x7e000000, 0x36000000, 5, 14), (0x3b000000, 0x18000000, 5, 19))


def pcrel(w):
    """The byte offset a PC-relative word reaches and the word with that field cleared, or None."""
    if w & 0x9f000000 == 0x10000000:   # adr
        imm = (w >> 5 & 0x7ffff) << 2 | w >> 29 & 3
        return imm - (1 << 21) if imm >> 20 else imm, w & ~(0x7ffff << 5 | 3 << 29)
    for mask, value, lo, bits in PCREL:
        if w & mask == value:
            f = w >> lo & ((1 << bits) - 1)
            return 4 * (f - (1 << bits) if f >> (bits - 1) else f), w & ~(((1 << bits) - 1) << lo)
    return None


def rip_entries(code, at, n):
    """FEX's RIP reconstruction entries (Utils/variable_length_integer.h vl64pair) at `at`: accumulated (host, guest)
    offsets, and where they end."""
    out, host, guest = [], 0, 0
    for _ in range(n):
        if at >= len(code): return None, at
        t = code[at]
        if t >> 7 == 0: dh, dg, size = ((t & 15) + 1) << 2, (t >> 4 & 7) + 1, 1
        elif t >> 6 == 2: dg, dh, size = (t & 63) - (64 if t & 32 else 0), struct.unpack_from('<b', code, at + 1)[0] << 2, 2
        elif t == 0xc0: (dh, dg), size = struct.unpack_from('<ii', code, at + 1), 9
        else: (dh, dg), size = struct.unpack_from('<qq', code, at + 1), 17
        host, guest, at = host + dh, guest + dg, at + size
        out.append((host, guest))
    return out, at


def parse(reply):
    """A reply's parts (fexc.cpp --serve): its 20-byte head, JSON, object header, code and the site, PC and relocation
    tables as records; None for a reply with no object."""
    u32 = lambda b, o: struct.unpack_from('<I', b, o)[0]
    if len(reply) < 20 or u32(reply, 0) != 0: return None
    at = 20 + u32(reply, 4)
    o = reply[at:]
    if o[:8] != MAGIC or len(o) != u32(reply, 8): return None
    h = o[:128]
    table = lambda off, size: [o[u32(h, off) + size * k:u32(h, off) + size * (k + 1)] for k in range(u32(h, off + 4))]
    return reply[:20], reply[20:at], h, o[u32(h, 40):][:u32(h, 44)], table(64, 40), table(72, 12), table(80, 16)


def with_arg(record, arg: bytes):
    """A recorded request with one more NUL-terminated argument after its arguments (fexc.cpp --serve's body: u32 length,
    u32 arguments' length, the arguments, the rest)."""
    body = record[4:]
    n = struct.unpack_from('<I', body, 0)[0]
    body = struct.pack('<I', n + len(arg)) + body[4:4 + n] + arg + body[4 + n:]
    return struct.pack('<I', len(body)) + body


# Bulk REP plan (section 14): FexcRepMovs's first word and its two flag-byte loads, three and five words on (fexc_sites.inc).
MOVS_WORDS = (0xd341fce0, 0x39501f80, 0x394feb80)


def has_bulk_movs(reply) -> bool:
    """The reply's code holds FexcRepMovs (its first word, then its fast-string and DF loads)."""
    p = parse(reply)
    if not p: return False
    code, words = p[3], MOVS_WORDS
    w = [struct.unpack_from('<I', code, o)[0] for o in range(0, len(code) - 3, 4)]
    return any(w[i] == words[0] and w[i + 3] == words[1] and w[i + 5] == words[2] for i in range(len(w) - 5))


def only_bulk_movs(old, new, answer_without):
    """Bulk REP plan (section 14): an intended change of 64-bit output, bounded here. None when NEW's reply holds FexcRepMovs
    and NEW's reply to the same request with `--no-bulk-movs` (FexcRepMovs not emitted) is OLD's, or differs from it only as
    only_probes allows; else what differs. So every other word of the reply is OLD's."""
    if not has_bulk_movs(new): return 'no FexcRepMovs in the new reply'
    without = answer_without()
    if without == old: return None
    return None if only_probes(old, without) is None else 'differs without FexcRepMovs too'


def only_bulk_read(old, new):
    """Bulk REP plan (section 14): NEW reads `--bulk` for every REP MOVSB/MOVSQ it decodes (a request without the flag: a
    step, alignment gating; or with it, for an instruction decoded past the block's final end), so its args_read may also
    have FEXC_ARG_BULK (bit 10, fexc_hook.h) where the reply is otherwise OLD's, byte for byte; None when that is all."""
    u32 = lambda b, o: struct.unpack_from('<I', b, o)[0]
    if len(old) < 20 or len(old) != len(new) or old[:16] != new[:16] or old[20:] != new[20:]: return 'more than args_read differs'
    xo, xn = u32(old, 16), u32(new, 16)
    return None if not xo & 1 << 10 and xn == xo | 1 << 10 else 'args_read differs otherwise'


def only_moves(old, new):
    """32-bit code shared position-independent (fexc: every guest-RIP move of 32-bit code is `movz w; movk w, lsl #16;
    nop`, FEX's InsertGuestRIPMove under fexc_bits32): None when NEW's reply is OLD's but for that, else what else
    differs. Each of OLD's moves (FEX's LoadConstant form: one or two words) is NEW's three words building the same value
    in the same register, at the offset OLD's maps to; every other word of the code is the same, or a PC-relative word
    (b, b.cond, cbz/cbnz, tbz/tbnz, adr, ldr literal) of the same form whose target maps to its target (a far form FEX
    restarted to, its range exceeded by the longer code, is a real difference: reported); the zero words before FEX's tail
    may differ in number; the tail is the same but for its size, its RIP-reconstruction entries the same with their host
    offsets mapped; the head, JSON, header (but its sizes, offsets and the code's size), every site, PC entry and
    relocation the same with their native offsets mapped."""
    a, b = parse(old), parse(new)
    if a is None or b is None: return 'no object in a reply' if old != new else None
    (ha, ja, Ha, ca, sa, pa, ra), (hb, jb, Hb, cb, sb, pb, rb) = a, b
    u32 = lambda c, o: struct.unpack_from('<I', c, o)[0]
    if ha[:8] != hb[:8] or ha[12:20] != hb[12:20] or ja != jb: return 'the status, report, extent or args_read differ'
    # (Header words that move with the code: +16 total_size and +44 code_size, which the longer code changes (the parts they
    # cover are compared below), +64/+72/+80 the tables' offsets behind it, +48/+52/+56 code offsets, mapped below.)
    for o in range(0, 128, 4):
        if o not in (16, 44, 48, 52, 56, 64, 72, 80) and u32(Ha, o) != u32(Hb, o): return f'header word +{o}'
    if len(sa) != len(sb) or len(pa) != len(pb) or len(ra) != len(rb): return 'the number of sites, PC entries or relocations'
    # The moves, in OLD's offset order: (OLD offset, OLD bytes, NEW offset).
    edits, shift = [], 0
    for x, y in sorted(zip(ra, rb), key=lambda r: u32(r[0], 0)):
        if x[4:] != y[4:]: return 'a relocation differs'
        if x[4] != GUEST_RIP_MOVE: continue
        o, n, reg, value = u32(x, 0), u32(y, 0), x[5], struct.unpack_from('<Q', x, 8)[0]
        # (OLD's move: FEX's LoadConstant form, or already the one form, as in a self-check of NEW against itself.)
        m = (12, value) if move32(ca, o, reg) == value else move_value(ca, o, reg)
        if m is None or m[1] != value or n != o + shift or move32(cb, n, reg) != value:
            return f'the move at +{o:#x} (NEW +{n:#x}) is not the same value in the one form'
        edits.append((o, m[0], n)); shift += 12 - m[0]
    tail = (u32(ca, 0), u32(cb, 0))   # FEX's block header word: the offset of its tail (JITCodeTail)

    def code_at(x):
        """NEW's offset of OLD's offset `x` in the code before the tail (a word boundary outside a move's later words), or None."""
        d = 0
        for o, size, n in edits:
            if o < x < o + size: return None
            if o == x: return n
            if o + size <= x: d = n + 12 - (o + size)
        return x + d
    at = lambda x: x - tail[0] + tail[1] if x >= tail[0] else code_at(x)
    # The code before the tail: OLD's last non-zero word maps to NEW's.
    end = [t for t in tail]
    for k, c in enumerate((ca, cb)):
        while end[k] > 4 and u32(c, end[k] - 4) == 0: end[k] -= 4
    if code_at(end[0]) != end[1]: return f'the code before the tail: {end[0]:#x} | {end[1]:#x}'
    moves = {o: size for o, size, _ in edits}
    x = 4
    while x < end[0]:
        if x in moves: x += moves[x]; continue
        y, wa, wb = at(x), u32(ca, x), u32(cb, at(x))
        pa_, pb_ = pcrel(wa), pcrel(wb)
        # (A PC-relative word is checked by its target whether or not its words differ: one that spans a move must change.)
        if wa != wb or pa_ is not None:
            if pa_ is None or pb_ is None or pa_[1] != pb_[1] or at(x + pa_[0]) != y + pb_[0]:
                far = pa_ is not None and pb_ is not None and wa >> 24 in (0x34, 0x35, 0x36, 0x37, 0xb4, 0xb5, 0xb6, 0xb7) and pb_[0] == 8
                return (f'a far form at +{x:#x} (NEW +{y:#x}): FEX restarted with it' if far else
                        f'the code differs at +{x:#x} (NEW +{y:#x}): {wa:08x} | {wb:08x}')
        x += 4
    # The tail: its size, then the same fields, its RIP entries mapped, zero padding.
    ta, tb = ca[tail[0]:], cb[tail[1]:]
    if len(ta) < 40 or len(tb) < 40 or ta[8:40] != tb[8:40]: return 'the tail differs'
    n, off = u32(ta, 24), u32(ta, 28)
    ea, enda = rip_entries(ta, off, n)
    eb, endb = rip_entries(tb, off, n)
    if ea is None or eb is None or [(at(h), g) for h, g in ea] != eb: return 'the RIP entries differ'
    if ta[enda:].strip(b'\0') or tb[endb:].strip(b'\0'): return 'the bytes after the RIP entries'
    same = lambda x, y: x == y == NONE32 or (x != NONE32 and at(x) == y)
    for x, y in zip(sa, sb):
        nat = (0, 12) + ((8,) if x[20] in STUB and u32(x, 8) != X87_INT64 else ())
        if x[20] == 25:   # SEGMENT: imm is the native continuation after its guard
            nat += (16,)
        if any(not same(u32(x, k), u32(y, k)) for k in nat) or any(x[k:k + 4] != y[k:k + 4] for k in range(4, 40, 4) if k not in nat):
            return 'a site record differs'
    if any(not same(u32(x, 0), u32(y, 0)) or x[4:] != y[4:] for x, y in zip(pa, pb)): return 'a PC entry differs'
    if any(not same(u32(x, 0), u32(y, 0)) for x, y in zip(ra, rb)): return 'a relocation offset differs'
    for o in (48, 52, 56):
        if not same(u32(Ha, o), u32(Hb, o)): return f'header word +{o} does not map'
    return None


def mutations(reply):
    """NEW's reply changed in one place each, each a change only_moves must report: a move's value, the `nop` after it, a
    word of the code, a PC-relative word's target, a site's native offset."""
    p = parse(reply)
    if p is None: return []
    u32 = lambda b, o: struct.unpack_from('<I', b, o)[0]
    head, json, h, code, sites, pcs, relocs = p
    base = 20 + len(json) + u32(h, 40)   # the code's offset in the reply
    def poke(o, f):
        b = bytearray(reply); struct.pack_into('<I', b, o, f(u32(reply, o))); return bytes(b)
    out = []
    moves = [u32(r, 0) for r in relocs if r[4] == GUEST_RIP_MOVE]
    if moves:
        out += [poke(base + moves[0] + 4, lambda w: w ^ 1 << 5), poke(base + moves[0] + 8, lambda w: 0xaa0003e0)]
    inside = {m + k for m in moves for k in (0, 4, 8)}
    words = [o for o in range(4, u32(code, 0), 4) if o not in inside and u32(code, o)]
    if words: out.append(poke(base + words[len(words) // 2], lambda w: w ^ 1 << 12))
    def retarget(w):   # the target one unit on: b's field is at bit 0, adr's low bits at 29, the others' at bit 5
        return w + 1 if w & 0x7c000000 == 0x14000000 else w + (1 << 29) if w & 0x9f000000 == 0x10000000 else w + (1 << 5)
    rel = [o for o in words if pcrel(u32(code, o)) and pcrel(u32(code, o))[0]]
    if rel: out.append(poke(base + rel[0], retarget))
    # A PC-relative word over a move, its target 4 bytes short (OLD's own word where OLD's move was one word shorter).
    span = [o for o in rel if any(min(o, o + pcrel(u32(code, o))[0]) < m < max(o, o + pcrel(u32(code, o))[0]) for m in moves)]
    if span:
        o = span[0]
        def shorter(w, back=pcrel(u32(code, o))[0] < 0):
            unit = 1 if w & 0x7c000000 == 0x14000000 else 1 << 5   # 4 bytes: b's field at bit 0, adr's high bits and the others' at 5
            return (w + unit if back else w - unit) & 0xffffffff
        out.append(poke(base + o, shorter))
    if sites: out.append(poke(20 + len(json) + u32(h, 64), lambda w: w + 4))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', required=True); ap.add_argument('--old', required=True)
    ap.add_argument('--stream', type=pathlib.Path)
    ap.add_argument('--stub'); ap.add_argument('--runtime'); ap.add_argument('--kernel'); ap.add_argument('--initrd')
    ap.add_argument('--out', type=pathlib.Path); ap.add_argument('--timeout', type=int, default=600)
    ap.add_argument('--program32', help='a static 32-bit program (nasm source) the recording boot also runs')
    ap.add_argument('--old32', help="a compiler that answers the 32-bit requests too, compared with NEW's by only_moves")
    a = ap.parse_args()
    if not a.stream:
        if not all((a.stub, a.runtime, a.kernel, a.initrd, a.out)): ap.error('--stream, or --stub --runtime --kernel --initrd --out')
        a.out.mkdir(parents=True, exist_ok=True)
    stream = a.stream or record(a)
    old, new = serve(a.old), serve(a.fexc)
    old32 = serve(a.old32) if a.old32 else None
    n = differ = n32 = probes = movs = 0
    same32 = moves32 = differ32 = uncaught = 0
    size32 = [0, 0]   # the 32-bit replies' code bytes, OLD32's and NEW's
    code_size = lambda r: (lambda p: len(p[3]) if p else 0)(parse(r))
    with open(stream, 'rb') as f:
        while head := f.read(4):
            rec = head + f.read(struct.unpack('<I', head)[0])
            n += 1
            args = rec[8:8 + struct.unpack('<I', rec[4:8])[0]]
            if b'--bitness\x0032\0' in args:        # NEW only (and --old32): main's compiler refuses 32-bit code
                n32 += 1
                rn = answer(new, rec)
                if old32:
                    ro = answer(old32, rec)
                    size32[0] += code_size(ro); size32[1] += code_size(rn)
                    why = only_moves(ro, rn)
                    if why is not None:
                        differ32 += 1
                        if differ32 <= 5:
                            print(f'request {n} (32-bit) differs ({why[:400]}): ' + args[:60].replace(b'\0', b' ').decode(errors='replace'))
                    else:
                        same32 += ro == rn; moves32 += ro != rn
                        # (The check, shown to fail: each change of NEW's reply in one place is reported.)
                        missed = [k for k, m in enumerate(mutations(rn)) if only_moves(ro, m) is None]
                        if missed:
                            uncaught += 1
                            if uncaught <= 5: print(f'request {n} (32-bit): mutations {missed} not reported')
                continue
            ro, rn = answer(old, rec), answer(new, rec)
            if ro != rn and b'--bulk\0' in args and only_bulk_movs(ro, rn, lambda: answer(new, with_arg(rec, b'--no-bulk-movs\0'))) is None:
                movs += 1
                continue
            if ro != rn and only_bulk_read(ro, rn) is None:
                movs += 1
                continue
            if ro != rn and (why := only_probes(ro, rn)) is not None:
                differ += 1
                if differ <= 5:
                    print(f'request {n} differs ({why[:400]}): ' + args[:60].replace(b'\0', b' ').decode(errors='replace'))
            elif ro != rn:
                probes += 1
    for p in (old, new, old32):
        if p: p.stdin.close(); p.wait()
    same = os.path.realpath(a.old) == os.path.realpath(a.fexc)
    print(f'replay: {n} requests ({stream.stat().st_size >> 20} MiB), {differ} differences' +
          (f'; {probes} more replies differ only by split stubs that probe (M2 task 4: only_probes)' if probes else '') +
          (f'; {movs} more differ only by bulk REP MOVS (only_bulk_movs, only_bulk_read)' if movs else '') + (' (OLD is NEW: the self-check only)' if same else '') +
          (f'; {n32} of them for 32-bit code, answered by NEW only (none compared: --old32 compares them)' if n32 and not old32 else ''))
    if old32:
        growth = 100 * (size32[1] - size32[0]) / max(size32[0], 1)
        print(f'32-bit: {n32} requests: {same32} replies the same, {moves32} differ only by moves (only_moves), {differ32} otherwise; '
              f'{uncaught} with a mutation not reported; code bytes {size32[0]} -> {size32[1]} ({growth:+.2f}%)')
    return 1 if differ or not n or (a.program32 and not n32) or (old32 and (differ32 or uncaught or not n32)) else 0


if __name__ == '__main__':
    sys.exit(main())
