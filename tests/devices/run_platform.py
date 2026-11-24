#!/usr/bin/env python3
"""The devices family's runner: stock Linux drivers against natemu's platform devices, through the `natemu` CLI.

prepare builds one small initramfs for every case: Debian's unchanged BusyBox and stock modules from the Debian kernel
package (MODULES, with every module they depend on), and an init that runs the case the kernel command line names
(`platform_case=NAME`). run boots one VM per case (120 s each) and judges it from the guest's serial markers and the
host's exit line; it never builds or fetches anything. self-check runs the judges on synthetic logs and the init
through `sh -n`, without a VM.

A case is three things that share its name: an entry in CASES (its extra kernel command line words and its judge), a
`case_<name>` shell function in INIT, and its line in INIT's dispatch at the end. The init's helpers: `fail REASON`
(prints PLATFORM-FAIL, the judge reports it, then powers off), `pass` (PLATFORM-PASS, then an S5 power-off: natemu exits
with `natemu: Power(1)`), `word W` (is W a word of the kernel command line). A module the case loads goes into MODULES.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import dataclasses
import fcntl
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from typing import Callable

from io_common import ROOT, VM_LOCK, clone, digest, stop_group, write_json

KVER = '6.12.107+deb13-amd64'
KERNEL_SHA = '2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08'        # the netboot bzImage (debian-netboot-linux)
KERNEL_DEB_SHA = '7794643cf4560de2a7e3b069e8ca8511ad2a568d652ea285f0fe051f79bc9e99'    # its package (debian-kernel-deb)
# Stock modules the cases load (module names; files are found by name in the kernel package, `-` and `_` alike), each
# with the modules its `depends=` names, transitively. pvpanic_mmio needs pvpanic; psmouse (the PS/2 task) needs none
# (serio, libps2 and i8042 are built in: CONFIG_SERIO_I8042=y, CONFIG_SERIO_LIBPS2=y).
MODULES = ('pvpanic_mmio', 'psmouse')
APPLETS = ('sh', 'mount', 'nproc', 'uname', 'modprobe', 'cat', 'grep', 'ls', 'basename', 'readlink', 'dmesg', 'sync',
           'poweroff', 'reboot', 'sleep', 'stty')
VM_SECONDS = 120
CMDLINE = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr'
PVPANIC_CAPABILITY = '/sys/bus/platform/devices/QEMU0001:00/capability'
# platform: the SMBIOS identity natemu's tables give (crates/host/src/boot/smbios.rs) and the structure types they hold,
# each once but type 4 (one per vCPU); with the runner's 2 GiB of RAM one type 17 and one type 19.
DMI_IDS = {'sys_vendor': 'natemu', 'product_name': 'natemu x86-64', 'bios_vendor': 'natemu'}
SMBIOS_TYPES = (0, 1, 3, 16, 17, 19, 32, 127)
BIOS_DATE = '11/23/2026'
# platform, PS/2: the stock i8042 driver finds the 8042 through ACPI (PNP0303 and PNP0F13) and the built-in atkbd binds
# the keyboard on serio0 (translated by the controller); the psmouse module binds the mouse on serio1.
PS2_KEYBOARD = ('AT Translated Set 2 keyboard', 'isa0060/serio0/input0')
PS2_MOUSE_PHYS = 'isa0060/serio1/input0'
PS2_PNP = re.compile(r'i8042: PNP: PS/2 Controller \[.*\] at 0x60,0x64 irq 1,12$')
PS2_FAILURES = re.compile(r"i8042: Can't read CTR|psmouse.*failed", re.I)
# platform-off: ACPI device ids that are there only with a platform device on (pvpanic; the 8042's keyboard and mouse),
# and one that is always there (the ACPI namespace root), so an empty or missing listing cannot pass.
DEVICE_HIDS = ('QEMU0001', 'PNP0303', 'PNP0F13')
ACPI_ROOT = 'LNXSYSTM:00'

INIT = r'''#!/bin/sh
export PATH=/bin
# poweroff -f does not wait for queued console output; BusyBox stty restores attributes with TCSADRAIN.
drain_console() { console_mode=$(stty -F /dev/console -g) && stty -F /dev/console "$console_mode"; }
fail() { echo "PLATFORM-FAIL $*"; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
pass() { echo PLATFORM-PASS; sync; drain_console || fail console-drain; poweroff -f; fail poweroff-returned; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
exec </dev/console >/dev/console 2>&1
cmdline=" $(cat /proc/cmdline) "
word() { case "$cmdline" in *" $1 "*) return 0;; esac; return 1; }
name=
for item in $cmdline; do case "$item" in platform_case=*) name=${item#*=};; esac; done
echo "PLATFORM-BEGIN case=$name cpus=$(nproc) kernel=$(uname -r)"
test "$(uname -r)" = '@KVER@' || fail kernel

# pci-multibar (debug.pci_testdev): the PCI test function's two functions as the stock kernel leaves them with no driver
# bound, reported for the runner to judge: the PCI lines of dmesg; per function (found by vendor and device) the
# Command register before and after `enable` (the kernel's pci_enable_device: memory and I/O decoding), the BARs of
# `resource`, BAR0's header (test 2 selected by its byte at offset 0) through /dev/mem, BAR1's (test 1 of BAR1, test 4)
# through /dev/port, and on function 1 BAR2's dwords, each reading its own offset.
pci_command() { busybox hexdump -s 4 -n 2 -e '1/2 "%04x"' "$1/config"; }
case_pci_multibar() {
  for applet in devmem hexdump dd printf; do
    busybox --list | grep -x "$applet" >/dev/null || fail "BusyBox has no $applet applet"
  done
  dmesg | while read -r line; do
    case "$line" in *'root bus resource'*|*"can't claim"*|*'no space'*|*'failed to assign'*) echo "PLATFORM-PCI-DMESG $line";; esac
  done
  for dev in /sys/bus/pci/devices/*; do
    test "$(cat "$dev/vendor") $(cat "$dev/device")" = '0x1b36 0x0005' || continue
    fn=$(basename "$dev")
    echo "PLATFORM-PCI-FUNCTION $fn"
    echo "PLATFORM-PCI-COMMAND $fn before $(pci_command "$dev")"
    echo 1 > "$dev/enable" || fail "$fn: enable"
    echo "PLATFORM-PCI-COMMAND $fn after $(pci_command "$dev")"
    bar=0
    while test $bar -lt 6 && read -r start end flags; do
      echo "PLATFORM-PCI-RESOURCE $fn $bar $start $end $flags"
      eval "bar$bar=$start"
      bar=$((bar + 1))
    done < "$dev/resource"
    busybox devmem $((bar0)) 8 2 || fail "$fn: devmem write to BAR0"
    echo "PLATFORM-PCI-BAR0 $fn $(busybox devmem $((bar0)) 32) $(busybox devmem $((bar0 + 4)) 32)"
    busybox printf '\001' | busybox dd of=/dev/port bs=1 seek=$((bar1)) count=1 conv=notrunc 2>/dev/null || fail "$fn: write to BAR1"
    echo "PLATFORM-PCI-BAR1 $fn $(busybox dd if=/dev/port bs=1 skip=$((bar1)) count=8 2>/dev/null | busybox hexdump -v -e '1/1 "%02x"')"
    case "$fn" in *.1) echo "PLATFORM-PCI-BAR2 $fn $(busybox devmem $((bar2)) 32) $(busybox devmem $((bar2 + 0x10)) 32)" \
                            "$(busybox devmem $((bar2 + 0xfffc)) 32) $(busybox devmem $((bar2 + 8)) 64)";; esac
  done
  pass
}

# pvpanic-panic: the stock driver finds the ACPI device and its capability (PANICKED and CRASH_LOADED), then a panic
# (panic=0: the kernel does not reboot) must end the run through the device. platform_skip_capability (the runner's
# --skip-capability) goes straight to the panic.
case_pvpanic_panic() {
  modprobe pvpanic_mmio || fail modprobe-pvpanic_mmio
  if ! word platform_skip_capability; then
    test -e @CAPABILITY@ || fail "pvpanic capability file @CAPABILITY@ missing (no QEMU0001 device bound)"
    echo "PLATFORM-PVPANIC capability=$(cat @CAPABILITY@)"
  fi
  echo PLATFORM-TRIGGER sysrq-c
  drain_console || fail console-drain
  echo c > /proc/sysrq-trigger
  fail sysrq-trigger-returned
}

# platform: the kernel's DMI scan of F0000H-FFFFFH found natemu's SMBIOS 3.0 entry point (its dmesg lines), and the
# DMI identity and the structure table's entries in sysfs (CONFIG_DMIID=y, CONFIG_DMI_SYSFS=y: built in). Then PS/2:
# psmouse loaded, the input devices (serio binds asynchronously: up to 20 s for the mouse on serio1) and the dmesg
# lines of i8042, atkbd, psmouse and serio.
case_platform() {
  dmesg | grep -e 'SMBIOS ' -e 'DMI: ' | while read -r line; do echo "PLATFORM-DMESG $line"; done
  for id in sys_vendor product_name bios_vendor; do
    test -r /sys/class/dmi/id/$id || fail "/sys/class/dmi/id/$id missing (no DMI)"
    echo "PLATFORM-DMI $id=$(cat /sys/class/dmi/id/$id)"
  done
  test -d /sys/firmware/dmi/entries || fail "/sys/firmware/dmi/entries missing (no DMI)"
  echo PLATFORM-ENTRIES $(ls /sys/firmware/dmi/entries)
  modprobe psmouse || fail modprobe-psmouse
  n=0
  until grep -q '@MOUSE_PHYS@' /proc/bus/input/devices || test $n -ge 20; do sleep 1; n=$((n + 1)); done
  # Through cat: BusyBox `read` polls before each read, and /proc/bus/input/devices implements poll, so it reports readable
  # only when the device list changed since the last read; after the first line the loop blocks (guest behaviour, not a natemu stall).
  cat /proc/bus/input/devices | while read -r line; do echo "PLATFORM-INPUT $line"; done
  dmesg | grep -e i8042 -e atkbd -e psmouse -e serio | while read -r line; do echo "PLATFORM-PS2 $line"; done
  pass
}

# platform-off: the three devices off (--set machine.pvpanic=false, machine.smbios=false, machine.ps2=false). No SMBIOS:
# the kernel's DMI scan finds nothing (its dmesg lines; no /sys/firmware/dmi/entries); no 8042: no serio port; no
# pvpanic and no PS/2 device among the ACPI devices. Then `reboot -f` (reboot=k: the kernel writes FEH to port 64H, the
# PM device's reset-only register) must end the run with Power(2).
case_platform_off() {
  dmesg | grep -e 'SMBIOS' -e 'DMI' | while read -r line; do echo "PLATFORM-DMESG $line"; done
  if test -e /sys/firmware/dmi/entries; then echo PLATFORM-DMI-ENTRIES present; else echo PLATFORM-DMI-ENTRIES absent; fi
  test -d /sys/bus/serio/devices || fail "/sys/bus/serio/devices missing (no serio bus)"
  echo PLATFORM-SERIO $(ls /sys/bus/serio/devices)
  test -d /sys/bus/acpi/devices || fail "/sys/bus/acpi/devices missing (no ACPI)"
  echo PLATFORM-ACPI $(ls /sys/bus/acpi/devices)
  echo PLATFORM-TRIGGER reboot
  drain_console || fail console-drain
  reboot -f
  fail reboot-returned
}

case "$name" in
  pci-multibar) case_pci_multibar;;
  pvpanic-panic) case_pvpanic_panic;;
  platform) case_platform;;
  platform-off) case_platform_off;;
  *) fail "unknown case '$name'";;
esac
fail "case $name returned"
'''.replace('@KVER@', KVER).replace('@CAPABILITY@', PVPANIC_CAPABILITY).replace('@MOUSE_PHYS@', PS2_MOUSE_PHYS)


@dataclasses.dataclass(frozen=True)
class Run:
    """What one VM left for its judge."""
    case: str
    vcpus: int
    serial: str                     # COM1, with \r removed
    stderr: str
    returncode: int | None
    timeout: bool                   # killed at VM_SECONDS
    after_trigger: float | None     # seconds from PLATFORM-TRIGGER appearing on the serial file to the host's exit
    options: frozenset = frozenset()   # runner options that change what the case checks ('skip-capability')


def common(run: Run) -> list:
    """What every case requires: the init started this case on the pinned kernel, it reported no failure, no timeout."""
    errors = []
    begin = re.findall(r'^PLATFORM-BEGIN case=(\S*) cpus=(\d+) kernel=(\S+)$', run.serial, re.M)
    if begin != [(run.case, str(run.vcpus), KVER)]:
        errors.append(f'the init did not start case {run.case} at {run.vcpus} vCPUs on {KVER} (PLATFORM-BEGIN: {begin})')
    errors += [f'guest: {why}' for why in re.findall(r'^PLATFORM-FAIL (.*)$', run.serial, re.M)]
    if run.timeout: errors.append(f'timed out: no stop within {VM_SECONDS} s')
    return errors


def stopped(run: Run, line: str, status: int) -> list:
    """The host's last line on standard error is `line` and it exited with `status`."""
    lines = [l for l in run.stderr.splitlines() if l.strip()]
    last = lines[-1] if lines else ''
    errors = [] if last == line else [f'host ended with {last!r}, expected {line!r}']
    if run.returncode != status: errors.append(f'exit status {run.returncode}, expected {status}')
    return errors


