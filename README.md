# natemu

natemu runs x86-64 Linux on Apple Silicon Macs, faster than QEMU TCG.

natemu is a *captive* emulator: the guest gets its own hardware VM through Hypervisor.framework.
natemu translates the guest's x86-64 code to ARM64 and runs it inside that VM, next to a small
runtime, so the Mac's hardware does most of the work:

- **Memory:** guest page tables become real ARM page tables, so ordinary loads and stores need no
  software lookup.
- **Memory ordering:** Apple Silicon's hardware x86 memory-ordering mode (TSO) replaces barriers in
  the generated code.
- **Kernel work:** system calls, page faults, interrupts and translated-code lookups are handled
  inside the VM. natemu leaves the VM mainly to translate new code and for device I/O.

QEMU TCG instead looks up every guest memory access in a software TLB and
handles every page fault, system call and interrupt in the emulator itself.

natemu runs 11 benchmarks about 4.5× faster than QEMU TCG (geometric mean; 1.4× to 41× per
benchmark), and a context-switch-heavy Debian workload (package installs and a CPython build, with
network and disk traffic alongside) 1.7× faster. See [benchmarks](docs/benchmarks.md).

The translator is built on [FEX](https://github.com/FEX-Emu/FEX)'s x86 compiler. FEX runs Linux
applications, not whole systems. natemu builds FEX's compiler natively on macOS, without FEX's Linux
runtime, and extends it to compile kernel code and to raise exceptions at the exact faulting
instruction. Privileged instructions go to natemu's runtime inside the VM. The changes live in
[natemu-fex](https://github.com/ArArgon/natemu-fex).

## Status: experimental

natemu boots the stock Debian 13 cloud image unchanged, with networking and a disk. The cloud-image
workflow has been tested on an M4 Mac running macOS 26; targeted networking checks also pass on
M2 Max. Other Apple Silicon chips or macOS versions may lack hardware features natemu needs.
Other distributions and kernels are untested. Major features still missing:

- **Booting:** no BIOS or UEFI firmware. natemu starts a Linux kernel directly, so it cannot boot
  installer ISOs or bootloaders.
- **Devices:** a serial console, a network card, raw disk images and a random-number device only. No
  PCI bus by default (an experimental one: `--virtio-transport pci`), display, keyboard, sound, USB or file sharing with the Mac. Disk images must be raw, not qcow2.
- **Networking:** IPv4 user-mode NAT, with localhost port forwarding and opt-in guest access to
  the Mac's `localhost`. No IPv6 or bridged networking.
- **CPU:** 64-bit code only, so 32-bit programs do not run. No AVX-512, AMX or CET. x87 floating
  point computes at 64-bit rather than 80-bit precision.
- **Performance:** no fast path for process address-space switches (PCID), which makes Linux's
  default Meltdown mitigation slow (see [Performance](#performance-and-cpu-vulnerability-mitigations)).

## Requirements

- An Apple Silicon Mac running macOS 26 (tested on M4). The minimum is macOS 15: natemu uses
  Hypervisor.framework's interrupt-controller API (`hv_gic_*`), which is linked as a required
  import, so an older macOS refuses to load the binary. There is no runtime check for it. At startup natemu checks the
  vCPU for the hardware features it needs (FlagM, LSE, LSE2, the crypto extensions, Apple's TSO bit) and
  fails with one line naming everything missing, for example `natemu: host lacks lse2 pmull128`. Trapped floating-point
  exceptions are checked separately, by the resident at its first run (after the Linux path's startup lines): a host
  without them stops with `natemu: host lacks fptraps`, not merged into that line.
  A boot prints startup lines of the form `natemu: <topic>: <value>`, one decision each: `cpu` (the x86-64-v3 level plus
  what the guest CPU adds, computed from the host's features), `fp` (`fast mode`; on a Mac without FEAT_AFP, M1 or M2,
  `fast mode, degraded (host lacks FEAT_AFP)`: a few x86 floating-point corner cases differ, listed under "Floating point on
  Macs without AFP" below; `--fp accurate` or `NATEMU_FP=accurate` prints `accurate mode`, which makes them exact: see "Accurate mode" below), `acceleration` (each host feature natemu can use for speed, `+` used and `-` the slower fallback code),
  `meltdown` (`speculation barriers` or `none`), and `code cache` (`pool on, disk 512 MiB at ~/Library/Caches/natemu/fexc`: the shared template pool and the compile cache on disk, or `disk off`). Tools recognise these lines by those
  topics only: errors and warnings can also read `natemu: disk lock: ...`. If the compile cache cannot be opened, a later
  `natemu: code cache: disk off (reason)` line corrects it.
- Xcode Command Line Tools (`xcode-select --install`)
- [rustup](https://rustup.rs). The repository pins the Rust version, and rustup installs it automatically.
- [Homebrew](https://brew.sh) packages: `brew install cmake ninja pkg-config libslirp e2fsprogs`
- Python 3 and git

### Floating point on Macs without AFP (fast mode)

On a Mac without FEAT_AFP (M1, M2) natemu's default floating-point mode, fast, keeps x86 results exact except for these
documented deviations (the M4 has none; the names are the ones `tools/fp_deviations.py` and the test runners use):

- `masked-sse-de`: with DE masked, an SSE or AVX instruction on a denormal operand does not set MXCSR.DE (the result is right).
- `nan-results`: a NaN result can differ from x86 in sign or choice: the default NaN is positive, the one-instruction negated FMA forms (the scalar ones, the packed FNMADD and the first multiplicand of FNMSUB) flip a NaN's sign, two-NaN and three-NaN choices follow Arm's order (the addend of the packed FMSUB and FNMSUB and the subtracted lanes of FMADDSUB and FMSUBADD arrive quieted, so an SNaN there takes no priority), RSQRT's NaN has the other sign.
- `fma-invalid-flag`: an FMA of infinity times zero plus a QNaN, or plus an SNaN addend of the packed FMSUB, FNMSUB or the subtracted lanes of FMADDSUB and FMSUBADD, returns the default NaN and sets MXCSR.IE (x86: the NaN, quieted; no flag for a QNaN).
- `band-ue-flag`: with UE masked, a result just below the smallest normal number that rounds up to it can set MXCSR.UE (x86 judges tininess after rounding).
- `daz-ftz-band`: with DAZ and FTZ both set, a result just below the smallest normal number that rounds up to it is 0 instead of the smallest normal.
- `ftz-band`: with FTZ set and DAZ clear, a result whose exact value lies just below the smallest normal number, which x86 flushes to 0 (tiny after rounding to the destination precision) but Arm's denormal rounding carries up to the smallest normal, is the smallest normal instead of 0 (the flags are right).

#### Accurate mode

`--fp accurate` (or `NATEMU_FP=accurate`; the flag wins) makes every item in the list above exact on a Mac without AFP, so x86 floating-point
results and flags match what real x86 gives. It does this by letting the CPU trap on the cases that differ and correcting the result in natemu's
small EL1 runtime, so it needs a host that reports precise floating-point traps (M1 Pro, M2 Max and M4 do; this is a required host feature).
natemu prints `natemu: fp: accurate mode` at startup. On an M4 (or any Mac with AFP) accurate mode changes nothing: no code, no cost.

The price is speed, only where a program really hits those cases or is built to (measured in M2 emulation, accurate against fast mode):

| Benchmark | Extra time |
| --- | --- |
| cray (built with `-ffast-math`, so DAZ and FTZ) | +163% (was +623% before the DAZ screen) |
| luajit | +29.3% |
| luajit-interp | +16.7% |
| sqlite | about +5.5% (x87 floating-point mode switching around x87 instructions, a known remaining cost) |
| avx | about +2.5% |
| zstd | about 0 |

Fast mode stays the default: making only the underflow or only the NaN fixes always-on cost +16% to +18% on average, which was too much.

To check a Mac, run `sh tools/fp_check/run.sh` from a checkout (after the setup below): it builds into `$TMPDIR`, runs the trap probe
and the floating-point test runners in both modes, and writes `$TMPDIR/fp_check/report.txt`.

## Setup

### 1. Get the source

```sh
git clone https://github.com/ArArgon/natemu.git
cd natemu
git submodule update --init
```

Check your machine (this installs nothing and starts no VM):

```sh
./tools/with-env python3 -B tools/check_setup.py
```

### 2. Build natemu

```sh
cargo build --release
cargo build --release -p natemu-runtime --target aarch64-unknown-none
codesign --force --sign - --entitlements crates/host/entitlements.plist target/release/natemu
```

The `codesign` step grants the binary access to Hypervisor.framework; repeat it after every rebuild.

### 3. Build the x86 compiler

```sh
tools/fexc/build.sh ~/natemu-fexc
```

The directory must not exist yet. It takes about 30 seconds and produces `~/natemu-fexc/obj/fexc`.

## Running a Debian cloud image

natemu has no firmware or bootloader. It starts the kernel directly, so first copy the kernel and
initramfs out of the image. `tools/boot_files.py` does that and prints the `root=` option for the
image's root partition:

```sh
mkdir -p ~/natemu-vm && cd ~/natemu-vm
IMG=debian-13-nocloud-amd64-20260914-2601
curl -fLO https://cloud.debian.org/images/cloud/trixie/20260914-2601/$IMG.tar.xz
tar -xJf $IMG.tar.xz disk.raw
truncate -s 16G disk.raw                                  # room to grow; the file stays sparse
ROOT=$(python3 ~/natemu/tools/boot_files.py disk.raw)     # writes vmlinuz and initrd.img here
```

The first boot also answers systemd's first-boot questions. Replace `CHANGE-ME` with the root
password you want:

```sh
FIRSTBOOT="systemd.set_credential=firstboot.locale:C.UTF-8 systemd.set_credential=firstboot.timezone:UTC"
FIRSTBOOT="$FIRSTBOOT systemd.set_credential=firstboot.keymap:us"
FIRSTBOOT="$FIRSTBOOT systemd.set_credential=passwd.plaintext-password.root:CHANGE-ME"

~/natemu/target/release/natemu --fexc ~/natemu-fexc/obj/fexc \
  --kernel vmlinuz --initrd initrd.img --disk disk.raw --net --rng \
  --ram 4G --vcpus 4 --serial-stdin --speculation-barriers \
  --cmdline "$ROOT ro console=ttyS0 consoleblank=0 $FIRSTBOOT"
```

Later boots need only `--cmdline "$ROOT ro console=ttyS0 consoleblank=0"`.
`--speculation-barriers` avoids a large slowdown (see [Performance](#performance-and-cpu-vulnerability-mitigations)).
Log in as `root` on the console. To use the extra disk space, run
`apt install cloud-guest-utils && growpart /dev/vda 1 && resize2fs /dev/vda1` once inside the guest.

This is the build natemu has been tested with. Newer builds should work the same way, since nothing
above depends on the build. They are listed at <https://cloud.debian.org/images/cloud/trixie/>.

## Running a small RAM-only system

`tools/play` builds a minimal Debian 13 system (bash, Python, gcc, curl, tmux and more) that runs
entirely from an initramfs. It boots in seconds and needs no disk. Building it needs an x86-64 Linux
machine you can reach over ssh (no root needed; it needs `apt-get`, `dpkg`, `curl`, `python3`,
`zstd`, `kmod` and internet access):

```sh
tools/play/build.sh user@linux-host            # about 5 minutes; writes to ~/Library/Caches/natemu/play

P=~/Library/Caches/natemu/play
target/release/natemu --fexc ~/natemu-fexc/obj/fexc \
  --kernel $P/bzImage --initrd $P/play.cpio.zst \
  --cmdline 'console=ttyS0 mitigations=off nokaslr' \
  --ram 2G --vcpus 2 --serial-stdin
```

You get a root shell with no login. See [tools/play/README.md](tools/play/README.md) for details.

## Using natemu

With `--serial-stdin` your terminal is the guest's console. **Ctrl-A x** quits natemu at any time,
and Ctrl-C goes to the guest. `poweroff` inside the guest shuts it down cleanly.

The first boot translates everything and takes a few seconds longer. Later boots reuse the
translation cache in `~/Library/Caches/natemu/fexc` (`--compile-cache off` disables it).

| Option | Effect |
| --- | --- |
| `--net` | IPv4 network over user-mode NAT. The guest gets `10.0.2.15` by DHCP. Guest access to the Mac's `localhost` is off by default; its LAN address remains reachable. |
| `--net-forward HOST:GUEST` | Forward a TCP port from the Mac's `127.0.0.1` to the guest. Repeat for more ports; use `udp:HOST:GUEST` for UDP. Requires `--net`. |
| `--net-host-loopback` | Let the guest reach the Mac's `127.0.0.1:PORT` through `10.0.2.2:PORT`. Requires `--net`; also required for UDP forwarding. |
| `--disk FILE` | A raw disk image, seen as `/dev/vda`. Only one natemu can use a disk at a time. |
| `--rng` | A hardware random-number device. |

`target/release/natemu --help` lists every option. natemu exits 0 when the guest powers off,
2 for any other stop (reset, crash, Ctrl-A x) and 1 on a host error.

### Host–guest networking

To reach a guest service from the Mac, add `--net --net-forward 8080:8000` to the launch
command. For example, start an HTTP server inside the guest:

```sh
python3 -m http.server 8000 --bind 0.0.0.0
```

Then run `curl http://127.0.0.1:8080/` on the Mac. Forwards bind only the Mac's `127.0.0.1`
and target `10.0.2.15`; guest services must listen on that address or all interfaces, not
just guest localhost. With an SSH server and account configured in the guest, use
`--net-forward 2222:22` and connect with `ssh -p 2222 user@127.0.0.1`.

TCP forwarding works with host-loopback access disabled. For UDP, add both
`--net-host-loopback --net-forward udp:19000:9000` alongside `--net`. The explicit flag is
required because libslirp otherwise blocks forwarded UDP replies. It also grants broad access
to the Mac's IPv4 localhost services, including ones that trust local connections, by using
`10.0.2.2:PORT`. It does not join the guest's and Mac's loopback interfaces or limit access
to the forwarded ports.

Use different host ports for concurrent VMs; an occupied port fails the launch. UDP host
port 53 and multiple UDP forwards to the same guest port are unsupported. UDP clients must
not use source ports 53, 67 or 69. See [host–guest service details](tools/play/README.md#hostguest-services)
for the full rules and file-copy examples.

At startup, natemu prints the expected guest address, gateway/DNS, loopback setting and
installed forwards. This confirms configuration, not that the guest service is ready.

## Performance and CPU vulnerability mitigations

By default natemu reports its virtual CPU as affected by Meltdown. A Linux guest then turns on page
table isolation (PTI), which switches page tables on every system call, interrupt and fault.
natemu has no fast path for these switches yet, so PTI is by far the largest cost on a default
kernel command line. Linux's other mitigations together cost about 1%.

On the context-switch-heavy Debian workload, the default command line took more than twice as
long as `mitigations=off` ([numbers](docs/benchmarks.md#cpu-vulnerability-mitigations)).

Ways to recover the cost:

- **`--speculation-barriers` (recommended).** natemu places speculation barriers in its own guest
  page-table walk, so guest code cannot read memory it has no permission for, even speculatively.
  natemu then reports the CPU as not affected by Meltdown, and Linux skips PTI on its own. Every
  other guest mitigation stays on. natemu refuses this option on a CPU without the Arm speculation
  barrier instruction (FEAT_SB).
- **`mitigations=off` on the kernel command line.** Turns off every mitigation inside the guest.
  It is as fast as the option above, but it also removes the guest's own protection between its
  processes, so use it only when everything running in the guest is trusted. The RAM-only system
  above uses it.

## Development

```sh
cargo test --locked
python3 -B -m unittest discover -s tools -p 'test_*.py'
```

The full correctness suite (`tools/run_suite.py`) compares natemu against QEMU and needs a local
QEMU install. Its docstring lists the options.
