#!/usr/bin/env python3
"""The devices family's control-socket runner: natemu's control API (`[control] socket`, docs/control-api.md,
crates/host/src/control) driven from the host, observed by stock Linux drivers in the guest, through the `natemu` CLI.

prepare builds one small initramfs: Debian's unchanged BusyBox and the stock modules evdev, psmouse and button (the
ACPI power button driver) from the Debian kernel package (CONFIG_INPUT_EVDEV=m, CONFIG_MOUSE_PS2=m,
CONFIG_ACPI_BUTTON=m; i8042 and atkbd are built in), and an init that runs the case the kernel command line names
(`control_case=NAME`). run boots one VM with `--set control.socket=PATH`, connects to the socket with
tests/lib/control_client.py once the guest says it is ready, sends the case's requests and judges what the guest's
evdev devices reported, the answers and the `stopped` notification, and the host's exit line. self-check runs the
init's shell (syntax, and its device lookup on a sample listing), the event parser on what the host's hexdump prints
for the init's format, and the judges' negative controls, without a VM.

The guest observer: the init finds each device's /dev/input/eventN in /proc/bus/input/devices (the keyboard and mouse
by their serio Phys, the power button by its Name) and prints every 24-byte `struct input_event` (x86-64: 16 bytes of
timeval, u16 type, u16 code, s32 value) on COM1 with BusyBox `hexdump -v -e` as `CONTROL-EV DEV TIME TYPE CODE VALUE`.
The keyboard's and mouse's readers are a background `cat` from the device to a file in the initramfs (it takes
everything queued in one read, so a reader on the serial console cannot hold evdev back) and `tail -f` of that file
into a hexdump writing to the console (a tty: line-buffered, one write per event line). The init prints CONTROL-READY
only once each cat holds its device open (its /proc/PID/fd/0), from when evdev queues every event for it, so nothing
the runner sends after READY is missed. A SYN_DROPPED in any stream fails the case. Kernel messages are kept off the
console (printk level 1) so they cannot split an event line. The power button's reader is the init itself
(`dd bs=24 count=1` per event from a descriptor kept open, so it can act on KEY_POWER). The judges compare parsed
events, never timing: EV_KEY presses (1) and releases (0) (autorepeats, 2, are left out), EV_REL movements
summed over each run of one axis; EV_SYN, EV_MSC and EV_LED are not judged.

Cases (each ends as the judge expects: `quit`, so `stopped: cancelled` and exit status 2, unless it says otherwise):
- control-keyboard: every KeyCode's press and release, KEYS_PER_SEND keys per `send_input`, each after the guest
  reported the one before; the keyboard's events must be exactly KEY_CODES' presses and releases in order. Then a
  second client presses ShiftLeft, and once the guest reported the press, disconnects: the release must arrive.
  Then a third client taps KeyZ, the sentinel: the stream must end with it, so an extra event before it (a second
  release at the disconnect, say) fails the case.
  Why in parts: natemu passes a key's bytes on as fast as the guest reads 60H, so a send_input's keys reach the guest
  as one run of interrupts and the reader does not get to run before evdev's buffer fills. evdev keeps at most 64
  events per reader for this keyboard (8 packets of the device's hint, here 8 events, and at least 64:
  drivers/input/evdev.c evdev_compute_buffer_size, drivers/input/input.c input_estimate_events_per_packet); with more
  it drops what had piled up and reports SYN_DROPPED. Every transition is three events here (MSC_SCAN, the key,
  SYN_REPORT), so one send_input of all 208 transitions loses all but the last few (seen in control-keyboard at 1 and
  2 vCPUs on the M4, when it sent everything at once). That all of them reach the 8042 in order is Task 1's
  queue_waits_for_room.
- control-mouse: dx 1000 (REL_X summing to 1000: natemu splits it into packets of at most 255), dy 40 (REL_Y +40:
  the API's dy is downward like Linux's, PS/2's upward), scroll dy 3 (REL_WHEEL -3), each button down and up
  (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE), scroll dy -3 (REL_WHEEL +3), each its own `send_input`; then the sentinel, a
  left click from a fresh client, which must end the stream.
- control-power-button: `power_button`; the "Power Button" device (Phys LNXPWRBN/button/input0, the fixed button) must
  report KEY_POWER down and up, then the init powers off: `stopped: power_off`, `natemu: Power(1)`, exit status 0.
- control-status (with --set machine.ps2=false): `query_status` while running (protocol 1, running, the vCPUs, no
  ps2), `send_input` is -32001 naming the device ps2 (with events and empty), then `quit`.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import dataclasses
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time

import control_client
from control_client import Control, RpcError, key, tap
from io_common import ROOT, VM_LOCK, clone, digest, stop_group, write_json
from run_platform import KERNEL_DEB_SHA, KERNEL_SHA, KVER, busybox_from, busybox_applets, module_closure, modules_dep, stopped

# Stock modules (run_platform.py's module_closure: each with what its depends= names). The self-check fails if one of
# these is missing or the init loads one that is not here.
MODULES = ('evdev', 'psmouse', 'button')
APPLETS = ('sh', 'mount', 'nproc', 'uname', 'modprobe', 'cat', 'grep', 'ls', 'readlink', 'dmesg', 'sync', 'poweroff',
           'sleep', 'stty', 'hexdump', 'dd', 'tail')
VM_SECONDS = 240
CMDLINE = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr'
SUN_PATH = 103                  # the longest socket path macOS takes
SOCKET_MARGIN = 16              # natemu makes the socket as DIR/.cPIDn/s first (docs/control-api.md: about 10 bytes)
KEYBOARD_PHYS = 'isa0060/serio0/input0'
MOUSE_PHYS = 'isa0060/serio1/input0'
POWER_NAME = 'Power Button'
POWER_PHYS = 'LNXPWRBN/button/input0'   # drivers/acpi/button.c: the fixed power button (ACPI_BUTTON_HID_POWERF)
READY_SECONDS = 150             # boot, modprobe and the readers, at most
KEYS_PER_SEND = 6               # 12 transitions, 36 events (and a lock key's EV_LED): within evdev's 64 per reader
QUIET_SECONDS = 20              # how long the runner waits for an expected event before it gives up

# Linux UAPI constants (include/uapi/linux/input-event-codes.h, Linux 6.12): event types and codes.
EV_SYN, EV_KEY, EV_REL, EV_MSC = 0x00, 0x01, 0x02, 0x04
REL_X, REL_Y, REL_WHEEL = 0x00, 0x01, 0x08
SYN_DROPPED = 3
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
KEY_POWER, KEY_LEFTSHIFT = 116, 42
# Each KeyCode (crates/host/src/control/protocol.rs, in declaration order) and the KEY_* (input-event-codes.h) Linux
# reports for it. natemu sends set 2 codes; the 8042 translates them to set 1, and atkbd (built in; it registers as "AT
# Translated Set 2 keyboard") maps those through atkbd_unxlate_table and atkbd_set2_keycode
# (drivers/input/keyboard/atkbd.c, Linux 6.12). Two keys behave unlike the rest there:
# - PrintScreen is E0 7C (translated E0 37), set 2 code 0xFC in atkbd's table: KEY_SYSRQ, press and release.
# - Pause's press is E1 14 77 E1 F0 14 F0 77 (translated E1 1D 45 E1 9D C5) and its release sends nothing. atkbd takes
#   each E1 as a two-byte prefix (emul = 2) and maps 45 after it as 0xF7 (KEY_PAUSE), the first time as a press and
#   the second, whose bytes carry the translated break bit, as a release. So its tap reports press and release too.
KEY_CODES = {
    'KeyA': ('KEY_A', 30), 'KeyB': ('KEY_B', 48), 'KeyC': ('KEY_C', 46), 'KeyD': ('KEY_D', 32), 'KeyE': ('KEY_E', 18),
    'KeyF': ('KEY_F', 33), 'KeyG': ('KEY_G', 34), 'KeyH': ('KEY_H', 35), 'KeyI': ('KEY_I', 23), 'KeyJ': ('KEY_J', 36),
    'KeyK': ('KEY_K', 37), 'KeyL': ('KEY_L', 38), 'KeyM': ('KEY_M', 50), 'KeyN': ('KEY_N', 49), 'KeyO': ('KEY_O', 24),
    'KeyP': ('KEY_P', 25), 'KeyQ': ('KEY_Q', 16), 'KeyR': ('KEY_R', 19), 'KeyS': ('KEY_S', 31), 'KeyT': ('KEY_T', 20),
    'KeyU': ('KEY_U', 22), 'KeyV': ('KEY_V', 47), 'KeyW': ('KEY_W', 17), 'KeyX': ('KEY_X', 45), 'KeyY': ('KEY_Y', 21),
    'KeyZ': ('KEY_Z', 44),
    'Digit1': ('KEY_1', 2), 'Digit2': ('KEY_2', 3), 'Digit3': ('KEY_3', 4), 'Digit4': ('KEY_4', 5), 'Digit5': ('KEY_5', 6),
    'Digit6': ('KEY_6', 7), 'Digit7': ('KEY_7', 8), 'Digit8': ('KEY_8', 9), 'Digit9': ('KEY_9', 10), 'Digit0': ('KEY_0', 11),
    'Enter': ('KEY_ENTER', 28), 'Escape': ('KEY_ESC', 1), 'Backspace': ('KEY_BACKSPACE', 14), 'Tab': ('KEY_TAB', 15),
    'Space': ('KEY_SPACE', 57), 'Minus': ('KEY_MINUS', 12), 'Equal': ('KEY_EQUAL', 13),
    'BracketLeft': ('KEY_LEFTBRACE', 26), 'BracketRight': ('KEY_RIGHTBRACE', 27), 'Backslash': ('KEY_BACKSLASH', 43),
    'Semicolon': ('KEY_SEMICOLON', 39), 'Quote': ('KEY_APOSTROPHE', 40), 'Backquote': ('KEY_GRAVE', 41),
    'Comma': ('KEY_COMMA', 51), 'Period': ('KEY_DOT', 52), 'Slash': ('KEY_SLASH', 53), 'CapsLock': ('KEY_CAPSLOCK', 58),
    'ShiftLeft': ('KEY_LEFTSHIFT', 42), 'ShiftRight': ('KEY_RIGHTSHIFT', 54), 'ControlLeft': ('KEY_LEFTCTRL', 29),
    'ControlRight': ('KEY_RIGHTCTRL', 97), 'AltLeft': ('KEY_LEFTALT', 56), 'AltRight': ('KEY_RIGHTALT', 100),
    'MetaLeft': ('KEY_LEFTMETA', 125), 'MetaRight': ('KEY_RIGHTMETA', 126), 'ContextMenu': ('KEY_COMPOSE', 127),
    'F1': ('KEY_F1', 59), 'F2': ('KEY_F2', 60), 'F3': ('KEY_F3', 61), 'F4': ('KEY_F4', 62), 'F5': ('KEY_F5', 63),
    'F6': ('KEY_F6', 64), 'F7': ('KEY_F7', 65), 'F8': ('KEY_F8', 66), 'F9': ('KEY_F9', 67), 'F10': ('KEY_F10', 68),
    'F11': ('KEY_F11', 87), 'F12': ('KEY_F12', 88),
    'PrintScreen': ('KEY_SYSRQ', 99), 'ScrollLock': ('KEY_SCROLLLOCK', 70), 'Pause': ('KEY_PAUSE', 119),
    'Insert': ('KEY_INSERT', 110), 'Home': ('KEY_HOME', 102), 'PageUp': ('KEY_PAGEUP', 104), 'Delete': ('KEY_DELETE', 111),
    'End': ('KEY_END', 107), 'PageDown': ('KEY_PAGEDOWN', 109), 'ArrowRight': ('KEY_RIGHT', 106),
    'ArrowLeft': ('KEY_LEFT', 105), 'ArrowDown': ('KEY_DOWN', 108), 'ArrowUp': ('KEY_UP', 103),
    'NumLock': ('KEY_NUMLOCK', 69), 'NumpadDivide': ('KEY_KPSLASH', 98), 'NumpadMultiply': ('KEY_KPASTERISK', 55),
    'NumpadSubtract': ('KEY_KPMINUS', 74), 'NumpadAdd': ('KEY_KPPLUS', 78), 'NumpadEnter': ('KEY_KPENTER', 96),
    'Numpad1': ('KEY_KP1', 79), 'Numpad2': ('KEY_KP2', 80), 'Numpad3': ('KEY_KP3', 81), 'Numpad4': ('KEY_KP4', 75),
    'Numpad5': ('KEY_KP5', 76), 'Numpad6': ('KEY_KP6', 77), 'Numpad7': ('KEY_KP7', 71), 'Numpad8': ('KEY_KP8', 72),
    'Numpad9': ('KEY_KP9', 73), 'Numpad0': ('KEY_KP0', 82), 'NumpadDecimal': ('KEY_KPDOT', 83),
    'IntlBackslash': ('KEY_102ND', 86), 'IntlRo': ('KEY_RO', 89), 'IntlYen': ('KEY_YEN', 124),
}
# control-mouse: each step's events (one send_input each) and what Linux reports for it, merged (EV_REL summed per run
# of one axis). psmouse (drivers/input/mouse/psmouse-base.c) reports REL_Y as -y and REL_WHEEL as -(s8)packet[3].
MOUSE_STEPS = [
    ([{'type': 'pointer_motion', 'dx': 1000, 'dy': 0}], [(EV_REL, REL_X, 1000)]),
    ([{'type': 'pointer_motion', 'dx': 0, 'dy': 40}], [(EV_REL, REL_Y, 40)]),
    ([{'type': 'scroll', 'dy': 3}], [(EV_REL, REL_WHEEL, -3)]),
    *[([{'type': 'button', 'button': b, 'pressed': p}], [(EV_KEY, code, int(p))])
      for b, code in (('left', BTN_LEFT), ('right', BTN_RIGHT), ('middle', BTN_MIDDLE)) for p in (True, False)],
    ([{'type': 'scroll', 'dy': -3}], [(EV_REL, REL_WHEEL, 3)]),
]
# The sentinels: sent by a fresh client once everything else arrived, they must end the stream (an extra event before
# them fails the case).
SENTINEL_KEY, KEY_SENTINEL = 'KeyZ', KEY_CODES['KeyZ'][1]
MOUSE_SENTINEL = ([{'type': 'button', 'button': 'left', 'pressed': p} for p in (True, False)], [(EV_KEY, BTN_LEFT, 1), (EV_KEY, BTN_LEFT, 0)])
# The format of one event's line after `CONTROL-EV DEV `: timeval as four hex words, then type, code, value.
EVENT_FORMAT = r'4/4 "%08x" 1/2 " %u" 1/2 " %u" 1/4 " %d" "\n"'

INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain_console() { console_mode=$(stty -F /dev/console -g) && stty -F /dev/console "$console_mode"; }
fail() { echo "CONTROL-FAIL $*"; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
pass() { echo CONTROL-PASS; sync; drain_console || fail console-drain; poweroff -f; fail poweroff-returned; }
idle() { while :; do sleep 1; done; }
# event_of LINE: the eventN handler of each device in the listing on standard input that has the line LINE.
event_of() {
  hit=
  while read -r line; do
    case "$line" in
      '') hit=;;
      "$1") hit=1;;
      'H: Handlers='*) if test -n "$hit"; then for h in ${line#H: Handlers=}; do case "$h" in event*) echo "$h";; esac; done; fi;;
    esac
  done
}
# find_event VAR LINE: VAR=the eventN of the one device with LINE, waiting up to 20 s for it (serio binds
# asynchronously). Through cat: BusyBox `read` polls, and /proc/bus/input/devices reports readable only after a change.
find_event() {
  var=$1 want=$2 tries=0
  while :; do
    found=$(cat /proc/bus/input/devices | event_of "$want")
    count=0; for e in $found; do count=$((count + 1)); done
    test "$count" -le 1 || fail "$count input devices with '$want': $found"
    test "$count" = 1 && test -c "/dev/input/$found" && break
    tries=$((tries + 1)); test $tries -le 20 || fail "no input device with '$want' after 20 s"
    sleep 1
  done
  eval "$var=$found"
  echo "CONTROL-DEVICE $var=$found $want"
}
# watch NAME EVENT: every event of /dev/input/EVENT on the console as CONTROL-EV NAME ... . evdev keeps 64 events per
# reader here and drops them all when more pile up (SYN_DROPPED), so the device is not read by the hexdump, which is as
# slow as the serial console: `cat` copies it to a file in the initramfs (one read takes everything queued), and
# `tail -f` passes the file to hexdump, which prints it on the console.
# Returns once cat holds the device open (evdev queues events for it from then on).
watch() {
  cat </dev/input/$2 >/tmp/$1.ev &
  pid=$! tries=0
  until test "$(readlink /proc/$pid/fd/0)" = "/dev/input/$2"; do
    tries=$((tries + 1)); test $tries -le 20 || fail "the reader of $2 did not start"; sleep 1
  done
  tail -c +1 -f /tmp/$1.ev | hexdump -v -e '"CONTROL-EV '"$1"' " @FORMAT@' &
}
listing() { cat /proc/bus/input/devices | while read -r line; do echo "CONTROL-INPUT $line"; done; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
name=
for item in $(cat /proc/cmdline); do case "$item" in control_case=*) name=${item#*=};; esac; done
echo "CONTROL-BEGIN case=$name cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '@KVER@' || fail kernel
echo 1 > /proc/sys/kernel/printk || fail printk

# control-keyboard: the keyboard's events.
case_control_keyboard() {
  modprobe evdev || fail modprobe-evdev
  find_event keyboard 'P: Phys=@KEYBOARD_PHYS@'
  listing
  watch keyboard "$keyboard"
  echo CONTROL-READY
  idle
}

# control-mouse: the mouse's events, after psmouse binds the mouse on serio1.
case_control_mouse() {
  modprobe evdev || fail modprobe-evdev
  modprobe psmouse || fail modprobe-psmouse
  find_event mouse 'P: Phys=@MOUSE_PHYS@'
  listing
  watch mouse "$mouse"
  echo CONTROL-READY
  idle
}

# control-power-button: the power button's events, read here one at a time; its KEY_POWER release powers off.
case_control_power_button() {
  modprobe evdev || fail modprobe-evdev
  modprobe button || fail modprobe-button
  find_event power 'N: Name="@POWER_NAME@"'
  listing
  exec 4</dev/input/$power || fail "open $power"
  echo CONTROL-READY
  while :; do
    ev=$(dd bs=24 count=1 <&4 2>/dev/null | hexdump -v -e '"CONTROL-EV power " @FORMAT@')
    test -n "$ev" || fail "no event read from $power"
    echo "$ev"
    case "$ev" in *' 1 @KEY_POWER@ 0') break;; esac
  done
  echo CONTROL-POWEROFF
  pass
}

# control-status: nothing in the guest (machine.ps2=false); the runner quits.
case_control_status() {
  echo CONTROL-READY
  idle
}

case "$name" in
  control-keyboard) case_control_keyboard;;
  control-mouse) case_control_mouse;;
  control-power-button) case_control_power_button;;
  control-status) case_control_status;;
  *) fail "unknown case '$name'";;
esac
fail "case $name returned"
'''
for _k, _v in {'KVER': KVER, 'FORMAT': EVENT_FORMAT, 'KEYBOARD_PHYS': KEYBOARD_PHYS, 'MOUSE_PHYS': MOUSE_PHYS,
               'POWER_NAME': POWER_NAME, 'KEY_POWER': KEY_POWER}.items():
    INIT = INIT.replace(f'@{_k}@', str(_v))


@dataclasses.dataclass(frozen=True)
class Run:
    """What one VM left for its judge."""
    case: str
    vcpus: int
    serial: str                     # COM1, with \r removed
    stderr: str
    returncode: int | None
    timeout: bool                   # killed at VM_SECONDS, or after the driver ended with the VM still running
    control: dict                   # what the driver sent and got: answers, errors, the `stopped` reason


def lines(serial: str) -> list:
    """The complete lines of the serial log (a last line still being written is left out)."""
    return serial.replace('\r', '').split('\n')[:-1]


def events(serial: str, device: str) -> list:
    """(type, code, value) of each CONTROL-EV line of `device`, in order; value as the s32 it is."""
    out = []
    for line in lines(serial):
        if m := re.fullmatch(rf'CONTROL-EV {device} [0-9a-f]{{32}} (\d+) (\d+) (-?\d+)', line):
            t, c, v = map(int, m.groups())
            out.append((t, c, v - (1 << 32) if v >= 1 << 31 else v))
    return out


def keys(evs: list) -> list:
    """EV_KEY presses and releases (autorepeats left out), as (code, value)."""
    return [(c, v) for t, c, v in evs if t == EV_KEY and v in (0, 1)]


def merged(evs: list) -> list:
    """EV_KEY presses and releases and EV_REL movements, each run of one EV_REL axis summed; nothing else."""
    out = []
    for t, c, v in evs:
        if t == EV_KEY and v in (0, 1): out.append((t, c, v))
        elif t == EV_REL:
            if out and out[-1][:2] == (t, c): out[-1] = (t, c, out[-1][2] + v)
            else: out.append((t, c, v))
    return out


def keyboard_marks() -> dict:
    """How many EV_KEY presses and releases the guest has reported at each point the keyboard driver waits for: every
    table key tapped, ShiftLeft pressed, ShiftLeft released (at the disconnect), the sentinel tapped (all of them)."""
    table = 2 * len(KEY_CODES)
    return {'table': table, 'shift_down': table + 1, 'shift_up': table + 2, 'sentinel': table + 4}


def keyboard_expected() -> list:
    """Every key's press and release, ShiftLeft's press and its release at the disconnect, the sentinel's tap."""
    return ([(code, v) for _, code in KEY_CODES.values() for v in (1, 0)] + [(KEY_LEFTSHIFT, 1), (KEY_LEFTSHIFT, 0)]
            + [(KEY_SENTINEL, 1), (KEY_SENTINEL, 0)])


