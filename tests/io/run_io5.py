#!/usr/bin/env python3
"""IO5 combined acceptance: the stock Debian 13 cloud image (nocloud, amd64, 20260914-2601: systemd, its own Debian
6.12.107-1 kernel and initramfs, unchanged) used as an ordinary machine with disk, network and CPU busy at once.

    python3 tests/io/run_io5.py prepare --inputs DIR
    python3 tests/io/run_io5.py run --backend natemu|qemu|kvm --inputs DIR --out DIR [--fexc FEXC] [--vcpus 4] [--ram 8]
                                 [--speculation-barriers] [--mitigations-off] [--solo-build] [--i386] [--transport mmio|pci]

prepare downloads and verifies the image (Debian's SHA512SUMS), extracts its /boot kernel and initramfs, and fetches the
CPython 3.13.18 source (pinned SHA-256; signed by its release manager, checked when pinned).
run clones the image (APFS clone), grows the file to 16 GiB and boots it twice through the serial console, logging in as
root (first-boot answers come as systemd credentials on the kernel command line; the image's root is "unprovisioned"):
  use     wait for systemd to be running; stop the apt timers; point apt at snapshot.debian.org (20260915T000000Z, the
          play image's date) so both backends install the same versions; apt-get update/install (cloud-guest-utils,
          build tools, CPython's build dependencies); grow the root partition and ext4 online; CPython ./configure; then
          at the same time, until `make -jN` ends: make, a loop downloading linux-6.12.tar.xz from cdn.kernel.org (each
          SHA-256-checked against kernel.org's list), and a loop writing 8 x 256 MiB of random data, dropping caches
          and re-reading it against its sums (each loop runs at least once); then a fixed set of CPython tests, a
          SHA-256 manifest of every file under /data and /build, and poweroff.
  verify  a fresh launch on the same disk: systemd running, the manifest (the same bytes the first boot fingerprinted on
          the console) checks, the built python imports its C extensions, an HTTPS fetch, then poweroff.
Each launch passes only with: every step ok; exit status 0 and "reboot: Power down" on the console; offline (on the
Mac) e2fsck -fn clean with no journal left to recover and the superblock state "clean"; the guest's dmesg and journal error
lines (copied out with debugfs) all in EXPECTED; no natemu network guest-error lines. Every step's start and duration
are recorded (apt-update and apt-install depend on Debian's servers: they must pass, but each launch's `seconds_compared`
leaves them out and their times are not compared); times are not judged (the QEMU run is the matched comparison; compare first boots: natemu's
second boot reuses the first boot's compile cache). --mitigations-off adds mitigations=off on both backends, so both
guests do the same kernel work; the stock line leaves PTI on, which QEMU TCG's guest also chooses. The download and
generate loops run for as long as make does, so their work differs between backends; --solo-build times the build alone. The VM lock is held exclusively (a timed comparison).

run --i386 is the 32-bit toolchain build (M2 checkpoint B; bench case io5-solo32). The first boot then runs no loops and no
tests: after the 64-bit configure, `make -jN` alone; then `debootstrap --arch=i386 --variant=buildd` (same snapshot) installs a
Debian i386 userland in /i386, in which every program is 32-bit (shell, make, perl, gcc, as, ld), and that userland configures and
makes the same source (`linux32 chroot /i386`: steps i386-configure and i386-make; i386-setup is untimed, it downloads). The built
32-bit interpreter then runs CHECK and the CPython tests I386TESTS; the hash of what they print must be I386_CHECK, which a run
with --backend kvm records: the same boots on the x86 machine NATEMU_X86_HOST under KVM (the same kernel, disk and script; the
disk stays there, so its launches get the console checks only), whose step times are the build's times on real x86 (result.json
`reference_host`: that machine's load and other containers before and after each boot; `times_indicative` if it was not idle).

run --image-format qcow2 (default raw; the raw records stay comparable): natemu's qcow2 disk. INPUTS/disk.qcow2 is made once from
disk.raw (`qemu-img convert -O qcow2 -o preallocation=metadata`, then `qemu-img resize --preallocation=metadata` to 16 GiB, so
the run measures steady state, not allocation; the seconds go in result.json), cloned per run with `cp -c` and given as
--disk. The offline checks (e2fsck, debugfs) run on a temporary raw conversion deleted right after each launch, and
`qemu-img check` must find nothing. If the volume cannot hold that copy (needs about the allocated size of the overlay and its base; checked first, and printed when skipped)
the launch records `offline_skipped` and runs console_checks instead: kernel and journal error lines from the console and (verify boot) no journal recovery, but NOT e2fsck -fn, the superblock state or the files copied out. --keep-disk keeps the qcow2; `profile` steps need a raw snapshot. --image-format overlay:
a new, empty 16 GiB qcow2 overlay over INPUTS/disk.raw made per run (`qemu-img create -b -F raw`; past the base's end it
reads zeros), given as --disk with --set disk.disk0.backing=INPUTS/disk.raw; the same checks as qcow2, plus disk.raw's hash
unchanged after each launch. Needs qemu-img.

run --transport pci (natemu only; default mmio, natemu's transport without a configuration file): the disk, network and
entropy devices attach as virtio-PCI functions (`--virtio-transport pci`), as QEMU's do, so that every device access goes through
the PCI bus's BAR decoding. No bench case runs it by default (the bench tier has no opt-in case); one is the io5-solo case with
`--transport pci` added to its command line."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time

from io_common import clone, IMAGE_FORMATS, ROOT, VM_LOCK, check_image_format, overlay_create, overlay_set, digest, qcow2_input, qcow2_problems, raw_of, raw_shortfall, stop_group, write_json
from raw_root import RAW_ROOT
from run_net_linux import lan_address

IMAGE = 'debian-13-nocloud-amd64-20260914-2601'
IMAGE_URL = 'https://cloud.debian.org/images/cloud/trixie/20260914-2601'
IMAGE_SHA512 = ('97b0ebbeb72ecc7713a50821cd0769f555c77aa94a262c1c42a9604977c4f242'
                '9b6ed4b7b01d7a709feb69779cf4ad4cbf874f56b82b66ab5294193209314bcb')   # the .tar.xz, from SHA512SUMS
KVER = '6.12.107+deb13-amd64'
BZIMAGE_SHA = '2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08'      # = tools/play/build.sh's
PYTHON = 'Python-3.13.18.tar.xz'
PYTHON_SHA = 'abb0102e6d4ae027509bcb82327d9dd85515b44a3ba8ad538cfc5e9847029f68'
LINUX_SHA = 'b1a2562be56e42afb3f8489d4c2a7ac472ac23098f1ef1c1e40da601f54625eb'        # linux-6.12.tar.xz, kernel.org sha256sums.asc
SNAPSHOT = '20260915T000000Z'
ROOT_PARTUUID = '19943023-2413-48ef-ae3d-c0972ee1b031'
ROOT_OFFSET = 262144 * 512                                                           # partition 1 of the image's GPT
DISK_BYTES = 16 << 30
E2 = Path('/opt/homebrew/opt/e2fsprogs/sbin')
QEMU = '/opt/homebrew/bin/qemu-system-x86_64'
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip'  # tools/bench
FEXC = RAW_ROOT / re.search(r'^F=\.\./natemu-raw/(\S+)', (ROOT / 'docs/restructure/boot-handoff.md').read_text(), re.M)[1]
CMDLINE = (f'root=PARTUUID={ROOT_PARTUUID} ro console=ttyS0,115200 consoleblank=0 '   # the image's grub entry minus tty0/earlyprintk
           'systemd.set_credential=firstboot.locale:C.UTF-8 systemd.set_credential=firstboot.timezone:UTC '
           'systemd.set_credential=firstboot.keymap:us systemd.set_credential=passwd.plaintext-password.root:natemu')
PASSWORD = 'natemu'
PYTESTS = ('test_os test_io test_fileio test_shutil test_tempfile test_subprocess test_threading test_socket test_select '
           'test_mmap test_sqlite3 test_zlib test_lzma test_bz2 test_hashlib test_json test_math test_re test_struct')
REQUIRED = {'use': ('apt-update', 'apt-install', 'grow', 'fetch-python', 'configure', 'make', 'download', 'generate',
                    'pytest', 'manifest'),
            'verify': ('check', 'python', 'net')}
REQUIRED_I386 = ('apt-update', 'apt-install', 'grow', 'fetch-python', 'configure', 'make', 'i386-setup', 'i386-configure',
                 'i386-make', 'i386-check', 'manifest')   # --i386's first boot: no loops, no 64-bit tests
# Steps whose time is Debian's servers' as much as the machine's: they must pass, but their seconds are left out of the
# launch's compared time (`seconds_compared`) and are not to be compared between runs or backends.
UNTIMED = ('apt-update', 'apt-install', 'i386-setup')
# Kernel lines the guest may log (journal priority err, and dmesg lines matching BAD), each explained:
EXPECTED = (
    # natemu: devices are virtio-MMIO, no PCI host bridge; with 8 GiB of RAM no hole below 4 GiB is left for PCI BARs.
    r'Cannot find an available gap in the 32-bit address range',
    r'PCI devices with unassigned 32-bit BARs may not work!',
    r'PCI: Fatal: No config space access function found',
    # natemu: the CPU profile exposes no MCE/MCA.
    r'mce: Unable to init MCE device \(rc: -5\)',
    # natemu: Linux zeroes the MADT wakeup structure's mailbox address after reading it (arch/x86/kernel/acpi/
    # madt_wakeup.c, acpi_mp_disable_offlining: a Linux convention against kexec) and ACPICA's later full verification
    # then reports the table's checksum; "should be" prints stored minus correct (0x94 - 0x04 = 0x90 at 4 vCPUs).
    r'ACPI BIOS Warning \(bug\): Incorrect checksum in table \[APIC\]',
)
BAD = (r'EXT4-fs (error|warning)|I/O error|BUG:|WARNING:|Call Trace|segfault|general protection|Out of memory|oom-kill|'
       r'rcu_.*stall|blocked for more than|soft lockup|clocksource.*unstable|virtio.*(error|broken)|not a head|'
       r'ACPI (BIOS )?(Warning|Error)')
I386TESTS = 'test_struct test_long test_float test_json test_re test_hashlib test_zlib test_sqlite3'   # none that fails now and then at -j4 (regrtest's rerun changes the lines)
# --i386: the built 32-bit interpreter runs this; its output (and the result lines of I386TESTS) must hash to I386_CHECK.
# Lines starting "x87:" are not hashed: glibc's i386 log, exp, lgamma and pow are x87 code that leans on the 80-bit registers,
# which natemu's fast x87 keeps as doubles (docs/restructure/m2-fast-x87-findings.md), so their last digits can differ from real
# x86. They must still be the reference's lines (I386_X87) with each number within X87_ULPS units in the last place. Lines starting
# "note:" (test_math's failures, which follow from the same deviation) are printed and kept (OUT/i386-check.txt), not compared.
CHECK = r'''import sys, platform, struct, math, json, hashlib, zlib, re, sqlite3, decimal, fractions, threading, sysconfig
import ssl, lzma, bz2, ctypes, readline, uuid, curses, dbm.gnu
print(platform.machine(), struct.calcsize('P'), sys.maxsize, sys.byteorder, sysconfig.get_config_var('HOST_GNU_TYPE'), sys.float_info.dig)
print(ctypes.sizeof(ctypes.c_long), ctypes.sizeof(ctypes.c_longdouble), ctypes.sizeof(ctypes.c_longlong))
print(repr(math.sqrt(2) ** 2), repr(0.1 + 0.2), repr(1e308 * 10), repr(5e-324 / 2), repr(sum(i / 7 for i in range(1000))))
for f in (math.sin, math.cos, math.tan, math.atan, math.erf, math.log, math.exp, math.lgamma):
    for x in (0.5, 1e-5, 37.25): print('x87:' if f in (math.log, math.exp, math.lgamma) else 'libm', f.__name__, x, repr(f(x)))
print('x87: pow 2.0 0.5', repr(2.0 ** 0.5))
print(repr(float('1.7976931348623157e308')), 10 ** 40 // 7 ** 20, pow(3, 10 ** 5, 2 ** 61 - 1), (-7) // 2, float(2 ** 53 + 1))
print(decimal.Decimal(1) / decimal.Decimal(7), fractions.Fraction(0.1), struct.pack('<dfq', 1 / 3, 1 / 3, -2 ** 62).hex())
print(hashlib.sha256(bytes(range(256)) * 4096).hexdigest(), zlib.crc32(b'natemu' * 10000), len(lzma.compress(b'a' * 100000)), len(bz2.compress(b'b' * 100000)))
print(json.dumps({'a': [1.5, 2 ** 64, None, 'x'], 'b': {'c': 1e-7}}, sort_keys=True), re.findall(r'(\w+)@(\w+)', 'a@b c@d'))
db = sqlite3.connect(':memory:'); db.execute('create table t(a, b)'); db.executemany('insert into t values (?, ?)', [(i, i * 0.37) for i in range(1000)])
print(db.execute('select count(*), sum(a), round(avg(b), 9), max(b) from t where a % 7 = 3').fetchone())
total = []; lock = threading.Lock()
def work(n):
    s = sum(i * i for i in range(n))
    with lock: total.append(s)
ts = [threading.Thread(target=work, args=(20000 + i,)) for i in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
print(sorted(total), ssl.OPENSSL_VERSION, uuid.UUID(int=2 ** 100 + 12345))
'''
# What the x86 reference machine gave: the check's sha256 (first 16 hex digits, "x87:" and "note:" lines left out), its "x87:"
# lines, and the i386 userland's package list (its sha256, as `packages`). Recorded by bench case io5-solo32-reference, November
# 21, 2026: a Ryzen 7 8845HS (host kernel 7.0.0-43) under KVM, QEMU 10.0.13 (Debian 1:10.0.13+ds-0+deb13u1), -cpu host,-pku.
I386_CHECK = '5ae62b487d302512'
I386_PACKAGES = '241c2854d4043dc6'
I386_X87 = '''
x87: log 0.5 -0.6931471805599453
x87: log 1e-05 -11.512925464970229
x87: log 37.25 3.6176519448255684
x87: exp 0.5 1.6487212707001282
x87: exp 1e-05 1.00001000005
x87: exp 37.25 1.504767666846084e+16
x87: lgamma 0.5 0.5723649429247004
x87: lgamma 1e-05 11.512919692895824
x87: lgamma 37.25 96.6198845882781
x87: pow 2.0 0.5 1.4142135623730951
'''
X87_ULPS = 16


def x87_far(lines):
    """The check's "x87:" lines that are not the reference's: another name or argument, or a number more than X87_ULPS away."""
    ref = I386_X87.strip().splitlines()
    if len(lines) != len(ref): return [f'{len(lines)} x87 lines, the reference has {len(ref)}']
    far = []
    for got, want in zip(lines, ref):
        *g, gv = got.split(); *w, wv = want.split()
        if g != w or not abs(float(gv) - float(wv)) <= X87_ULPS * math.ulp(float(wv)): far.append(f'{got!r}: the reference has {want!r}')
    return far


GUEST = r'''#!/bin/bash
# io5.sh PHASE HOST: markers "IO5 ..." on the console; each step's output in /root/io5/STEP.log.
P=$1 H=$2 L=/root/io5; mkdir -p $L
export DEBIAN_FRONTEND=noninteractive PYSHA=@PYSHA@ KSHA=@KSHA@ H
say() { echo "IO5 $*"; echo "IO5 $*" >> $L/markers; }
now() { date +%s.%N; }
T0=$(now)
since() { awk "BEGIN{printf \"%.1f\", $(now)-$1}"; }
# natemu.profile=1 (sampling runs only; docs/restructure/el0-sampler-plan.md section 3): the kernel's _text once (for the
# KASLR slide), and around each step a start (port F8H, 1), a mark (F9H: the step's number, "IO5 mark N STEP") and a stop
# (F8H, 2), through /dev/port. The device is never probed: without the word no port is touched. The steps that run at once
# (make, download and generate) share one window and mark, "combined" (PROF_HELD set: their own spans write nothing).
PROF=0; grep -qw natemu.profile=1 /proc/cmdline && PROF=1; PROFN=0
port() { printf "\\$(printf %03o $2)" | dd of=/dev/port bs=1 seek=$1 count=1 conv=notrunc 2>/dev/null; }
prof() { [ $PROF = 1 ] && [ -c /dev/port ] || return 0
         case $1 in start) port 248 1;; stop) port 248 2;; mark) port 249 $(($2 % 256));; esac; }
pstep() { [ $PROF = 1 ] && [ -z "${PROF_HELD:-}" ] || return 0; PROFN=$((PROFN+1)); prof start; prof mark $PROFN; say mark $PROFN $1; }
pend() { [ $PROF = 1 ] && [ -z "${PROF_HELD:-}" ] || return 0; prof stop; }
span() { local n=$1 s=$(now) r; shift; pstep $n; if "$@" > $L/$n.log 2>&1; then r=ok; else r=fail:$?; fi; pend
         say step $n $r $(since $s) start=$(awk "BEGIN{printf \"%.1f\", $s-$T0}"); }
run() { bash -o pipefail -c "$1"; }
DEVS='pkg-config xz-utils libssl-dev zlib1g-dev libffi-dev libbz2-dev liblzma-dev libsqlite3-dev libreadline-dev uuid-dev libgdbm-dev libncurses-dev'
in32() { linux32 chroot /i386 /bin/bash -o pipefail -c "$1"; }
i386() {   # --i386: Debian's i386 userland in /i386 (debootstrap, same snapshot; untimed), which then builds the same source
  span make run 'cd /build/Python-3.13.18 && make -j$(nproc)'
  span i386-setup run "apt-get install -y --no-install-recommends debootstrap &&
      debootstrap --arch=i386 --variant=buildd --include=ca-certificates trixie /i386 https://snapshot.debian.org/archive/debian/@SNAP@/ &&
      cp /etc/apt/apt.conf.d/99io5 /i386/etc/apt/apt.conf.d/ && mount -t proc proc /i386/proc && mount --rbind /dev /i386/dev && mount --make-rslave /i386/dev &&
      linux32 chroot /i386 apt-get install -y --no-install-recommends $DEVS &&
      mkdir /i386/build && tar -C /i386/build -xf /root/py.tar.xz"
  chroot /i386 dpkg-query -W > $L/i386-packages.txt; say i386-packages $(sha256sum < $L/i386-packages.txt | cut -c1-16)
  say i386-userland $(in32 'uname -m; dpkg --print-architecture; gcc -dumpmachine' | tr '\n' ' ')
  span i386-configure in32 'cd /build/Python-3.13.18 && ./configure'
  span i386-make in32 'cd /build/Python-3.13.18 && make -j$(nproc)'
  # The built interpreter's answers (run_io5.py I386_CHECK pins their hash, recorded on real x86 under KVM: --backend kvm).
  curl -fsS http://$H/check.py -o /i386/build/check.py
  span i386-check in32 "cd /build/Python-3.13.18 && ./python /build/check.py &&
      ./python -m test -j\$(nproc) @I386TESTS@ 2>&1 | grep -E '^(Result|Total tests)' &&
      { ./python -m test test_math > /tmp/test_math.log 2>&1; rc=\$?; grep -E '^(FAIL|ERROR): |^Total tests' /tmp/test_math.log | sed 's/^/note: test_math /'
        [ \$rc = 0 ] || [ \$rc = 2 ]; }"   # 2: tests failed (expected under natemu); anything else is a crash
  sed 's/^/IO5chk /' $L/i386-check.log; say i386-check $(grep -Ev '^(x87|note): ' $L/i386-check.log | sha256sum | cut -c1-16)
  umount -R /i386/dev /i386/proc
}
say begin $P cpus=$(nproc) mem_mib=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo) kernel=$(uname -r)
if [ $PROF = 1 ]; then
  kt=$(grep -m1 ' _text$' /proc/kallsyms | cut -d' ' -f1)   # all zeros: kptr_restrict hides it (read the boot as nokaslr's)
  [[ $kt =~ ^0*$ ]] && say ktext hidden || say ktext $kt
  [ -c /dev/port ] && say profile port=/dev/port || say profile no-dev-port: the control port cannot be written
fi
say system $(systemctl is-system-running --wait) boot=$(systemd-analyze | head -1 | sed 's/.*= //; s/ //g')
say pti $(grep -c ' pti' /proc/cpuinfo | head -1) meltdown=$(tr ' ' _ < /sys/devices/system/cpu/vulnerabilities/meltdown)
if [ $P = use ]; then
  systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service
  echo https://snapshot.debian.org/archive/debian/@SNAP@/ > /etc/apt/mirrors/debian.list
  echo https://snapshot.debian.org/archive/debian-security/@SNAP@/ > /etc/apt/mirrors/debian-security.list
  sed -i 's/^Types: deb deb-src$/Types: deb/' /etc/apt/sources.list.d/debian.sources
  # One request at a time: Debian's servers sometimes stop answering pipelined HTTPS requests for 30 s (io-plan, November 18).
  echo 'Acquire::Check-Valid-Until "false"; Acquire::Retries "5"; Acquire::http::Pipeline-Depth "0";' > /etc/apt/apt.conf.d/99io5
  span apt-update apt-get update
  span apt-install apt-get install -y --no-install-recommends cloud-guest-utils fdisk build-essential pkg-config xz-utils \
      curl ca-certificates libssl-dev zlib1g-dev libffi-dev libbz2-dev liblzma-dev libsqlite3-dev libreadline-dev \
      uuid-dev libgdbm-dev libncurses-dev
  dpkg-query -W > $L/packages.txt; say packages $(sha256sum < $L/packages.txt | cut -c1-16)
  span grow run 'growpart /dev/vda 1 && resize2fs /dev/vda1'
  say root_mib $(df -B1M --output=size / | tail -1)
  span fetch-python run 'curl -fsS http://$H/py.tar.xz -o /root/py.tar.xz && echo "$PYSHA  /root/py.tar.xz" | sha256sum -c &&
      mkdir -p /build && tar -C /build -xf /root/py.tar.xz'
  span configure run 'cd /build/Python-3.13.18 && ./configure'
  mkdir -p /data/dl /data/gen; rm -f /tmp/make.done; s=$(now)
  if [ "@I386@" = 1 ]; then i386   # the 64-bit make alone (no loops, no tests), then the same build by an i386 userland
  else
  pstep combined; PROF_HELD=1   # one sampling window for the three steps that run at once
  ( span make run 'cd /build/Python-3.13.18 && make -j$(nproc)'; touch /tmp/make.done ) &
  span download run 'n=0; while :; do f=/data/dl/linux-$((n % 4)).tar.xz
      curl -fsS https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.tar.xz -o $f && echo "$KSHA  $f" | sha256sum -c || exit 1
      n=$((n+1)); [ -e /tmp/make.done ] && break; done; echo rounds=$n' &
  span generate run 'n=0; while :; do for i in $(seq 8); do head -c 256M /dev/urandom > /data/gen/f$i || exit 1; done
      cd /data/gen && sha256sum f? > SUMS && sync && echo 3 > /proc/sys/vm/drop_caches && sha256sum -c --quiet SUMS || exit 1
      n=$((n+1)); [ -e /tmp/make.done ] && break; done; echo rounds=$n' &
  wait; PROF_HELD=; pend; say combined $(since $s) download_$(tail -1 $L/download.log) generate_$(tail -1 $L/generate.log)
  span pytest run "cd /build/Python-3.13.18 && ./python -m test -j\$(nproc) @PYTESTS@"
  say pytest-result $(grep -E '^Result: ' $L/pytest.log | tail -1 | tr ' ' _)
  fi
  span manifest run 'find /data /build $(ls -d /i386/build 2>/dev/null) -type f -print0 | sort -z | xargs -0 sha256sum > /root/io5/manifest'
  say manifest $(sha256sum < $L/manifest | cut -c1-16) files=$(wc -l < $L/manifest)
  if [ "@SOLO@" = 1 ]; then   # diagnostic: the same build with nothing else running, outside the manifest's trees
    span solo-configure run 'mkdir -p /solo && tar -C /solo -xf /root/py.tar.xz && cd /solo/Python-3.13.18 && ./configure'
    span solo-make run 'cd /solo/Python-3.13.18 && make -j$(nproc)'
  fi
else
  span check sha256sum -c --quiet $L/manifest
  span python /build/Python-3.13.18/python -c 'import ssl, sqlite3, zlib, lzma, bz2, ctypes, readline, hashlib, json; print(ssl.OPENSSL_VERSION)'
  span net run 'curl -fsS https://deb.debian.org/debian/dists/trixie/Release -o /tmp/Release && grep -m1 "^Codename: trixie" /tmp/Release'
fi
dmesg > $L/dmesg-$P.log; journalctl -b -p err --no-pager -q > $L/journal-err-$P.log
[ $P = use ] || say manifest $(sha256sum < $L/manifest | cut -c1-16) files=$(wc -l < $L/manifest)
sed 's/^/IO5jerr /' $L/journal-err-$P.log   # for the host's console-only checks (no raw view of the disk)
say df $(df -B1M --output=used / | tail -1) uptime=$(cut -d' ' -f1 /proc/uptime)
'''


KVM_DIR = 'natemu-io5-reference'      # on the x86 machine, in its user's home: the inputs (kept) and each run's folder (removed)
KVM_NAME = f'natemu-io5-ref-{os.getpid()}'; KVM_RUN = f'{KVM_DIR}/run-{os.getpid()}'   # this run's container and folder there
KVM_IMAGE = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'   # = testkit/artifacts.py BENCH_BUILDER


def kvm_disk(args, make):
    """--backend kvm: copy the inputs to the x86 machine (once) and make the run's 16 GiB disk there; or stop the run's
    container, should it still be there (a killed run), and remove its folder."""
    host = os.environ['NATEMU_X86_HOST']
    if not make: return subprocess.run(['ssh', host, f'docker rm -f {KVM_NAME} > /dev/null 2>&1; rm -rf {KVM_RUN}'])
    sh('ssh', host, f'mkdir -p {KVM_DIR}/inputs {KVM_RUN}')
    sh('rsync', '-S', *(args.inputs / n for n in ('disk.raw', 'vmlinuz', 'initrd.img')), f'{host}:{KVM_DIR}/inputs/')
    sh('ssh', host, f'cd {KVM_RUN} && cp --sparse=always ../inputs/disk.raw disk.raw && chmod 666 disk.raw && truncate -s {DISK_BYTES} disk.raw && cp ../inputs/vmlinuz ../inputs/initrd.img .')


def kvm_load():
    """The x86 machine now: its load averages and the other containers running (its times are real x86's only if it is idle)."""
    r = subprocess.run(['ssh', os.environ['NATEMU_X86_HOST'], "cut -d' ' -f1-3 /proc/loadavg; docker ps --format '{{.Names}}'"], capture_output=True, text=True)
    load, *names = r.stdout.splitlines() or ['?']
    return {'load': load, 'containers': [n for n in names if n != KVM_NAME]}


def sh(*cmd, **kw): return subprocess.run([str(c) for c in cmd], check=True, **kw)


def others():
    """Other VMs running now (natemu, its test driver, QEMU) that are not this run's own: a timed run they overlap is
    contended (recorded; its times are not comparable)."""
    out = subprocess.run(['ps', '-axo', 'pid=,ppid=,command='], capture_output=True, text=True).stdout
    mine = {os.getpid()}
    rows = [l.split(None, 2) for l in out.splitlines() if len(l.split(None, 2)) == 3]
    for _ in range(3):                                      # this runner's descendants
        mine |= {int(p) for p, pp, _ in rows if int(pp) in mine}
    return [c[:120] for p, _, c in rows if int(p) not in mine
            and re.search(r'(/natemu|/natemu-host|qemu-system-x86_64)$', c.split()[0])]


def serve_files(directory):
    """An HTTP server for `directory` on the host's LAN address and a free port (other runners use 8000): the server and
    the guest's "address:port"."""
    import socket
    with socket.socket() as s:
        s.bind((lan_address(), 0))
        port = s.getsockname()[1]
    server = subprocess.Popen([sys.executable, '-m', 'http.server', '-b', lan_address(), str(port)], cwd=directory,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return server, f'{lan_address()}:{port}'


def prepare(inputs):
    inputs.mkdir(parents=True, exist_ok=True)
    tarball = inputs / f'{IMAGE}.tar.xz'
    if not tarball.exists(): sh('curl', '-fsSL', '-o', tarball, f'{IMAGE_URL}/{IMAGE}.tar.xz')
    assert subprocess.run(['shasum', '-a', '512', tarball], capture_output=True, text=True, check=True).stdout.split()[0] == IMAGE_SHA512
    if not (inputs / 'disk.raw').exists(): sh('tar', '-C', inputs, '-xJf', tarball, 'disk.raw')
    image = f'{inputs / "disk.raw"}?offset={ROOT_OFFSET}'
    for name, src in (('vmlinuz', f'/boot/vmlinuz-{KVER}'), ('initrd.img', f'/boot/initrd.img-{KVER}')):
        if not (inputs / name).exists(): sh(E2 / 'debugfs', '-R', f'dump {src} {inputs / name}', image, capture_output=True)
    assert digest(inputs / 'vmlinuz') == BZIMAGE_SHA
    if not (inputs / PYTHON).exists():
        sh('curl', '-fsSL', '-o', inputs / PYTHON, f'https://www.python.org/ftp/python/3.13.18/{PYTHON}')
    assert digest(inputs / PYTHON) == PYTHON_SHA
    write_json(inputs / 'inputs.json', {n: digest(inputs / n) for n in ('disk.raw', 'vmlinuz', 'initrd.img', PYTHON)})
    print('prepared', inputs)


def command(args, disk, cache, phase='use'):
    cmdline = CMDLINE + (' mitigations=off' if args.mitigations_off else '')
    if args.backend == 'natemu':
        return [getattr(args, 'natemu', None) or ROOT / 'target/release/natemu'] + (['--runtime', args.runtime] if getattr(args, 'runtime', None) else []) + ['--fexc', args.fexc, '--kernel', args.inputs / 'vmlinuz', '--initrd',
                args.inputs / 'initrd.img', '--cmdline', cmdline, '--ram', f'{args.ram}G', '--vcpus', str(args.vcpus),
                '--serial-stdin', '--disk', disk, '--net', '--rng', '--compile-cache', cache] + \
               (overlay_set(args.inputs / 'disk.raw') if getattr(args, 'image_format', 'raw') == 'overlay' else []) + \
               (['--speculation-barriers'] if args.speculation_barriers else []) + \
               (['--virtio-transport', 'pci'] if getattr(args, 'transport', 'mmio') == 'pci' else []) + \
               (['--perf-log', args.out / f'perf-{phase}.log'] if args.perf_log else []) + \
               [w for kv in getattr(args, 'natemu_set', []) for w in ('--set', kv)]
    if args.backend == 'kvm':   # the x86 reference machine: Debian's QEMU (the run's snapshot) in the builds' container, as ltp32's reference boots
        qemu = ('qemu-system-x86_64 -enable-kvm -cpu host,-pku -nodefaults -display none -serial stdio -no-reboot '
                f'-m {args.ram * 1024} -smp {args.vcpus} -kernel /b/vmlinuz -initrd /b/initrd.img -append {shlex.quote(cmdline)} '
                '-drive file=/b/disk.raw,format=raw,if=none,id=d -device virtio-blk-pci,drive=d,num-queues=1 '
                '-netdev user,id=n -device virtio-net-pci,netdev=n -device virtio-rng-pci')
        guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/{SNAPSHOT}/ trixie main' "
                 '> /etc/apt/sources.list; apt-get -o Acquire::Retries=5 update -qq > /dev/null 2>&1 && apt-get -o Acquire::Retries=5 install -y -qq '
                 f'--no-install-recommends qemu-system-x86 > /dev/null 2>&1 && echo "IO5 reference $(qemu-system-x86_64 --version | head -1 | tr " " _) '
                 f'$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | tr " " _) host-kernel=$(uname -r)" && exec {qemu}')
        return ['ssh', '-tt', os.environ['NATEMU_X86_HOST'], f'docker run --rm -it --name {KVM_NAME} --device /dev/kvm -v $HOME/{KVM_RUN}:/b {KVM_IMAGE} sh -c {shlex.quote(guest)}']
    # The other QEMU baselines' board and disk (run_fio_baselines.py): pinned machine version, one virtio-blk queue as natemu.
    return [QEMU, '-nodefaults', '-machine', 'pc-i440fx-11.1,hpet=off,i8042=off,vmport=off', '-accel', 'tcg,thread=multi',
            '-cpu', QCPU, '-m', str(args.ram * 1024), '-smp', str(args.vcpus), '-kernel', args.inputs / 'vmlinuz',
            '-initrd', args.inputs / 'initrd.img', '-append', cmdline, '-display', 'none', '-monitor', 'none',
            '-serial', 'stdio', '-no-reboot', '-drive', f'file={disk},format={"qcow2" if str(disk).endswith(".qcow2") else "raw"},if=none,id=d,cache=writeback,aio=threads',
            '-device', 'virtio-blk-pci,drive=d,num-queues=1', '-netdev', 'user,id=n', '-device', 'virtio-net-pci,netdev=n',
            '-device', 'virtio-rng-pci']


def launch(cmd, work, phase, host, limit, script='io5'):
    """Boot, log in on the console, run SCRIPT.sh PHASE HOST (served from `host`), power off. Returns (exit status, timed
    out, seconds). The script's console markers start with "SCRIPT-upper-cased " ("IO5 "); tests/bench/run_paper.py
    runs its own script through this too."""
    tag = script.upper().encode()
    master, slave = pty.openpty()
    log = open(work / f'serial-{phase}.log', 'wb')
    # Host arrival time of each marker line on CLOCK_UPTIME_RAW, natemu's perf-log clock (Python's monotonic_ns counts from
    # process start on macOS).
    stamps = open(work / f'markers-{phase}.tsv', 'w'); pending = b''
    p = subprocess.Popen([str(c) for c in cmd], stdin=slave, stdout=slave, stderr=open(work / f'stderr-{phase}.log', 'wb'),
                         start_new_session=True)
    os.close(slave)
    t0 = time.time(); deadline = t0 + limit; buf = b''
    # The serial line ends lines with \r\r\n; the end marker is computed in the guest so the echoed command cannot match.
    steps = ((rb'login: $', 'root\n'), (rb'Password: $', PASSWORD + '\n'), (rb'# $',
             f'curl -fsS http://{host}/{script}.sh -o /root/{script}.sh && bash /root/{script}.sh {phase} {host}; '
             f'echo {tag.decode()}-$((40+2))-END\n'),
             (tag + rb'-42-END\r*\n', 'poweroff\n'))
    timed_out = False
    try:
        for pattern, text in steps:
            while not re.search(pattern, buf):
                if time.time() > deadline or p.poll() is not None: raise TimeoutError(pattern)
                if select.select([master], [], [], 1)[0]:
                    try: data = os.read(master, 65536)
                    except OSError: data = b''
                    log.write(data); log.flush(); buf = (buf + data)[-4096:]
                    pending += data; *lines, pending = pending.split(b'\n')
                    for line in lines:
                        if tag + b' ' in line:   # a terminal escape can precede the first marker after the command line
                            marker = line[line.index(tag + b' '):].decode(errors='replace').strip()
                            stamps.write(f'{time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)}\t{marker}\n'); stamps.flush()
            buf = b''; os.write(master, text.encode())
        while p.poll() is None and time.time() < deadline:
            if select.select([master], [], [], 1)[0]:
                try: log.write(os.read(master, 65536))
                except OSError: pass
        timed_out = p.poll() is None
    except TimeoutError as e:
        timed_out = p.poll() is None
        print(f'  {phase}: no {e.args[0]!r}', flush=True)
    if p.poll() is None: stop_group(p)
    os.close(master); log.close(); stamps.close()
    return p.wait(), timed_out, round(time.time() - t0, 1)


def offline(disk, work, phase):
    """Problems found on the stopped disk: e2fsck -fn, the superblock state, and the guest's logs copied out."""
    image = f'{disk}?offset={ROOT_OFFSET}'
    fsck = subprocess.run([E2 / 'e2fsck', '-fn', image], capture_output=True, text=True)
    # The superblock header (dumpe2fs does not take the ?offset= device form; debugfs does).
    stats = subprocess.run([E2 / 'debugfs', '-R', 'stats', image], capture_output=True, text=True)
    header = stats.stdout + stats.stderr
    (work / f'fsck-{phase}.log').write_text(fsck.stdout + fsck.stderr + header)
    problems = [] if fsck.returncode == 0 else [f'e2fsck -fn exit {fsck.returncode}']
    if re.search(r'journal recovery|recovering journal', fsck.stdout + fsck.stderr): problems.append('journal left to recover')
    if not re.search(r'^Filesystem state:\s+clean$', header, re.M) or 'needs_recovery' in header:
        problems.append('superblock: not clean')
    out = work / f'guest-{phase}'; out.mkdir()
    subprocess.run([E2 / 'debugfs', '-R', f'rdump /root/io5 {out}', image], capture_output=True)
    if (out / 'io5/manifest').exists(): shutil.move(out / 'io5/manifest', work / f'manifest-{phase}')
    for name in (f'dmesg-{phase}.log', f'journal-err-{phase}.log'):
        if not (out / 'io5' / name).exists(): problems.append(f'{name} not copied out'); continue
        text = (out / 'io5' / name).read_text(errors='replace')
        lines = text.splitlines() if name.startswith('journal') else [l for l in text.splitlines() if re.search(BAD, l)]
        unexpected = [l for l in lines if not any(re.search(e, l) for e in EXPECTED)]
        problems += [f'{name}: {l.strip()[:160]}' for l in unexpected]
    return problems


def console_checks(work, phase):
    """The checks of offline() that the console can answer, for when the volume cannot hold a raw view of the disk: the kernel
    lines on the serial log (dmesg, at the console log level) and the journal errors the guest echoed; in the verify boot
    also that the journal was not recovered (the first boot's unmount was clean). NOT done: e2fsck -fn, the superblock
    state, copying the guest's files out (the manifest fingerprint is compared from the verify boot's console line)."""
    text = (work / f'serial-{phase}.log').read_bytes().decode(errors='replace').replace('\r', '')
    lines = [(l, 'dmesg') for l in text.splitlines() if re.search(BAD, l) and not l.startswith('IO5jerr ')]
    lines += [(l[8:], 'journal-err') for l in text.splitlines() if l.startswith('IO5jerr ')]
    if phase == 'verify': lines += [(l, 'journal') for l in text.splitlines() if re.search(r'EXT4-fs \(\w+\): (recovery complete|orphan cleanup)', l)]
    return [f'{k}: {l.strip()[:160]}' for l, k in lines if not any(re.search(e, l) for e in EXPECTED)]


def judge(work, phase, code, timed_out, i386=False, reference=False):
    text = (work / f'serial-{phase}.log').read_bytes().decode(errors='replace').replace('\r', '')
    markers = re.findall(r'^IO5 (.*)$', text, re.M)
    steps = {m.split()[1]: m.split()[2:] for m in markers if m.startswith('step ')}
    values = {m.split()[0]: m.split()[1:] for m in markers if not m.startswith('step ')}
    problems = [f'step {s} missing' for s in (REQUIRED_I386 if i386 and phase == 'use' else REQUIRED[phase]) if s not in steps]
    problems += [f'step {s} {v[0]}' for s, v in steps.items() if v[0] != 'ok']
    if code != 0 or timed_out: problems.append(f'exit {code} timed_out={timed_out}')
    if 'reboot: Power down' not in text: problems.append('no "reboot: Power down" on the console')
    if values.get('system', ['?'])[0] != 'running': problems.append(f'systemd {values.get("system")}')
    if phase == 'use' and not i386 and values.get('pytest-result', ['?'])[0] != 'Result:_SUCCESS': problems.append(f'pytest {values.get("pytest-result")}')
    if phase == 'use' and i386:
        if values.get('i386-userland') != ['i686', 'i386', 'i686-linux-gnu']: problems.append(f'i386 userland {values.get("i386-userland")}')
        check = [l[7:] for l in text.splitlines() if l.startswith('IO5chk ')]
        (work / 'i386-check.txt').write_text(''.join(l + '\n' for l in check))
        if values.get('i386-packages') != [I386_PACKAGES]: problems.append(f'i386 packages {values.get("i386-packages")}: not {I386_PACKAGES}')
        if not reference:
            if values.get('i386-check', ['?'])[0] != I386_CHECK: problems.append(f'i386 check {values.get("i386-check")}: not the x86 reference\'s {I386_CHECK}')
            problems += [f'i386 check, x87: {p}' for p in x87_far([l for l in check if l.startswith('x87: ')])]
    if 'natemu: network:' in (work / f'stderr-{phase}.log').read_text(errors='replace'): problems.append('natemu network guest-error lines')
    return {'steps': steps, 'values': values, 'problems': problems}


def run(args):
    if args.transport == 'pci' and args.backend != 'natemu': raise SystemExit('--transport pci is a natemu option')
    overlay = check_image_format(args.image_format) == 'overlay'
    qcow = args.image_format != 'raw'
    args.out.mkdir(parents=True)
    recorded = json.loads((args.inputs / 'inputs.json').read_text())
    for name in ('disk.raw', 'vmlinuz', 'initrd.img', PYTHON): assert digest(args.inputs / name) == recorded[name], name
    disk = args.out / ('disk.qcow2' if qcow else 'disk.raw')
    if overlay: overlay_create(args.inputs / 'disk.raw', disk, DISK_BYTES)
    elif qcow:   # prepared once per inputs directory: preallocated metadata, grown to 16 GiB (also preallocated metadata)
        prep = qcow2_input(args.inputs / 'disk.raw', args.inputs / 'disk.qcow2', DISK_BYTES); print(f'qcow2 input: {prep:.1f}s (0 = reused)', flush=True)
        sh('cp', '-c', args.inputs / 'disk.qcow2', disk)
    else: sh('cp', '-c', args.inputs / 'disk.raw', disk); os.truncate(disk, DISK_BYTES)
    serve = args.out / 'serve'; serve.mkdir()
    (serve / 'io5.sh').write_text(GUEST.replace('@PYSHA@', PYTHON_SHA).replace('@KSHA@', LINUX_SHA)
                                  .replace('@PYTESTS@', PYTESTS).replace('@SNAP@', SNAPSHOT)
                                  .replace('@SOLO@', '1' if args.solo_build else '0')
                                  .replace('@I386@', '1' if args.i386 else '0').replace('@I386TESTS@', I386TESTS))
    (serve / 'check.py').write_text(CHECK)
    clone(args.inputs / PYTHON, serve / 'py.tar.xz')
    server, host = serve_files(serve)
    result = {'image_format': args.image_format, 'transport': args.transport, 'qcow2_prep_seconds': prep if qcow and not overlay else None, 'backend': args.backend, 'vcpus': args.vcpus, 'ram_gib': args.ram, 'host': host, 'inputs': recorded,
              'speculation_barriers': args.speculation_barriers, 'mitigations_off': args.mitigations_off, 'solo_build': args.solo_build, 'i386': args.i386, 'perf_log': args.perf_log, 'command': [str(c) for c in command(args, disk, 'CACHE')],
              'natemu': digest(args.natemu or ROOT / 'target/release/natemu') if args.backend == 'natemu' else None,
              'qemu': subprocess.run([QEMU, '--version'], capture_output=True, text=True).stdout.splitlines()[0] if args.backend == 'qemu' else None, 'phases': {}}
    try:
        time.sleep(1); assert server.poll() is None, 'http server'
        if args.backend == 'kvm': kvm_disk(args, True)
        with open(args.lock, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for phase, limit in (('use', args.limit), ('verify', 1800)):
                print(f'{phase}: launching', flush=True)
                before = others(); load = [kvm_load()] if args.backend == 'kvm' else None
                code, timed_out, seconds = launch(command(args, disk, args.out / 'compile-cache', phase), args.out, phase, host, limit)
                contended = before + others()
                if load:   # other work on the x86 machine: this boot's times are indicative only (after: the VM's own load is in it)
                    load.append(kvm_load())
                    try: busy = phase == 'use' and float(load[0]['load'].split()[0]) > 1 or any(x['containers'] for x in load)
                    except ValueError: busy = True     # the probe gave no load: not known to be idle
                    result.setdefault('reference_host', {})[phase] = {'before': load[0], 'after': load[1], 'times_indicative': busy}
                r = judge(args.out, phase, code, timed_out, args.i386, args.backend == 'kvm')
                short = raw_shortfall(disk, args.out) if qcow else 'the disk is on the x86 machine' if args.backend == 'kvm' else None
                if short:    # no room for the raw view: console checks only, recorded (not a silent pass)
                    print(f'{phase}: offline checks (e2fsck, superblock, guest files) SKIPPED: {short}', flush=True)
                    r['offline_skipped'] = short   # kvm: the journal lines too are left out (the reference's own disk is not what is tested)
                    r['problems'] += [p for p in console_checks(args.out, phase) if args.backend != 'kvm' or not p.startswith('journal: ')]
                else:
                    with raw_of(disk, qcow, args.out / 'view.raw') as raw: r['problems'] += offline(raw, args.out, phase)
                if qcow: found, report = qcow2_problems(disk); write_json(args.out / f'qemu-img-check-{phase}.json', report); r['problems'] += found
                if overlay and digest(args.inputs / 'disk.raw') != recorded['disk.raw']: r['problems'].append('the overlay\'s base disk.raw changed')
                untimed = sum(float(v[1]) for s, v in r['steps'].items() if s in UNTIMED and len(v) > 1)
                r.update(exit=code, seconds=seconds, seconds_compared=round(seconds - untimed, 1), untimed=list(UNTIMED), contended=contended); result['phases'][phase] = r
                print(f'{phase}: exit {code} in {seconds}s ({r["seconds_compared"]}s without {"/".join(UNTIMED)}), problems {r["problems"]}', flush=True)
                if code != 0 or timed_out: break
    finally:
        stop_group(server)
        if args.backend == 'kvm': kvm_disk(args, False)
        if disk.exists(): print(f'disk {disk}: {os.path.getsize(disk) / 2**30:.1f} GiB apparent, {os.stat(disk).st_blocks * 512 / 2**30:.1f} GiB allocated', flush=True)
        if not args.keep_disk: disk.unlink(missing_ok=True)    # also after a failed or interrupted launch
    # The manifest the second boot checked must be the one the first boot fingerprinted on the console (not a shorter copy).
    use, verify = result['phases'].get('use', {}), result['phases'].get('verify', {})
    fingerprint = use.get('values', {}).get('manifest', [])
    checked = args.out / 'manifest-verify'
    seen = [hashlib.sha256(checked.read_bytes()).hexdigest()[:16], f'files={len(checked.read_bytes().splitlines())}'] if checked.exists() else verify.get('values', {}).get('manifest')   # file copied out, else the verify boot's console line
    if verify and fingerprint != seen:
        verify['problems'].append(f'manifest checked on relaunch differs from the first boot fingerprint {fingerprint}')
    result['pass'] = len(result['phases']) == 2 and not any(r['problems'] for r in result['phases'].values())
    write_json(args.out / 'result.json', result)
    print('PASS' if result['pass'] else 'FAIL', args.out)
    return 0 if result['pass'] else 1



# profile: one step per boot from a snapshot disk (a kept `run --keep-disk` disk: packages installed, /root/py.tar.xz,
# /build and /solo built). Each step prints "IO5 step NAME ..." at its end; the host stamps it (markers-STEP.tsv).
PROFILE = r"""#!/bin/bash
S=$1 L=/root/io5p; mkdir -p $L
say() { echo "IO5 $*"; }
now() { date +%s.%N; }
span() { local n=$1 s=$(now) r; shift; if bash -o pipefail -c "$1" > $L/$n.log 2>&1; then r=ok; else r=fail:$?; fi
         say step $n $r $(awk "BEGIN{printf \"%.1f\", $(now)-$s}"); }
say system $(systemctl is-system-running --wait) boot=$(systemd-analyze | head -1 | sed 's/.*= //; s/ //g')
systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service
case $S in
boot) span idle 'sleep 10';;
configure) span prep 'rm -rf /p && mkdir /p && tar -C /p -xf /root/py.tar.xz'; span configure 'cd /p/Python-3.13.18 && ./configure';;
micro)   # cost per trapping event (tests/io/io5_micro.c, served by the host)
  span micro "curl -fsS http://$2/m.c -o /root/m.c && gcc -O2 -msse4.2 /root/m.c -o /root/m && /root/m"
  while read l; do say micro $l; done < $L/micro.log;;
trap-pcmp|trap-rdtsc|trap-seg|trap-loop|trap-none)   # 30 M iterations of one trapping instruction (none: the same boot without)
  curl -fsS http://$2/m.c -o /root/m.c && gcc -O2 -msse4.2 /root/m.c -o /root/m
  N=10; case $S in trap-pcmp) T=pcmpistri;; trap-rdtsc) T=rdtscp;; trap-seg) T=seg_read;; trap-loop) T=empty; N=100;; *) T=zzz;; esac
  span $S "/root/m $T $N"; say micro $(cat $L/$S.log);;
cfile)   # a C file the host serves as x.c (NATEMU_PROFILE_C), compiled -O1 and run; its lines are printed as markers
  curl -fsS http://$2/x.c -o /root/x.c && gcc -O1 /root/x.c -o /root/x
  span cfile "/root/x"; while read l; do say micro $l; done < $L/cfile.log;;
forkexec|forkexit)   # one micro test alone, long enough for the timing log: process creation
  T=fork_exec_true; [ $S = forkexit ] && T=fork_exit_wait
  curl -fsS http://$2/m.c -o /root/m.c && gcc -O2 -msse4.2 /root/m.c -o /root/m
  span $S "/root/m $T 6"; say micro $(cat $L/$S.log);;
configure2)   # configure twice in one boot: the second finds every translation already compiled
  span prep 'rm -rf /p && mkdir /p && tar -C /p -xf /root/py.tar.xz'; span configure 'cd /p/Python-3.13.18 && ./configure'
  span prep2 'rm -rf /p && mkdir /p && tar -C /p -xf /root/py.tar.xz'; span configure-again 'cd /p/Python-3.13.18 && ./configure';;
make) span prep 'cd /solo/Python-3.13.18 && make clean'; span make 'cd /solo/Python-3.13.18 && make -j$(nproc)';;
make-net|make-disk|make-rng)   # the build beside one of the run's loops, until make ends (the loop's rounds are printed)
  case $S in
  make-net) LOOP='f=/data/dl/p.tar.xz; curl -fsS https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.tar.xz -o $f && echo "@KSHA@  $f" | sha256sum -c --quiet';;
  make-disk) LOOP='for i in $(seq 8); do head -c 256M /dev/urandom > /data/gen/f$i; done; cd /data/gen && sha256sum f? > SUMS && sync && echo 3 > /proc/sys/vm/drop_caches && sha256sum -c --quiet SUMS';;
  make-rng) LOOP='head -c 2G /dev/urandom | sha256sum > /dev/null';;
  esac
  span prep 'cd /solo/Python-3.13.18 && make clean'; rm -f /tmp/make.done
  ( span make 'cd /solo/Python-3.13.18 && make -j$(nproc)'; touch /tmp/make.done ) &
  span loop "n=0; while :; do $LOOP || exit 1; n=\$((n+1)); [ -e /tmp/make.done ] && break; done; echo rounds=\$n"
  wait; say loop $(tail -1 $L/loop.log);;
pytest) span pytest "cd /build/Python-3.13.18 && ./python -m test -j\$(nproc) @PYTESTS@";;
trim)   # host blocks freed (profile.json's allocated); every other file still reads back right from disk; fsck after
  span trim 'du -sh /data && rm -rf /data/gen && sync && fstrim -v /'
  span verify 'echo 3 > /proc/sys/vm/drop_caches && grep -v "  /data/gen/" /root/io5/manifest > /root/io5p/kept && sha256sum -c --quiet /root/io5p/kept && wc -l < /root/io5p/kept';;
timerfd)   # the barriers run's one failure, alone then beside 4 busy loops: count failures of 30 runs each
  T='cd /build/Python-3.13.18 && f=0; for i in $(seq 30); do ./python -m test test_os -m test_timerfd_ns_TFD_TIMER_ABSTIME >> /root/io5p/timerfd-all.log 2>&1 || f=$((f+1)); done; echo failures=$f'
  span timerfd-idle "$T"; say timerfd-idle $(tail -1 $L/timerfd-idle.log)
  for i in 1 2 3 4; do (while :; do :; done) & done
  span timerfd-load "$T"; say timerfd-load $(tail -1 $L/timerfd-load.log); kill $(jobs -p);;
esac
"""


def profile(args):
    """Boots each step from a clone of the snapshot; windows.json maps STEP:NAME to its host [start, end] ns."""
    args.out.mkdir(parents=True)
    serve = args.out / 'serve'; serve.mkdir()
    (serve / 'io5.sh').write_text(PROFILE.replace('@PYTESTS@', PYTESTS).replace('@KSHA@', LINUX_SHA))
    shutil.copy(ROOT / 'tests/io/io5_micro.c', serve / 'm.c')
    if os.environ.get('NATEMU_PROFILE_C'): shutil.copy(os.environ['NATEMU_PROFILE_C'], serve / 'x.c')
    server, host = serve_files(serve)
    windows, ok, allocated, contended = {}, True, {}, {}
    try:
        time.sleep(1); assert server.poll() is None, 'http server'
        with open(args.lock, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for step in args.steps:
                disk = args.out / f'disk-{step}.raw'; sh('cp', '-c', args.snapshot, disk); disk.chmod(0o644)   # the snapshot is read-only
                kept = os.environ.get('NATEMU_PROFILE_CACHE')       # a cache kept between launches (warm from the second)
                cache = Path(kept) if kept else args.out / f'compile-cache-{step}'          # cold: a new cache per boot
                before = others()
                code, timed_out, seconds = launch(command(args, disk, cache, step), args.out, step, host, args.limit)
                if before or others():
                    contended[step] = before + others()
                    print(f'  {step}: CONTENDED by {contended[step]}', flush=True)
                logs = args.out / f'guest-{step}'; logs.mkdir()   # the guest's step logs, before the clone goes
                subprocess.run([E2 / 'debugfs', '-R', f'rdump /root/io5p {logs}', f'{disk}?offset={ROOT_OFFSET}'], capture_output=True)
                allocated[step] = disk.stat().st_blocks * 512
                if step == 'trim':
                    fsck = subprocess.run([E2 / 'e2fsck', '-fn', f'{disk}?offset={ROOT_OFFSET}'], capture_output=True, text=True)
                    (args.out / 'fsck-trim.log').write_text(fsck.stdout + fsck.stderr); ok &= fsck.returncode == 0
                disk.unlink()
                if not kept: shutil.rmtree(cache, ignore_errors=True)
                if not args.keep_request_logs:   # ~0.4 GiB per vCPU per step; the summary (`vcpu {...}` lines) stays in perf-STEP.log
                    for raw in args.out.glob(f'perf-{step}.log.v[0-9]*'): raw.unlink()
                for line in (args.out / f'markers-{step}.tsv').read_text().splitlines():
                    t, text = line.split('\t'); m = text.split()
                    if m[1] == 'step':
                        windows[f'{step}:{m[2]}'] = [int(t) - int(float(m[4]) * 1e9), int(t)]
                        ok &= m[3] == 'ok'
                ok &= code == 0 and not timed_out
                print(f'{step}: exit {code} in {seconds}s', flush=True)
    finally:
        stop_group(server)
    write_json(args.out / 'windows.json', windows)
    write_json(args.out / 'profile.json', {'steps': args.steps, 'snapshot': str(args.snapshot), 'runtime': str(args.runtime),
                                           'mitigations_off': args.mitigations_off, 'ok': ok,
                                           'allocated': allocated, 'snapshot_allocated': args.snapshot.stat().st_blocks * 512,
                                           'contended': contended,
                                           'command': [str(c) for c in command(args, 'DISK', 'CACHE', 'STEP')]})
    print('OK' if ok else 'FAILED', args.out)
    return 0 if ok else 1


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--inputs', type=Path, required=True)
    r = sub.add_parser('run')
    r.add_argument('--backend', choices=('natemu', 'qemu', 'kvm'), required=True, help='kvm: on the x86 reference machine NATEMU_X86_HOST (ssh, Docker, /dev/kvm), which records --i386\'s check; console checks only')
    r.add_argument('--inputs', type=Path, required=True); r.add_argument('--out', type=Path, required=True)
    r.add_argument('--natemu', type=Path, help='the natemu binary (default: target/release/natemu)')
    r.add_argument('--runtime', type=Path, help='the resident natemu-runtime (default: beside the binary)')
    r.add_argument('--fexc', type=Path, default=FEXC); r.add_argument('--vcpus', type=int, default=4)
    r.add_argument('--ram', type=int, default=8, help='GiB'); r.add_argument('--limit', type=int, default=7200, help='use-phase seconds')
    r.add_argument('--speculation-barriers', action='store_true', help="natemu's page-walk barriers (RDCL_NO: Linux runs without PTI)")
    r.add_argument('--mitigations-off', action='store_true', help='add mitigations=off to the kernel command line (both backends)')
    r.add_argument('--solo-build', action='store_true', help='diagnostic: after the manifest, configure and make the source again alone')
    r.add_argument('--i386', action='store_true', help='the 32-bit toolchain build: configure and make alone (no loops or tests), then again by a Debian i386 userland installed in /i386')
    r.add_argument('--perf-log', action='store_true', help='natemu: write each launch\'s timing log to OUT/perf-PHASE.log')
    r.add_argument('--lock', type=Path, default=VM_LOCK); r.add_argument('--keep-disk', action='store_true')
    r.add_argument('--image-format', choices=IMAGE_FORMATS, default='raw', help='raw (default), qcow2 or overlay (see the docstring)')
    r.add_argument('--natemu-set', action='append', default=[], metavar='KEY=VALUE', help='natemu: passed on as --set KEY=VALUE (the bench harness turns the platform devices off with it)')
    r.add_argument('--transport', choices=('mmio', 'pci'), default='mmio', help='natemu virtio transport (see the docstring)')
    f = sub.add_parser('profile', help='one step per boot from a snapshot disk, with the timing log')
    f.add_argument('--snapshot', type=Path, required=True); f.add_argument('--inputs', type=Path, required=True)
    f.add_argument('--out', type=Path, required=True); f.add_argument('--steps', nargs='+', required=True,
                   choices=('boot', 'configure', 'configure2', 'micro', 'cfile', 'forkexec', 'forkexit', 'trap-pcmp', 'trap-rdtsc', 'trap-seg', 'trap-loop', 'trap-none', 'make', 'make-net', 'make-disk', 'make-rng', 'pytest', 'timerfd', 'trim'))
    f.add_argument('--runtime', type=Path, help='the resident (a `perf` build for 5 ms bucket snapshots)')
    f.add_argument('--natemu', type=Path, help='the natemu binary (default: target/release/natemu)')
    f.add_argument('--fexc', type=Path, default=FEXC); f.add_argument('--vcpus', type=int, default=4)
    f.add_argument('--ram', type=int, default=8); f.add_argument('--limit', type=int, default=3600)
    f.add_argument('--mitigations-off', action='store_true'); f.add_argument('--speculation-barriers', action='store_true')
    f.add_argument('--lock', type=Path, default=VM_LOCK)
    f.add_argument('--no-perf-log', dest='perf_log', action='store_false', help='timing only (no timing log, no request logs)')
    f.add_argument('--backend', choices=('natemu', 'qemu'), default='natemu', help='qemu: the same steps under QEMU (no timing log)')
    f.add_argument('--keep-request-logs', action='store_true', help='keep the per-vCPU request logs (perf-STEP.log.vN, ~0.4 GiB each; tests/io/io5_breakdown.py needs them); deleted after each step by default')
    f.set_defaults(perf_log=True)
    args = a.parse_args()
    if args.cmd == 'prepare': prepare(args.inputs); return 0
    if args.cmd == 'profile': signal.signal(signal.SIGTERM, lambda *_: sys.exit(3)); return profile(args)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(3))
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
