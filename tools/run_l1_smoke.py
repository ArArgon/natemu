#!/usr/bin/env python3
"""L1 smoke test (owner-allowed, boot-features plan §5 Q8): the Debian bzImage's decompressor runs in natemu from the
loader's 64-bit entry state up to its jump into the decompressed kernel, and the same memory image and entry state run
under QEMU to the same point.

    tools/with-env python3 tools/run_l1_smoke.py --out DIR --host HOST --runtime RUNTIME --fexc FEXC [--ram BYTES]
        [--kernel BZIMAGE] [--vmlinux ELF] [--cmdline TEXT] [--keep]

The stop RIP is the decompressed kernel's entry: the `e_entry` of `--vmlinux` (the ELF in the bzImage's payload,
extracted by `experiments/track03/kernel-census/extract.py`), a physical address; with `nokaslr` the decompressor
places that ELF at its link address. natemu: `boot-linux --stop-at ENTRY` (the run ends when the BSP is about to execute
it), `--dump` (what the loader wrote) and `--save` (the kernel's init_size window afterwards). QEMU (Homebrew
qemu-system-x86_64, TCG, `-cpu Haswell`: no LA57, extended leaves to 0x80000008 as natemu's profile): no BIOS; a
64 KiB firmware built here goes from the reset vector to long mode and enters the loader's state (CR0/CR3/CR4/EFER,
GDTR, IDTR, CS/DS/ES/SS/FS/GS, RFLAGS, RSP, RSI, every other GPR 0, FNINIT) at the loader's RIP; the loader's writes
are `-device loader` blobs at their guest-physical addresses (RAM is otherwise zero); a gdbstub hardware breakpoint at
ENTRY stops it, `info registers` and `pmemsave` read the state and the window.

`--ram` (default 2 GiB, the boot map's default L) is both natemu's guest RAM (M1: a run-time size) and QEMU's `-m`.

Compared: the zero page after the decompressor; every GPR, RIP, RFLAGS, CR0, CR2, CR3, CR4, EFER, the segment selectors, GDTR and IDTR; the window's bytes
(each vmlinux PT_LOAD segment also against the ELF's file bytes, and the whole window, where the only accepted
difference is QEMU's missing accessed bit on a far-transfer CS load, S2b's known QEMU difference). Writes DIR/smoke.json; exit 1 on
a difference or a natemu stop other than reaching ENTRY."""
import argparse, hashlib, json, os, pathlib, re, socket, struct, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT.parent / 'natemu-raw/kernel-census-20261028-01'
QEMU = '/opt/homebrew/bin/qemu-system-x86_64'
REGS = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi'] + [f'r{i}' for i in range(8, 16)]
ROM = 0xFFFF0000
# Where the firmware copies its paging-on part (paging maps only the loader's identity range, not the ROM): the
# reserved loader-page-table range above the loader's tables (no compared byte).
COPY = 0x1F0000
ZERO_PAGE = 0x10000  # the loader's boot_params (crates/host/src/boot.rs ZERO_PAGE, claim 69's map)


def elf_loads(elf):
    entry, phoff, phnum = struct.unpack_from('<Q', elf, 0x18)[0], struct.unpack_from('<Q', elf, 0x20)[0], struct.unpack_from('<H', elf, 0x38)[0]
    segs = []
    for i in range(phnum):
        t, _, off, _, pa, fs, _, _ = struct.unpack_from('<IIQQQQQQ', elf, phoff + 56 * i)
        if t == 1:
            segs.append((pa, off, fs))
    return entry, segs