def mouse_expected() -> list:
    """Each step's events, then the sentinel's click."""
    return [e for _, want in MOUSE_STEPS for e in want] + MOUSE_SENTINEL[1]


def input_devices(serial: str) -> list:
    """/proc/bus/input/devices from the CONTROL-INPUT lines: (name, phys) of each device."""
    devices, name, phys = [], None, None
    for line in [*(l[len('CONTROL-INPUT'):].strip() for l in lines(serial) if l.startswith('CONTROL-INPUT')), '']:
        if not line:
            if name is not None or phys is not None: devices.append((name, phys))
            name = phys = None
        elif m := re.fullmatch(r'N: Name="(.*)"', line): name = m.group(1)
        elif m := re.fullmatch(r'P: Phys=(.*)', line): phys = m.group(1)
    return devices


def common(run: Run) -> list:
    """Every case: the init started it on the pinned kernel, the guest and the driver reported no failure, the guest
    got ready, no kernel error, no timeout."""
    errors = []
    begin = re.findall(r'^CONTROL-BEGIN case=(\S*) cpus=(\d+) kernel=(\S+)$', run.serial, re.M)
    if begin != [(run.case, str(run.vcpus), KVER)]:
        errors.append(f'the init did not start case {run.case} at {run.vcpus} vCPUs on {KVER} (CONTROL-BEGIN: {begin})')
    errors += [f'guest: {why}' for why in re.findall(r'^CONTROL-FAIL (.*)$', run.serial, re.M)]
    if len(re.findall(r'^CONTROL-READY$', run.serial, re.M)) != 1: errors.append('the guest never reported its readers ready (CONTROL-READY)')
    if (bad := re.search(r'^.*(?:BUG:|Oops:|Kernel panic|Call Trace:).*$', run.serial, re.M)):
        errors.append(f'kernel error: {bad.group(0)!r}')
    if run.timeout: errors.append(f'timed out: no stop within {VM_SECONDS} s, or the VM ran on after the driver ended')
    if run.control.get('error'): errors.append(f'driver: {run.control["error"]}')
    for device in ('keyboard', 'mouse', 'power'):
        if (EV_SYN, SYN_DROPPED) in [e[:2] for e in events(run.serial, device)]:
            errors.append(f'{device}: evdev dropped events (SYN_DROPPED): the guest\'s reader fell behind')
    return errors


