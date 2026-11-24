#!/usr/bin/env python3
"""The devices family's virtio-console runner: Linux's built-in virtio_console driver against natemu's virtio console
(`[[console]]`, crates/host/src/virtio/console.rs), through the `natemu` CLI with a config file.

prepare builds one small initramfs: Debian's unchanged BusyBox, the stock virtio_mmio module from the Debian kernel
package (CONFIG_VIRTIO_MMIO=m; virtio_console and virtio_pci are built in), the 1 MiB pattern console-file writes, and
an init that runs the case the kernel command line names (`console_case=file|socket`, `console_transport=mmio|pci`).
run boots one VM (VM_SECONDS at most) and judges it from the guest's serial markers, what the host end of the console
received, and the host's exit line; it never builds or fetches anything. self-check runs the judges on synthetic runs
and the init through `sh -n`, without a VM.

The guest's tty (checked in Linux 6.12's drivers/tty/hvc/hvc_console.c, drivers/char/virtio_console.c,
drivers/tty/n_tty.c and BusyBox 1.37's stty):
- hvc's tty driver is TTY_DRIVER_RESET_TERMIOS: its settings go back to the defaults (ONLCR adds CR before LF, ICRNL,
  ECHO, ICANON) when the last descriptor closes. So the init opens /dev/hvc0 on descriptor 3 first and only then runs
  `stty -F /dev/hvc0 raw -echo` (BusyBox raw: c_iflag 0, -opost, -isig -icanon; with -echo n_tty takes its raw path),
  and keeps descriptor 3 open to the end.
- When the device has every byte: hvc_write copies at most a page into the port's outbuf and calls virtio_console's
  put_chars, which adds the buffer, kicks and spins until the device returns it (`__send_to_port`, nonblock=false),
  then returns the whole count, so hvc_write's loop only returns once every byte was returned by the device (the khvcd
  thread pushes only if a push failed). `dd`'s exit therefore means the model holds (or its backend has written) the
  whole pattern; hvc_close's HVC_CLOSE_WAIT (10 ms) never comes into it, since the descriptor stays open. As a guard
  for the failure path (a push that failed leaves bytes for khvcd), the init then drains /dev/hvc0 the way it drains
  COM1: `stty -g` and back, which BusyBox applies with TCSADRAIN, i.e. tty_wait_until_sent(tty, 0) until hvc's
  chars_in_buffer (its outbuf) is empty, with no timeout. Both return at once when nothing is left, so they cannot hide
  a device that loses its tail: no sleep or wait stands between the write and the power-off.

console-file: the init writes the 1 MiB pattern (FILE_BYTES, every byte value, LF and CR included) to /dev/hvc0 with
`dd bs=4096`, drains it, prints CONSOLE-WRITTEN and powers off. The console's output is (`--sink fifo`, the default) a
named pipe the runner holds open and does not read until RELEASE seconds after the kernel's `reboot: Power down` line
on COM1: the backend can only put a pipe's buffer (16-64 KiB) into it, so at the guest's power-off the model still
holds about 1 MiB (within TX_BUFFER, so the guest never waits), and only the device's power-off drain (up to 2 s) can
get it to the runner. A drain that returns at once ends natemu before the runner reads, and the tail is lost. With
`--sink file` the output is a regular file natemu creates; the backend usually keeps up with the guest there, so that
sink shows create-and-append, not the drain. The runner checks the bytes are exactly the pattern (no CR added, nothing
lost or repeated), nothing was reported discarded, and the run ends with `natemu: Power(1)`.

console-socket: the console is a Unix socket in a private folder from `tempfile.mkdtemp(prefix='nm-')` (run folders
can pass sun_path's 104 bytes), removed afterwards. The runner connects after the guest's CONSOLE-READY on COM1 (the
init has set raw mode by then) and sends `ping NONCE\\n` and SOCKET_BYTES of pattern; it never half-closes (natemu
treats that as a hang-up). The init reads the line, waits PAUSE seconds without reading (the input then fills the
guest's receive buffers, about 1 MiB of posted receiveq pages and 640 KiB of tty buffers, and the device must hold
back: `deliver` takes less than it is offered), reads exactly SOCKET_BYTES with `dd iflag=fullblock` into sha256sum,
and writes back `pong REVERSED-NONCE SHA256\\n`. The runner checks that reply byte for byte, that natemu removed the
socket when it exited, and the exit line.

A case's guest markers are CONSOLE-*: CONSOLE-FAIL REASON (the judge reports it; the init then powers off), CONSOLE-PASS
before an S5 power-off.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import dataclasses
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from io_common import ROOT, VM_LOCK, clone, digest, stop_group, write_json
from run_platform import KERNEL_DEB_SHA, KERNEL_SHA, KVER, busybox_from, module_closure, modules_dep, stopped

MODULES = ('virtio_mmio',)
APPLETS = ('sh', 'mount', 'nproc', 'uname', 'modprobe', 'cat', 'grep', 'ls', 'basename', 'readlink', 'dmesg', 'sync',
           'poweroff', 'sleep', 'stty', 'dd', 'sha256sum')
VM_SECONDS = 240
CMDLINE = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr'
FILE_BYTES = 1 << 20            # the device's TX_BUFFER: the whole pattern fits what the model may hold
SOCKET_BYTES = 4 << 20          # well over what the guest buffers (about 1.7 MiB) and the device's 64 + 64 KiB
PAUSE = 3                       # seconds the init leaves the socket's input unread
RELEASE = 0.5                   # seconds after `reboot: Power down` before the runner reads the pipe (drain grace: 2 s)
SOCKET_NAME = 'console.sock'
SUN_PATH = 103                  # the longest socket path macOS takes
PCI_IDS = ('0x1af4', '0x1043', '0x078000')      # virtio-console, modern (0x1040 + 3), class 07H subclass 80H
CASES = ('console-file', 'console-socket')

INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain() { mode=$(stty -F "$1" -g) && stty -F "$1" "$mode"; }
fail() { echo "CONSOLE-FAIL $*"; dmesg; drain /dev/console || true; poweroff -f; while :; do sleep 1; done; }
pass() { echo CONSOLE-PASS; sync; drain /dev/console || fail console-drain; poweroff -f; fail poweroff-returned; }
reverse() { rest=$1 out=; while [ -n "$rest" ]; do out=${rest%"${rest#?}"}$out; rest=${rest#?}; done; echo "$out"; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
name= transport=
for item in $(cat /proc/cmdline); do
  case "$item" in console_case=*) name=${item#*=};; console_transport=*) transport=${item#*=};; esac
done
echo "CONSOLE-BEGIN case=$name transport=$transport cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '@KVER@' || fail kernel
modprobe virtio_mmio || fail modprobe-virtio_mmio
tries=0
while [ ! -c /dev/hvc0 ]; do
  tries=$((tries + 1)); test $tries -le 20 || fail "no /dev/hvc0 after 20 s"; sleep 1
done
# The one virtio device with ID 3, bound to virtio_console; its parent's bus says the transport.
found=
for d in /sys/bus/virtio/devices/*; do
  test -e "$d/device" || continue
  test "$(cat "$d/device")" = 0x0003 || continue
  test -z "$found" || fail "more than one virtio console"
  found=$(readlink -f "$d")
done
test -n "$found" || fail "no virtio device with ID 3"
driver=$(basename "$(readlink -f "$found/driver")")
parent=$(readlink -f "$found/..")
bus=$(basename "$(readlink -f "$parent/subsystem")")
ids=-
test "$bus" != pci || ids="$(cat "$parent/vendor"):$(cat "$parent/device"):$(cat "$parent/class")"
echo "CONSOLE-DEVICE bus=$bus parent=$(basename "$parent") driver=$driver pci=$ids"
test "$driver" = virtio_console || fail "virtio console bound to '$driver'"
# Open first: the hvc tty resets its settings when its last descriptor closes.
exec 3<>/dev/hvc0 || fail open-hvc0
stty -F /dev/hvc0 raw -echo || fail stty-raw

# console-file: the pattern to /dev/hvc0, drained, then the power-off (nothing waits in between).
case_file() {
  echo "CONSOLE-WRITE bytes=@FILE_BYTES@"
  dd if=/pattern bs=4096 2>/tmp/dd.log >&3 || fail "dd: $(cat /tmp/dd.log)"
  grep -q '^@FILE_BLOCKS@+0 records out$' /tmp/dd.log || fail "dd: $(cat /tmp/dd.log)"
  drain /dev/hvc0 || fail hvc0-drain
  echo CONSOLE-WRITTEN
  pass
}

# console-socket: `ping NONCE` and the pattern in, `pong REVERSED SHA256` out.
case_socket() {
  echo CONSOLE-READY
  read -r word nonce <&3 || fail read-ping
  test "$word" = ping || fail "first line '$word $nonce', expected ping NONCE"
  echo "CONSOLE-PING nonce=$nonce"
  sleep @PAUSE@
  dd bs=4096 count=@SOCKET_BLOCKS@ iflag=fullblock <&3 2>/tmp/dd.log | sha256sum >/tmp/sum || fail sha256sum
  grep -q '^@SOCKET_BLOCKS@+0 records in$' /tmp/dd.log || fail "dd: $(cat /tmp/dd.log)"
  sum=$(cat /tmp/sum); sum=${sum%% *}
  echo "pong $(reverse "$nonce") $sum" >&3 || fail write-pong
  drain /dev/hvc0 || fail hvc0-drain
  echo "CONSOLE-REPLIED sha256=$sum"
  pass
}

case "$name" in
  file) case_file;;
  socket) case_socket;;
  *) fail "unknown case '$name'";;
esac
fail "case $name returned"
'''
for _k, _v in {'KVER': KVER, 'FILE_BYTES': FILE_BYTES, 'FILE_BLOCKS': FILE_BYTES // 4096, 'PAUSE': PAUSE,
               'SOCKET_BLOCKS': SOCKET_BYTES // 4096}.items():
    INIT = INIT.replace(f'@{_k}@', str(_v))


@functools.cache
def pattern(label: str, size: int) -> bytes:
    """`size` bytes of SHA-256 in counter mode: every byte value, in no repeating order."""
    blocks = (hashlib.sha256(label.encode() + i.to_bytes(8, 'little')).digest() for i in range((size + 31) // 32))
    return b''.join(blocks)[:size]


def file_pattern() -> bytes:
    return pattern('natemu console-file', FILE_BYTES)


def socket_pattern() -> bytes:
    return pattern('natemu console-socket', SOCKET_BYTES)


@dataclasses.dataclass(frozen=True)
class Run:
    """What one VM left for its judge."""
    case: str                       # console-file or console-socket
    transport: str
    vcpus: int
    serial: str                     # COM1, with \r removed
    stderr: str
    returncode: int | None
    timeout: bool
    received: bytes                 # what the console's host end got (the output, or the socket's bytes)
    sink: str = 'fifo'              # console-file: fifo or file
    nonce: str = ''                 # console-socket
    sent: int = 0                   # console-socket: bytes the runner sent
    socket_left: bool = False       # console-socket: the socket file still there after natemu exited


def common(run: Run) -> list:
    """Every case: the init started this case on the pinned kernel, found the console on the transport asked for,
    reported no failure, passed once, no kernel error, no timeout, nothing discarded, S5."""
    errors = []
    short = run.case.removeprefix('console-')
    begin = re.findall(r'^CONSOLE-BEGIN case=(\S*) transport=(\S*) cpus=(\d+) kernel=(\S+)$', run.serial, re.M)
    if begin != [(short, run.transport, str(run.vcpus), KVER)]:
        errors.append(f'the init did not start case {short} on {run.transport} at {run.vcpus} vCPUs on {KVER} (CONSOLE-BEGIN: {begin})')
    errors += [f'guest: {why}' for why in re.findall(r'^CONSOLE-FAIL (.*)$', run.serial, re.M)]
    device = re.findall(r'^CONSOLE-DEVICE bus=(\S+) parent=(\S+) driver=(\S+) pci=(\S+)$', run.serial, re.M)
    want = 'pci' if run.transport == 'pci' else 'platform'
    if len(device) != 1 or device[0][0] != want or device[0][2] != 'virtio_console':
        errors.append(f'the console is not one virtio_console device on {want} (CONSOLE-DEVICE: {device})')
    elif run.transport == 'pci' and device[0][3] != ':'.join(PCI_IDS):
        errors.append(f'PCI vendor:device:class {device[0][3]}, expected {":".join(PCI_IDS)}')
    elif run.transport == 'mmio' and not re.fullmatch(r'LNRO0005:\d+', device[0][1]):
        errors.append(f'MMIO console parent {device[0][1]}, expected the ACPI device LNRO0005')
    if len(re.findall(r'^CONSOLE-PASS$', run.serial, re.M)) != 1: errors.append('no CONSOLE-PASS (once) on the serial console')
    if (bad := re.search(r'^.*(?:BUG:|Oops:|Kernel panic|Call Trace:).*$', run.serial, re.M)):
        errors.append(f'kernel error: {bad.group(0)!r}')
    if run.timeout: errors.append(f'timed out: no stop within {VM_SECONDS} s')
    errors += [f'host: {l}' for l in run.stderr.splitlines() if l.startswith('virtio-console:')]
    return errors + stopped(run, 'natemu: Power(1)', 0)


def compare(got: bytes, want: bytes, what: str) -> list:
    """Empty if `got` is exactly `want`; else what differs, in words."""
    if got == want: return []
    at = next((i for i in range(0, min(len(got), len(want)), 4096) if got[i:i + 4096] != want[i:i + 4096]), None)
    if at is not None: at += next(i for i, (a, b) in enumerate(zip(got[at:at + 4096], want[at:at + 4096])) if a != b)
    else: at = min(len(got), len(want))
    why = f'{what}: {len(got)} bytes, expected exactly {len(want)}; the first difference is at byte {at}'
    if want.startswith(got): why += f' (a prefix: the last {len(want) - len(got)} bytes are missing)'
    elif got == want.replace(b'\n', b'\r\n'):
        why += ' (CR added before LF: the tty was not raw)'
    return [why]


def judge_file(run: Run) -> list:
    errors = common(run)
    if len(re.findall(r'^CONSOLE-WRITTEN$', run.serial, re.M)) != 1: errors.append('the guest never reported the pattern written')
    return errors + compare(run.received, file_pattern(), f'the console output ({run.sink})')


def reply(nonce: str) -> bytes:
    return f'pong {nonce[::-1]} {hashlib.sha256(socket_pattern()).hexdigest()}\n'.encode()


def judge_socket(run: Run) -> list:
    errors = common(run)
    if not re.fullmatch(r'[0-9a-f]{16}', run.nonce): errors.append(f'no nonce sent ({run.nonce!r})')
    if re.findall(r'^CONSOLE-PING nonce=(\S*)$', run.serial, re.M) != [run.nonce]: errors.append('the guest did not read the ping line')
    if run.sent != len(f'ping {run.nonce}\n') + SOCKET_BYTES: errors.append(f'the runner sent {run.sent} bytes, not the ping line and {SOCKET_BYTES}')
    if run.socket_left: errors.append('the socket file is still there after natemu exited')
    return errors + compare(run.received, reply(run.nonce), 'the reply on the socket')


JUDGES = {'console-file': judge_file, 'console-socket': judge_socket}


def prepare(args):
    args.out.mkdir(parents=True, exist_ok=False)
    assert digest(args.kernel) == KERNEL_SHA, 'not the pinned Debian kernel'
    assert digest(args.kernel_deb) == KERNEL_DEB_SHA, 'not the pinned Debian kernel package'
    clone(args.kernel, args.out / 'linux')
    root = args.out / 'rootfs'
    for name in ('bin', 'dev', 'proc', 'sys', 'tmp', 'lib/modules/' + KVER):
        (root / name).mkdir(parents=True, exist_ok=True)
    busybox = busybox_from(args.busybox_deb)
    assert b'fullblock' in busybox, 'BusyBox dd needs iflag=fullblock'
    (root / 'bin/busybox').write_bytes(busybox)
    (root / 'bin/busybox').chmod(0o755)
    for applet in APPLETS: (root / 'bin' / applet).symlink_to('busybox')
    modules = module_closure(args.kernel_deb, MODULES)
    for rel, data, _ in modules.values():
        dest = root / 'lib/modules' / KVER / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (root / 'lib/modules' / KVER / 'modules.dep').write_text(modules_dep(modules))
    (root / 'pattern').write_bytes(file_pattern())
    (root / 'init').write_text(INIT)
    (root / 'init').chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mkcpio)
    mkcpio.main(str(root), str(args.out / 'console.cpio'))
    platform = Path(__file__).resolve().parent / 'run_platform.py'
    manifest = {'purpose': 'stock Linux virtio_console against natemu virtio-console (tests/devices)', 'kernel_version': KVER,
                'sources': {p.name: digest(p) for p in (args.kernel, args.kernel_deb, args.busybox_deb)},
                'producer': {str(p.relative_to(ROOT)): digest(p) for p in (Path(__file__).resolve(), platform, ROOT / 'tools/play/mkcpio.py')},
                'modules': {n: {'path': rel, 'sha256': digest(root / 'lib/modules' / KVER / rel), 'depends': deps}
                            for n, (rel, _, deps) in sorted(modules.items())},
                'outputs': {str(p.relative_to(args.out)): digest(p)
                            for p in (args.out / 'linux', args.out / 'console.cpio', root / 'init', root / 'bin/busybox', root / 'pattern')}}
    write_json(args.out / 'inputs.json', manifest)
    print(json.dumps(manifest, indent=2))


def read_some(fd: int) -> tuple[bytes, bool]:
    """What the non-blocking pipe has now, and whether it reached its end (no writer left)."""
    got = []
    while True:
        try: chunk = os.read(fd, 1 << 20)
        except BlockingIOError: return b''.join(got), False
        if not chunk: return b''.join(got), True
        got.append(chunk)


def receive(client: socket.socket, seconds: float) -> tuple[bytes, bool]:
    """What the socket (blocking: the sender thread writes to it) has within `seconds`, and whether it ended."""
    if not select.select([client], [], [], seconds)[0]: return b'', False
    try: chunk = client.recv(1 << 20)
    except ConnectionResetError: return b'', True
    return chunk, not chunk


def read_to_end(read, seconds: float) -> bytes:
    """Everything up to the end (natemu exited), waiting at most `seconds`; `read()` gives (bytes, ended)."""
    got, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        chunk, done = read()
        got.append(chunk)
        if done: break
    return b''.join(got)


def read_pipe(fd: int):
    def read():
        select.select([fd], [], [], 0.1)
        return read_some(fd)
    return read


def boot(args, work: Path) -> Run:
    """One VM: started, its console's host end served, stopped at VM_SECONDS."""
    output, fifo, folder, sock_path, client, sender = work / 'console.out', None, None, None, None, None
    nonce, sent, received = '', [0], []
    if args.case == 'console-file' and args.sink == 'fifo':
        os.mkfifo(output)
        fifo = os.open(output, os.O_RDONLY | os.O_NONBLOCK)      # natemu's open for writing does not wait then
    if args.case == 'console-socket':
        folder = Path(tempfile.mkdtemp(prefix='nm-'))
        if len(str(folder / SOCKET_NAME).encode()) > SUN_PATH:  # a long TMPDIR
            shutil.rmtree(folder)
            folder = Path(tempfile.mkdtemp(prefix='nm-', dir='/tmp'))
        sock_path = folder / SOCKET_NAME
    backend = f'socket = {json.dumps(str(sock_path))}' if sock_path else f'output = {json.dumps(str(output.resolve()))}'
    config = work / 'natemu.toml'
    config.write_text(f'[machine]\ntransport = {{ console = "{args.transport}" }}\n\n[[console]]\nid = "hvc0"\n{backend}\n')
    command = [str(args.natemu.resolve()), '--config', str(config.resolve()), '--runtime', str(args.runtime.resolve()),
               '--fexc', str(args.fexc.resolve()), '--kernel', str((args.inputs / 'linux').resolve()),
               '--initrd', str((args.inputs / 'console.cpio').resolve()), '--vcpus', str(args.cpus), '--ram', '2G',
               '--compile-cache', 'off', '--serial', str((work / 'serial.log').resolve()),
               '--cmdline', f'{CMDLINE} console_case={args.case.removeprefix("console-")} console_transport={args.transport}']
    write_json(work / 'command.json', command)
    env = {k: v for k, v in os.environ.items() if k not in ('NATEMU_PERF_LOG', 'NATEMU_COMPILE_CACHE')}   # settings the CLI reads (deprecated)
    serial = work / 'serial.log'
    started, timeout, power_down, reading = time.monotonic(), False, None, False
    try:
        with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
            try:
                while process.poll() is None:
                    if time.monotonic() - started >= VM_SECONDS:
                        timeout = True
                        stop_group(process)
                        break
                    text = serial.read_text(errors='replace') if serial.exists() and not (reading or client) else ''
                    if fifo is not None and not reading:
                        if power_down is None and 'reboot: Power down' in text: power_down = time.monotonic()
                        reading = power_down is not None and time.monotonic() - power_down >= RELEASE
                    if sock_path and client is None and re.search(r'^CONSOLE-READY\r?$', text, re.M):
                        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        client.connect(str(sock_path))
                        nonce = secrets.token_hex(8)
                        message = f'ping {nonce}\n'.encode() + socket_pattern()

                        def send(c=client, m=message):
                            try:
                                with memoryview(m) as view:
                                    while sent[0] < len(m): sent[0] += c.send(view[sent[0]:sent[0] + 65536])
                            except OSError: pass        # natemu ended: the judge sees the count
                        sender = threading.Thread(target=send, daemon=True)
                        sender.start()
                    if reading:
                        select.select([fifo], [], [], 0.05)
                        received.append(read_some(fifo)[0])
                    elif client:
                        chunk, done = receive(client, 0.05)
                        received.append(chunk)
                        if done: client.close(); client = False       # natemu closed it (it is ending)
                    else:
                        time.sleep(0.05)
            except BaseException:
                stop_group(process)
                raise
        if fifo is not None: received.append(read_to_end(read_pipe(fifo), 10))
        if client: received.append(read_to_end(lambda: receive(client, 0.1), 10))
        if sender: sender.join(10)
        socket_left = bool(sock_path) and os.path.lexists(sock_path)
    finally:
        if fifo is not None: os.close(fifo)
        if client: client.close()
        if folder: shutil.rmtree(folder, ignore_errors=True)
    if args.case == 'console-file' and args.sink == 'file' and output.exists(): received = [output.read_bytes()]
    return Run(args.case, args.transport, args.cpus, serial.read_text(errors='replace').replace('\r', '') if serial.exists() else '',
               (work / 'stderr.log').read_text(errors='replace'), process.returncode, timeout, b''.join(received),
               sink=args.sink, nonce=nonce, sent=sent[0], socket_left=socket_left)


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.inputs / name) == sha, name
    name = f'{args.case}-{args.transport}-{args.cpus}' + (f'-{args.sink}' if args.case == 'console-file' else '')
    write_json(args.out / 'manifest.json', {'artifacts': {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))},
                                            'inputs': manifest, 'case': args.case, 'transport': args.transport, 'vcpus': args.cpus,
                                            'sink': args.sink, 'seconds_per_vm': VM_SECONDS})
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)          # the VM lock, shared as the other Linux runners hold it
        work = args.out / name
        work.mkdir()
        t0 = time.monotonic()
        r = boot(args, work)
        seconds = time.monotonic() - t0
    (work / 'received.bin').write_bytes(r.received)
    errors = JUDGES[args.case](r)
    result = {'name': name, 'ok': not errors, 'errors': errors, 'returncode': r.returncode, 'timeout': r.timeout,
              'seconds': round(seconds, 1), 'received_bytes': len(r.received), 'received_sha256': hashlib.sha256(r.received).hexdigest(),
              'sent_bytes': r.sent, 'nonce': r.nonce}
    write_json(work / 'result.json', result)
    write_json(args.out / 'results.json', {'results': [result]})
    print(json.dumps(result), flush=True)
    return int(bool(errors))


