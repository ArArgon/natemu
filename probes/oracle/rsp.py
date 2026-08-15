#!/usr/bin/env python3
"""
rsp.py -- a minimal GDB Remote Serial Protocol client, enough to drive QEMU's
built-in gdbstub for lockstep differential testing.

Deliberately dependency-free and ~150 lines: the point of this file is to show
that "drive two emulators in lockstep and diff their architectural state" needs
no GDB, no pygdbmi, and no scripting inside GDB.  It needs a socket.

Only the packets a lockstep oracle needs are implemented:
    qSupported / QStartNoAckMode   handshake, and turning off +/- acking
    g                              read the whole register file
    m addr,len                     read memory
    s / c                          single-step / continue
    Z0,addr,1 / z0                 set/clear a software breakpoint
"""
import socket


class RSPError(Exception):
    pass


class RSP:
    def __init__(self, host="127.0.0.1", port=1234, timeout=30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""
        self.noack = False
        self._handshake()

    # ---------------------------------------------------------------- framing
    def _send_raw(self, payload: bytes):
        csum = sum(payload) & 0xFF
        pkt = b"$" + payload + b"#" + b"%02x" % csum
        self.sock.sendall(pkt)

    def _read_byte(self) -> bytes:
        while not self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RSPError("connection closed")
            self.buf = chunk
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def _recv_raw(self) -> bytes:
        # skip acks and any interrupt noise until a '$'
        while True:
            b = self._read_byte()
            if b == b"$":
                break
        out = bytearray()
        while True:
            b = self._read_byte()
            if b == b"#":
                break
            out += b
        self._read_byte()
        self._read_byte()  # checksum, not verified over loopback
        if not self.noack:
            self.sock.sendall(b"+")
        return bytes(out)

    def cmd(self, payload: str) -> str:
        self._send_raw(payload.encode())
        if not self.noack:
            # swallow the '+' ack
            while True:
                b = self._read_byte()
                if b == b"+":
                    break
                if b == b"$":
                    self.buf = b"$" + self.buf
                    break
        return self._recv_raw().decode(errors="replace")

    def _handshake(self):
        self.cmd("qSupported:multiprocess+;xmlRegisters=i386")
        if self.cmd("QStartNoAckMode") == "OK":
            self.noack = True

    # ---------------------------------------------------------------- targets
    def regs_hex(self) -> str:
        return self.cmd("g")

    def read_mem(self, addr: int, length: int) -> bytes:
        r = self.cmd("m%x,%x" % (addr, length))
        if r.startswith("E"):
            raise RSPError("m%x,%x -> %s" % (addr, length, r))
        return bytes.fromhex(r)

    def step(self) -> str:
        return self.cmd("s")

    def cont(self) -> str:
        return self.cmd("c")

    def set_break(self, addr: int):
        return self.cmd("Z0,%x,1" % addr)

    def clear_break(self, addr: int):
        return self.cmd("z0,%x,1" % addr)

    def kill(self):
        try:
            self._send_raw(b"k")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# x86-64 'g' packet decode.
#
# QEMU's gdbstub emits registers in the order of the target description XML.
# For x86-64 the first 20 entries are fixed and are all a state-diff oracle
# needs; everything after (x87/SSE/AVX) is appended and can be sliced off.
X86_64_CORE = [
    ("rax", 8), ("rbx", 8), ("rcx", 8), ("rdx", 8),
    ("rsi", 8), ("rdi", 8), ("rbp", 8), ("rsp", 8),
    ("r8", 8), ("r9", 8), ("r10", 8), ("r11", 8),
    ("r12", 8), ("r13", 8), ("r14", 8), ("r15", 8),
    ("rip", 8), ("eflags", 4),
    ("cs", 4), ("ss", 4), ("ds", 4), ("es", 4), ("fs", 4), ("gs", 4),
]
CORE_HEXLEN = sum(sz for _, sz in X86_64_CORE) * 2  # 164 bytes -> 328 hex chars


def decode_core(hexstr: str) -> dict:
    """Little-endian hex -> {name: int} for the 24 core x86-64 registers."""
    out, off = {}, 0
    for name, size in X86_64_CORE:
        chunk = hexstr[off:off + size * 2]
        off += size * 2
        out[name] = int.from_bytes(bytes.fromhex(chunk), "little")
    return out


EFLAGS_BITS = [(0, "CF"), (2, "PF"), (4, "AF"), (6, "ZF"), (7, "SF"),
               (8, "TF"), (9, "IF"), (10, "DF"), (11, "OF"), (16, "RF"),
               (17, "VM"), (18, "AC"), (21, "ID")]


def eflags_str(v: int) -> str:
    return "".join(n if (v >> b) & 1 else "." for b, n in EFLAGS_BITS)


def diff_core(a: dict, b: dict) -> list:
    """Return [(name, a, b)] for every differing core register."""
    return [(k, a[k], b[k]) for k in a if a[k] != b[k]]