def ended(run: Run, reason: str, line: str, status: int) -> list:
    """The driver got `stopped` with `reason`; the host's last line is `line` and its exit status `status`."""
    errors = [] if run.control.get('stopped') == reason else [f'stopped reason {run.control.get("stopped")!r}, expected {reason!r}']
    return errors + stopped(run, line, status)


def cancelled(run: Run) -> list:
    """Ended by the driver's quit: answered {}, `stopped: cancelled`, `natemu: Cancelled`, exit status 2."""
    errors = [] if run.control.get('quit') == {} else [f'quit answered {run.control.get("quit")!r}, expected {{}}']
    return errors + ended(run, 'cancelled', 'natemu: Cancelled', 2)


def compare(got: list, want: list, what: str) -> list:
    if got == want: return []
    at = next((i for i, (g, w) in enumerate(zip(got, want)) if g != w), min(len(got), len(want)))
    return [f'{what}: {len(got)} events, expected {len(want)}; the first difference is at {at}: '
            f'got {got[at:at + 3]}, expected {want[at:at + 3]}']


def judge_keyboard(run: Run) -> list:
    errors = common(run)
    if (KEYBOARD_PHYS not in [p for _, p in input_devices(run.serial)]):
        errors.append(f'no input device on {KEYBOARD_PHYS} listed')
    parts = -(-len(KEY_CODES) // KEYS_PER_SEND)
    if run.control.get('sends') != [{}] * parts: errors.append(f'the {parts} send_inputs of the keys answered {run.control.get("sends")!r}')
    if run.control.get('shift') != {}: errors.append(f'the ShiftLeft press answered {run.control.get("shift")!r}')
    got, want = keys(events(run.serial, 'keyboard')), keyboard_expected()
    errors += compare(got, want, 'keyboard EV_KEY presses and releases')
    if got[:len(want) - 4] == want[:-4] and got[len(want) - 4:] != want[-4:]:
        errors.append(f'after the table: {got[len(want) - 4:]}, expected ShiftLeft down, at the disconnect up, then the '
                      f'sentinel {SENTINEL_KEY} down and up')
    if run.control.get('sentinel') != {}: errors.append(f'the sentinel answered {run.control.get("sentinel")!r}')
    names = {code: (name, k) for k, (name, code) in KEY_CODES.items()}
    if wrong := sorted({f'{names[w[0]][1]} gave {g[0]} not {names[w[0]][0]} ({w[0]})' for g, w in zip(got, want)
                        if g != w and w[0] in names and g[1] == w[1]}):
        errors.append('keys: ' + '; '.join(wrong[:8]))
    return errors + cancelled(run)


def judge_mouse(run: Run) -> list:
    errors = common(run)
    if (MOUSE_PHYS not in [p for _, p in input_devices(run.serial)]):
        errors.append(f'no input device on {MOUSE_PHYS} listed')
    answers = run.control.get('steps', [])
    if answers != [{}] * len(MOUSE_STEPS): errors.append(f'the steps answered {answers}, expected {{}} each')
    if run.control.get('sentinel') != {}: errors.append(f'the sentinel answered {run.control.get("sentinel")!r}')
    errors += compare(merged(events(run.serial, 'mouse')), mouse_expected(), 'mouse EV_KEY and summed EV_REL')
    return errors + cancelled(run)


def judge_power_button(run: Run) -> list:
    errors = common(run)
    if (buttons := [p for n, p in input_devices(run.serial) if n == POWER_NAME]) != [POWER_PHYS]:
        errors.append(f'"{POWER_NAME}" devices on {buttons}, expected one on {POWER_PHYS}')
    if run.control.get('power_button') != {}: errors.append(f'power_button answered {run.control.get("power_button")!r}')
    errors += compare(keys(events(run.serial, 'power')), [(KEY_POWER, 1), (KEY_POWER, 0)], 'power button EV_KEY')
    if len(re.findall(r'^CONTROL-POWEROFF$', run.serial, re.M)) != 1: errors.append('the init did not power off (CONTROL-POWEROFF)')
    return errors + ended(run, 'power_off', 'natemu: Power(1)', 0)


def judge_status(run: Run) -> list:
    errors = common(run)
    status = run.control.get('status')
    want = {'protocol': 1, 'state': 'running', 'vcpus': run.vcpus, 'devices': {'ps2': False}}
    if not isinstance(status, dict) or {k: status.get(k) for k in want} != want or not isinstance(status.get('uptime_ms'), int) \
            or status['uptime_ms'] <= 0:
        errors.append(f'query_status gave {status!r}, expected {want} and a positive uptime_ms')
    for what in ('input', 'empty'):
        got = run.control.get(what)
        if got != {'code': -32001, 'data': {'device': 'ps2'}}:
            errors.append(f'send_input ({what}) gave {got!r}, expected error -32001 with data {{"device": "ps2"}}')
    return errors + cancelled(run)


@dataclasses.dataclass(frozen=True)
class Case:
    words: tuple                    # kernel command line words beyond CMDLINE and control_case=NAME
    judge: object
    drive: str                      # the driver function's name


CASES = {
    'control-keyboard': Case((), judge_keyboard, 'drive_keyboard'),
    'control-mouse': Case((), judge_mouse, 'drive_mouse'),
    'control-power-button': Case((), judge_power_button, 'drive_power_button'),
    'control-status': Case((), judge_status, 'drive_status'),
}


class Driver:
    """The host's side of one run: the socket, the serial log, and whether natemu still runs."""
    def __init__(self, sock: Path, serial: Path, process):
        self.sock, self.serial, self.process, self.control = sock, serial, process, {}

    def alive(self) -> bool:
        return self.process.poll() is None

    def text(self) -> str:
        return self.serial.read_text(errors='replace').replace('\r', '') if self.serial.exists() else ''

    def until(self, test, seconds: float, what: str):
        """Waits until test(serial text) is true; raises if natemu ends or `seconds` pass first."""
        end = time.monotonic() + seconds
        while not test(self.text()):
            if not self.alive(): raise RuntimeError(f'natemu exited while waiting for {what}')
            if time.monotonic() >= end: raise RuntimeError(f'no {what} within {seconds} s')
            time.sleep(0.1)

    def connect(self) -> Control:
        return Control.connect(self.sock, timeout=READY_SECONDS, alive=self.alive)

    def ready(self) -> Control:
        """A connection once the guest's readers are ready."""
        vm = self.connect()
        self.until(lambda t: re.search(r'^CONTROL-READY$', t, re.M), READY_SECONDS, 'CONTROL-READY from the guest')
        return vm

    def quit(self):
        vm = self.connect()
        self.control['quit'] = vm.call('quit')
        self.control['stopped'] = vm.wait_stopped()
        vm.close()


def answer(call):
    """A call's result, or its error as {'code', 'data'}."""
    try: return call()
    except RpcError as e: return {'code': e.code, 'data': e.data}


def drive_keyboard(d: Driver):
    vm = d.ready()
    want = keyboard_expected()
    codes, d.control['sends'] = list(KEY_CODES), []
    for at in range(0, len(codes), KEYS_PER_SEND):
        part = codes[at:at + KEYS_PER_SEND]
        d.control['sends'].append(answer(lambda: vm.send_input([e for k in part for e in tap(k)])))
        d.until(lambda t: len(keys(events(t, 'keyboard'))) >= 2 * (at + len(part)), QUIET_SECONDS, f'{part} from the guest')
    holder = d.connect()
    d.control['shift'] = answer(lambda: holder.send_input([key('ShiftLeft', True)]))
    mark = keyboard_marks()
    d.until(lambda t: len(keys(events(t, 'keyboard'))) >= mark['shift_down'], QUIET_SECONDS, 'the ShiftLeft press from the guest')
    holder.close()                  # held: natemu must release it
    d.until(lambda t: len(keys(events(t, 'keyboard'))) >= mark['shift_up'], QUIET_SECONDS, 'the ShiftLeft release after the disconnect')
    last = d.connect()
    d.control['sentinel'] = answer(lambda: last.send_input(tap(SENTINEL_KEY)))
    d.until(lambda t: len(keys(events(t, 'keyboard'))) >= mark['sentinel'] and keys(events(t, 'keyboard'))[-1:] == want[-1:],
            QUIET_SECONDS, f'the sentinel {SENTINEL_KEY} from the guest')
    last.close()
    vm.close()
    d.quit()


def drive_mouse(d: Driver):
    vm = d.ready()
    d.control['steps'] = [answer(lambda s=s: vm.send_input(s)) for s, _ in MOUSE_STEPS]
    want = mouse_expected()
    d.until(lambda t: len(merged(events(t, 'mouse'))) >= len(want) - 2, QUIET_SECONDS, 'every mouse step from the guest')
    last = d.connect()
    d.control['sentinel'] = answer(lambda: last.send_input(MOUSE_SENTINEL[0]))
    d.until(lambda t: len(merged(events(t, 'mouse'))) >= len(want) and merged(events(t, 'mouse'))[-1:] == want[-1:],
            QUIET_SECONDS, 'the sentinel click from the guest')
    last.close()
    vm.close()
    d.quit()


def drive_power_button(d: Driver):
    vm = d.ready()
    d.control['power_button'] = answer(lambda: vm.call('power_button'))
    d.control['stopped'] = vm.wait_stopped(timeout=60)
    vm.close()


def drive_status(d: Driver):
    vm = d.ready()
    d.control['status'] = answer(lambda: vm.call('query_status'))
    d.control['input'] = answer(lambda: vm.send_input(tap('KeyA')))
    d.control['empty'] = answer(lambda: vm.send_input([]))
    vm.close()
    d.quit()


def socket_folder(work: Path) -> tuple[Path, bool]:
    """Where the socket goes: the case's folder if the path (with natemu's temporary folder) fits sun_path, else a
    private folder the runner makes (and removes): (folder, made)."""
    if len(str(work.resolve() / 'c.sock').encode()) + SOCKET_MARGIN <= SUN_PATH: return work.resolve(), False
    folder = Path(tempfile.mkdtemp(prefix='nm-'))
    if len(str(folder / 'c.sock').encode()) + SOCKET_MARGIN > SUN_PATH:     # a long TMPDIR
        shutil.rmtree(folder)
        folder = Path(tempfile.mkdtemp(prefix='nm-', dir='/tmp'))
    return folder, True


def boot(args, name: str, work: Path, cases=None, cpio='control.cpio', key='control_case', driver=None) -> Run:
    """One VM for case `name`, its driver on a thread; stopped at VM_SECONDS, or 20 s after the driver ended. Another
    runner of this family (run_display.py) passes its own cases, initramfs, command-line key and Driver."""
    case = (cases or CASES)[name]
    folder, made = socket_folder(work)
    sock = folder / 'c.sock'
    serial = work / 'serial.log'
    command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
               '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / cpio).resolve()),
               '--vcpus', str(args.vcpus), '--ram', '2G', '--compile-cache', 'off', '--serial', str(serial.resolve()),
               '--cmdline', ' '.join([CMDLINE, *getattr(case, 'words', ()), f'{key}={name}']), '--set', f'control.socket={sock}']
    for setting in args.set: command += ['--set', setting]
    write_json(work / 'command.json', command)
    env = {k: v for k, v in os.environ.items() if k not in ('NATEMU_PERF_LOG', 'NATEMU_COMPILE_CACHE')}   # settings the CLI reads (deprecated)
    started, timeout = time.monotonic(), False
    try:
        with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
            driver = (driver or Driver)(sock, serial, process)
            drive_case = case.drive if callable(case.drive) else globals()[case.drive]

            def drive():
                try: drive_case(driver)
                except Exception as e: driver.control['error'] = f'{type(e).__name__}: {e}'
            thread = threading.Thread(target=drive, daemon=True)
            thread.start()
            driver_end = None
            try:
                while process.poll() is None:
                    if driver_end is None and not thread.is_alive(): driver_end = time.monotonic()
                    if time.monotonic() - started >= VM_SECONDS or (driver_end and time.monotonic() - driver_end >= 20):
                        timeout = True
                        stop_group(process)
                        break
                    time.sleep(0.1)
            except BaseException:
                stop_group(process)
                raise
            thread.join(30)
            if thread.is_alive(): driver.control.setdefault('error', 'the driver did not finish 30 s after natemu exited')
        socket_left = os.path.lexists(sock)
    finally:
        if made: shutil.rmtree(folder, ignore_errors=True)
    control = dict(driver.control, socket_left=socket_left)
    if socket_left: control.setdefault('error', f'the socket {sock} is still there after natemu exited')
    return Run(name, args.vcpus, serial.read_text(errors='replace').replace('\r', '') if serial.exists() else '',
               (work / 'stderr.log').read_text(errors='replace'), process.returncode, timeout, control)


