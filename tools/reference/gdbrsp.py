#!/usr/bin/env python3
"""A small GDB remote-protocol client for QEMU's gdbstub (system mode, x86-64), stdlib only so that it also runs in
the reference machine's container. tests/boot/run_ovmf.py uses it to stop a firmware at an address and read the CPU
there, the same way under TCG on this Mac and under KVM on the x86 reference machine.

    python3 gdbrsp.py --connect unix:PATH|HOST:PORT --break ADDR [--main32] [--debugcon FILE --until TEXT] --out ANSWER.json

With QEMU started paused at reset (-S): optionally puts the boot CPU at FFFFFFF0H in 32-bit protected mode (--main32,
`MAIN32` below); sets a breakpoint at ADDR (hardware, else software: `breakpoint`), continues, and at the stop there
writes the registers `REGS` as {name: int} to ANSWER.json with the stop reply; then, with --debugcon, removes the
breakpoint and lets the guest run on until FILE holds a line containing TEXT (or --wait seconds pass); and kills QEMU.

Under KVM, QEMU's `p` and `P` packets read and write its own copy of the CPU, which only `g` (all registers) refreshes
from KVM: `sync` sends one at every stop before anything is read or written (without it every read gave the reset
state under KVM on the reference machine; QEMU 10.0.13, Debian's, November 22, 2026). The register layout comes from the
stub's own target description (qXfer:features:read), never from a table here."""
import argparse, json, pathlib, re, socket, sys, time
import xml.etree.ElementTree as ET

REGS = ('rax', 'rbx', 'rcx', 'rdx', 'rsi', 'rdi', 'rbp', 'rsp', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15',
        'rip', 'eflags', 'cs', 'ss', 'ds', 'es', 'fs', 'gs', 'fs_base', 'gs_base', 'cr0', 'cr3', 'cr4', 'efer')
OTHER_STOPS = 8                         # stops elsewhere than at the breakpoint that are continued from (session)

# --main32: the boot CPU at the reset vector with CR0.PE already set (edk2's ResetVectorVtf0 then takes Main32): the
# power-on CR0 with PE, CS a flat 32-bit code segment and the data segments flat, loaded from a GDT the rig writes at
# physical 0 (the reset GDTR: base 0, limit FFFFH), EIP FFFFFFF0H. Nothing else changes.
MAIN32_GDT = bytes.fromhex('0000000000000000' 'ffff0000009bcf00' 'ffff00000093cf00')   # null, 08H code32, 10H data32
MAIN32 = (('cr0', 0x60000011), ('cs', 0x08), ('ss', 0x10), ('ds', 0x10), ('es', 0x10), ('fs', 0x10), ('gs', 0x10),
          ('rip', 0xFFFFFFF0))


class Stub:
    def __init__(self, where: str, timeout: float = 30):
        if where.startswith('unix:'):
            self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            target = where[5:]
        else:
            host, port = where.rsplit(':', 1)
            self.s, target = socket.socket(socket.AF_INET, socket.SOCK_STREAM), (host, int(port))
        deadline = time.time() + timeout
        while True:
            try:
                self.s.connect(target); break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.time() > deadline: raise
                time.sleep(0.1)
        self.buf, self.ack = b'', True
        self.s.settimeout(timeout)

    def _read(self) -> bytes:
        b = self.s.recv(65536)
        if not b: raise EOFError('the gdbstub closed the connection')
        return b

    def send(self, payload: str):
        data = payload.encode()
        self.s.sendall(b'$' + data + b'#' + b'%02x' % (sum(data) & 0xFF))
        if self.ack:
            while not self.buf: self.buf += self._read()
            c, self.buf = self.buf[:1], self.buf[1:]
            if c != b'+': raise RuntimeError(f'no ack for {payload[:40]!r}: {c!r}')

    def recv(self, timeout: float | None = None) -> str:
        if timeout is not None: self.s.settimeout(timeout)
        while True:
            m = re.match(rb'[+]*\$([^#]*)#([0-9a-fA-F]{2})', self.buf, re.S)
            if m:
                self.buf = self.buf[m.end():]
                if self.ack: self.s.sendall(b'+')
                return _unescape(m[1]).decode(errors='replace')
            self.buf += self._read()

    def ask(self, payload: str, timeout: float | None = None) -> str:
        self.send(payload)
        return self.recv(timeout)

    def interrupt(self, timeout: float = 10) -> str:
        """Stop the running guest (^C): the stop reply."""
        self.s.sendall(b'\x03')
        return self.recv(timeout)

    def close(self):
        self.s.close()