# pci-multibar: what natemu's launch places for the test function (crates/host/src/pci/place.rs, testdev.rs) at the first
# free slot S, as each function's `resource` lines (BAR: base, length, the kernel's IORESOURCE_ type bits):
# function 0's BAR0 keeps the slot's 64 KiB stride address; the rest of memory goes from E0200000H largest first
# (function 1's 64 KiB BAR2-3, then its BAR0); I/O from C000H, lowest function first. The judged bits also take
# IORESOURCE_DISABLED and IORESOURCE_UNSET, expected clear: a BAR the kernel left unassigned or disabled fails.
PCI_APPLETS = ('devmem', 'hexdump', 'dd', 'printf')
PCI_IO, PCI_MEM, PCI_PREFETCH, PCI_MEM_64 = 0x100, 0x200, 0x2000, 0x100000
PCI_DISABLED, PCI_UNSET = 0x1000_0000, 0x2000_0000
PCI_TYPE = PCI_IO | PCI_MEM | PCI_PREFETCH | PCI_MEM_64 | PCI_DISABLED | PCI_UNSET
PCI_IO_WINDOW = 'root bus resource [io  0xc000-0xffff window]'
PCI_PLACEMENT_ERRORS = ("can't claim", 'no space', 'failed to assign')


def pci_expected(slot: int) -> dict:
    """Function name -> {BAR: (base, length, type bits)}; BARs not listed read 0 0 0."""
    at = f'0000:00:{slot:02x}'
    return {f'{at}.0': {0: (0xE000_0000 + 0x1_0000 * slot, 0x1000, PCI_MEM), 1: (0xC000, 0x100, PCI_IO)},
            f'{at}.1': {0: (0xE021_0000, 0x1000, PCI_MEM), 1: (0xC100, 0x100, PCI_IO),
                        2: (0xE020_0000, 0x1_0000, PCI_MEM | PCI_PREFETCH | PCI_MEM_64)}}


