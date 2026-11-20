#!/usr/bin/env python3
"""M2 task D: the `natemu` stub's diagnostics, on a minimal bzImage whose 64-bit entry is a few instructions.

`--case` picks the guest and what its report must show (standard error):
  unsupported  MOV DR7 arming an I/O breakpoint with CR4.DE clear (natemu stops: unsupported_system): the stop's report,
               exact RIP at the MOV, its bytes, RAX as set.
  spin         `inc rax; jmp` back: SIGINFO until two inspections find it in translated code with different RAX (the run
               goes on after each), then the watchdog's report: the RIP mapped from the native PC (approximate), RAX from the
               Arm registers.
  host-wait    `out dx, al` in a loop to COM1 on a standard output nobody reads: the pipe fills and the vCPU waits in the
               host's write. SIGINFO until an inspection finds it there, then one report of the host-side progress check
               while the run goes on; then SIGTERM, standard output drained, and the run ends cancelled.
Each passes with exit status 2 and the lines it expects; writes OUT/results.json, OUT/stderr.txt.

    python3 tests/diag/run_diag.py --stub NATEMU --runtime RESIDENT --fexc FEXC --out DIR --case NAME
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, os, pathlib, signal, struct, subprocess, sys, time

HOST_CHECK = 'the host-side progress check'
CODE = {
    # mov eax, 0x20001 (DR7: L0, RW0 = 10b, an I/O breakpoint); mov dr7, rax; then a hlt loop never reached
    'unsupported': bytes.fromhex('b801000200' '0f23f8' 'f4ebfd'),
    # inc rax; jmp back to the inc
    'spin': bytes.fromhex('48ffc0' 'ebfb'),
    # mov dx, 0x3f8; mov al, 'x'; out dx, al; jmp back to the out
    'host-wait': bytes.fromhex('66baf803' 'b078' 'ee' 'ebfd'),
}
WATCHDOG = 4  # seconds (debug.watchdog)


def bzimage(code: bytes) -> bytes:
    """crates/host/src/boot.rs's test image: protocol 2.15, relocatable, 64-bit entry at +0x200 of 4 KiB of code."""
    pm = 0x1000
    d = bytearray(1024 + pm)
    put = lambda at, fmt, v: struct.pack_into(fmt, d, at, v)
    d[0x1f1] = 1                                  # setup_sects
    put(0x1f4, '<I', pm // 16)                    # syssize
    put(0x1fe, '<H', 0xaa55)
    d[0x200:0x202] = b'\xeb\x6a'
    d[0x202:0x206] = b'HdrS'
    put(0x206, '<H', 0x020f)
    d[0x211] = 1                                  # LOADED_HIGH
    put(0x22c, '<I', 0x7fffffff)                  # initrd_addr_max
    put(0x230, '<I', 0x200000)                    # kernel_alignment
    d[0x234], d[0x235] = 1, 21                    # relocatable, min_alignment
    put(0x236, '<H', 3)                           # XLF_KERNEL_64 | XLF_CAN_BE_LOADED_ABOVE_4G
    put(0x238, '<I', 2047)                        # cmdline_size
    put(0x258, '<Q', 0x1000000)                   # pref_address
    put(0x260, '<I', 0x400000)                    # init_size
    d[1024 + 0x200:1024 + 0x200 + len(code)] = code
    return bytes(d)


def main():
    ap = argparse.ArgumentParser()
    for a in ('--stub', '--runtime', '--fexc', '--out'):
        ap.add_argument(a, required=True)
    ap.add_argument('--case', required=True, choices=sorted(CODE))
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    kernel = out / 'kernel.bin'
    kernel.write_bytes(bzimage(CODE[a.case]))
    err_path = out / 'stderr.txt'
    cmd = [a.stub, '--runtime', a.runtime, '--fexc', a.fexc, '--kernel', str(kernel), '--ram', '256M',
           '--set', f'debug.watchdog={WATCHDOG}']
    if a.case != 'host-wait':
        cmd += ['--serial', str(out / 'serial.txt')]
    problems = []
    with open(err_path, 'wb') as err:
        # host-wait: standard output is a pipe nobody reads, so COM1's output blocks once it fills.
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stderr=err,
                             stdout=subprocess.PIPE if a.case == 'host-wait' else subprocess.DEVNULL)
        text = lambda: err_path.read_text(errors='replace')
        alive = lambda why: p.poll() is None or problems.append(f'the run ended {why}')
        inspected = lambda want: [r for r in reports(text()) if want in r and 'inspected (it runs on)' in r]
        rc = None
        if a.case != 'unsupported':
            # Ask until inspections show the vCPU where the case puts it (an early one may find it compiling).
            want, n = ('in translated code', 2) if a.case == 'spin' else ('serving a port access', 1)
            t0 = time.monotonic()
            while len(set(rax(r) for r in inspected(want))) < n and p.poll() is None and time.monotonic() - t0 < 60:
                p.send_signal(signal.SIGINFO)
                time.sleep(0.5)
                alive('at an inspection')
            if len(set(rax(r) for r in inspected(want))) < n:
                problems.append(f'not {n} inspection reports with {want!r} (and different RAX)')
        if a.case == 'host-wait':
            t0 = time.monotonic()
            while HOST_CHECK not in text() and p.poll() is None and time.monotonic() - t0 < WATCHDOG + 30:
                time.sleep(0.5)
            time.sleep(1)
            alive('at the host-side progress check')
            p.send_signal(signal.SIGTERM)
            try:
                p.communicate(timeout=60)      # drains standard output: the vCPU's write returns and it sees the cancel
            except subprocess.TimeoutExpired:
                pass
        try:
            rc = p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
            problems.append('no exit within 120 s')
    if rc != 2:
        problems.append(f'exit status {rc}, not 2')
    final = [r for r in reports(text()) if 'inspected' not in r]
    last = final[-1] if final else ''
    expect = {
        'unsupported': ['stop Resident { stop: 12', '(unsupported_system)', 'stopped at an instruction boundary', '(exact), 64-bit mode, CPL 0',
                        'bytes at rip: 0f 23 f8', 'rax 0x0000000000020001', 'code identity 0x'],
        'spin': ['stop Watchdog', 'in translated code at native pc', '(approximate: an instruction in [', 'the block has no access sites',
                 'bytes at rip: 48 ff c0 eb fb', 'code identity 0x'],
        'host-wait': [HOST_CHECK, '(the run goes on)', 'serving a port access', '(exact, as of that exit)',
                      'bytes at rip: ee eb fd', 'rdx 0x00000000000003f8', 'code identity 0x'],
    }[a.case]
    problems += [f'final report lacks {e!r}' for e in expect if e not in last]
    if a.case == 'spin' and rax(last) in (None, 0):
        problems.append('the watchdog report has no RAX count')
    if a.case == 'host-wait' and text().count(HOST_CHECK) != 1:
        problems.append(f'{text().count(HOST_CHECK)} host-side progress reports, not one for the one stay')
    print(text())
    print('\n'.join(problems) or 'pass')
    (out / 'results.json').write_text(json.dumps({'pass': not problems, 'errors': problems}))
    sys.exit(1 if problems else 0)


def rax(report: str):
    """The RAX a report shows."""
    for line in report.splitlines():
        if ': rax ' in line:
            return int(line.split(': rax ')[1].split()[0], 16)
    return None


def reports(text: str) -> list:
    """Each vCPU report: from a `natemu vCPU N: <what>` line through its `code identity` line."""
    out, cur = [], None
    for line in text.splitlines():
        if cur is None and line.startswith('natemu vCPU ') and 'code identity' not in line:
            cur = [line]
        elif cur is not None:
            cur.append(line)
            if 'code identity' in line:
                out.append('\n'.join(cur))
                cur = None
    return out


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