def prepare(args, init=INIT, modules=MODULES, applets=APPLETS, cpio='control.cpio', files=None,
            purpose="natemu's control socket observed by stock Linux evdev, psmouse and button (tests/devices)"):
    """The initramfs: BusyBox with `applets`, `modules` and what they need, `init`, and `files` (path in the root ->
    an executable to copy there; run_display.py's kms_pattern)."""
    files = files or {}
    args.out.mkdir(parents=True, exist_ok=False)
    assert digest(args.kernel) == KERNEL_SHA, 'not the pinned Debian kernel'
    assert digest(args.kernel_deb) == KERNEL_DEB_SHA, 'not the pinned Debian kernel package'
    clone(args.kernel, args.out / 'linux')
    root = args.out / 'rootfs'
    for name in ('bin', 'dev', 'proc', 'sys', 'tmp', 'lib/modules/' + KVER):
        (root / name).mkdir(parents=True, exist_ok=True)
    busybox = busybox_from(args.busybox_deb)
    assert not set(applets) - set(busybox_applets(busybox)), f'BusyBox lacks one of {applets}'
    (root / 'bin/busybox').write_bytes(busybox)
    (root / 'bin/busybox').chmod(0o755)
    for applet in applets: (root / 'bin' / applet).symlink_to('busybox')
    for rel, src in files.items():
        (root / rel).write_bytes(src.read_bytes())
        (root / rel).chmod(0o755)
    modules = module_closure(args.kernel_deb, modules)
    for rel, data, _ in modules.values():
        dest = root / 'lib/modules' / KVER / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (root / 'lib/modules' / KVER / 'modules.dep').write_text(modules_dep(modules))
    (root / 'init').write_text(init)
    (root / 'init').chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mkcpio)
    mkcpio.main(str(root), str(args.out / cpio))
    platform = Path(__file__).resolve().parent / 'run_platform.py'
    producer = Path(sys.modules['__main__'].__file__).resolve()
    manifest = {'purpose': purpose,
                'kernel_version': KVER, 'sources': {p.name: digest(p) for p in (args.kernel, args.kernel_deb, args.busybox_deb, *files.values())},
                'producer': {str(p.relative_to(ROOT)): digest(p) for p in sorted({producer, Path(__file__).resolve(), platform, ROOT / 'tools/play/mkcpio.py'})},
                'modules': {n: {'path': rel, 'sha256': digest(root / 'lib/modules' / KVER / rel), 'depends': deps}
                            for n, (rel, _, deps) in sorted(modules.items())},
                'outputs': {str(p.relative_to(args.out)): digest(p)
                            for p in (args.out / 'linux', args.out / cpio, root / 'init', root / 'bin/busybox', *(root / r for r in files))}}
    write_json(args.out / 'inputs.json', manifest)
    print(json.dumps(manifest, indent=2))


