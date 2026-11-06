# play: an interactive Debian userland that runs from RAM

A minimal Debian 13 (trixie) initramfs (no disk, no network) for using the emulated machine interactively over the
serial console: bash with job control, coreutils, procps, util-linux, iproute2, less, vi (vim-tiny), python3, gcc and
libc6-dev, make, binutils, file, htop, busybox (`poweroff -f`, `reboot -f`). 282 MB unpacked, 74 MB compressed (zstd),
needs about 1 GiB of guest RAM unpacked; boot with `-m 2048`. Kernel: Debian 6.12.107-1 (the one natemu boots).

## Build

```sh
tools/play/build.sh [HOST] [OUTDIR]     # defaults: ubuntu@192.168.12.11, ~/Library/Caches/natemu/play
```

HOST is any x86-64 Linux machine reachable by ssh (no root; needs apt-get, dpkg, curl, python3, zstd). About 5 minutes.
Output in OUTDIR: `bzImage` (the Debian kernel, fetched by exact version and checked against SHA-256s pinned in
`build.sh`), `play.cpio.zst`, `packages.txt` (every package and version), `SHA256SUMS`. Packages come from the
snapshot.debian.org archive date pinned in `rootfs.sh` (`SNAP`), so a rebuild gives the same `play.cpio.zst`.

## Boot

Kernel command line: `console=ttyS0 mitigations=off nokaslr` (no `rdinit=` needed: `/init`). With natemu, from the
repository root (build once: `cargo build --release -p natemu-cli` (it fetches clap from crates.io the first time; `--offline` builds work after that), `cargo build --release -p natemu-runtime --target
aarch64-unknown-none`, then `codesign --force --sign - --entitlements crates/host/entitlements.plist
target/release/natemu`; FEXC is a compiler built by `tools/fexc/build.sh`):

```sh
P=~/Library/Caches/natemu/play
target/release/natemu --fexc "$FEXC" --kernel $P/bzImage --initrd $P/play.cpio.zst \
  --cmdline 'console=ttyS0 mitigations=off nokaslr' --ram 2G --vcpus 2 --serial-stdin
```

`--runtime FILE` names the resident; left out, it is the one built beside the binary
(`target/aarch64-unknown-none/release/natemu-runtime`). `target/release/natemu --help` lists every option.
`--serial-stdin` makes the terminal the guest's console (raw mode; Ctrl-C goes to the guest; Ctrl-A x quits natemu,
Ctrl-A Ctrl-A sends Ctrl-A). The first boot compiles everything (a few seconds); later boots reuse the compile cache
(`--compile-cache off` disables it). Terminal size is not passed on: run `stty rows R cols C` for full-screen
programs.

The stub's command line is named since November 5 (clap). Scripts kept as evidence in natemu-raw use the older
positional form, `natemu RUNTIME FEXC BZIMAGE [OPTIONS]`, which the stub no longer accepts: run them against a stub built
before that change, or rewrite the first three arguments as `--runtime`, `--fexc` and `--kernel`.

You get a root shell on ttyS0 (no login, hostname natemu-play). Leave with `poweroff -f`; typing `exit` restarts the
shell. QEMU check: `qemu-system-x86_64 -nodefaults -machine pc,hpet=off,i8042=off,vmport=off -accel tcg,thread=multi
-cpu <model> -m 2048 -smp 2 -kernel OUTDIR/bzImage -initrd OUTDIR/play.cpio.zst -append 'console=ttyS0 mitigations=off
nokaslr' -display none -serial stdio`.

Files: `build.sh` (runs on the Mac), `rootfs.sh` (runs on HOST), `init`, `mkcpio.py` (deterministic newc cpio, adds
`/dev/console`).
