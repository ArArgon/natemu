#!/usr/bin/env python3
"""The devices family's display runner: natemu's Bochs display (`[display] device = "bochs"`, crates/host/src/display)
driven by the stock bochs DRM driver in the guest and read from the host through the control API (`snapshot`,
`framebuffer`, `display_changed`: docs/control-api.md), through the `natemu` CLI.

prepare builds one small initramfs: Debian's unchanged BusyBox, the stock bochs module with the DRM modules it depends
on (from the Debian kernel package, as run_platform.py's display-probe takes them), kms_pattern (tests/devices/
kms_pattern.c, built static by testkit/inputs.py kms_pattern() in the amd64 Debian builder) and an init that runs the
case the kernel command line names (`display_case=NAME`). run boots one VM with `--set control.socket=PATH`, connects
to the socket at once (before the guest's driver loads, so every display_changed reaches it), waits for the guest's
marker, takes what the case reads through tests/lib/control_client.py and judges it, then quits. self-check runs the
init's shell through `sh -n`, checks the pattern against kms_pattern.c, and runs the judges' negative controls (the
pixel comparisons included: B and R swapped, the offset ignored) without a VM.

Cases (each ends with `quit`: `stopped: cancelled`, `natemu: Cancelled`, exit status 2):
- display: after `modprobe bochs` the init starts kms_pattern, which draws pattern(x, y) into a dumb buffer of
  800 x 496 pixels (pitch 3200 bytes) through the buffer's own mapping, adds a 640 x 480 framebuffer on it at
  offsets[0] = 16 x 3200 and sets the CRTC to the connector's 640 x 480 mode; at its READY the runner takes
  `framebuffer` (width 640, height 480, stride 3200, x8r8g8b8; the offset is the device's, the framebuffer's 16 rows
  plus where the buffer object lies in VRAM: the judge reads it from the answer and requires its row part at least 16),
  maps the descriptor read-only and compares every visible pixel with the pattern; takes a `snapshot`, writes it to
  {out}/display.png and compares the same pixels (snapshot_matches_guest_pattern); requires a display_changed for the
  mode set (640 x 480, stride 3200, the framebuffer's offset) and query_status's display to be that mode.
- display-boot-console: after `modprobe bochs` fbdev's console (fbcon) shows tty1 at the EDID's preferred mode; the
  init writes a few lines there ("natemu bochs display", `uname -a`, the mode from /sys), waits a second (fbdev copies
  its shadow to VRAM about every 50 ms), and the runner's snapshot (1024 x 768, written to {out}/boot-console.png) must
  show text: at least two colours, and between 0.05 % and 25 % of the pixels not the background's.
- display-off (no `[display]`): `snapshot` and `framebuffer` are -32001 naming the display, query_status has no
  display member, the guest's PCI bus has no 1234:1111 function and `modprobe bochs` makes no /dev/dri/card0.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import dataclasses
import mmap
import os
from pathlib import Path
import re
import subprocess

import png                      # pypng (testkit/requirements.lock)
from control_client import Control, RpcError
from io_common import VM_LOCK
import run_control
from run_control import Run
from run_platform import KVER, stopped

MODULES = ('bochs', 'drm_kms_helper')
APPLETS = ('sh', 'mount', 'nproc', 'uname', 'modprobe', 'cat', 'ls', 'readlink', 'basename', 'dmesg', 'sync', 'poweroff',
           'sleep', 'stty')
KMS_PATTERN = 'bin/kms_pattern'
VM_SECONDS = 240
READY_SECONDS = 150
CMDLINE = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr'
DISPLAY_ID = '0x1234 0x1111'
# kms_pattern's geometry (tests/devices/kms_pattern.c; self-check reads them from the source): the framebuffer, the
# dumb buffer's width (its pitch is BUF_W x 4) and the rows before the framebuffer (offsets[0] = Y0 x pitch).
WIDTH, HEIGHT, BUF_W, Y0 = 640, 480, 800, 16
STRIDE = BUF_W * 4
# display-boot-console: fbdev's mode (the EDID's preferred, [display]'s default) and the text it must show.
CONSOLE_MODE = (1024, 768)
CONSOLE_TEXT = (0.0005, 0.25)        # the share of pixels not the background's colour
CONSOLE_TITLE = 'natemu bochs display'


def pattern(x: int, y: int) -> tuple:
    """kms_pattern's pattern(x, y) as (R, G, B)."""
    return (x * 3 + y) & 0xff, (y * 5 + (x >> 4)) & 0xff, (x ^ (y * 7)) & 0xff