def busybox_applets(busybox: bytes) -> list:
    """The applet names in BusyBox's name table (what `busybox --list` prints): NUL-separated and sorted, from `[`."""
    start = busybox.find(b'[\0[[\0')
    assert start >= 0, 'no BusyBox applet name table'
    names = [n.decode() for n in busybox[start:busybox.index(b'\0\0', start)].split(b'\0')]
    assert names == sorted(names) and 'sh' in names, 'not a BusyBox applet name table'
    return names


def hexes(text: str | None) -> list | None:
    """The hexadecimal numbers of `text` (with or without 0x), or None if it has a word that is not one."""
    try: return [int(v, 16) for v in (text or '').split()]
    except ValueError: return None


def judge_pci_multibar(run: Run) -> list:
    errors = common(run)
    marks = lambda tag: re.findall(rf'^PLATFORM-PCI-{tag} (.*)$', run.serial, re.M)
    dmesg = marks('DMESG')
    if not any(PCI_IO_WINDOW in line for line in dmesg): errors.append(f'dmesg has no "{PCI_IO_WINDOW}"')
    errors += [f'dmesg: {line}' for line in dmesg if any(e in line for e in PCI_PLACEMENT_ERRORS)]
    functions = marks('FUNCTION')
    slot = re.fullmatch(r'0000:00:([0-9a-f]{2})\.0', functions[0]) if functions else None
    expected = pci_expected(int(slot.group(1), 16)) if slot else {}
    if not expected or functions != list(expected):
        return errors + [f'test functions {functions}, expected functions 0 and 1 of one slot (vendor 1b36, device 0005)']
    command = {m.split()[0]: hexes(m.split()[2]) for m in marks('COMMAND') if m.split()[1:2] == ['after']}
    resource = {}
    for line in marks('RESOURCE'):
        fn, bar, *values = line.split()
        resource.setdefault(fn, {})[bar] = tuple(hexes(' '.join(values)) or ())
    reads = {tag: dict(m.split(None, 1) for m in marks(tag)) for tag in ('BAR0', 'BAR1', 'BAR2')}
    for fn, bars in expected.items():
        if (command.get(fn) or [0])[0] & 3 != 3: errors.append(f'{fn}: Command after enable {command.get(fn)}, expected I/O and memory decoding (bits 0-1)')
        for bar in range(6):
            base, length, kind = bars.get(bar, (0, 0, 0))
            want = (base, base + length - 1, kind) if length else (0, 0, 0)
            got = resource.get(fn, {}).get(str(bar), ())
            if len(got) != 3 or (got[0], got[1], got[2] & PCI_TYPE) != want:
                errors.append(f'{fn}: BAR{bar} resource {tuple(map(hex, got))}, expected {tuple(map(hex, want))} (base, end, type)')
        # Test 2 (mmio-datamatch-eventfd) selected: index 2, width 1; its write at 802H.
        if hexes(reads['BAR0'].get(fn)) != [0x102, 0x802]: errors.append(f'{fn}: BAR0 header {reads["BAR0"].get(fn)!r}, expected 0x00000102 0x00000802')
        # Test 4 (portio-wildcard-eventfd) selected: index 4, width 1, zero; its write at 84H.
        if reads['BAR1'].get(fn) != '0401000084000000': errors.append(f'{fn}: BAR1 header {reads["BAR1"].get(fn)!r}, expected 0401000084000000')
    f1 = list(expected)[1]
    if hexes(reads['BAR2'].get(f1)) != [0, 0x10, 0xfffc, 0xc_0000_0008]:
        errors.append(f'{f1}: BAR2 reads {reads["BAR2"].get(f1)!r}, expected its offsets 0 10 FFFC and (64 bits at 8) C00000008')
    if 'PLATFORM-PASS' not in run.serial: errors.append('the init did not finish (no PLATFORM-PASS)')
    return errors + stopped(run, 'natemu: Power(1)', 0)


