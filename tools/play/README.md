# play: an interactive Debian userland that runs from RAM

A minimal Debian 13 (trixie) initramfs for using the emulated machine interactively over the serial console: bash with
job control, coreutils, procps, util-linux, iproute2, less, vi (vim-tiny), python3, gcc and libc6-dev, make, binutils,
file, htop, curl (with CA certificates), e2fsprogs, kmod, tmux, script (bsdutils), busybox. 297 MB unpacked, 80 MB compressed (zstd), needs about
1 GiB of guest RAM unpacked; boot with `-m 2048`. Kernel: Debian 6.12.107-1 (the one natemu boots).

It runs from RAM; virtio devices are optional. The image carries the kernel's own virtio_mmio, virtio_net,
virtio_blk, virtio-rng and ext4 modules (and their dependencies), and `/init` loads them:
- network (natemu `--net`): eth0 comes up by DHCP (busybox udhcpc; with natemu: 10.0.2.15, gateway 10.0.2.2, DNS
  10.0.2.3); `curl https://...` works. Host-loopback access is off by default; use the host's LAN address or explicitly
  add `--net-host-loopback`. Static localhost forwards are described below.
- disk (natemu `--disk FILE`): `/dev/vda`, mounted on `/mnt/disk` if it holds ext4. A new disk: make the file on the
  Mac (`mkfile -n 1g disk.raw`), then in the guest `mkfs.ext4 /dev/vda && mount /dev/vda /mnt/disk`; it is mounted at
  the next boot. `poweroff` and `reboot` sync and unmount it first (busybox init's shutdown action,
  `/etc/init.d/rcK`); `poweroff -f` skips that and can lose data the guest had not written yet.
- entropy (natemu `--rng`): virtio-rng.

## Build

```sh
tools/play/build.sh HOST [OUTDIR]       # HOST: user@ an x86-64 Linux machine; OUTDIR default ~/Library/Caches/natemu/play
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

## Host–guest services

Add `--net --net-forward 8080:8000` to the launch command, then start a guest HTTP
server with `python3 -m http.server 8000 --bind 0.0.0.0`. On the Mac, run
`curl http://127.0.0.1:8080/`. Every forward binds only the Mac's `127.0.0.1` and
targets only the guest's expected DHCP address, `10.0.2.15`; the guest service
must bind that NIC address or all interfaces, not just guest localhost.

Repeat `--net-forward` for more services. `2222:22` and `tcp:2222:22` mean the
same thing; `--net-host-loopback --net-forward udp:19000:9000` enables a UDP
forward. UDP requires this explicit opt-in because libslirp otherwise blocks its
localhost replies; it grants the broader localhost access described below.
Ports are decimal 1–65535, except UDP host port 53, which is unsupported. TCP and UDP
may share a host port, but duplicate listeners for the same protocol are refused.
An occupied port fails the launch; each concurrently running VM needs its own
host ports. TCP forwarding works with host-loopback access off. UDP rules must
target distinct guest ports; TCP rules may reuse guest ports, and TCP/UDP may
share one. UDP clients must not use source ports 53, 67 or 69, which libslirp
handles as DNS, BOOTP or TFTP on the reply path. UDP guest port 53 and host ports
67/69 remain valid.

For a guest with an SSH server and a configured account, add
`--net-forward 2222:22` and use these Mac commands:

```sh
ssh -p 2222 user@127.0.0.1
scp -P 2222 local.txt user@127.0.0.1:/path/
rsync -av -e 'ssh -p 2222' local-dir/ user@127.0.0.1:/path/
```

The play image does not include an SSH server. SSH/SFTP/rsync require the
corresponding guest software and configuration; forwarding does not provision it.

To reach a Mac service bound to localhost from the guest, launch with
`--net --net-host-loopback` and address it as `10.0.2.2:PORT` in the guest, for
example `curl http://10.0.2.2:8000/`. This grants broad access to the Mac's localhost
services, including ones that rely on local connections instead of authentication.
It is not a per-port permission or a host-isolation guarantee. Default-off still
allows existing access to the Mac's LAN address.

Both options require `--net`. After successful preparation, stderr shows the
expected DHCP address, gateway/DNS, loopback policy and installed forwards with a
`natemu-net:` prefix. This does not mean the guest has DHCP or a service is ready.
Custom addresses/subnets, automatic ports, live rule changes and IPv6 are unsupported.

## Compatibility and image details

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