def firmware(e):
    """Reset vector -> protected mode -> (copied to COPY) long mode under the loader's CR3 -> the loader's GDT/IDT/segments
    -> its state."""
    n = lambda v: int(v, 0)  # noqa: E731
    gpr = [n(x) for x in e['gpr']]
    seg = [n(x) for x in e['seg']]  # ES CS SS DS FS GS
    assert seg[0] == seg[2] == seg[3] and seg[4] == seg[5], 'firmware loads one data selector and one FS/GS selector'
    zero = '\n'.join(f'  movabs ${gpr[i]:#x}, %{r}' for i, r in enumerate(REGS) if r not in ('rsp',))
    asm = f'''
.code16
.org 0xf000
start:
  cli
  cld
  lgdtl %cs:(fw_gdt_ptr - start + 0xf000)
  movl %cr0, %eax
  orl $1, %eax
  movl %eax, %cr0
  ljmpl $0x08, ${ROM:#x} + (pm32 - start + 0xf000)
.code32
pm32:
  movw $0x10, %ax
  movw %ax, %ds
  movw %ax, %es
  movw %ax, %ss
  movl ${ROM:#x} + (low - start + 0xf000), %esi
  movl ${COPY:#x}, %edi
  movl $(end - low), %ecx
  rep movsb
  movl ${COPY:#x}, %eax
  jmp *%eax
low:
  lgdt {COPY:#x} + (ram_gdt_ptr - low)
  movl ${n(e['cr4']):#x}, %eax
  movl %eax, %cr4
  movl ${n(e['cr3']):#x}, %eax
  movl %eax, %cr3
  movl $0xc0000080, %ecx
  movl ${n(e['efer']) & ~0x400:#x}, %eax
  xorl %edx, %edx
  wrmsr
  movl ${n(e['cr0']):#x}, %eax
  movl %eax, %cr0
  ljmp $0x18, ${COPY:#x} + (pm64 - low)
.code64
pm64:
  lgdt gdt_ptr(%rip)
  lidt idt_ptr(%rip)
  movl ${seg[3]:#x}, %eax
  movw %ax, %ds
  movw %ax, %es
  movw %ax, %ss
  movl ${seg[4]:#x}, %eax
  movw %ax, %fs
  movw %ax, %gs
  movq $0x200000, %rsp
  pushq ${seg[1]:#x}
  leaq final(%rip), %rax
  pushq %rax
  lretq
final:
  fninit
  pushq ${n(e['rflags']):#x}
  popfq
  movabs ${gpr[4]:#x}, %rsp
{zero}
  jmp *entry(%rip)
.balign 8
entry: .quad {n(e['rip']):#x}
gdt_ptr: .word {n(e['gdtr'][1]):#x}
  .quad {n(e['gdtr'][0]):#x}
idt_ptr: .word {n(e['idtr'][1]):#x}
  .quad {n(e['idtr'][0]):#x}
.balign 8
fw_gdt: .quad 0, 0x00cf9b000000ffff, 0x00cf93000000ffff, 0x00af9b000000ffff
fw_gdt_ptr: .word 31
  .long {ROM:#x} + (fw_gdt - start + 0xf000)
ram_gdt_ptr: .word 31
  .long {COPY:#x} + (fw_gdt - low)
end:
.org 0xfff0
  jmp start
.org 0x10000
'''
    with tempfile.TemporaryDirectory() as d:
        src, obj = pathlib.Path(d, 'fw.s'), pathlib.Path(d, 'fw.o')
        src.write_text(asm)
        subprocess.run(['clang', '-c', '-target', 'x86_64-none-elf', src, '-o', obj], check=True)
        o = obj.read_bytes()
    shoff = struct.unpack_from('<Q', o, 0x28)[0]
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', o, 0x3a)
    secs = [struct.unpack_from('<IIQQQQIIQQ', o, shoff + i * shentsize) for i in range(shnum)]
    names = secs[shstrndx]
    name = lambda s: o[names[4] + s[0]:].split(b'\0', 1)[0]  # noqa: E731
    assert not any(name(s).startswith(b'.rela') for s in secs), 'firmware needs relocations'
    text = next(s for s in secs if name(s) == b'.text')
    rom = o[text[4]:text[4] + text[5]]
    assert len(rom) == 0x10000, hex(len(rom))
    return rom


class Gdb:
    """Minimal GDB remote-protocol client (packets with acks)."""

    def __init__(self, port):
        for _ in range(100):
            try:
                self.s = socket.create_connection(('127.0.0.1', port), timeout=900)
                break
            except OSError:
                time.sleep(0.1)
        self.buf = b''

    def _byte(self):
        while not self.buf:
            self.buf = self.s.recv(65536)
            if not self.buf:
                raise EOFError('gdbstub closed')
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def send(self, data):
        self.s.sendall(b'$' + data + b'#%02x' % (sum(data) & 0xFF))
        while (b := self._byte()) != b'+':
            if b == b'-':
                raise RuntimeError('nack')

    def recv(self):
        while self._byte() != b'$':
            pass
        data = b''
        while (b := self._byte()) != b'#':
            data += b
        self._byte(), self._byte()
        self.s.sendall(b'+')
        return data

    def cmd(self, data):
        self.send(data)
        return self.recv()

    def monitor(self, text):
        self.send(b'qRcmd,' + text.encode().hex().encode())
        out = ''
        while (r := self.recv()) != b'OK':
            if not r.startswith(b'O'):
                raise RuntimeError(f'monitor {text}: {r!r}')
            out += bytes.fromhex(r[1:].decode()).decode()
        return out


