"""Static expected final states of system-mode programs (Part C of the campaign-onboarding plan: an expectation that
follows from the program is written in the test, not answered by QEMU). `state(prog, cpus, writes)` gives the shape
smp.parse_dump returns: per CPU the program's initial registers with the given ones changed, RFLAGS and the fault record;
the captured memory regions' initial bytes with the given stores applied. smp.compare compares natemu's state with it.

    import expect
    want = expect.state(prog, [dict(expect.RESTORED, r8=1) for _ in range(prog['ncpu'])], writes=[(addr, bytes)])

`labels(text)` gives the offsets of a body's labels (from nasm's map), for registers that hold a code address
(`lea r13, [rel .c0]`)."""
import pathlib, re, subprocess, tempfile

import smp

# programs.RESTORE (every `system` body's end): its last writes of rax, rcx and rdx, then `cmp r11, r11` (ZF, PF set).
RESTORED = {'rax': 0, 'rcx': 0xC0000080, 'rdx': 0}
CMP_EQUAL = 0x46                       # RFLAGS after `cmp r11, r11`: bit 1, PF, ZF (the rest of the six clear)


def state(prog, cpus, writes=(), rflags=CMP_EQUAL):
    """`cpus`: per CPU a dict of register -> final value (the rest keep their initial values), with optional 'rflags'
    and 'fault' ((vector, error code, RIP, CR2) or None; `system` programs only). `writes`: (address, bytes) stores in
    program order; bytes outside the captured regions are not compared and are dropped."""
    out = []
    for i, c in enumerate(cpus):
        g = [smp.num(x) for x in prog['cpus'][i]['gpr']]
        for r, v in c.items():
            if r in ('rflags', 'fault'): continue
            g[smp.REGS.index(r)] = v & smp.M64
        cpu = {'gpr': g, 'rflags': c.get('rflags', rflags)}
        if prog.get('system'): cpu['fault'] = c.get('fault')
        out.append(cpu)
    memory = []
    for a, size, cap, r in smp.regions(prog):
        if not cap: continue
        buf = bytearray(smp.region_bytes(r))
        for at, data in writes:
            lo, hi = max(a, at), min(a + size, at + len(data))
            if lo < hi: buf[lo - a:hi - a] = data[lo - at:hi - at]
        memory.append({'addr': a, 'data': bytes(buf)})
    return {'cpus': out, 'memory': memory}


def q(addr, value, size=8):
    """A store of `size` bytes (little-endian) at `addr`, for `writes`."""
    return (addr, (value & ((1 << 8 * size) - 1)).to_bytes(size, 'little'))


def labels(text):
    """{label: offset} of the assembled body `text` (assembled at 0, as programs.asm does), from nasm's symbol map."""
    with tempfile.TemporaryDirectory() as d:
        src, m = pathlib.Path(d, 'b.asm'), pathlib.Path(d, 'b.map')
        src.write_text(f'bits 64\ndefault abs\n[map symbols {m}]\n' + text + '\n')
        subprocess.run(['nasm', '-f', 'bin', str(src), '-o', str(pathlib.Path(d, 'b.bin'))], check=True)
        rows = re.findall(r'^\s*([0-9A-F]+)\s+[0-9A-F]+\s+(\S+)\s*$', m.read_text(), re.M)
    return {name: int(off, 16) for off, name in rows}


def last_r13(text):
    """The label the last `lea r13, [rel L]` of `text` names (None without one): in the straight-line bodies with loops
    and handlers placed after them that the K1 programs are, the last one run (calls return to r13)."""
    found = re.findall(r'lea r13, \[rel (\.\w+)\]', text)
    return found[-1] if found else None
