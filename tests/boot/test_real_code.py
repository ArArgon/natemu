"""The real-code suites' runners (tests/boot/run_kut.py, run_ovmf.py; docs/restructure/real-code-suites.md): the judges
pass the references' answers and refuse wrong ones, answers are keyed by what an input is built from and never by
what came out, the image parsing finds what the reset vector and the boot find, the device trace proves nothing when
it shows nothing, and the GDB client speaks QEMU's protocol (tools/reference/gdbrsp.py, against a fake stub)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import socket, struct, subprocess, threading

import pytest

import run_kut as kut
import run_ovmf as ovmf
import gdbrsp

PROV = {'upstream': {'sources': {'kvm-unit-tests-src': '36' * 32}, 'recipe_sha256': 'ab' * 32, 'image': 'img@sha256:0'},
        'built': {'smptest.flat': '00' * 32, 'dummy.flat': '11' * 32}}


def kut_pass(test, n):
    lines = ['enabling apic'] * n + [f'smp: waiting for {n - 1} APs'] + [f'setup: CPU {k} online' for k in range(1, n)]
    lines += {'smptest': [f'found {n} cpus', 'PASS: IPI to each CPU', 'SUMMARY: 1 tests'] + [f'ipi called, cpu {k}' for k in range(n)],
              'dummy': ['Dummy Hello World!']}[test]
    return {'exit': 1, 'lines': sorted(lines)}


@pytest.mark.parametrize('test', kut.TESTS)
@pytest.mark.parametrize('n', kut.CPUS)
def test_kut_judge_passes_the_pass_and_refuses_each_fault(test, n):
    good = kut_pass(test, n)
    assert kut.judge(test, n, good) == []
    assert kut.judge(test, n, {**good, 'exit': 'debug-exit 1'}, want_exit='debug-exit 1') == []
    drop = lambda l: {**good, 'lines': [x for x in good['lines'] if x != l]}
    bad = [{**good, 'exit': 3}, {**good, 'exit': 'timeout'}, {'exit': 1, 'lines': []},
           {**good, 'lines': sorted(good['lines'] + ['FAIL: IPI to each CPU'])},
           {**good, 'lines': sorted(good['lines'] + ['SeaBIOS (version rel-1.17.0)'])},
           {**good, 'lines': sorted(good['lines'] + [f'setup: CPU {n} online'])},
           {**good, 'lines': sorted(good['lines'] + ['enabling apic'])},
           drop('Dummy Hello World!' if test == 'dummy' else 'PASS: IPI to each CPU'), drop(f'smp: waiting for {n - 1} APs')]
    if n > 1: bad += [drop(f'setup: CPU {n - 1} online'), {**good, 'lines': sorted(good['lines'] + ['setup: CPU 1 online'])}]
    for b in bad: assert kut.judge(test, n, b), b


def test_kut_answer_is_the_sorted_lines_without_crs_and_firmware_text_fails():
    a = kut.answer(1, b'b line\r\n\r\na line\r\nlast')
    assert a == {'exit': 1, 'lines': ['a line', 'b line', 'last']}
    assert kut.judge('dummy', 1, kut.answer(1, b'Booting from ROM..enabling apic\r\nsmp: waiting for 0 APs\r\nDummy Hello World!'))


def test_kut_keys_follow_the_upstream_and_never_the_built_images():
    k = kut.key('smptest', 2, 'kvm', PROV)
    assert k == kut.key('smptest', 2, 'kvm', {**PROV, 'built': {'smptest.flat': 'ff' * 32}, 'builder': 'elsewhere'})
    for other in ({'upstream': {**PROV['upstream'], 'recipe_sha256': 'cd' * 32}},
                  {'upstream': {**PROV['upstream'], 'sources': {'kvm-unit-tests-src': '00' * 32}}}):
        assert k != kut.key('smptest', 2, 'kvm', {**PROV, **other})
    assert len({kut.key(t, n, b, PROV) for t in kut.TESTS for n in kut.CPUS for b in ('kvm',)}) == len(kut.TESTS) * len(kut.CPUS)


def test_the_environment_module_gives_the_cpu_count():
    assert kut.environment(3) == 'NR_CPUS=3\n'
    args = kut.qemu_args(3, 'kvm')
    assert args[args.index('-initrd') + 1] == 'ENV' and args[args.index('-smp') + 1] == '3' and '-nographic' not in args


def _elf32(symbols: dict, code: bytes, base: int) -> bytes:
    """A minimal ELF32 image: one PROGBITS section holding `code` at `base`, a symbol table and its strings."""
    names = b'\0' + b''.join(n.encode() + b'\0' for n in symbols)
    syms, off = b'\0' * 16, 1
    for n, v in symbols.items():
        syms += struct.pack('<IIIBBH', off, v, 0, 0, 0, 1); off += len(n) + 1
    body = code + syms + names
    shoff = 52 + len(body)
    sh = [b'\0' * 40,
          struct.pack('<IIIIIIIIII', 0, 1, 6, base, 52, len(code), 0, 0, 1, 0),
          struct.pack('<IIIIIIIIII', 0, 2, 0, 0, 52 + len(code), len(syms), 3, 0, 4, 16),
          struct.pack('<IIIIIIIIII', 0, 3, 0, 0, 52 + len(code) + len(syms), len(names), 0, 0, 1, 0)]
    hdr = b'\x7fELF\x01\x01\x01' + b'\0' * 9 + struct.pack('<HHIIIIIHHHHHH', 2, 3, 1, base, 0, shoff, 0, 52, 0, 0, 40, len(sh), 0)
    return hdr + body + b''.join(sh)


def _assemble(tmp, bits, lines) -> bytes:
    src, out = tmp / 'c.asm', tmp / 'c.bin'
    src.write_text(f'bits {bits}\n' + '\n'.join(lines) + '\n')
    subprocess.run(['nasm', '-f', 'bin', '-o', str(out), str(src)], check=True)
    return out.read_bytes()


def test_kut_code_check_finds_the_regions_by_symbol_and_refuses_what_the_first_version_lacks(tmp_path):
    """The check over an image shaped like kvm-unit-tests' (cstart64.S, trampolines.S), and a negative control: port I/O
    in the 32-bit entry is refused."""
    parts = {'start': (32, ['mov [0x1000], ebx', 'mov esp, 0x2000', 'call 0x10040', 'jmp 0x8:0x400158']),
             'sipi_entry': (16, ['mov eax, cr0', 'or eax, 1', 'mov cr0, eax', 'o32 lgdt [0x18]', 'jmp dword 0x8:0x10080']),
             'prepare_64': (32, ['lgdt [edx]', 'mov cr4, eax', 'bts eax, 8', 'wrmsr', 'mov cr0, eax', 'ret']),
             'ap_start32': (32, ['lock xadd [edx], esp', 'push dword 8', 'push edx', 'retf'])}
    base, code, syms = 0x10000, b'', {}
    for name, (bits, lines) in parts.items():
        code += b'\x90' * (-len(code) % 64); syms[name] = base + len(code); code += _assemble(tmp_path, bits, lines) + b'\xcc' * 8
    regions, bad = kut.code(_elf32(syms, code, base), tmp_path)
    assert bad == [] and [len(regions[n]) for n in parts] == [len(l) for _, l in parts.values()], (regions, bad)
    parts['start'] = (32, ['in al, dx', 'jmp 0x8:0x400158'])
    code, syms = b'', {}
    for name, (bits, lines) in parts.items():
        code += b'\x90' * (-len(code) % 64); syms[name] = base + len(code); code += _assemble(tmp_path, bits, lines) + b'\xcc' * 8
    regions, bad = kut.code(_elf32(syms, code, base), tmp_path)
    assert any(b.startswith('start ') and 'in al,dx' in b for b in bad), bad


SEC, BFV = 0xFFFD5E24, 0xFFFCC000


def ovmf_pass(entry):
    regs = dict(ovmf.expected_registers(entry, SEC, BFV), eflags=0x46)
    return {'stop': 'breakpoint', 'regs': regs, 'devices': [], 'debugcon': [f'SecCoreStartupWithStack(0x{BFV:X}, 0x820000)']}


@pytest.mark.parametrize('entry', ovmf.ENTRIES)
def test_ovmf_judge_passes_the_pass_and_refuses_each_fault(entry):
    good = ovmf_pass(entry)
    assert ovmf.judge(entry, good, SEC, BFV) == []
    for k in good['regs']:
        if k != 'eflags': assert ovmf.judge(entry, {**good, 'regs': {**good['regs'], k: good['regs'][k] ^ 0x10}}, SEC, BFV), k
    other = 'main32' if entry == 'reset' else 'reset'
    for bad in ({**good, 'stop': 'timeout'}, {**good, 'devices': None},
                {**good, 'devices': [{'op': 'write', 'region': 'isa-debugcon', 'addr': '0x0', 'size': 1, 'count': 1}]},
                {**good, 'regs': {**good['regs'], 'eflags': 0x246}}, {**good, 'debugcon': ['(no SEC line)']},
                {**good, 'regs': dict(ovmf_pass(other)['regs'])}):
        assert ovmf.judge(entry, bad, SEC, BFV), bad
    assert ovmf.judge(entry, good, SEC + 1, BFV) and ovmf.judge(entry, good, SEC, BFV - 0x1000)


QEMU10 = "818@{t:.6f}:memory_region_ops_{op} cpu {cpu} mr 0x60a24df041b0 addr 0x{a:x} value 0x0 size 1 name '{name}'"
QEMU11 = "{iso}Z memory_region_ops_{op} cpu {cpu} mr 0x10485d240 addr 0x{a:x} value 0x0 size 1 name '{name}'"


@pytest.mark.parametrize('fmt', ['10', '11'])
def test_ovmf_devices_counts_only_the_cpus_accesses_before_the_stop_and_none_when_it_sees_nothing(fmt):
    import datetime
    def line(t, op, cpu, a, name):
        if fmt == '10': return QEMU10.format(t=t, op=op, cpu=cpu, a=a, name=name)
        iso = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')
        return QEMU11.format(iso=iso, op=op, cpu=cpu, a=a, name=name)
    t0 = 1795352624.5
    trace = '\n'.join([line(t0 - 2, 'write', -1, 0xfee00000, 'apic-msi'), line(t0 - 1, 'read', 0, 0x80, 'io'),
                       line(t0 - 0.5, 'read', 0, 0x80, 'io'), line(t0 + 1, 'read', 0, 0x402, 'isa-debugcon')]) + '\n'
    assert ovmf.devices(trace, t0) == [{'op': 'read', 'region': 'io', 'addr': '0x80', 'size': 1, 'count': 2}]
    assert ovmf.devices(trace, t0 - 1.5) == []
    assert ovmf.devices(trace, t0 + 2) is None, 'nothing after the stop: the trace proves nothing'
    assert ovmf.devices('2026-11-22 something else\n', t0) is None


def _fv_image(size=0x400000, bfv=BFV):
    img = bytearray(b'\xff' * size)
    base = (1 << 32) - size
    o = bfv - base
    img[o + 0x10:o + 0x20] = ovmf.FFS_GUIDS[1]
    img[o + 0x20:o + 0x28] = struct.pack('<Q', (1 << 32) - bfv)
    img[o + 0x28:o + 0x2C] = b'_FVH'
    img[-16:] = bytes.fromhex('0f20c0a8017405') + b'\xe9' + struct.pack('<i', -0xF4) + b'\xe9' + struct.pack('<h', -0x10F) + b'\x00'
    return bytes(img)


def test_ovmf_image_parsing_finds_the_boot_volume_sec_and_the_reset_vectors_targets():
    img = _fv_image()
    assert ovmf.boot_fv(img) == BFV
    rv = ovmf.reset_vector(img)
    assert rv['main32'] == 0xFFFFFF08 and rv['early_bsp_init_real16'] == 0xFFFFFEF0
    m = ('SecMain (Fixed Flash Address, BaseAddress=0x00fffcc094, EntryPoint=0x00fffd5e24, Type=PE)\n'
         '(GUID=DF1CCEF6-F301-4A63-9661-FC6030DCC880 .textbaseaddress=0x00fffcc2d4 .databaseaddress=0x00fffda314)\n')
    assert ovmf.sec_entry('EFI_FV_TOTAL_SIZE = 0x34000\n\n' + m) == SEC
    with pytest.raises(SystemExit): ovmf.sec_entry('PeiCore (Fixed Flash Address, EntryPoint=0x1)\n')
    with pytest.raises(SystemExit): ovmf.boot_fv(b'\xff' * 0x100000)


def test_ovmf_answer_masks_af_and_rf_and_cuts_the_debug_log_at_secs_first_line():
    got = {'stop': 'T05thread:01;', 'regs': {'eflags': 0x10056, 'rip': SEC}, 'resumed_at': 0}
    a = ovmf.answer(got, b'\r\nSecCoreStartupWithStack(0xFFFCC000, 0x820000)\r\nPEI line\n', None)
    assert a == {'stop': 'breakpoint', 'regs': {'eflags': 0x46, 'rip': SEC}, 'devices': None,
                 'debugcon': ['SecCoreStartupWithStack(0xFFFCC000, 0x820000)']}
    assert ovmf.answer({'stop': 'timeout'}, b'', None)['debugcon'] == ['(no SEC line)']


def test_ovmf_keys_follow_the_upstream_and_never_the_built_image():
    p = {'upstream': {'sources': {'edk2-src': '00' * 32}, 'recipe_sha256': 'ab' * 32, 'image': 'i'}, 'built': {'OVMF.fd': '1' * 64}}
    k = ovmf.key('reset', 'kvm', p)
    assert k == ovmf.key('reset', 'kvm', {**p, 'built': {'OVMF.fd': '2' * 64}})
    assert k != ovmf.key('main32', 'kvm', p) and k != ovmf.key('reset', 'kvm', {**p, 'upstream': {**p['upstream'], 'image': 'j'}})


# --- the GDB client against a fake stub: QEMU's target description layout, `g` before `p`, Z1, a stop, the CPU there.

TARGET = '<?xml version="1.0"?><!DOCTYPE target SYSTEM "gdb-target.dtd"><target><xi:include href="core.xml"/></target>'
CORE = ('<feature name="org.gnu.gdb.i386.core">' + ''.join(f'<reg name="{r}" bitsize="{32 if r in ("eflags", "cs", "ss", "ds", "es", "fs", "gs") else 64}"/>'
                                                          for r in gdbrsp.REGS) + '</feature>')


class FakeStub(threading.Thread):
    """Answers like QEMU's gdbstub in ack mode: `p` gives the copy of the CPU, which only `g` refreshes (as under KVM)."""
    def __init__(self, sock, live):
        super().__init__(daemon=True)
        self.sock, self.live, self.copy, self.log, self.bps = sock, live, {r: 0 for r in gdbrsp.REGS}, [], set()

    def reply(self, p):
        if p.startswith('qSupported'): return 'PacketSize=1000;qXfer:features:read+'
        if p.startswith('qXfer:features:read:'):
            annex, rng = p[len('qXfer:features:read:'):].rsplit(':', 1)
            text = {'target.xml': TARGET, 'core.xml': CORE}[annex]
            off, n = (int(x, 16) for x in rng.split(','))
            return ('l' if off + n >= len(text) else 'm') + text[off:off + n]
        if p == '?': return 'T05thread:01;'
        if p == 'g': self.copy = dict(self.live); return '00' * 8
        if p[0] == 'p':
            r = gdbrsp.REGS[int(p[1:], 16)]
            return self.copy[r].to_bytes(4 if r in ('eflags', 'cs', 'ss', 'ds', 'es', 'fs', 'gs') else 8, 'little').hex()
        if p[0] in 'Zz': (self.bps.add if p[0] == 'Z' else self.bps.discard)(int(p.split(',')[1], 16)); return 'OK'
        if p == 'c':
            self.live.update(rip=min(self.bps), cs=0x38, rbp=BFV); return 'T05thread:01;'
        if p == 'k': return None
        return ''

    def run(self):
        buf = b''
        while True:
            try: data = self.sock.recv(4096)
            except OSError: return
            if not data: return
            buf += data
            while True:
                buf = buf.lstrip(b'+')
                if buf.startswith(b'\x03'): buf = buf[1:]; self.sock.sendall(b'$T02#b6'); continue
                i = buf.find(b'#')
                if not buf.startswith(b'$') or i < 0 or len(buf) < i + 3: break
                p, buf = buf[1:i].decode(), buf[i + 3:]
                self.log.append(p)
                self.sock.sendall(b'+')
                r = self.reply(p)
                if r is None: return
                self.sock.sendall(b'$' + r.encode() + b'#' + b'%02x' % (sum(r.encode()) & 0xFF))


def test_gdbrsp_reads_the_cpu_at_the_breakpoint_after_refreshing_it(tmp_path, monkeypatch):
    a, b = socket.socketpair()
    live = {r: 0 for r in gdbrsp.REGS} | {'rip': 0xFFF0, 'cs': 0xF000}
    stub = FakeStub(b, live); stub.start()

    class Local(gdbrsp.Stub):
        def __init__(self, where, timeout=30):
            self.s, self.buf, self.ack = a, b'', True
            self.s.settimeout(timeout)
    monkeypatch.setattr(gdbrsp, 'Stub', Local)
    got = gdbrsp.session('unix:/nowhere', SEC, timeout=5)
    assert got['stop'].startswith('T05') and got['breakpoint'] == 'hw' and got['other_stops'] == []
    assert got['regs']['rip'] == SEC and got['regs']['cs'] == 0x38 and got['regs']['rbp'] == BFV
    i = stub.log.index('c')
    assert 'g' in stub.log[i:i + 2], f'no refresh before reading the CPU at the stop: {stub.log[i:]}'
    assert f'Z1,{SEC:x},1' in stub.log and stub.log[-1] == 'k'
