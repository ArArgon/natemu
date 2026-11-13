#!/bin/bash
# rootfs.sh DIR (run on the build host by build.sh): on an x86-64 Linux machine WITHOUT root, build the "play" initramfs
# DIR/out/play.cpio.zst: a minimal Debian trixie userland (the kernel is Debian 6.12 from trixie) that runs from RAM.
# Packages come from the snapshot.debian.org archive at SNAP (pinned below), so the package versions are reproducible.
# No debootstrap/chroot (they need root): packages are resolved and downloaded by a user-level apt (empty dpkg status,
# so the resolver pulls only the requested packages' Depends, no Recommends), unpacked with dpkg -x, and the few things
# maintainer scripts would have done (passwd, symlinks) are done here. Re-runnable: it wipes DIR/rootfs each time.
set -euo pipefail
SNAP=${SNAP:-20260915T000000Z}   # trixie 13.7 as of 2026-09-12
here=$(dirname "$(realpath "$0")"); mkdir -p "$1"; B=$(realpath "$1"); R=$B/rootfs
mkdir -p "$B"/{apt/lists/partial,apt/cache/archives/partial,apt/etc/preferences.d,apt/etc/apt.conf.d,out}
cd "$B"
[ -d kr ] || { apt-get download debian-archive-keyring >/dev/null; dpkg -x debian-archive-keyring_*.deb kr; rm debian-archive-keyring_*.deb; }
cat > apt/apt.conf <<EOC
Dir::State "$B/apt"; Dir::State::Lists "$B/apt/lists"; Dir::State::status "$B/apt/status"; Dir::Cache "$B/apt/cache";
Dir::Etc::SourceList "$B/apt/sources.list"; Dir::Etc::SourceParts "$B/apt/etc/none";
Dir::Etc::Preferences "$B/apt/etc/preferences"; Dir::Etc::PreferencesParts "$B/apt/etc/preferences.d";
Dir::Etc::Parts "$B/apt/etc/apt.conf.d"; APT::Architecture "amd64"; APT::Architectures { "amd64"; };
APT::Architecture-Variants "amd64"; Debug::NoLocking "true"; APT::Install-Recommends "false"; Acquire::Check-Valid-Until "false"; Acquire::Retries "5";
EOC
touch apt/status
echo "deb [signed-by=$B/kr/usr/share/keyrings/debian-archive-keyring.pgp] https://snapshot.debian.org/archive/debian/$SNAP/ trixie main" > apt/sources.list
export APT_CONFIG=$B/apt/apt.conf
apt-get update -qq
# Requested packages (the closure is whatever they Depend on). busybox-static supplies poweroff/reboot/halt (-f), the
# DHCP client (udhcpc) and fallback applets; nano is not included (vim-tiny is the editor). kmod loads the virtio
# drivers, curl and ca-certificates use the network, e2fsprogs formats and checks a virtio disk; tmux and script
# (bsdutils) give pseudo-terminals; libc-bin carries the C.UTF-8 locale tmux needs.
PKGS="bash dash coreutils procps util-linux iproute2 less vim-tiny python3-minimal gcc libc6-dev binutils make file htop
      busybox-static grep sed findutils tar gzip diffutils hostname mawk ncurses-base ncurses-bin base-files base-passwd
      kmod curl ca-certificates e2fsprogs tmux bsdutils libc-bin"
apt-get -y -qq -d install --no-install-recommends $PKGS
( cd apt/cache/archives && for d in *.deb; do echo "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)"; done | sort > "$B/out/packages.txt"
  sha256sum *.deb > "$B/out/debs.sha256" )
# Kernel modules for the virtio devices (natemu --net/--disk/--rng; QEMU virtio-*-device): from the same signed Debian
# kernel package build.sh takes bzImage from (KDEB_SHA, its SHA-256): the drivers below and what they depend on.
KVER=6.12.107+deb13-amd64; KPKG=linux-image-$KVER
rm -rf kernel
apt-get -qq download "$KPKG=6.12.107-1"
[ -z "${KDEB_SHA:-}" ] || echo "$KDEB_SHA  $(ls ${KPKG}_*.deb)" | sha256sum -c --quiet -
dpkg-deb --fsys-tarfile ${KPKG}_*.deb | (mkdir kernel && tar -x -C kernel ./usr/lib/modules); rm ${KPKG}_*.deb
KM=$B/kernel/usr/lib/modules/$KVER
# crc32c_generic: ext4's soft dependency (it asks for "crc32c" at mount; modinfo's depends omits soft ones).
MODULES="virtio_mmio virtio_net virtio_blk virtio_rng ext4 crc32c_generic"