def qemu_run(work, e, dump, ram, stop, save_at, save_len, port=47321):
    (work / 'fw.bin').write_bytes(firmware(e))
    blobs, o, k = [], 0, 0
    while o < len(dump):
        a, n = struct.unpack_from('<QQ', dump, o)
        p = work / f'blob{k}.bin'
        p.write_bytes(dump[o + 16:o + 16 + n])
        blobs += ['-device', f'loader,file={p},addr={a:#x},force-raw=on']
        o, k = o + 16 + n, k + 1
    cmd = [QEMU, '-machine', 'pc', '-accel', 'tcg', '-cpu', 'Haswell', '-m', f'{ram >> 20}M', '-bios', str(work / 'fw.bin'),
           '-nodefaults', '-no-reboot', '-display', 'none', '-serial', 'none', '-monitor', 'none', '-S', '-gdb', f'tcp:127.0.0.1:{port}', *blobs]
    q = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        g = Gdb(port)
        g.cmd(b'?')
        assert g.cmd(b'Z1,%x,1' % stop) == b'OK'
        t0 = time.time()
        g.send(b'c')
        stopped = g.recv()
        seconds = time.time() - t0
        regs = g.monitor('info registers')
        out = work / 'qemu-window.bin'
        saved = g.monitor(f'pmemsave {save_at:#x} {save_len:#x} "{out}"')
        if not out.exists():
            raise RuntimeError(f'pmemsave: {saved!r}')
        zp_out = work / 'qemu-zero-page.bin'
        g.monitor(f'pmemsave {ZERO_PAGE:#x} 0x1000 "{zp_out}"')
        g.send(b'k')
    finally:
        q.kill()
        _, err = q.communicate(timeout=30)
    return dict(stopped=stopped.decode(), seconds=round(seconds, 3), registers=regs, stderr=err.strip()[-2000:]), out, zp_out


def parse_info(text):
    """QEMU `info registers` (x86-64) -> dict."""
    r = {k.lower(): int(v, 16) for k, v in re.findall(r'\b(R[A-Z0-9]{1,2}|RIP|RFL|CR[0234]|EFER) *=([0-9a-f]+)', text)}
    out = {name: r[name] for name in REGS}
    out.update(rip=r['rip'], rflags=r['rfl'], cr0=r['cr0'], cr2=r['cr2'], cr3=r['cr3'], cr4=r['cr4'], efer=r['efer'])
    for s in ('ES', 'CS', 'SS', 'DS', 'FS', 'GS'):
        out[s.lower()] = int(re.search(rf'^{s} =([0-9a-f]{{4}})', text, re.M).group(1), 16)
    for t in ('GDT', 'IDT'):
        base, limit = re.search(rf'^{t}= *([0-9a-f]+) ([0-9a-f]+)', text, re.M).groups()
        out[t.lower() + 'r'] = (int(base, 16), int(limit, 16))
    return out