def self_check_pci_multibar():
    """pci-multibar's judge on a synthetic passing log and its negative controls, among them the three natemu breaks
    the case was shown to fail against (no multi-function bit, no I/O window, no WordIO in the root bridge's _CRS)."""
    e = pci_expected(1)
    lines = [f'PLATFORM-BEGIN case=pci-multibar cpus=2 kernel={KVER}',
             f'PLATFORM-PCI-DMESG pci_bus 0000:00: {PCI_IO_WINDOW}',
             'PLATFORM-PCI-DMESG pci_bus 0000:00: root bus resource [mem 0xe0000000-0xefffffff window]']
    for fn, bars in e.items():
        lines += [f'PLATFORM-PCI-FUNCTION {fn}', f'PLATFORM-PCI-COMMAND {fn} before 0000', f'PLATFORM-PCI-COMMAND {fn} after 0003']
        for bar in range(6):
            base, length, kind = bars.get(bar, (0, 0, 0))
            values = (base, base + length - 1, kind | 0x40000) if length else (0, 0, 0)
            lines.append(f'PLATFORM-PCI-RESOURCE {fn} {bar} ' + ' '.join(f'0x{v:016x}' for v in values))
        lines += [f'PLATFORM-PCI-BAR0 {fn} 0x00000102 0x00000802', f'PLATFORM-PCI-BAR1 {fn} 0401000084000000']
    lines += ['PLATFORM-PCI-BAR2 0000:00:01.1 0x00000000 0x00000010 0x0000FFFC 0x0000000C00000008', 'PLATFORM-PASS']
    good = Run('pci-multibar', 2, '\n'.join(lines) + '\n', 'natemu: Power(1)\n', 0, False, None)
    judge = CASES['pci-multibar'].judge
    assert not judge(good), judge(good)
    def drop(*tags): return dataclasses.replace(good, serial=''.join(l + '\n' for l in lines if not any(t in l for t in tags)))
    unclaimed = ["PLATFORM-PCI-DMESG pci 0000:00:01.0: BAR 1 [io  0xc000-0xc0ff]: can't claim; no compatible bridge window",
                 "PLATFORM-PCI-DMESG pci 0000:00:01.0: BAR 1 [io  size 0x0100]: can't assign; no space"]
    without = [l for l in lines if PCI_IO_WINDOW not in l]
    def swap(old, new): return dataclasses.replace(good, serial=good.serial.replace(old, new))
    # The three breaks as the guest showed them: function 1 absent; BAR1 reading FFH; no I/O window, and BAR1 neither
    # claimed nor assigned (its resource line unchanged).
    bad = {'no multi-function bit': drop('0000:00:01.1'),
           'no I/O window': swap('0000:00:01.0 0401000084000000', '0000:00:01.0 ffffffffffffffff'),
           'no WordIO': dataclasses.replace(good, serial='\n'.join(without[:2] + unclaimed + without[2:]) + '\n'),
           'no WordIO, quiet': drop(PCI_IO_WINDOW),
           'failed to assign': swap('[mem 0xe0000000-0xefffffff window]', '[mem 0xe0000000-0xefffffff window]\n'
                                    'PLATFORM-PCI-DMESG pci 0000:00:01.0: BAR 1: failed to assign [io  size 0x0100]'),
           'placement error': swap('[mem 0xe0000000-0xefffffff window]', "[mem 0xe0000000-0xefffffff window]\n"
                                   "PLATFORM-PCI-DMESG pci 0000:00:01.1: BAR 2: can't claim"),
           'other slot base': swap('0x00000000e0010000', '0x00000000e0020000'),
           'I/O as memory': swap('0x000000000000c0ff 0x0000000000040100', '0x000000000000c0ff 0x0000000000040200'),
           'not 64-bit': swap('0x00000000e020ffff 0x0000000000142200', '0x00000000e020ffff 0x0000000000042200'),
           'BAR unset': swap('0x00000000e020ffff 0x0000000000142200', '0x00000000e020ffff 0x0000000020142200'),
           'BAR disabled': swap('0x000000000000c0ff 0x0000000000040100', '0x000000000000c0ff 0x0000000010040100'),
           'extra BAR': swap('0000:00:01.0 5 0x0000000000000000', '0000:00:01.0 5 0x0000000000001000'),
           'no decoding': swap('0000:00:01.1 after 0003', '0000:00:01.1 after 0002'),
           'BAR0 zero': swap('0000:00:01.0 0x00000102 0x00000802', '0000:00:01.0 0x00000000 0x00000000'),
           'BAR2 offsets': swap('0x0000FFFC', '0x0000FFF8'),
           'no BAR2': drop('PLATFORM-PCI-BAR2'),
           'one function only, as 01.1': drop('0000:00:01.0'),
           'guest failure': swap('PLATFORM-PASS', 'PLATFORM-FAIL BusyBox has no devmem applet'),
           'no pass': drop('PLATFORM-PASS'),
           'cpus': dataclasses.replace(good, vcpus=1),
           'panic': dataclasses.replace(good, stderr='natemu: Power(4)\n', returncode=2),
           'timeout': dataclasses.replace(good, timeout=True, stderr='natemu: Cancelled\n', returncode=1)}
    for what, r in bad.items(): assert judge(r), f'pci-multibar {what}: passed'
    assert any('BAR1 header' in x for x in judge(bad['no I/O window'])), judge(bad['no I/O window'])
    assert any('root bus resource' in x for x in judge(bad['no WordIO'])), judge(bad['no WordIO'])
    assert sum("can't" in x for x in judge(bad['no WordIO'])) == 2, judge(bad['no WordIO'])
    assert any('test functions' in x for x in judge(bad['no multi-function bit'])), judge(bad['no multi-function bit'])
    assert any('01.1: BAR2 resource' in x for x in judge(bad['BAR unset'])), judge(bad['BAR unset'])
    assert any('01.0: BAR1 resource' in x for x in judge(bad['BAR disabled'])), judge(bad['BAR disabled'])
    assert busybox_applets(b'x\0[\0[[\0dd\0devmem\0sh\0\0') == ['[', '[[', 'dd', 'devmem', 'sh']
    print(f'pci-multibar: {len(bad)} negative judge controls passed.')


def judge_pvpanic_panic(run: Run) -> list:
    errors = common(run)
    if 'skip-capability' not in run.options:
        caps = re.findall(r'^PLATFORM-PVPANIC capability=(.*)$', run.serial, re.M)
        if caps != ['3']: errors.append(f'pvpanic capability {caps or "not reported"}, expected 3 (PANICKED and CRASH_LOADED)')
    errors += stopped(run, 'natemu: Power(4)', 2)
    if 'Kernel panic' not in run.serial: errors.append('no "Kernel panic" on the serial console')
    if run.after_trigger is None: errors.append('the guest never reached the trigger (PLATFORM-TRIGGER)')
    elif run.after_trigger > 30 and not run.timeout: errors.append(f'stopped {run.after_trigger:.1f} s after the trigger, limit 30 s')
    return errors


