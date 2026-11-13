# play: an interactive Debian userland that runs from RAM

A minimal Debian 13 (trixie) initramfs for using the emulated machine interactively over the serial console: bash with
job control, coreutils, procps, util-linux, iproute2, less, vi (vim-tiny), python3, gcc and libc6-dev, make, binutils,
file, htop, curl (with CA certificates), e2fsprogs, kmod, tmux, script (bsdutils), busybox. 297 MB unpacked, 80 MB compressed (zstd), needs about
1 GiB of guest RAM unpacked; boot with `-m 2048`. Kernel: Debian 6.12.107-1 (the one natemu boots).

It runs from RAM; virtio devices are optional. The image carries the kernel's own virtio_mmio, virtio_net,
virtio_blk, virtio-rng and ext4 modules (and their dependencies), and `/init` loads them:
- network (natemu `--net`): eth0 comes up by DHCP (busybox udhcpc; with natemu: 10.0.2.15, gateway 10.0.2.2, DNS
  10.0.2.3); `curl https://...` works. The host's loopback is not reachable from the guest: use its LAN address.
- disk (natemu `--disk FILE`): `/dev/vda`, mounted on `/mnt/disk` if it holds ext4. A new disk: make the file on the
  Mac (`mkfile -n 1g disk.raw`), then in the guest `mkfs.ext4 /dev/vda && mount /dev/vda /mnt/disk`; it is mounted at
  the next boot. `poweroff` and `reboot` sync and unmount it first (busybox init's shutdown action,
  `/etc/init.d/rcK`); `poweroff -f` skips that and can lose data the guest had not written yet.
- entropy (natemu `--rng`): virtio-rng.

## Build

```sh
tools/play/build.sh [HOST] [OUTDIR]     # defaults: ubuntu@192.168.12.11, ~/Library/Caches/natemu/play
```

HOST is any x86-64 Linux machine reachable by ssh (no root; needs apt-get, dpkg, curl, python3, zstd, and kmod's
modinfo and depmod; about 1 GB free in its /tmp). About 5 minutes. The kernel modules come from the same signed kernel
package as `bzImage` (its SHA-256 is checked on HOST too); the build directory on HOST is removed even on failure.
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
  --cmdline 'console=ttyS0 mitigations=off nokaslr' --ram 2G --vcpus 2 --serial-stdin \
  --net --disk ~/play-disk.raw     # both optional
```

`--runtime FILE` names the resident; left out, it is the one built beside the binary
(`target/aarch64-unknown-none/release/natemu-runtime`). `target/release/natemu --help` lists every option.
`--serial-stdin` makes the terminal the guest's console (raw mode; Ctrl-C goes to the guest; Ctrl-A x quits natemu,
Ctrl-A Ctrl-A sends Ctrl-A). The first boot compiles everything (a few seconds); later boots reuse the compile cache
(`--compile-cache off` disables it). TERM is `xterm-256color` (`term=NAME` on the kernel command line overrides it).
A serial line carries no window size, so the login shell asks the terminal for it (a cursor-position query) and sets
it; run `resize` after resizing the window. `tmux` gives more shells, on pseudo-terminals, over the one console.

The stub's command line is named since November 5 (clap). Scripts kept as evidence in natemu-raw use the older
positional form, `natemu RUNTIME FEXC BZIMAGE [OPTIONS]`, which the stub no longer accepts: run them against a stub built
before that change, or rewrite the first three arguments as `--runtime`, `--fexc` and `--kernel`.

You get a root shell on ttyS0 (no login, hostname natemu-play). PID 1 is busybox init (`/etc/inittab`, written by
`/init` for the console): it restarts the shell when you type `exit`, reaps orphaned processes, and on `poweroff` or
`reboot` runs `/etc/init.d/rcK` before stopping everything. QEMU check: `qemu-system-x86_64 -nodefaults -machine pc,hpet=off,i8042=off,vmport=off -accel tcg,thread=multi
-cpu <model> -m 2048 -smp 2 -kernel OUTDIR/bzImage -initrd OUTDIR/play.cpio.zst -append 'console=ttyS0 mitigations=off
nokaslr' -display none -serial stdio`; add `-netdev user,id=n -device virtio-net-pci,netdev=n` and
`-drive file=disk.raw,format=raw,if=virtio` for the network and disk (virtio-pci is built into the kernel).

Files: `build.sh` (runs on the Mac), `rootfs.sh` (runs on HOST), `init` (setup, then `exec busybox init`), `mkcpio.py` (deterministic newc cpio, adds
`/dev/console`).