rm -rf "$R"; mkdir -p "$R"
for d in apt/cache/archives/*.deb; do dpkg -x "$d" "$R"; done
# Merge any legacy top-level dirs into /usr (trixie packages are merged-/usr already, so normally nothing), then link.
for l in bin sbin lib lib64; do
    if [ -d "$R/$l" ] && [ ! -L "$R/$l" ]; then mkdir -p "$R/usr/$l"; cp -a "$R/$l/." "$R/usr/$l/"; rm -rf "$R/$l"; fi
    ln -sfn usr/$l "$R/$l"
done
# The drivers' dependency closure, then depmod (the build host's, on DIR/rootfs) for kmod's modprobe.
want=$MODULES; have=""
while set -- $want; [ $# -gt 0 ]; do
    m=$1; shift; want="$*"
    case " $have " in *" $m "*) continue;; esac
    f=$(find "$KM/kernel" \( -name "$m.ko*" -o -name "${m//_/-}.ko*" \) | head -1)
    [ -n "$f" ] || { echo "module $m missing from $KPKG" >&2; exit 1; }
    have="$have $m"; rel=${f#$KM/}
    mkdir -p "$R/usr/lib/modules/$KVER/$(dirname "$rel")"; cp "$f" "$R/usr/lib/modules/$KVER/$rel"
    want="$want $(modinfo -F depends "$f" | tr ',' ' ')"
done
cp "$KM"/modules.order "$KM"/modules.builtin "$KM"/modules.builtin.modinfo "$R/usr/lib/modules/$KVER/"
depmod -b "$R" "$KVER"
echo "modules:$have"
rm -rf "$B/kernel"   # the build host's /tmp may be small
cd "$R"
mkdir -p proc sys dev tmp run root etc var/tmp var/log mnt/disk etc/udhcpc
chmod 1777 tmp var/tmp; chmod 700 root
# Drop what a RAM-only play system does not read.
rm -rf usr/share/doc usr/share/man usr/share/info usr/share/locale usr/share/lintian usr/share/bug usr/share/menu var/lib/apt
# What maintainer scripts would do.
[ -e usr/bin/python3 ] || ln -s python3.13 usr/bin/python3
ln -sf x86_64-linux-gnu-gcc usr/bin/cc 2>/dev/null || true
[ -e usr/bin/gcc ] || ln -s x86_64-linux-gnu-gcc usr/bin/gcc
ln -sf vim.tiny usr/bin/vi; ln -sf vim.tiny usr/bin/view; ln -sf vim.tiny usr/bin/editor
ln -sf dash usr/bin/sh
cp usr/share/base-passwd/passwd.master etc/passwd; cp usr/share/base-passwd/group.master etc/group
sed -i 's/^root:[^:]*:/root::/' etc/passwd 2>/dev/null; sed -i 's/^root:x:/root::/' etc/passwd
printf 'root::19000:0:99999:7:::\n' > etc/shadow; chmod 640 etc/shadow
# What ca-certificates' maintainer script would do: the bundle curl reads.
mkdir -p etc/ssl/certs; cat usr/share/ca-certificates/*/*.crt > etc/ssl/certs/ca-certificates.crt
# busybox udhcpc's lease script (init runs `udhcpc -s` with it): address, default route, resolv.conf.
cat > etc/udhcpc/default.script <<'EOS'
#!/bin/sh
case "$1" in
deconfig) ip -4 addr flush dev "$interface"; ip link set "$interface" up ;;
bound|renew)
    ip -4 addr flush dev "$interface"; ip addr add "$ip/${mask:-24}" dev "$interface"
    for r in $router; do ip route replace default via "$r" dev "$interface"; break; done
    : > /etc/resolv.conf
    [ -n "$domain" ] && echo "search $domain" >> /etc/resolv.conf
    for d in $dns; do echo "nameserver $d" >> /etc/resolv.conf; done ;;