def judge_platform(run: Run) -> list:
    errors = common(run)
    dmesg = [re.sub(r'^\[[^]]*\] ?', '', l) for l in re.findall(r'^PLATFORM-DMESG (.*)$', run.serial, re.M)]
    if 'SMBIOS 3.0.0 present.' not in dmesg:
        errors.append(f'no "SMBIOS 3.0.0 present." in dmesg (its SMBIOS lines: {[l for l in dmesg if "SMBIOS" in l]})')
    ids = [l for l in dmesg if l.startswith('DMI: ') and not l.startswith('DMI: Memory')]
    if len(ids) != 1 or not (ids[0].startswith('DMI: natemu natemu x86-64, BIOS natemu ') and ids[0].endswith(' ' + BIOS_DATE)):
        errors.append(f'DMI identity line {ids}, expected one "DMI: natemu natemu x86-64, BIOS natemu VERSION {BIOS_DATE}"')
    got = dict(re.findall(r'^PLATFORM-DMI (\w+)=(.*)$', run.serial, re.M))
    if got != DMI_IDS: errors.append(f'/sys/class/dmi/id {got}, expected {DMI_IDS}')
    entries = re.findall(r'^PLATFORM-ENTRIES(.*)$', run.serial, re.M)
    counts = {}
    for e in (entries[0].split() if entries else []):
        counts[int(e.split('-')[0])] = counts.get(int(e.split('-')[0]), 0) + 1
    want = dict(sorted(({t: 1 for t in SMBIOS_TYPES} | {4: run.vcpus}).items()))
    if dict(sorted(counts.items())) != want:
        errors.append(f'/sys/firmware/dmi/entries by type {dict(sorted(counts.items())) if entries else "not listed"}, expected {want}')
    errors += judge_ps2(run)
    errors += stopped(run, 'natemu: Power(1)', 0)
    return errors


def input_devices(serial: str) -> list:
    """/proc/bus/input/devices from the PLATFORM-INPUT lines: (name, phys) of each device."""
    devices, name, phys = [], None, None
    for line in [*re.findall(r'^PLATFORM-INPUT ?(.*)$', serial, re.M), '']:
        if not line.strip():
            if name is not None or phys is not None: devices.append((name, phys))
            name = phys = None
        elif m := re.fullmatch(r'N: Name="(.*)"', line.strip()): name = m.group(1)
        elif m := re.fullmatch(r'P: Phys=(.*)', line.strip()): phys = m.group(1)
    return devices


def judge_ps2(run: Run) -> list:
    """platform's PS/2 part: the keyboard on serio0, a PS/2 mouse on serio1, the controller found through ACPI, and
    no i8042 or psmouse failure in dmesg."""
    errors = []
    devices = input_devices(run.serial)
    if devices.count(PS2_KEYBOARD) != 1:
        errors.append(f'input devices {devices}: expected one {PS2_KEYBOARD[0]!r} on {PS2_KEYBOARD[1]}')
    mice = [n for n, p in devices if p == PS2_MOUSE_PHYS]
    if len(mice) != 1 or not re.search(r'PS/2 .*Mouse', mice[0] or ''):
        errors.append(f'input devices on {PS2_MOUSE_PHYS}: {mice}, expected one PS/2 mouse (psmouse probe failed?)')
    dmesg = [re.sub(r'^\[[^]]*\] ?', '', l) for l in re.findall(r'^PLATFORM-PS2 (.*)$', run.serial, re.M)]
    if not any(PS2_PNP.search(l) for l in dmesg):
        errors.append(f'no "i8042: PNP: PS/2 Controller [...] at 0x60,0x64 irq 1,12" in dmesg (its i8042 lines: '
                      f'{[l for l in dmesg if "i8042" in l]})')
    errors += [f'dmesg: {l}' for l in dmesg if PS2_FAILURES.search(l)]
    return errors


def judge_platform_off(run: Run) -> list:
    """The devices off: no SMBIOS (no `DMI:` line, the kernel's `DMI not present or invalid.`, no
    /sys/firmware/dmi/entries), no serio port, none of DEVICE_HIDS among the ACPI devices; `reboot -f` ends with Power(2)."""
    errors = common(run)
    dmesg = [re.sub(r'^\[[^]]*\] ?', '', l) for l in re.findall(r'^PLATFORM-DMESG (.*)$', run.serial, re.M)]
    if found := [l for l in dmesg if l.startswith('DMI:') or re.match(r'SMBIOS .*present', l)]:
        errors.append(f'SMBIOS found with machine.smbios=false: {found}')
    if 'DMI not present or invalid.' not in dmesg:
        errors.append(f'no "DMI not present or invalid." in dmesg (its DMI and SMBIOS lines: {dmesg})')
    if (entries := re.findall(r'^PLATFORM-DMI-ENTRIES (\w+)$', run.serial, re.M)) != ['absent']:
        errors.append(f'/sys/firmware/dmi/entries {entries or "not reported"}, expected absent')
    serio = re.findall(r'^PLATFORM-SERIO(.*)$', run.serial, re.M)
    if [s.split() for s in serio] != [[]]:
        errors.append(f'serio devices {serio or "not listed"}, expected none (machine.ps2=false)')
    acpi = [s.split() for s in re.findall(r'^PLATFORM-ACPI(.*)$', run.serial, re.M)]
    if len(acpi) != 1 or ACPI_ROOT not in acpi[0]:
        errors.append(f'ACPI devices {acpi or "not listed"}: expected one listing with {ACPI_ROOT}')
    elif on := [d for d in acpi[0] if d.split(':')[0] in DEVICE_HIDS]:
        errors.append(f'ACPI devices {on} with the platform devices off')
    if run.after_trigger is None: errors.append('the guest never reached the reboot (PLATFORM-TRIGGER)')
    errors += stopped(run, 'natemu: Power(2)', 2)
    return errors


@dataclasses.dataclass(frozen=True)
class Case:
    words: tuple                    # kernel command line words beyond CMDLINE and platform_case=NAME
    judge: Callable[[Run], list]


CASES = {
    'pci-multibar': Case((), judge_pci_multibar),
    'pvpanic-panic': Case(('panic=0',), judge_pvpanic_panic),
    'platform': Case((), judge_platform),
    'platform-off': Case(('reboot=k',), judge_platform_off),
}
TRIGGER = re.compile(r'^PLATFORM-TRIGGER ', re.M)


def busybox_from(deb: Path) -> bytes:
    archive = subprocess.check_output(['ar', 'p', str(deb), 'data.tar.xz'], timeout=10)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        members = [m for m in package if m.name.removeprefix('./') in ('usr/bin/busybox', 'bin/busybox') and m.isfile()]
        assert len(members) == 1, 'one unchanged BusyBox executable required'
        busybox = package.extractfile(members[0]).read()
    assert busybox[:4] == b'\x7fELF' and int.from_bytes(busybox[18:20], 'little') == 62, 'not an x86-64 BusyBox'
    return busybox