def run(args, cases=None, boot=boot, libs=()):
    """One case's VM, judged; results.json in --out. run_display.py passes its cases, boot and extra libraries."""
    cases = cases or CASES
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.inputs / name) == sha, name
    client = [Path(m.__file__).resolve() for m in (control_client, *libs)]
    main = Path(sys.modules['__main__'].__file__).resolve()
    write_json(args.out / 'manifest.json', {'artifacts': {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, main, *client)},
                                            'inputs': manifest, 'case': args.case, 'set': args.set, 'vcpus': args.vcpus,
                                            'seconds_per_vm': VM_SECONDS})
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)          # the VM lock, shared as the other Linux runners hold it
        work = args.out / f'{args.case}-{args.vcpus}'
        work.mkdir()
        t0 = time.monotonic()
        r = boot(args, args.case, work)
        seconds = time.monotonic() - t0
    errors = cases[args.case].judge(r)
    result = {'name': work.name, 'ok': not errors, 'errors': errors, 'returncode': r.returncode, 'timeout': r.timeout,
              'seconds': round(seconds, 1), 'control': r.control}
    write_json(work / 'result.json', result)
    write_json(args.out / 'results.json', {'results': [result]})
    print(json.dumps(result), flush=True)
    return int(bool(errors))


def ev_line(device: str, t: int, c: int, v: int) -> str:
    return f'CONTROL-EV {device} {"0" * 32} {t} {c} {v}'