def pattern_rgb() -> bytes:
    """The framebuffer's picture as RGB rows, as a snapshot gives it."""
    return b''.join(bytes(c for x in range(WIDTH) for c in pattern(x, y)) for y in range(HEIGHT))


INIT = r'''#!/bin/sh
export PATH=/bin
drain_console() { console_mode=$(stty -F /dev/console -g) && stty -F /dev/console "$console_mode"; }
fail() { echo "DISPLAY-FAIL $*"; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
idle() { while :; do sleep 1; done; }
# wait_for PATH: up to 20 s for it to appear (the driver binds and registers asynchronously).
wait_for() { n=0; until test -e "$1"; do n=$((n + 1)); test $n -le 20 || fail "no $1 after 20 s"; sleep 1; done; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
name=
for item in $(cat /proc/cmdline); do case "$item" in display_case=*) name=${item#*=};; esac; done
echo "DISPLAY-BEGIN case=$name cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '@KVER@' || fail kernel
echo 1 > /proc/sys/kernel/printk || fail printk

# display: kms_pattern draws and sets the mode, prints READY and holds it.
case_display() {
  modprobe bochs || fail modprobe-bochs
  wait_for /dev/dri/card0
  /@KMS_PATTERN@ || fail "kms_pattern exited ($?)"
}

# display-boot-console: fbcon's tty1 at fbdev's mode, with a few lines of text.
case_display_boot_console() {
  modprobe bochs || fail modprobe-bochs
  wait_for /sys/class/graphics/fb0
  wait_for /dev/tty1
  read -r mode < /sys/class/drm/card0-Virtual-1/modes || fail modes
  {
    echo
    echo '@CONSOLE_TITLE@'
    echo
    uname -a
    echo "mode $mode, fb0 $(cat /sys/class/graphics/fb0/virtual_size) at $(cat /sys/class/graphics/fb0/bits_per_pixel) bpp"
  } > /dev/tty1 || fail "write to tty1"
  echo "DISPLAY-MODE $mode"
  sleep 1
  echo DISPLAY-CONSOLE-READY
  idle
}

# display-off: no display function, and the driver finds nothing.
case_display_off() {
  for d in /sys/bus/pci/devices/*; do
    test -e "$d/vendor" && echo "DISPLAY-PCI $(basename "$d") $(cat "$d/vendor") $(cat "$d/device")"
  done
  echo DISPLAY-PCI-END
  modprobe bochs || fail modprobe-bochs
  sleep 1
  if test -e /dev/dri/card0; then echo DISPLAY-CARD present; else echo DISPLAY-CARD absent; fi
  echo DISPLAY-OFF-READY
  idle
}

case "$name" in
  display) case_display;;
  display-boot-console) case_display_boot_console;;
  display-off) case_display_off;;
  *) fail "unknown case '$name'";;
esac
fail "case $name returned"
'''
for _k, _v in {'KVER': KVER, 'KMS_PATTERN': KMS_PATTERN, 'CONSOLE_TITLE': CONSOLE_TITLE}.items():
    INIT = INIT.replace(f'@{_k}@', str(_v))


# ---------------------------------------------------------------- comparisons (the driver's; self-checked)