def module_closure(deb: Path, names) -> dict:
    """Each module `names` lists and every module they depend on, from the kernel package: name -> (path under
    lib/modules/KVER, uncompressed .ko, its direct dependencies)."""
    archive = lzma.decompress(subprocess.check_output(['ar', 'p', str(deb), 'data.tar.xz'], timeout=60))   # plain tar: cheap to seek
    prefix = f'usr/lib/modules/{KVER}/'
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as package:
        index = {}
        for m in package.getmembers():
            rel = m.name.removeprefix('./')
            if m.isfile() and rel.startswith(prefix) and rel.endswith('.ko.xz'):
                index[Path(rel).name.removesuffix('.ko.xz').replace('-', '_')] = (rel.removeprefix(prefix).removesuffix('.xz'), m)
        out, todo = {}, [n.replace('-', '_') for n in names]
        while todo:
            name = todo.pop(0)
            if name in out: continue
            assert name in index, f'{name}: no such module in the kernel package (built in, or not built?)'
            rel, member = index[name]
            data = lzma.decompress(package.extractfile(member).read())
            assert data[:4] == b'\x7fELF' and ('vermagic=' + KVER).encode() in data, f'{name}: not a {KVER} module'
            found = re.search(rb'(?:^|\0)depends=([^\0]*)', data)
            assert found, f'{name}: no depends= in its modinfo'
            deps = [d.replace('-', '_') for d in found.group(1).decode().split(',') if d]
            out[name] = (rel, data, deps)
            todo += deps
    return out


def modules_dep(modules: dict) -> str:
    """modules.dep: each module's transitive dependencies, nearest first (modprobe loads them from the end)."""
    def chain(name):
        got = []
        for d in modules[name][2]:
            for x in [d, *chain(d)]:
                if x not in got: got.append(x)
        return got
    return ''.join(f'{rel}:' + ''.join(' ' + modules[d][0] for d in chain(n)) + '\n' for n, (rel, _, _) in sorted(modules.items()))


def prepare(args):
    args.out.mkdir(parents=True, exist_ok=False)
    assert digest(args.kernel) == KERNEL_SHA, 'not the pinned Debian kernel'
    assert digest(args.kernel_deb) == KERNEL_DEB_SHA, 'not the pinned Debian kernel package'
    clone(args.kernel, args.out / 'linux')
    root = args.out / 'rootfs'
    for name in ('bin', 'dev', 'proc', 'sys', 'tmp', 'lib/modules/' + KVER):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / 'bin/busybox').write_bytes(busybox_from(args.busybox_deb))
    (root / 'bin/busybox').chmod(0o755)
    assert not set(PCI_APPLETS) - set(busybox_applets((root / 'bin/busybox').read_bytes())), f'BusyBox lacks one of {PCI_APPLETS}'
    for applet in APPLETS: (root / 'bin' / applet).symlink_to('busybox')
    modules = module_closure(args.kernel_deb, MODULES)
    for rel, data, _ in modules.values():
        dest = root / 'lib/modules' / KVER / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (root / 'lib/modules' / KVER / 'modules.dep').write_text(modules_dep(modules))
    (root / 'init').write_text(INIT)
    (root / 'init').chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mkcpio)
    mkcpio.main(str(root), str(args.out / 'platform.cpio'))
    manifest = {'purpose': 'stock Linux drivers against natemu platform devices (tests/devices)', 'kernel_version': KVER,
                'sources': {p.name: digest(p) for p in (args.kernel, args.kernel_deb, args.busybox_deb)},
                'producer': {str(p.relative_to(ROOT)): digest(p) for p in (Path(__file__).resolve(), ROOT / 'tools/play/mkcpio.py')},
                'modules': {n: {'path': rel, 'sha256': digest(root / 'lib/modules' / KVER / rel), 'depends': deps}
                            for n, (rel, _, deps) in sorted(modules.items())},
                'outputs': {str(p.relative_to(args.out)): digest(p)
                            for p in (args.out / 'linux', args.out / 'platform.cpio', root / 'init', root / 'bin/busybox')}}
    write_json(args.out / 'inputs.json', manifest)
    print(json.dumps(manifest, indent=2))


def boot(args, name: str, work: Path) -> Run:
    """One VM for case `name`: started, watched for the trigger marker, stopped at VM_SECONDS."""
    case = CASES[name]
    words = [*case.words, f'platform_case={name}', *(['platform_skip_capability'] if args.skip_capability else [])]
    command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
               '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / 'platform.cpio').resolve()),
               '--vcpus', str(args.vcpus), '--ram', '2G', '--compile-cache', 'off', '--serial', str((work / 'serial.log').resolve()),
               '--cmdline', ' '.join([CMDLINE, *words])]
    for setting in args.set: command += ['--set', setting]
    write_json(work / 'command.json', command)
    env = {k: v for k, v in os.environ.items() if k not in ('NATEMU_PERF_LOG', 'NATEMU_COMPILE_CACHE')}   # settings the CLI reads (deprecated)
    serial = work / 'serial.log'
    started, timeout, triggered = time.monotonic(), False, None
    with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
        try:
            while process.poll() is None:
                if triggered is None and serial.exists() and TRIGGER.search(serial.read_text(errors='replace').replace('\r', '')):
                    triggered = time.monotonic()
                if time.monotonic() - started >= VM_SECONDS:
                    timeout = True
                    stop_group(process)
                    break
                time.sleep(0.1)
        except BaseException:
            stop_group(process)
            raise
    ended = time.monotonic()
    text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
    if triggered is None and TRIGGER.search(text): triggered = ended      # it appeared between the last look and the exit
    return Run(name, args.vcpus, text, (work / 'stderr.log').read_text(errors='replace'), process.returncode, timeout,
               None if triggered is None else ended - triggered, frozenset(['skip-capability'] if args.skip_capability else []))


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.inputs / name) == sha, name
    write_json(args.out / 'manifest.json', {'artifacts': {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, Path(__file__))},
                                            'inputs': manifest, 'cases': args.case, 'set': args.set, 'vcpus': args.vcpus,
                                            'skip_capability': args.skip_capability, 'seconds_per_vm': VM_SECONDS})
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)          # the VM lock, shared as the other Linux runners hold it
        for name in args.case:
            work = args.out / name
            work.mkdir()
            r = boot(args, name, work)
            errors = CASES[name].judge(r)
            result = {'name': name, 'ok': not errors, 'errors': errors, 'returncode': r.returncode, 'timeout': r.timeout,
                      'after_trigger': r.after_trigger, 'options': sorted(r.options)}
            write_json(work / 'result.json', result)
            results.append(result)
            write_json(args.out / 'results.json', {'results': results})
            print(json.dumps(result), flush=True)
    return int(any(not r['ok'] for r in results))