def self_check():
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    assert '@' not in INIT, 'a placeholder left in INIT'
    for name in CASES:
        assert f'\n  {name}) case_{name.replace("-", "_")};;\n' in INIT, f'{name}: no dispatch line in INIT'
        assert f'\ncase_{name.replace("-", "_")}() {{\n' in INIT, f'{name}: no case function in INIT'
        assert callable(globals().get(CASES[name].drive)), f'{name}: no driver'
    # The modules: evdev, psmouse and button are in MODULES (prepare takes each from the kernel package, or fails),
    # and every module the init loads is one of them.
    for needed in ('evdev', 'psmouse', 'button'): assert needed in MODULES, f'{needed} is not in MODULES'
    loaded = set(re.findall(r'modprobe (\w+)', INIT))
    assert loaded and loaded <= set(MODULES), f'the init loads {sorted(loaded - set(MODULES))}, not in MODULES'
    # KEY_CODES: every KeyCode of protocol.rs, in its order, each with a distinct code.
    protocol = (ROOT / 'crates/host/src/control/protocol.rs').read_text()
    block = re.search(r'key_codes! \{(.*?)\n\}', protocol, re.S).group(1)
    declared = re.findall(r'\b[A-Z]\w*', re.sub(r'//.*', '', block))
    assert declared == list(KEY_CODES), f'KEY_CODES differs from protocol.rs: {set(declared) ^ set(KEY_CODES)}'
    assert len({c for _, c in KEY_CODES.values()}) == len(KEY_CODES)
    # The init's device lookup, run by the host's sh on a sample listing.
    fn = re.search(r'^event_of\(\) \{\n.*?^\}\n', INIT, re.S | re.M).group(0)
    listing = ('I: Bus=0019 Vendor=0000 Product=0001 Version=0000\nN: Name="Power Button"\nP: Phys=LNXPWRBN/button/input0\n'
               'H: Handlers=kbd event0 \n\nI: Bus=0011 Vendor=0001 Product=0001 Version=ab41\n'
               'N: Name="AT Translated Set 2 keyboard"\nP: Phys=isa0060/serio0/input0\nH: Handlers=sysrq kbd leds event1 \n\n'
               'N: Name="ImPS/2 Generic Wheel Mouse"\nP: Phys=isa0060/serio1/input0\nH: Handlers=mouse0 event2 \n\n')
    for want, ev in ((f'P: Phys={KEYBOARD_PHYS}', 'event1\n'), (f'P: Phys={MOUSE_PHYS}', 'event2\n'),
                     (f'N: Name="{POWER_NAME}"', 'event0\n'), ('N: Name="Sleep Button"', '')):
        out = subprocess.run(['sh', '-c', fn + 'event_of "$1"', 'sh', want], input=listing, capture_output=True, text=True, check=True).stdout
        assert out == ev, (want, out)
    # The event format as the host's hexdump prints it (BusyBox's hexdump comes from the same BSD code), read back.
    raw = b''.join(struct.pack('<qqHHi', 1700000000 + i, 5, t, c, v) for i, (t, c, v) in enumerate([(1, 30, 1), (2, 8, -3), (0, 0, 0)]))
    fmt = re.search(r"hexdump -v -e '\"CONTROL-EV '\"\$1\"' \" (.*?)' &", INIT).group(1)
    out = subprocess.run(['hexdump', '-v', '-e', '"CONTROL-EV kb " ' + fmt], input=raw, capture_output=True, check=True).stdout.decode()
    assert events(out, 'kb') == [(1, 30, 1), (2, 8, -3), (0, 0, 0)], out
    assert events(ev_line('m', 2, 8, 4294967293) + '\n', 'm') == [(2, 8, -3)]
    assert events(ev_line('m', 2, 8, 3), 'm') == [], 'a line still being written counted'
    assert merged([(2, 0, 255), (0, 0, 0), (2, 0, 255), (2, 1, 3), (1, 272, 1), (1, 272, 2), (4, 4, 9)]) == [(2, 0, 510), (2, 1, 3), (1, 272, 1)]
    # The keyboard driver's wait points name the right events of the expected sequence.
    want, mark = keyboard_expected(), keyboard_marks()
    table = [(c, v) for _, c in KEY_CODES.values() for v in (1, 0)]
    assert want[:mark['table']] == table and want[mark['table']:mark['shift_down']] == [(KEY_LEFTSHIFT, 1)]
    assert want[mark['shift_down']:mark['shift_up']] == [(KEY_LEFTSHIFT, 0)]
    assert want[mark['shift_up']:] == [(KEY_SENTINEL, 1), (KEY_SENTINEL, 0)] and mark['sentinel'] == len(want)
    self_check_keyboard_driver()
    neg = self_check_judges()
    print(f'Init shell syntax and device lookup, {len(CASES)} case dispatches, KEY_CODES ({len(KEY_CODES)} keys), the event '
          f'format and {neg} negative judge controls passed; no VM launched.')