def _unescape(b: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(b):
        if b[i] == 0x7D: out.append(b[i + 1] ^ 0x20); i += 2
        else: out.append(b[i]); i += 1
    return bytes(out)


def features(stub: Stub, annex: str = 'target.xml') -> str:
    text, off = '', 0
    while True:
        r = stub.ask(f'qXfer:features:read:{annex}:{off:x},{0x800:x}')
        if not r or r[0] not in 'ml': raise RuntimeError(f'qXfer:features:read:{annex}: {r!r}')
        text += r[1:]; off += len(r) - 1
        if r[0] == 'l': return text


def registers(stub: Stub) -> dict:
    """{name: (regnum, bytes)} from the target description and the files it includes, in order (GDB's numbering)."""
    out, num = {}, [0]

    def walk(annex):
        text = re.sub(r'<(/?)xi:', r'<\1', features(stub, annex))     # QEMU's xi:include declares no namespace
        text = re.sub(r'<!DOCTYPE[^>]*>', '', text)
        for el in ET.fromstring(text).iter():
            tag = el.tag.split('}')[-1]
            if tag == 'include': walk(el.get('href'))
            elif tag == 'reg':
                n = int(el.get('regnum')) if el.get('regnum') else num[0]
                out[el.get('name')] = (n, int(el.get('bitsize')) // 8)
                num[0] = n + 1
    walk('target.xml')
    return out


def sync(stub):
    """QEMU's copy of the CPU refreshed from the accelerator (a `g` packet): see the module's docstring."""
    r = stub.ask('g')
    if not re.fullmatch(r'[0-9a-fA-Fx]+', r or ''): raise RuntimeError(f'g: {r[:60]!r}')


def read_reg(stub, layout, name) -> int:
    n, size = layout[name]
    r = stub.ask(f'p{n:x}')
    if not re.fullmatch(r'[0-9a-fA-F]+', r or ''): raise RuntimeError(f'p {name}: {r!r}')
    return int.from_bytes(bytes.fromhex(r)[:size], 'little')


def write_reg(stub, layout, name, value):
    n, size = layout[name]
    r = stub.ask(f'P{n:x}=' + value.to_bytes(size, 'little').hex())
    if r != 'OK': raise RuntimeError(f'P {name}={value:#x}: {r!r}')


def write_mem(stub, addr, data: bytes):
    r = stub.ask(f'M{addr:x},{len(data):x}:{data.hex()}')
    if r != 'OK': raise RuntimeError(f'M {addr:#x}: {r!r}')


def breakpoint(stub, addr) -> str:
    """A hardware breakpoint at `addr` (Z1), else a software one (Z0): which one was set ('hw', 'sw'), '' if none."""
    for z, kind in (('1', 'hw'), ('0', 'sw')):
        if stub.ask(f'Z{z},{addr:x},1') == 'OK': return kind
    return ''


def unbreak(stub, addr, kind):
    stub.ask(f"z{'1' if kind == 'hw' else '0'},{addr:x},1")


def session(connect: str, addr: int, main32: bool = False, timeout: float = 120, debugcon: pathlib.Path | None = None,
            until: str = '', wait: float = 60) -> dict:
    """The run described above against a stub at `connect`: {'stop': the stop reply, or 'timeout', 'breakpoint': 'hw'
    or 'sw', 'other_stops': [RIP of each stop elsewhere, continued from, at most OTHER_STOPS], 'regs': {name: int},
    'resumed_at': the time (time.time()) the guest went on from the stop}. QEMU is killed at the end."""
    stub = Stub(connect)
    try:
        if 'QStartNoAckMode+' in stub.ask('qSupported:xmlRegisters=i386'):
            if stub.ask('QStartNoAckMode') == 'OK': stub.ack = False
        layout = registers(stub)
        if missing := [r for r in REGS if r not in layout]: raise RuntimeError(f'the stub describes no {missing}')
        stub.ask('?')
        sync(stub)
        if main32:
            write_mem(stub, 0, MAIN32_GDT)
            for name, v in MAIN32: write_reg(stub, layout, name, v)
        kind = breakpoint(stub, addr)
        if not kind: raise RuntimeError(f'the stub sets no breakpoint at {addr:#x}')
        other, end = [], time.time() + timeout
        while True:
            stub.send('c')
            try:
                stop = stub.recv(max(1.0, end - time.time()))
            except socket.timeout:
                stub.interrupt(); stop = 'timeout'
            sync(stub)
            rip = read_reg(stub, layout, 'rip')
            if stop == 'timeout' or rip == addr or len(other) == OTHER_STOPS: break
            other.append(rip)
        answer = {'stop': stop, 'breakpoint': kind, 'other_stops': other, 'regs': {r: read_reg(stub, layout, r) for r in REGS}}
        answer['resumed_at'] = time.time()          # the guest ran nothing from the stop until now (QEMU's -trace times)
        if debugcon and rip == addr:
            unbreak(stub, addr, kind)
            stub.send('c')
            end = time.time() + wait
            while time.time() < end:
                text = debugcon.read_text(errors='replace') if debugcon.exists() else ''
                if any(until in l for l in text.splitlines(keepends=True) if l.endswith('\n')): break
                time.sleep(0.2)
            try: stub.interrupt()
            except (socket.timeout, EOFError): pass
        try: stub.send('k')
        except (OSError, EOFError, RuntimeError): pass
        return answer
    finally:
        stub.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--connect', required=True)
    ap.add_argument('--break', dest='brk', type=lambda s: int(s, 0), required=True)
    ap.add_argument('--main32', action='store_true')
    ap.add_argument('--timeout', type=float, default=120, help='seconds to wait for the stop at ADDR')
    ap.add_argument('--debugcon', type=pathlib.Path)
    ap.add_argument('--until', default='')
    ap.add_argument('--wait', type=float, default=60)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    answer = session(a.connect, a.brk, a.main32, a.timeout, a.debugcon, a.until, a.wait)
    a.out.write_text(json.dumps(answer, indent=1) + '\n')
    print(json.dumps(answer))


if __name__ == '__main__':
    sys.exit(main())