def self_check():
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    for name in CASES:
        assert f'\n  {name}) case_{name.replace("-", "_")};;\n' in INIT, f'{name}: no dispatch line in INIT'
        assert f'\ncase_{name.replace("-", "_")}() {{\n' in INIT, f'{name}: no case function in INIT'
    begin = f'PLATFORM-BEGIN case=pvpanic-panic cpus=1 kernel={KVER}\n'
    serial = begin + 'PLATFORM-PVPANIC capability=3\nPLATFORM-TRIGGER sysrq-c\nsysrq: Trigger a crash\nKernel panic - not syncing: sysrq triggered crash\n'
    good = Run('pvpanic-panic', 1, serial, 'natemu: cpu: x\nnatemu: Power(4)\n', 2, False, 1.5)
    judge = CASES['pvpanic-panic'].judge
    assert not judge(good), judge(good)
    skipped = dataclasses.replace(good, serial=serial.replace('PLATFORM-PVPANIC capability=3\n', ''), options=frozenset(['skip-capability']))
    assert not judge(skipped), judge(skipped)
    swap = lambda old, new: dataclasses.replace(good, serial=good.serial.replace(old, new))
    bad = {'capability 0': swap('capability=3', 'capability=0'),
           'no capability': swap('PLATFORM-PVPANIC capability=3\n', ''),
           'guest failure': swap('PLATFORM-TRIGGER', f'PLATFORM-FAIL pvpanic capability file {PVPANIC_CAPABILITY} missing\nPLATFORM-TRIGGER'),
           'no panic': swap('Kernel panic', 'Kernel'),
           'other case': swap('case=pvpanic-panic', 'case=platform'),
           'other kernel': swap(KVER, '6.1.0'),
           'S5': dataclasses.replace(good, stderr='natemu: Power(1)\n', returncode=0),
           'reset': dataclasses.replace(good, stderr='natemu: Power(2)\n'),
           'not last': dataclasses.replace(good, stderr='natemu: Power(4)\nnatemu: later\n'),
           'status': dataclasses.replace(good, returncode=1),
           'timeout': dataclasses.replace(good, timeout=True, stderr='natemu: Cancelled\n', after_trigger=118.0),
           'slow': dataclasses.replace(good, after_trigger=30.5),
           'no trigger': dataclasses.replace(good, after_trigger=None)}
    for what, r in bad.items(): assert judge(r), f'{what}: passed'
    assert any('missing' in e for e in judge(bad['guest failure'])), judge(bad['guest failure'])
    assert any('timed out' in e for e in judge(bad['timeout'])), judge(bad['timeout'])
    # platform, at 2 vCPUs: a passing log, then one break of each thing it checks.
    entries = '0-0 1-0 127-0 16-0 17-0 19-0 3-0 32-0 4-0 4-1'
    serial = (f'PLATFORM-BEGIN case=platform cpus=2 kernel={KVER}\n'
              '[    0.000000] PLATFORM-DMESG-NOT-THIS SMBIOS 2.8 present.\n'
              'PLATFORM-DMESG [    0.000000] SMBIOS 3.0.0 present.\n'
              f'PLATFORM-DMESG [    0.000000] DMI: natemu natemu x86-64, BIOS natemu 0.1.0 {BIOS_DATE}\n'
              'PLATFORM-DMESG [    0.012345] DMI: Memory slots populated: 1/1\n'
              'PLATFORM-DMI sys_vendor=natemu\nPLATFORM-DMI product_name=natemu x86-64\nPLATFORM-DMI bios_vendor=natemu\n'
              f'PLATFORM-ENTRIES {entries}\n'
              'PLATFORM-INPUT I: Bus=0019 Vendor=0000 Product=0001 Version=0000\nPLATFORM-INPUT N: Name="Power Button"\n'
              'PLATFORM-INPUT P: Phys=LNXPWRBN/button/input0\nPLATFORM-INPUT \n'
              'PLATFORM-INPUT I: Bus=0011 Vendor=0001 Product=0001 Version=ab41\n'
              'PLATFORM-INPUT N: Name="AT Translated Set 2 keyboard"\nPLATFORM-INPUT P: Phys=isa0060/serio0/input0\n'
              'PLATFORM-INPUT S: Sysfs=/devices/platform/i8042/serio0/input/input1\nPLATFORM-INPUT \n'
              'PLATFORM-INPUT I: Bus=0011 Vendor=0002 Product=0005 Version=0000\n'
              'PLATFORM-INPUT N: Name="ImPS/2 Generic Wheel Mouse"\nPLATFORM-INPUT P: Phys=isa0060/serio1/input0\n'
              'PLATFORM-INPUT \n'
              'PLATFORM-PS2 [    0.512345] i8042: PNP: PS/2 Controller [PNP0303:KBD,PNP0f13:MOU] at 0x60,0x64 irq 1,12\n'
              'PLATFORM-PS2 [    0.523456] serio: i8042 KBD port at 0x60,0x64 irq 1\n'
              'PLATFORM-PS2 [    0.523457] serio: i8042 AUX port at 0x60,0x64 irq 12\n'
              'PLATFORM-PS2 [    0.601234] input: AT Translated Set 2 keyboard as /devices/platform/i8042/serio0/input/input1\n'
              'PLATFORM-PASS\n')
    good = Run('platform', 2, serial, 'natemu: Power(1)\n', 0, False, None)
    judge = CASES['platform'].judge
    assert not judge(good), judge(good)
    swap = lambda old, new: dataclasses.replace(good, serial=good.serial.replace(old, new))
    platform_bad = {'no SMBIOS line': swap('SMBIOS 3.0.0 present.', 'Booting'),
                    'SMBIOS 3.2.0': swap('SMBIOS 3.0.0', 'SMBIOS 3.2.0'),
                    'other product': swap('natemu natemu x86-64, BIOS', 'natemu natemu, BIOS'),
                    'other date': swap(f'0.1.0 {BIOS_DATE}', '0.1.0 01/01/2026'),
                    'no DMI line': swap('DMI: natemu', 'XMI: natemu'),
                    'sysfs vendor': swap('sys_vendor=natemu', 'sys_vendor=QEMU'),
                    'sysfs missing': swap('PLATFORM-DMI bios_vendor=natemu\n', ''),
                    'one type 4': swap(' 4-1', ''),
                    'no type 32': swap(' 32-0', ''),
                    'no type 127': swap('127-0 ', ''),
                    'extra type 11': swap(' 4-0', ' 11-0 4-0'),
                    'no entries': swap(f'PLATFORM-ENTRIES {entries}\n', ''),
                    'guest failure': swap('PLATFORM-PASS', 'PLATFORM-FAIL /sys/firmware/dmi/entries missing (no DMI)'),
                    'one vCPU': dataclasses.replace(good, vcpus=1),
                    'panic': dataclasses.replace(good, stderr='natemu: Power(4)\n', returncode=2),
                    'stopped': dataclasses.replace(good, stderr='natemu: UnsupportedMmio\n', returncode=2),
                    'timeout': dataclasses.replace(good, timeout=True, stderr='natemu: Cancelled\n', returncode=2),
                    'no keyboard': swap('N: Name="AT Translated Set 2 keyboard"', 'N: Name="Power Button"'),
                    'untranslated keyboard': swap('AT Translated Set 2 keyboard"', 'AT Set 2 keyboard"'),
                    'keyboard elsewhere': swap('Phys=isa0060/serio0/input0', 'Phys=isa0060/serio2/input0'),
                    'no mouse': swap('PLATFORM-INPUT P: Phys=isa0060/serio1/input0\n', ''),
                    'not a mouse': swap('ImPS/2 Generic Wheel Mouse', 'Some Touchscreen'),
                    'no input list': dataclasses.replace(good, serial=re.sub(r'^PLATFORM-INPUT.*\n', '', serial, flags=re.M)),
                    'no PNP': swap('i8042: PNP: PS/2 Controller', 'i8042: PNP: No PS/2 controller found.'),
                    'no aux irq': swap('at 0x60,0x64 irq 1,12', 'at 0x60,0x64 irq 1'),
                    "can't read CTR": swap('PLATFORM-PASS', "PLATFORM-PS2 [    0.5] i8042: Can't read CTR while initializing i8042\nPLATFORM-PASS"),
                    'psmouse failed': swap('PLATFORM-PASS', 'PLATFORM-PS2 [    1.2] psmouse serio1: Failed to enable mouse on isa0060/serio1\nPLATFORM-PASS')}
    for what, r in platform_bad.items(): assert judge(r), f'platform {what}: passed'
    assert any('PS/2 mouse' in e for e in judge(platform_bad['no mouse'])), judge(platform_bad['no mouse'])
    assert any('SMBIOS 3.0.0' in e for e in judge(platform_bad['no SMBIOS line'])), judge(platform_bad['no SMBIOS line'])
    # platform-off: a passing log, then each device left on and each other thing it checks broken.
    serial = (f'PLATFORM-BEGIN case=platform-off cpus=1 kernel={KVER}\n'
              'PLATFORM-DMESG [    0.000000] DMI not present or invalid.\n'
              'PLATFORM-DMI-ENTRIES absent\nPLATFORM-SERIO\n'
              'PLATFORM-ACPI LNXCPU:00 LNXPWRBN:00 LNXSYBUS:00 LNXSYSTM:00 PNP0103:00 PNP0A03:00 PNP0B00:00 PNP0C0F:00\n'
              'PLATFORM-TRIGGER reboot\n[    1.234567] reboot: Restarting system\n')
    good = Run('platform-off', 1, serial, 'natemu: Power(2)\n', 2, False, 0.4)
    judge = CASES['platform-off'].judge
    assert not judge(good), judge(good)
    swap = lambda old, new: dataclasses.replace(good, serial=good.serial.replace(old, new))
    smbios_on = ('PLATFORM-DMESG [    0.000000] SMBIOS 3.0.0 present.\n'
                 f'PLATFORM-DMESG [    0.000000] DMI: natemu natemu x86-64, BIOS natemu 0.1.0 {BIOS_DATE}\n')
    off_bad = {'SMBIOS on': swap('PLATFORM-DMESG [    0.000000] DMI not present or invalid.\n', smbios_on),
               'DMI line': swap('DMI not present or invalid.', f'DMI: natemu natemu x86-64, BIOS natemu 0.1.0 {BIOS_DATE}'),
               'SMBIOS line': swap('PLATFORM-DMI-ENTRIES', 'PLATFORM-DMESG [    0.0] SMBIOS 2.8 present.\nPLATFORM-DMI-ENTRIES'),
               'no DMI scan line': swap('PLATFORM-DMESG [    0.000000] DMI not present or invalid.\n', ''),
               'entries': swap('ENTRIES absent', 'ENTRIES present'),
               'no entries line': swap('PLATFORM-DMI-ENTRIES absent\n', ''),
               'PS/2 on': swap('PLATFORM-SERIO\n', 'PLATFORM-SERIO serio0 serio1\n'),
               'no serio line': swap('PLATFORM-SERIO\n', ''),
               'pvpanic on': swap('PNP0C0F:00', 'PNP0C0F:00 QEMU0001:00'),
               'keyboard device': swap('PNP0B00:00', 'PNP0303:00 PNP0B00:00'),
               'mouse device': swap('PNP0C0F:00', 'PNP0C0F:00 PNP0F13:00'),
               'no ACPI root': swap(' LNXSYSTM:00', ''),
               'no ACPI line': dataclasses.replace(good, serial=re.sub(r'^PLATFORM-ACPI.*\n', '', serial, flags=re.M)),
               'guest failure': swap('PLATFORM-TRIGGER reboot', 'PLATFORM-FAIL reboot-returned'),
               'no trigger': dataclasses.replace(good, after_trigger=None),
               'S5': dataclasses.replace(good, stderr='natemu: Power(1)\n', returncode=0),
               'panic': dataclasses.replace(good, stderr='natemu: Power(4)\n'),
               'stopped': dataclasses.replace(good, stderr='natemu: memory fetch at 0xffff0\n'),
               'status': dataclasses.replace(good, returncode=0),
               'timeout': dataclasses.replace(good, timeout=True, stderr='natemu: Cancelled\n'),
               'other case': swap('case=platform-off', 'case=platform')}
    for what, r in off_bad.items(): assert judge(r), f'platform-off {what}: passed'
    assert any('SMBIOS found' in e for e in judge(off_bad['SMBIOS on'])), judge(off_bad['SMBIOS on'])
    assert any('QEMU0001' in e for e in judge(off_bad['pvpanic on'])), judge(off_bad['pvpanic on'])
    assert any('serio' in e for e in judge(off_bad['PS/2 on'])), judge(off_bad['PS/2 on'])
    self_check_pci_multibar()
    print(f'Init shell syntax, {len(CASES)} case dispatch(es) and {len(bad) + len(platform_bad) + len(off_bad)} negative '
          'judge controls passed; no VM launched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('kernel-deb', 'kernel', 'busybox-deb', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    runner = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out'): runner.add_argument('--' + name, type=Path, required=True)
    runner.add_argument('--lock', type=Path, default=VM_LOCK, help='the VM lock file, held shared (default: the state root\'s)')
    runner.add_argument('--case', nargs='+', choices=sorted(CASES), required=True, help='one VM each, in this order')
    runner.add_argument('--set', action='append', default=[], metavar='KEY=VALUE', help="passed to the natemu CLI's --set")
    runner.add_argument('--vcpus', type=int, default=1)
    runner.add_argument('--skip-capability', action='store_true',
                        help='pvpanic-panic: panic without checking the capability file first (a failure demonstration)')
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': return prepare(args)
    if args.command == 'self-check': return self_check()
    if len(args.case) != len(set(args.case)): parser.error('duplicate cases')
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    raise SystemExit(main())