def self_check_keyboard_driver():
    """drive_keyboard against a fake guest that reports each key at once and a ShiftLeft release when its holder
    disconnects: every wait must be met by exactly what the guest has reported by then (a wait for an event that
    comes only later raises, as it would time out in a run)."""
    lines, held = [], set()

    class Guest:
        def send_input(self, evs):
            for e in evs:
                code = KEY_CODES[e['code']][1]
                if e['pressed']: held.add((self, code))
                else: held.discard((self, code))
                lines.append(ev_line('keyboard', EV_KEY, code, int(e['pressed'])))
            return {}
        def close(self):
            for who, code in sorted(h for h in held if h[0] is self): lines.append(ev_line('keyboard', EV_KEY, code, 0))
            held.difference_update({h for h in held if h[0] is self})
        def call(self, method, params=None): return {}
        def wait_stopped(self, timeout=60.0): return 'cancelled'

    class Fake(Driver):
        def __init__(self): self.control = {}
        def connect(self): return Guest()
        def ready(self): return Guest()
        def until(self, test, seconds, what):
            if not test(''.join(l + '\n' for l in lines)): raise RuntimeError(f'{what}: not reported yet')
    fake = Fake()
    drive_keyboard(fake)
    assert keys(events(''.join(l + '\n' for l in lines), 'keyboard')) == keyboard_expected()
    assert fake.control['stopped'] == 'cancelled' and fake.control['sentinel'] == {}