esac
EOS
chmod 755 etc/udhcpc/default.script
# busybox init's shutdown action (poweroff/reboot, Ctrl-Alt-Del): flush the virtio disk and unmount it, or at least
# remount it read-only if something still holds it. init then stops the remaining processes and syncs again.
mkdir -p etc/init.d
cat > etc/init.d/rcK <<'EOS'
#!/bin/sh
sync
if grep -q ' /mnt/disk ' /proc/mounts; then
    umount /mnt/disk 2>/dev/null || mount -o remount,ro /mnt/disk 2>/dev/null
fi
sync
EOS
chmod 755 etc/init.d/rcK
echo natemu-play > etc/hostname
printf '127.0.0.1 localhost\n127.0.1.1 natemu-play\n' > etc/hosts
printf '/bin/bash\n/bin/sh\n/bin/dash\n' > etc/shells
printf 'NAME="Debian GNU/Linux"\nVERSION_ID="13"\nID=debian\nPRETTY_NAME="Debian GNU/Linux 13 (trixie), natemu play initramfs"\n' > etc/os-release
cat > etc/profile <<'EOP'
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root LANG=C.UTF-8 TERM=${TERM:-xterm-256color}
PS1='\u@\h:\w\$ '
alias ls='ls --color=auto' ll='ls -l'
# A serial console carries no window size: ask the terminal (move the cursor far down-right, read back its position;
# the host terminal answers through natemu's --serial-stdin). Run `resize` again after resizing the window.
resize() {
    [ -t 0 ] && [ -z "$TMUX" ] || return 0
    local saved r c
    saved=$(stty -g)
    stty raw -echo
    printf '\0337\033[999;999H\033[6n\0338' > /dev/tty
    IFS='[;' read -r -s -t 1 -d R _ r c < /dev/tty
    stty "$saved"
    [ "${r:-0}" -gt 0 ] 2>/dev/null && [ "${c:-0}" -gt 0 ] && stty rows "$r" cols "$c"
}
case $(tty) in /dev/ttyS*|/dev/console) resize;; esac
EOP
# tmux: several shells on pseudo-terminals over the one serial console (it needs the UTF-8 locale: C.UTF-8, glibc's).
[ -d usr/lib/locale/C.utf8 ] || { echo "C.UTF-8 locale missing" >&2; exit 1; }
printf 'set -g default-terminal tmux-256color\nset -g escape-time 10\n' > etc/tmux.conf
cat > root/.bash_profile <<'EOP'
. /etc/profile
. ~/.bashrc
EOP
cat > root/.bashrc <<'EOP'
PS1='\u@\h:\w\$ '
EOP
echo 'set enable-bracketed-paste off' > root/.inputrc
cat > etc/motd <<'EOP'

  natemu play: Debian trixie, running from RAM (changes outside /mnt/disk are lost at power off).
  bash, coreutils, procps, util-linux, iproute2, less, vi, python3, gcc, make, file, htop, curl, e2fsprogs.
  Network (natemu --net): eth0 by DHCP.  Disk (natemu --disk FILE): /dev/vda, mounted on /mnt/disk if ext4
  (a new one: mkfs.ext4 /dev/vda && mount /dev/vda /mnt/disk).
  More shells: tmux.  After resizing the terminal window: resize.
  Leave with: poweroff      Restart with: reboot      (both unmount /mnt/disk first; exit restarts the shell)

EOP
# Busybox applets for names no real tool owns (poweroff, reboot, halt, ...).
for a in $(usr/bin/busybox --list); do [ -e "usr/bin/$a" ] || [ -L "usr/bin/$a" ] || ln -s busybox "usr/bin/$a"; done
cp "$here/init" init; chmod 755 init
cd "$B"
python3 "$here/mkcpio.py" "$R" out/play.cpio
zstd -q -19 -T0 -f --rm out/play.cpio -o out/play.cpio.zst
du -sb "$R" | sed 's/^/rootfs bytes: /'
ls -la out/play.cpio.zst; sha256sum out/play.cpio.zst