def framebuffer_mismatches(fb, offset: int, stride: int, limit: int = 5) -> tuple:
    """Compares every visible pixel of the framebuffer mapping `fb` (x8r8g8b8 little-endian: B, G, R, X) at `offset`,
    rows `stride` apart, with the pattern: (how many differ, the first few as (x, y, got RGB, expected RGB))."""
    count, first = 0, []
    for y in range(HEIGHT):
        start = offset + y * stride
        row = fb[start:start + 4 * WIDTH]
        if len(row) != 4 * WIDTH:
            count += WIDTH; first += [(0, y, None, pattern(0, y))][:limit - len(first)]; continue
        want = pattern_row_bgrx(y)
        if row[0::4] == want[0::4] and row[1::4] == want[1::4] and row[2::4] == want[2::4]: continue
        for x in range(WIDTH):
            got = (row[4 * x + 2], row[4 * x + 1], row[4 * x])
            if got != pattern(x, y):
                count += 1
                if len(first) < limit: first.append((x, y, got, pattern(x, y)))
    return count, first


_ROWS = {}


def pattern_row_bgrx(y: int) -> bytes:
    if y not in _ROWS: _ROWS[y] = bytes(c for x in range(WIDTH) for c in (*reversed(pattern(x, y)), 0))
    return _ROWS[y]


