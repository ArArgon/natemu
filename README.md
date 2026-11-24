# natemu

natemu runs x86-64 Linux on Apple Silicon Macs, faster than QEMU TCG.

natemu runs the guest in its own hardware VM (Hypervisor.framework), translates its x86-64 code to ARM64 and lets the
Mac's hardware do most of the work: guest page tables become real page tables, Apple Silicon's x86 memory-ordering mode
keeps multithreaded code correct without extra barriers, and system calls, page faults and interrupts are handled inside
the VM. QEMU TCG does all of these in software.

On an M4 Mac, natemu runs 12 benchmark programs about 5.8× faster than QEMU TCG (1.5× to 41× per program), and a Debian
package-install and build workload 1.7× faster. See [benchmarks](docs/benchmarks.md).

The x86 translator is [FEX](https://github.com/FEX-Emu/FEX)'s compiler, extended to run whole systems; the changes live in
[natemu-fex](https://github.com/ArArgon/natemu-fex).

## Status: experimental

natemu boots the unmodified Debian 13 cloud image with networking and a disk. It is tested on an M4 Mac running macOS 26
(an M2 passes the floating-point and networking checks); other distributions and kernels are untested. Not yet supported:

- **Booting:** no BIOS or UEFI. natemu starts a Linux kernel directly, so installer ISOs and bootloaders do not work.
- **Devices:** serial console, network, disk images (raw or qcow2; several, some read-only, from a `--config` file, see
  [docs/configuration.md](docs/configuration.md)) and a random-number device only. A launch with a `--config` file uses a PCI
  bus by default; one without it uses MMIO devices (PCI with `--virtio-transport pci`). No display, keyboard, sound, USB or
  shared folders.
- **Networking:** IPv4 only, through NAT, with port forwarding. No IPv6 or bridged networking.
- **CPU:** 64-bit and 32-bit (i386) Linux programs, including programs that switch between the two. Not 16-bit
  programs, nor 32-bit ones that use the old segmented memory model (16-bit Windows programs under Wine, DOS
  emulators). No AVX-512. x87 floating point uses 64-bit rather than 80-bit precision.
- **Performance:** Linux's default Meltdown protection is slow under natemu; see
  [Performance](#performance-and-cpu-vulnerability-mitigations) for the fix.

## Requirements

- An Apple Silicon Mac running macOS 15 or later; tested on M4 and M2 running macOS 26. natemu refuses to start on a Mac
  that lacks a hardware feature it needs.
- Xcode Command Line Tools (`xcode-select --install`)
- [rustup](https://rustup.rs). The repository pins the Rust version, and rustup installs it automatically.
- [Homebrew](https://brew.sh) packages: `brew install cmake ninja pkg-config libslirp e2fsprogs`
- Python 3 and git

### Floating point on M1 and M2

On M1 and M2, a few rare floating-point edge cases come out slightly differently from real x86 by default. Ordinary
programs do not notice. If you need results that match x86 bit for bit, use `--fp accurate`; it can make math-heavy
programs several times slower, and 32-bit programs that do their arithmetic in the old x87 unit more than a hundred times
slower. On M4 these cases are exact at no cost. Details:
[docs/floating-point.md](docs/floating-point.md).

## Setup

### 1. Get the source

```sh
git clone https://github.com/ArArgon/natemu.git
cd natemu
git submodule update --init
```

Check your machine (this installs nothing and starts no VM):

```sh
python3 -B tools/check_setup.py
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

The first boot translates everything and takes a few seconds longer; later boots reuse a cache in
`~/Library/Caches/natemu/fexc` (`--compile-cache off` disables it).

| Option | Effect |
| --- | --- |
| `--net` | IPv4 network over user-mode NAT. The guest gets `10.0.2.15` by DHCP. Guest access to the Mac's `localhost` is off by default; its LAN address remains reachable. |
| `--net-forward HOST:GUEST` | Forward a TCP port from the Mac's `127.0.0.1` to the guest. Repeat for more ports; use `udp:HOST:GUEST` for UDP. Requires `--net`. |
| `--net-host-loopback` | Let the guest reach the Mac's `127.0.0.1:PORT` through `10.0.2.2:PORT`. Requires `--net`; also required for UDP forwarding. |
| `--disk FILE` | A raw or qcow2 disk image, seen as `/dev/vda`. Only one natemu can use a disk at a time. |
| `--rng` | A hardware random-number device. |

At startup natemu prints a few `natemu: ...` lines describing how it will run on this Mac: the CPU it shows the guest,
the floating-point mode, which hardware speed-ups it uses, and where it caches translated code.

A launch can also be described in a TOML file (`--config FILE`), which adds several disks and
read-only disks; see [docs/configuration.md](docs/configuration.md).

`target/release/natemu --help` lists every option. natemu exits 0 when the guest powers off,
2 for any other stop (reset, crash, Ctrl-A x) and 1 on a host error.

When natemu stops for any reason other than a power-off, a reboot or Ctrl-C, it prints on standard error where the processor was: the
address of the guest instruction, its bytes and the registers. If the guest seems stuck, press Ctrl-T in the terminal
(with `--serial-stdin` the keys go to the guest: run `kill -INFO` on natemu's process instead) to print the same for every processor while the guest keeps running. A guest
that loops forever without natemu's involvement for a minute ends the run with that report. If natemu itself waits that
long (for example because its output is paused with Ctrl-S or piped into a program that stopped reading), it prints the
report once and keeps running.

### Host–guest networking

To reach a service in the guest from the Mac, forward a port: `--net --net-forward 8080:8000` makes the guest's port 8000
available at `127.0.0.1:8080` on the Mac. The guest service must listen on all interfaces (for example
`python3 -m http.server 8000 --bind 0.0.0.0`), not only on its own localhost. For SSH, use `--net-forward 2222:22` and
`ssh -p 2222 user@127.0.0.1`.

`--net-host-loopback` lets the guest reach services on the Mac's localhost at `10.0.2.2`. Turn it on only if you trust
the guest: it exposes every localhost service on the Mac. UDP forwarding needs it too.

Each running VM needs its own host ports. The full rules, UDP limits and file-copy examples are in
[tools/play/README.md](tools/play/README.md#hostguest-services).

## Performance and CPU vulnerability mitigations

Linux protects itself from the Meltdown CPU flaw by switching page tables on every system call and interrupt (PTI).
natemu has no fast path for that yet, so with a default kernel command line a busy Debian guest runs more than twice as
slowly ([numbers](docs/benchmarks.md#cpu-vulnerability-mitigations)). Two ways to avoid it:

- **`--speculation-barriers` (recommended).** natemu protects guest memory itself, tells the guest its CPU is not
  affected, and Linux skips PTI. All other protections stay on. natemu refuses this option on a Mac that lacks the CPU
  feature it needs.
- **`mitigations=off` on the kernel command line.** Equally fast, but it turns off all of the guest's own protections, so
  use it only when everything in the guest is trusted. The RAM-only system above uses it.

## Development

```sh
tools/natemu-test run          # the tests your change concerns
tools/natemu-test run --gate   # all of them
```

Both include the Rust and Python unit tests, so there is no need to run `cargo test` separately. The
tests need two more Homebrew packages: `brew install nasm container`. `nasm` assembles small x86 test programs, and
`container` is Apple's tool for running Linux containers, which the tests use to build x86 Linux files. Start it once
after each restart of the Mac with `container system start`. The first run on a Mac downloads and builds what the tests
need, which takes a few minutes. [Testing](docs/testing.md) explains the rest.