def natemu_state(s):
    n = lambda v: int(v, 0)  # noqa: E731
    out = {name: n(v) for name, v in zip(REGS, s['gpr'])}
    out.update({k: n(s[k]) for k in ('rip', 'rflags', 'cr0', 'cr2', 'cr3', 'cr4', 'efer')})
    out.update({k: n(v) for k, v in zip(('es', 'cs', 'ss', 'ds', 'fs', 'gs'), s['seg'])})
    out.update(gdtr=tuple(map(n, s['gdtr'])), idtr=tuple(map(n, s['idtr'])))
    return out


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--out', required=True, type=pathlib.Path)
    a.add_argument('--host', required=True)
    a.add_argument('--runtime', required=True)
    a.add_argument('--fexc', required=True)
    a.add_argument('--ram', type=lambda v: int(v, 0), default=2 << 30)
    a.add_argument('--kernel', type=pathlib.Path, default=RAW / 'dl/linux')
    a.add_argument('--vmlinux', type=pathlib.Path, default=RAW / 'x/vmlinux.elf')
    a.add_argument('--cmdline', default='mitigations=off nokaslr')
    a.add_argument('--keep', action='store_true')
    o = a.parse_args()
    w = o.out
    w.mkdir(parents=True, exist_ok=True)
    elf = o.vmlinux.read_bytes()
    entry, segs = elf_loads(elf)
    report = dict(kernel=str(o.kernel), kernel_sha256=hashlib.sha256(o.kernel.read_bytes()).hexdigest(), cmdline=o.cmdline,
                  ram=hex(o.ram), stop=hex(entry))
    cmd = [o.host, o.runtime, 'boot-linux', o.fexc, o.kernel, '--ram', hex(o.ram), '--cmdline', o.cmdline,
           '--stop-at', hex(entry), '--dump', w / 'loaded.dump', '--watchdog-ms', '300000']
    probe = subprocess.run(cmd + ['--load-only'], capture_output=True, text=True)
    if probe.returncode:
        report.update(natemu_stop='load_refused', reason=probe.stderr.strip())
        print(json.dumps(report, indent=1))
        (w / 'smoke.json').write_text(json.dumps(report, indent=1))
        sys.exit(1)
    window = int(json.loads(probe.stdout)['window'], 0)
    kernel = int(json.loads(probe.stdout)['kernel'], 0)
    for attempt in range(2):  # one retry on concurrent hv_vm_create (0xfae94007)
        r = subprocess.run(cmd + ['--save', hex(kernel), hex(window), w / 'natemu-window.bin', '--save', hex(ZERO_PAGE), '0x1000',
                                  w / 'natemu-zero-page.bin'], capture_output=True, text=True)
        if r.returncode == 0 or 'fae94007' not in r.stderr:
            break
        time.sleep(5)
    if r.returncode:
        report.update(natemu_stop='error', reason=r.stderr.strip()[-2000:])
        (w / 'smoke.json').write_text(json.dumps(report, indent=1))
        print(json.dumps(report, indent=1))
        sys.exit(1)
    nat = json.loads(r.stdout)
    report.update(natemu_stop=nat['stop'], natemu_reason=nat['reason'], natemu_stats=nat['stats'], entry_state=nat['entry'])
    ok = nat['stop'] == 'reached'
    if ok:
        q, qwin, qzp = qemu_run(w, nat['entry'], (w / 'loaded.dump').read_bytes(), o.ram, entry, kernel, window)
        report['qemu'] = {k: v for k, v in q.items() if k != 'registers'}
        (w / 'qemu-registers.txt').write_text(q['registers'])
        ns, qs = natemu_state(nat['state']), parse_info(q['registers'])
        sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
        report['registers'] = {k: [hex(ns[k]) if isinstance(ns[k], int) else [hex(x) for x in ns[k]],
                                   hex(qs[k]) if isinstance(qs[k], int) else [hex(x) for x in qs[k]]] for k in ns}
        diffs = [k for k in ns if ns[k] != qs[k]]
        report['register_differences'] = diffs
        nb, qb = (w / 'natemu-window.bin').read_bytes(), qwin.read_bytes()
        nz, qz = (w / 'natemu-zero-page.bin').read_bytes(), qzp.read_bytes()
        report['zero_page_after'] = dict(natemu=sha(nz), qemu=sha(qz), equal=nz == qz,
                                         differing=[hex(ZERO_PAGE + i) for i in range(len(nz)) if nz[i] != qz[i]][:20])
        report['window'] = dict(start=hex(kernel), bytes=hex(window), natemu=sha(nb), qemu=sha(qb), equal=nb == qb)
        report['segments'] = []
        for pa, off, fs in segs:
            at = pa - kernel
            report['segments'].append(dict(paddr=hex(pa), bytes=hex(fs), natemu=sha(nb[at:at + fs]), qemu=sha(qb[at:at + fs]),
                                           elf=sha(elf[off:off + fs]), equal=nb[at:at + fs] == qb[at:at + fs] == elf[off:off + fs]))
        # Known QEMU difference (boot-features plan, Built (S2b)): QEMU sets no accessed bit for a CS load by a far
        # transfer, where the SDM (and natemu) set it. Accepted only as a descriptor's type byte in the final GDT that
        # differs in bit 0 alone, natemu's set.
        base, limit = ns['gdtr']
        explained, unexplained = [], []
        for i in (i for i in range(min(len(nb), len(qb))) if nb[i] != qb[i]):
            a = kernel + i
            # A code or data descriptor's type byte (S = 1 and type bit 3: code), differing in A alone.
            known = base <= a <= base + limit and (a - base) % 8 == 5 and qb[i] & 0x18 == 0x18 and nb[i] == qb[i] | 1
            (explained if known else unexplained).append(dict(gpa=hex(a), natemu=hex(nb[i]), qemu=hex(qb[i])))
        report['window'].update(qemu_differs_accessed_bit=explained, unexplained=unexplained[:20], unexplained_count=len(unexplained))
        ok = not diffs and len(nb) == len(qb) and not unexplained and nz == qz and all(s['equal'] for s in report['segments'])
        if not o.keep:
            for p in [qwin, qzp, w / 'natemu-zero-page.bin', w / 'natemu-window.bin', w / 'loaded.dump', *w.glob('blob*.bin')]:
                p.unlink(missing_ok=True)
    report['ok'] = ok
    (w / 'smoke.json').write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k not in ('registers', 'entry_state')}, indent=1))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