def rgb_mismatches(rgb: bytes, limit: int = 5) -> tuple:
    """A snapshot's pixels against the pattern: (how many differ, the first few as (x, y, got, expected))."""
    want = pattern_rgb()
    if rgb == want: return 0, []
    if len(rgb) != len(want): return WIDTH * HEIGHT, [('size', len(rgb), len(want))]
    count, first = 0, []
    for i in range(0, len(want), 3):
        if rgb[i:i + 3] != want[i:i + 3]:
            count += 1
            if len(first) < limit: first.append(((i // 3) % WIDTH, (i // 3) // WIDTH, tuple(rgb[i:i + 3]), tuple(want[i:i + 3])))
    return count, first


def text_share(rgb: bytes) -> tuple:
    """(colours, the share of pixels not the most common colour) of an RGB picture."""
    counts = {}
    for i in range(0, len(rgb), 3):
        k = rgb[i:i + 3]
        counts[k] = counts.get(k, 0) + 1
    n = len(rgb) // 3
    return len(counts), (n - max(counts.values())) / n if n else 0.0


# ---------------------------------------------------------------- judges

def common(run: Run) -> list:
    errors = []
    begin = re.findall(r'^DISPLAY-BEGIN case=(\S*) cpus=(\d+) kernel=(\S+)$', run.serial, re.M)
    if begin != [(run.case, str(run.vcpus), KVER)]:
        errors.append(f'the init did not start case {run.case} at {run.vcpus} vCPUs on {KVER} (DISPLAY-BEGIN: {begin})')
    errors += [f'guest: {why}' for why in re.findall(r'^(?:DISPLAY|KMS)-FAIL (.*)$', run.serial, re.M)]
    if (bad := re.search(r'^.*(?:BUG:|Oops:|Kernel panic|Call Trace:).*$', run.serial, re.M)):
        errors.append(f'kernel error: {bad.group(0)!r}')
    if run.timeout: errors.append(f'timed out: no stop within {VM_SECONDS} s, or the VM ran on after the driver ended')
    if run.control.get('error'): errors.append(f'driver: {run.control["error"]}')
    return errors


def cancelled(run: Run) -> list:
    errors = [] if run.control.get('quit') == {} else [f'quit answered {run.control.get("quit")!r}, expected {{}}']
    if run.control.get('stopped') != 'cancelled': errors.append(f'stopped reason {run.control.get("stopped")!r}, expected cancelled')
    return errors + stopped(run, 'natemu: Cancelled', 2)


def judge_display(run: Run) -> list:
    errors = common(run)
    if len(re.findall(r'^READY$', run.serial, re.M)) != 1: errors.append('kms_pattern never reported READY')
    c = run.control
    fb = c.get('framebuffer')
    want = {'width': WIDTH, 'height': HEIGHT, 'stride': STRIDE, 'format': 'x8r8g8b8'}
    if not isinstance(fb, dict) or {k: fb.get(k) for k in want} != want:
        errors.append(f'framebuffer answered {fb!r}, expected {want} and an offset and size')
        return errors + cancelled(run)
    offset = fb.get('offset')
    if not isinstance(offset, int) or offset % 4 or offset // STRIDE < Y0 or offset + (HEIGHT - 1) * STRIDE + 4 * WIDTH > fb.get('size', 0):
        errors.append(f'framebuffer offset {offset!r}: a 4-aligned offset of at least {Y0} rows of {STRIDE} whose picture '
                      f'lies inside the {fb.get("size")} bytes')
    if c.get('descriptors') != 1: errors.append(f'{c.get("descriptors")!r} descriptors came with the framebuffer answer, expected 1')
    if c.get('fb_mismatch') != [0, []]: errors.append(f'the mapped framebuffer differs from the pattern at {c.get("fb_mismatch")} (count, first)')
    snap = c.get('snapshot')
    if snap != [WIDTH, HEIGHT]: errors.append(f'snapshot gave {snap!r}, expected {WIDTH} x {HEIGHT}')
    if c.get('snapshot_mismatch') != [0, []]:
        errors.append(f'snapshot_matches_guest_pattern: the snapshot differs from the pattern at {c.get("snapshot_mismatch")} (count, first)')
    mode = dict(want, offset=offset)
    changes = c.get('display_changed') or []
    if dict(mode, enabled=True) not in changes:
        errors.append(f'no display_changed for the mode set ({dict(mode, enabled=True)}); got {changes[-4:]}')
    if c.get('status_display') != mode: errors.append(f'query_status display {c.get("status_display")!r}, expected {mode}')
    return errors + cancelled(run)


def judge_boot_console(run: Run) -> list:
    errors = common(run)
    if len(re.findall(r'^DISPLAY-CONSOLE-READY$', run.serial, re.M)) != 1: errors.append('the init never wrote the console (DISPLAY-CONSOLE-READY)')
    c = run.control
    if c.get('snapshot') != list(CONSOLE_MODE): errors.append(f'snapshot gave {c.get("snapshot")!r}, expected {list(CONSOLE_MODE)}')
    colours, share = c.get('colours', 0), c.get('text_share', 0.0)
    if colours < 2 or not CONSOLE_TEXT[0] <= share <= CONSOLE_TEXT[1]:
        errors.append(f'the snapshot shows no text: {colours} colours, {share:.4%} of the pixels not the background '
                      f'(expected at least 2, and {CONSOLE_TEXT[0]:.2%} to {CONSOLE_TEXT[1]:.0%})')
    status = c.get('status_display')
    if not isinstance(status, dict) or (status.get('width'), status.get('height')) != CONSOLE_MODE:
        errors.append(f'query_status display {status!r}, expected {CONSOLE_MODE[0]} x {CONSOLE_MODE[1]}')
    return errors + cancelled(run)


def judge_off(run: Run) -> list:
    errors = common(run)
    if len(re.findall(r'^DISPLAY-OFF-READY$', run.serial, re.M)) != 1: errors.append('the init never finished (DISPLAY-OFF-READY)')
    if not re.search(r'^DISPLAY-PCI-END$', run.serial, re.M): errors.append('no PCI listing (DISPLAY-PCI-END)')
    if (found := re.findall(rf'^DISPLAY-PCI (\S+) {DISPLAY_ID}$', run.serial, re.M)):
        errors.append(f'a display function at {found} although [display] is off')
    if re.findall(r'^DISPLAY-CARD (\S+)$', run.serial, re.M) != ['absent']: errors.append('/dev/dri/card0 present (or not reported) with the display off')
    absent = {'code': -32001, 'data': {'device': 'display'}}
    for what in ('snapshot', 'framebuffer'):
        if run.control.get(what) != absent: errors.append(f'{what} gave {run.control.get(what)!r}, expected {absent}')
    devices = run.control.get('devices')
    if devices != {'ps2': True}: errors.append(f'query_status devices {devices!r}, expected {{"ps2": true}} (no display member)')
    return errors + cancelled(run)


# ---------------------------------------------------------------- drivers

class Driver(run_control.Driver):
    """run_control's driver, waiting for one marker line, with the case's output folder for its PNGs."""
    def __init__(self, sock: Path, serial: Path, process, out: Path):
        super().__init__(sock, serial, process)
        self.out = out

    def until(self, marker: str, seconds: float = READY_SECONDS):
        super().until(lambda t: re.search(rf'^{marker}$', t, re.M), seconds, marker)

    def quit(self, vm: Control):
        self.control['quit'] = vm.call('quit')
        self.control['stopped'] = vm.wait_stopped()
        vm.close()


def answer(call):
    try: return call()
    except RpcError as e: return {'code': e.code, 'data': e.data}


def snapshot_png(d: Driver, vm: Control, name: str) -> bytes:
    """A snapshot, written to the case's output folder as `name`; its RGB pixels."""
    width, height, data = vm.snapshot(timeout=60)
    (d.out / name).write_bytes(data)
    w, h, rows, _ = png.Reader(bytes=data).asRGB8()
    rgb = b''.join(bytes(r) for r in rows)
    d.control['snapshot'] = [w, h]
    if (w, h) != (width, height): raise RuntimeError(f'the PNG is {w} x {h}, the answer says {width} x {height}')
    return rgb


def drive_display(d: Driver):
    vm = d.connect()                # before the guest's driver loads: every display_changed comes to this client
    d.until('READY')
    fb, fd = vm.framebuffer(timeout=30)
    d.control['framebuffer'], d.control['descriptors'] = fb, 1
    try:
        with mmap.mmap(fd, fb['size'], flags=mmap.MAP_SHARED, prot=mmap.PROT_READ) as m:
            d.control['fb_mismatch'] = list(framebuffer_mismatches(m, fb['offset'], fb['stride']))
    finally:
        os.close(fd)
    d.control['snapshot_mismatch'] = list(rgb_mismatches(snapshot_png(d, vm, 'display.png')))
    # the display_changed for the mode set: sent within 50 ms of it, so long before READY was seen; a last call reads it
    status = vm.call('query_status', timeout=30)
    d.control['status_display'] = status['devices'].get('display')
    d.control['display_changed'] = vm.display_changes()
    d.quit(vm)


def drive_boot_console(d: Driver):
    vm = d.connect()
    d.until('DISPLAY-CONSOLE-READY')
    colours, share = text_share(snapshot_png(d, vm, 'boot-console.png'))
    d.control['colours'], d.control['text_share'] = colours, share
    d.control['status_display'] = vm.call('query_status', timeout=30)['devices'].get('display')
    d.quit(vm)


def drive_off(d: Driver):
    vm = d.connect()
    d.until('DISPLAY-OFF-READY')
    d.control['snapshot'] = answer(lambda: vm.call('snapshot', {'format': 'png'}, timeout=30))
    d.control['framebuffer'] = answer(lambda: vm.call('framebuffer', None, timeout=30))
    d.control['devices'] = vm.call('query_status', timeout=30)['devices']
    d.quit(vm)


@dataclasses.dataclass(frozen=True)
class Case:
    judge: object
    drive: object


CASES = {
    'display': Case(judge_display, drive_display),
    'display-boot-console': Case(judge_boot_console, drive_boot_console),
    'display-off': Case(judge_off, drive_off),
}


def boot(args, name: str, work: Path) -> Run:
    return run_control.boot(args, name, work, CASES, 'display.cpio', 'display_case',
                            lambda sock, serial, process: Driver(sock, serial, process, args.out))


def prepare(args):
    kms = args.kms_pattern.read_bytes()
    assert kms[:4] == b'\x7fELF' and int.from_bytes(kms[18:20], 'little') == 62, 'kms_pattern: not an x86-64 program'
    run_control.prepare(args, INIT, MODULES, APPLETS, 'display.cpio', {KMS_PATTERN: args.kms_pattern},
                        "natemu's Bochs display driven by the stock bochs DRM driver and kms_pattern (tests/devices)")


def run(args):
    return run_control.run(args, CASES, boot, (png,))


# ---------------------------------------------------------------- self-check

def self_check():
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    assert '@' not in INIT, 'a placeholder left in INIT'
    for name in CASES:
        assert f'\n  {name}) case_{name.replace("-", "_")};;\n' in INIT, f'{name}: no dispatch line in INIT'
        assert f'\ncase_{name.replace("-", "_")}() {{\n' in INIT, f'{name}: no case function in INIT'
    loaded = set(re.findall(r'modprobe (\w+)', INIT))
    assert loaded and loaded <= set(MODULES), f'the init loads {sorted(loaded - set(MODULES))}, not in MODULES'
    # kms_pattern.c's geometry and pattern are the runner's.
    source = (Path(__file__).resolve().parent / 'kms_pattern.c').read_text()
    defines = dict(re.findall(r'^#define (\w+) (\d+)\b', source, re.M))
    assert {k: int(defines[k]) for k in ('WIDTH', 'HEIGHT', 'BUF_W', 'Y0')} == {'WIDTH': WIDTH, 'HEIGHT': HEIGHT, 'BUF_W': BUF_W, 'Y0': Y0}, defines
    assert 'uint32_t r = (x * 3 + y) & 0xff, g = (y * 5 + (x >> 4)) & 0xff, b = (x ^ (y * 7)) & 0xff;' in source, 'pattern() differs from kms_pattern.c'
    assert 'fb.offsets[0] = y0 * create.pitch;' in source and '#define BUF_H (HEIGHT + Y0)' in source
    assert 'y0 = bench ? 0 : Y0;' in source and 'bw = bench ? BENCH_W : BUF_W, bh = bench ? BENCH_H : BUF_H' in source
    neg = self_check_comparisons() + self_check_judges()
    print(f'Init shell syntax, {len(CASES)} case dispatches, kms_pattern.c\'s geometry and pattern, and {neg} negative '
          f'controls passed; no VM launched.')


def guest_buffer(offset: int, swap: bool = False) -> bytearray:
    """A framebuffer as kms_pattern leaves it: FILL everywhere, the pattern at `offset` in rows of STRIDE (B and R
    swapped when `swap`)."""
    size = offset + HEIGHT * STRIDE + STRIDE
    buf = bytearray(b'\xff\x00\xff\x00' * (size // 4))
    for y in range(HEIGHT):
        row = pattern_row_bgrx(y)
        if swap: row = bytes(c for x in range(WIDTH) for c in (*pattern(x, y), 0))
        buf[offset + y * STRIDE:offset + y * STRIDE + 4 * WIDTH] = row
    return buf


def self_check_comparisons() -> int:
    """The comparisons the display case's driver makes: right pixels pass; B and R swapped, the offset ignored (the
    row part only, or the X part), a pitch of the picture's width, and one pixel off each fail."""
    offset = Y0 * STRIDE + 1088                        # an X part, as the buffer object's place in VRAM gives
    good = guest_buffer(offset)
    assert framebuffer_mismatches(good, offset, STRIDE) == (0, [])
    bad = {'B and R swapped': (guest_buffer(offset, swap=True), offset, STRIDE),
           'offset ignored': (good, 0, STRIDE), 'X part ignored': (good, Y0 * STRIDE, STRIDE),
           'pitch of the width': (good, offset, 4 * WIDTH)}
    one = bytearray(good); one[offset + 5 * STRIDE + 4 * 7 + 1] ^= 1
    bad['one pixel'] = (one, offset, STRIDE)
    for what, (buf, off, stride) in bad.items():
        assert framebuffer_mismatches(buf, off, stride)[0], f'{what}: passed'
    assert framebuffer_mismatches(one, offset, STRIDE) == (1, [(7, 5, pattern(7, 5)[:1] + ((pattern(7, 5)[1] ^ 1),) + pattern(7, 5)[2:], pattern(7, 5))])
    rgb = pattern_rgb()
    assert rgb_mismatches(rgb) == (0, [])
    swapped = b''.join(rgb[i + 2:i + 3] + rgb[i + 1:i + 2] + rgb[i:i + 1] for i in range(0, len(rgb), 3))
    assert rgb_mismatches(swapped)[0] > WIDTH * HEIGHT // 2, 'a B/R swap differs almost everywhere'
    assert rgb_mismatches(rgb[3 * WIDTH:] + rgb[:3 * WIDTH])[0], 'a picture one row off'
    assert rgb_mismatches(rgb[:-3])[0], 'a short picture'
    assert text_share(bytes(30)) == (1, 0.0) and text_share(bytes(27) + b'\xff\xff\xff') == (2, 0.1)
    return len(bad) + 3


def self_check_judges() -> int:
    def serial(case, body):
        return f'DISPLAY-BEGIN case={case} cpus=1 kernel={KVER}\n{body}'
    quit = {'quit': {}, 'stopped': 'cancelled', 'socket_left': False}
    host = 'natemu: Cancelled\n'
    offset = Y0 * STRIDE + 1088
    fb = {'width': WIDTH, 'height': HEIGHT, 'stride': STRIDE, 'offset': offset, 'format': 'x8r8g8b8', 'size': 16 << 20}
    mode = {k: fb[k] for k in ('width', 'height', 'stride', 'offset', 'format')}
    good_display = Run('display', 1, serial('display', 'KMS-MODE connector=35 crtc=34 fb=36\nREADY\n'), host, 2, False,
                       dict(quit, framebuffer=fb, descriptors=1, fb_mismatch=[0, []], snapshot=[WIDTH, HEIGHT],
                            snapshot_mismatch=[0, []], status_display=mode,
                            display_changed=[{'enabled': True, 'width': 1024, 'height': 768, 'stride': 4096, 'offset': 0, 'format': 'x8r8g8b8'},
                                             dict(mode, enabled=True)]))
    good_console = Run('display-boot-console', 1, serial('display-boot-console', 'DISPLAY-MODE 1024x768\nDISPLAY-CONSOLE-READY\n'), host, 2,
                       False, dict(quit, snapshot=[1024, 768], colours=2, text_share=0.004,
                                   status_display={'width': 1024, 'height': 768, 'stride': 4096, 'offset': 0, 'format': 'x8r8g8b8'}))
    absent = {'code': -32001, 'data': {'device': 'display'}}
    good_off = Run('display-off', 1, serial('display-off', 'DISPLAY-PCI 0000:00:00.0 0x8086 0x29c0\nDISPLAY-PCI-END\n'
                                                           'DISPLAY-CARD absent\nDISPLAY-OFF-READY\n'), host, 2, False,
                   dict(quit, snapshot=absent, framebuffer=absent, devices={'ps2': True}))
    for good in (good_display, good_console, good_off):
        assert not CASES[good.case].judge(good), (good.case, CASES[good.case].judge(good))
    ctl = lambda r, **kw: dataclasses.replace(r, control=dict(r.control, **kw))
    swap = lambda r, old, new: dataclasses.replace(r, serial=r.serial.replace(old, new, 1))
    bad = {
        'fb mismatch': ctl(good_display, fb_mismatch=[640, [(0, 0, (1, 2, 3), (3, 2, 1))]]),
        'snapshot mismatch': ctl(good_display, snapshot_mismatch=[5, []]),
        'wrong stride': ctl(good_display, framebuffer=dict(fb, stride=2560)),
        'offset under 16 rows': ctl(good_display, framebuffer=dict(fb, offset=15 * STRIDE + 1088), status_display=dict(mode, offset=15 * STRIDE + 1088)),
        'offset not 4-aligned': ctl(good_display, framebuffer=dict(fb, offset=offset + 2)),
        'past the framebuffer': ctl(good_display, framebuffer=dict(fb, size=offset + (HEIGHT - 1) * STRIDE + 4 * WIDTH - 1)),
        'big-endian': ctl(good_display, framebuffer=dict(fb, format='b8g8r8x8')),
        'no descriptor': ctl(good_display, descriptors=0),
        'no display_changed': ctl(good_display, display_changed=good_display.control['display_changed'][:1]),
        'display_changed other offset': ctl(good_display, display_changed=[dict(mode, enabled=True, offset=Y0 * STRIDE)]),
        'status other': ctl(good_display, status_display=None),
        'snapshot size': ctl(good_display, snapshot=[1024, 768]),
        'no READY': swap(good_display, 'READY\n', ''),
        'kms failed': swap(good_display, 'READY', 'KMS-FAIL SETCRTC: Invalid argument\nREADY'),
        'driver error': ctl(good_display, error='RuntimeError: no READY within 150 s'),
        'not cancelled': ctl(good_display, stopped='power_off'),
        'exit 0': dataclasses.replace(good_display, returncode=0),
        'timeout': dataclasses.replace(good_display, timeout=True),
        'oops': swap(good_display, 'READY', 'Oops: 0000 [#1]\nREADY'),
        'uniform console': ctl(good_console, colours=1, text_share=0.0),
        'console almost empty': ctl(good_console, text_share=0.0001),
        'console noise': ctl(good_console, colours=4000, text_share=0.6),
        'console size': ctl(good_console, snapshot=[640, 480]),
        'console not written': swap(good_console, 'DISPLAY-CONSOLE-READY\n', ''),
        'console status': ctl(good_console, status_display=None),
        'off: display found': swap(good_off, 'DISPLAY-PCI-END', f'DISPLAY-PCI 0000:00:01.0 {DISPLAY_ID}\nDISPLAY-PCI-END'),
        'off: card': swap(good_off, 'DISPLAY-CARD absent', 'DISPLAY-CARD present'),
        'off: no listing': swap(good_off, 'DISPLAY-PCI-END\n', ''),
        'off: snapshot answered': ctl(good_off, snapshot={'width': 1, 'height': 1, 'png': ''}),
        'off: -32004': ctl(good_off, framebuffer={'code': -32004, 'data': None}),
        'off: display member': ctl(good_off, devices={'ps2': True, 'display': None}),
    }
    for what, r in bad.items(): assert CASES[r.case].judge(r), f'{what}: passed'
    expect = {'snapshot mismatch': 'snapshot_matches_guest_pattern', 'offset under 16 rows': 'at least 16 rows',
              'no display_changed': 'no display_changed', 'uniform console': 'shows no text'}
    for what, words in expect.items():
        assert any(words in e for e in CASES[bad[what].case].judge(bad[what])), (what, CASES[bad[what].case].judge(bad[what]))
    return len(bad)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('kernel-deb', 'kernel', 'busybox-deb', 'kms-pattern', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    runner = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out'): runner.add_argument('--' + name, type=Path, required=True)
    runner.add_argument('--lock', type=Path, default=VM_LOCK, help='the VM lock file, held shared (default: the state root\'s)')
    runner.add_argument('--case', choices=sorted(CASES), required=True)
    runner.add_argument('--set', action='append', default=[], metavar='KEY=VALUE', help="passed to the natemu CLI's --set")
    runner.add_argument('--vcpus', type=int, default=1)
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': return prepare(args)
    if args.command == 'self-check': return self_check()
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    raise SystemExit(main())