def self_check():
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    for short in ('file', 'socket'):
        assert f'\n  {short}) case_{short};;\n' in INIT and f'\ncase_{short}() {{\n' in INIT, f'{short}: no case function or dispatch'
    assert '@' not in INIT, 'a placeholder left in INIT'
    # The init's reverse(), run by the host's sh.
    rev = re.search(r'^reverse\(\) .*$', INIT, re.M).group(0)
    out = subprocess.run(['sh', '-c', rev + '\nreverse 0123456789abcdef'], capture_output=True, text=True, check=True).stdout
    assert out == 'fedcba9876543210\n', out
    for data in (file_pattern(), socket_pattern()):
        assert len(set(data)) == 256, 'the pattern lacks byte values'
    assert file_pattern() != socket_pattern()[:FILE_BYTES]

    def serial(short, transport, extra):
        dev = ('CONSOLE-DEVICE bus=pci parent=0000:00:04.0 driver=virtio_console pci=0x1af4:0x1043:0x078000' if transport == 'pci'
               else 'CONSOLE-DEVICE bus=platform parent=LNRO0005:00 driver=virtio_console pci=-')
        return (f'CONSOLE-BEGIN case={short} transport={transport} cpus=2 kernel={KVER}\n{dev}\n{extra}CONSOLE-PASS\n'
                'reboot: Power down\n')
    host = 'natemu: cpu: x\nnatemu: Power(1)\n'
    good_file = Run('console-file', 'mmio', 2, serial('file', 'mmio', f'CONSOLE-WRITE bytes={FILE_BYTES}\nCONSOLE-WRITTEN\n'),
                    host, 0, False, file_pattern())
    nonce = '0123456789abcdef'
    good_socket = Run('console-socket', 'pci', 2, serial('socket', 'pci', f'CONSOLE-READY\nCONSOLE-PING nonce={nonce}\nCONSOLE-REPLIED sha256=x\n'),
                      host, 0, False, reply(nonce), nonce=nonce, sent=len(f'ping {nonce}\n') + SOCKET_BYTES)
    pci_file = dataclasses.replace(good_file, transport='pci', serial=serial('file', 'pci', 'CONSOLE-WRITTEN\n'))
    for good in (good_file, good_socket, dataclasses.replace(good_file, sink='file'), pci_file):
        assert not JUDGES[good.case](good), JUDGES[good.case](good)
    swap = lambda run, old, new: dataclasses.replace(run, serial=run.serial.replace(old, new))
    data = file_pattern()
    bad = {
        'tail lost': dataclasses.replace(good_file, received=data[:-4096]),
        'output empty': dataclasses.replace(good_file, received=b''),
        'pipe buffer only': dataclasses.replace(good_file, received=data[:65536]),
        'CR added': dataclasses.replace(good_file, received=data.replace(b'\n', b'\r\n')),
        'byte repeated': dataclasses.replace(good_file, received=data[:1000] + data[999:]),
        'byte changed': dataclasses.replace(good_file, received=data[:5000] + bytes([data[5000] ^ 1]) + data[5001:]),
        'extra after': dataclasses.replace(good_file, received=data + b'\n'),
        'not written': swap(good_file, 'CONSOLE-WRITTEN\n', ''),
        'guest failure': swap(good_file, 'CONSOLE-WRITTEN\n', 'CONSOLE-FAIL dd: short\n'),
        'no pass': swap(good_file, 'CONSOLE-PASS\n', ''),
        'other case': swap(good_file, 'case=file', 'case=socket'),
        'other transport': swap(good_file, 'transport=mmio', 'transport=pci'),
        'other cpus': swap(good_file, 'cpus=2', 'cpus=1'),
        'other kernel': swap(good_file, KVER, '6.1.0'),
        'on pci, asked mmio': swap(good_file, 'bus=platform parent=LNRO0005:00', 'bus=pci parent=0000:00:04.0'),
        'other driver': swap(good_file, 'driver=virtio_console', 'driver=none'),
        'other parent': swap(good_file, 'LNRO0005:00', 'QEMU0001:00'),
        'kernel oops': swap(good_file, 'CONSOLE-PASS', 'Oops: 0000 [#1]\nCONSOLE-PASS'),
        'discarded': dataclasses.replace(good_file, stderr='virtio-console: 4096 bytes of guest output were discarded\n' + host),
        'reset': dataclasses.replace(good_file, stderr='natemu: Power(2)\n'),
        'not last': dataclasses.replace(good_file, stderr=host + 'natemu: later\n'),
        'status': dataclasses.replace(good_file, returncode=1),
        'timeout': dataclasses.replace(good_file, timeout=True, stderr='natemu: Cancelled\n', returncode=-15),
        'pci ids': swap(good_socket, '0x1043', '0x1003'),
        'nonce not reversed': dataclasses.replace(good_socket, received=reply(nonce[::-1])),
        'wrong sha': dataclasses.replace(good_socket, received=f'pong {nonce[::-1]} {"0" * 64}\n'.encode()),
        'no reply': dataclasses.replace(good_socket, received=b''),
        'reply with CR': dataclasses.replace(good_socket, received=reply(nonce).replace(b'\n', b'\r\n')),
        'echoed input': dataclasses.replace(good_socket, received=f'ping {nonce}\n'.encode() + reply(nonce)),
        'ping not read': swap(good_socket, f'CONSOLE-PING nonce={nonce}\n', ''),
        'short send': dataclasses.replace(good_socket, sent=4096),
        'no nonce': dataclasses.replace(good_socket, nonce='', received=reply('')),
        'socket left': dataclasses.replace(good_socket, socket_left=True),
        'socket on mmio': swap(good_socket, 'transport=pci', 'transport=mmio'),
    }
    for what, r in bad.items(): assert JUDGES[r.case](r), f'{what}: passed'
    expect = {'tail lost': 'the last 4096 bytes are missing', 'CR added': 'CR added before LF', 'timeout': 'timed out',
              'discarded': 'host: virtio-console:', 'socket left': 'still there', 'guest failure': 'guest: dd: short'}
    for what, words in expect.items():
        assert any(words in e for e in JUDGES[bad[what].case](bad[what])), (what, JUDGES[bad[what].case](bad[what]))
    print(f'Init shell syntax, its reverse(), the patterns and {len(bad)} negative judge controls passed; no VM launched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('kernel-deb', 'kernel', 'busybox-deb', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    runner = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out'): runner.add_argument('--' + name, type=Path, required=True)
    runner.add_argument('--lock', type=Path, default=VM_LOCK, help='the VM lock file, held shared (default: the state root\'s)')
    runner.add_argument('--case', choices=CASES, required=True)
    runner.add_argument('--transport', choices=('mmio', 'pci'), required=True)
    runner.add_argument('--cpus', type=int, choices=(1, 2, 4), default=1)
    runner.add_argument('--sink', choices=('fifo', 'file'), default='fifo',
                        help='console-file: a pipe read only after the power-off starts (the drain), or a regular file')
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': return prepare(args)
    if args.command == 'self-check': return self_check()
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    raise SystemExit(main())