def self_check_judges() -> int:
    def serial(case, cpus, body, listing=''):
        return f'CONTROL-BEGIN case={case} cpus={cpus} kernel={KVER}\n{listing}CONTROL-READY\n{body}'
    kb_listing = f'CONTROL-INPUT N: Name="AT Translated Set 2 keyboard"\nCONTROL-INPUT P: Phys={KEYBOARD_PHYS}\nCONTROL-INPUT \n'
    kb_lines = []
    for code, v in keyboard_expected():
        kb_lines += [ev_line('keyboard', EV_MSC, 4, 0x1e), ev_line('keyboard', EV_KEY, code, v), ev_line('keyboard', EV_SYN, 0, 0)]
    held = 3 * keyboard_marks()['shift_down']               # the lines up to and including the ShiftLeft press
    kb_lines.insert(held, ev_line('keyboard', EV_KEY, KEY_LEFTSHIFT, 2))     # an autorepeat while held
    host = 'natemu: Cancelled\n'
    quit = {'quit': {}, 'stopped': 'cancelled'}
    good_kb = Run('control-keyboard', 2, serial('control-keyboard', 2, '\n'.join(kb_lines) + '\n', kb_listing), host, 2, False,
                  dict(quit, sends=[{}] * -(-len(KEY_CODES) // KEYS_PER_SEND), shift={}, sentinel={}))
    mouse_listing = f'CONTROL-INPUT N: Name="ImPS/2 Generic Wheel Mouse"\nCONTROL-INPUT P: Phys={MOUSE_PHYS}\nCONTROL-INPUT \n'
    m_lines = [ev_line('mouse', EV_REL, REL_X, 255)] * 3 + [ev_line('mouse', EV_REL, REL_X, 235), ev_line('mouse', EV_SYN, 0, 0),
                                                             ev_line('mouse', EV_REL, REL_Y, 40)]
    m_lines += [ev_line('mouse', t, c, v) for t, c, v in mouse_expected()[2:]]
    good_mouse = Run('control-mouse', 1, serial('control-mouse', 1, '\n'.join(m_lines) + '\n', mouse_listing), host, 2, False,
                     dict(quit, steps=[{}] * len(MOUSE_STEPS), sentinel={}))
    p_listing = f'CONTROL-INPUT N: Name="{POWER_NAME}"\nCONTROL-INPUT P: Phys={POWER_PHYS}\nCONTROL-INPUT \n'
    p_body = '\n'.join([ev_line('power', EV_KEY, KEY_POWER, 1), ev_line('power', EV_SYN, 0, 0), ev_line('power', EV_KEY, KEY_POWER, 0),
                        ev_line('power', EV_SYN, 0, 0), 'CONTROL-POWEROFF', 'CONTROL-PASS', 'reboot: Power down']) + '\n'
    good_power = Run('control-power-button', 1, serial('control-power-button', 1, p_body, p_listing), 'natemu: Power(1)\n', 0, False,
                     {'power_button': {}, 'stopped': 'power_off'})
    no_ps2 = {'code': -32001, 'data': {'device': 'ps2'}}
    good_status = Run('control-status', 1, serial('control-status', 1, ''), host, 2, False,
                      dict(quit, status={'protocol': 1, 'state': 'running', 'vcpus': 1, 'uptime_ms': 900, 'devices': {'ps2': False}},
                           input=no_ps2, empty=no_ps2))
    for good in (good_kb, good_mouse, good_power, good_status):
        assert not CASES[good.case].judge(good), (good.case, CASES[good.case].judge(good))
    swap = lambda r, old, new: dataclasses.replace(r, serial=r.serial.replace(old, new, 1))
    rswap = lambda r, old, new: dataclasses.replace(r, serial=new.join(r.serial.rsplit(old, 1)))     # the last one
    ctl = lambda r, **kw: dataclasses.replace(r, control=dict(r.control, **kw))
    drop = lambda r, *keys: dataclasses.replace(r, control={k: v for k, v in r.control.items() if k not in keys})
    kb = lambda code, v: ev_line('keyboard', EV_KEY, code, v) + '\n'
    bad = {
        # control-keyboard
        'one key wrong (KEY_A as KEY_Q)': swap(good_kb, kb(30, 1), kb(16, 1)),
        'a release lost': swap(good_kb, kb(48, 0), ''),
        'a key repeated': swap(good_kb, kb(48, 0), kb(48, 0) + kb(48, 1) + kb(48, 0)),
        'PrintScreen as KEY_PRINT': swap(good_kb, kb(99, 1), kb(210, 1)),
        'Pause without its release': swap(good_kb, kb(119, 0), ''),
        'no release at the disconnect': dataclasses.replace(good_kb, serial=good_kb.serial[:good_kb.serial.rindex(kb(KEY_LEFTSHIFT, 0))]),
        'press and release swapped': dataclasses.replace(good_kb, serial=good_kb.serial.replace(kb(30, 1), '#', 1)
                                                         .replace(kb(30, 0), kb(30, 1), 1).replace('#', kb(30, 0), 1)),
        'no keyboard listed': swap(good_kb, f'P: Phys={KEYBOARD_PHYS}', 'P: Phys=isa0060/serio2/input0'),
        'not ready': swap(good_kb, 'CONTROL-READY\n', ''),
        'send stalled': ctl(good_kb, sends=[{}] * 17 + [{'code': -32003, 'data': None}]),
        'driver error': ctl(good_kb, error='RuntimeError: no every key from the guest within 20 s'),
        'other vcpus': dataclasses.replace(good_kb, vcpus=1),
        'guest failure': swap(good_kb, 'CONTROL-READY', 'CONTROL-FAIL no input device\nCONTROL-READY'),
        'kernel oops': swap(good_kb, 'CONTROL-READY', 'Oops: 0000 [#1]\nCONTROL-READY'),
        'not cancelled': ctl(good_kb, stopped='power_off'),
        'no stopped': drop(good_kb, 'stopped'),
        'quit failed': ctl(good_kb, quit={'code': -32002, 'data': None}),
        'host line': dataclasses.replace(good_kb, stderr='natemu: Power(1)\n', returncode=0),
        'timeout': dataclasses.replace(good_kb, timeout=True),
        'second release at the disconnect': rswap(good_kb, kb(KEY_SENTINEL, 1), kb(KEY_LEFTSHIFT, 0) + kb(KEY_SENTINEL, 1)),
        'no sentinel': dataclasses.replace(good_kb, serial=good_kb.serial[:good_kb.serial.rindex(kb(KEY_SENTINEL, 1))]),
        'sentinel failed': ctl(good_kb, sentinel={'code': -32002, 'data': None}),
        'reader fell behind': swap(good_kb, kb(48, 0), ev_line('keyboard', EV_SYN, SYN_DROPPED, 0) + '\n' + kb(48, 0)),
        # control-mouse
        'extra mouse packet': rswap(good_mouse, ev_line('mouse', EV_KEY, BTN_LEFT, 1) + '\n' + ev_line('mouse', EV_KEY, BTN_LEFT, 0) + '\n',
                                   ev_line('mouse', EV_REL, REL_X, 1) + '\n' + ev_line('mouse', EV_KEY, BTN_LEFT, 1) + '\n'
                                   + ev_line('mouse', EV_KEY, BTN_LEFT, 0) + '\n'),
        'dy not negated': swap(good_mouse, ev_line('mouse', EV_REL, REL_Y, 40), ev_line('mouse', EV_REL, REL_Y, -40)),
        'one packet of 255': dataclasses.replace(good_mouse, serial=good_mouse.serial.replace(ev_line('mouse', EV_REL, REL_X, 255) + '\n', '')
                                                 .replace(ev_line('mouse', EV_REL, REL_X, 235), ev_line('mouse', EV_REL, REL_X, 255))),
        'overflowed': swap(good_mouse, ev_line('mouse', EV_REL, REL_X, 235), ev_line('mouse', EV_REL, REL_X, -21)),
        'wheel sign': swap(good_mouse, ev_line('mouse', EV_REL, REL_WHEEL, -3), ev_line('mouse', EV_REL, REL_WHEEL, 3)),
        'right as middle': swap(good_mouse, ev_line('mouse', EV_KEY, BTN_RIGHT, 1), ev_line('mouse', EV_KEY, BTN_MIDDLE, 1)),
        'button stuck': swap(good_mouse, ev_line('mouse', EV_KEY, BTN_LEFT, 0) + '\n', ''),
        'no wheel': swap(good_mouse, ev_line('mouse', EV_REL, REL_WHEEL, 3) + '\n', ''),
        'step failed': ctl(good_mouse, steps=[{}] * (len(MOUSE_STEPS) - 1) + [{'code': -32602, 'data': 'x'}]),
        # control-power-button
        'no KEY_POWER': dataclasses.replace(good_power, serial=re.sub(r'^CONTROL-EV power .* 1 116 [01]\n', '', good_power.serial, flags=re.M)),
        'other device': swap(good_power, f'P: Phys={POWER_PHYS}', 'P: Phys=PNP0C0C/button/input0'),
        'two buttons': swap(good_power, 'CONTROL-INPUT \n', f'CONTROL-INPUT \nCONTROL-INPUT N: Name="{POWER_NAME}"\nCONTROL-INPUT P: Phys=PNP0C0C/button/input0\n'),
        'no power-off': swap(good_power, 'CONTROL-POWEROFF\n', ''),
        'power stopped cancelled': dataclasses.replace(good_power, stderr=host, returncode=2, control={'power_button': {}, 'stopped': 'cancelled'}),
        'power_button absent': ctl(good_power, power_button={'code': -32001, 'data': {'device': 'power_button'}}),
        'power status': dataclasses.replace(good_power, returncode=1),
        # control-status
        'ps2 on': ctl(good_status, status=dict(good_status.control['status'], devices={'ps2': True})),
        'stopping': ctl(good_status, status=dict(good_status.control['status'], state='stopping')),
        'no uptime': ctl(good_status, status={k: v for k, v in good_status.control['status'].items() if k != 'uptime_ms'}),
        'input accepted': ctl(good_status, input={}),
        'other device named': ctl(good_status, empty={'code': -32001, 'data': {'device': 'power_button'}}),
        'other code': ctl(good_status, input={'code': -32602, 'data': None}),
        'status vcpus': dataclasses.replace(good_status, vcpus=2, serial=good_status.serial.replace('cpus=1', 'cpus=2')),
        'status exit 0': dataclasses.replace(good_status, returncode=0),
    }
    for what, r in bad.items(): assert CASES[r.case].judge(r), f'{what}: passed'
    expect = {'one key wrong (KEY_A as KEY_Q)': 'KeyA gave 16 not KEY_A (30)', 'dy not negated': "(2, 1, -40)",
              'no release at the disconnect': 'at the disconnect', 'reader fell behind': 'SYN_DROPPED', 'second release at the disconnect': 'after the table',
              'extra mouse packet': '13 events, expected 12', 'two buttons': 'expected one on', 'driver error': 'driver:'}
    for what, words in expect.items():
        assert any(words in e for e in CASES[bad[what].case].judge(bad[what])), (what, CASES[bad[what].case].judge(bad[what]))
    return len(bad)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('kernel-deb', 'kernel', 'busybox-deb', 'out'): prep.add_argument('--' + name, type=Path, required=True)
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
