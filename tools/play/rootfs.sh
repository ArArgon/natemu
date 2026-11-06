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
# Requested packages (the closure is whatever they Depend on). busybox-static supplies poweroff/reboot/halt (-f) and
# fallback applets; nano is not included (vim-tiny is the editor).
PKGS="bash dash coreutils procps util-linux iproute2 less vim-tiny python3-minimal gcc libc6-dev binutils make file htop
      busybox-static grep sed findutils tar gzip diffutils hostname mawk ncurses-base ncurses-bin base-files base-passwd"
apt-get -y -qq -d install --no-install-recommends $PKGS
( cd apt/cache/archives && for d in *.deb; do echo "$(dpkg-deb -f "$d" Package)=$(dpkg-deb -f "$d" Version)"; done | sort > "$B/out/packages.txt"
  sha256sum *.deb > "$B/out/debs.sha256" )

rm -rf "$R"; mkdir -p "$R"
for d in apt/cache/archives/*.deb; do dpkg -x "$d" "$R"; done
# Merge any legacy top-level dirs into /usr (trixie packages are merged-/usr already, so normally nothing), then link.
for l in bin sbin lib lib64; do
    if [ -d "$R/$l" ] && [ ! -L "$R/$l" ]; then mkdir -p "$R/usr/$l"; cp -a "$R/$l/." "$R/usr/$l/"; rm -rf "$R/$l"; fi
    ln -sfn usr/$l "$R/$l"
done
cd "$R"
mkdir -p proc sys dev tmp run root etc var/tmp var/log mnt
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
echo natemu-play > etc/hostname
printf '127.0.0.1 localhost\n127.0.1.1 natemu-play\n' > etc/hosts
printf '/bin/bash\n/bin/sh\n/bin/dash\n' > etc/shells
printf 'NAME="Debian GNU/Linux"\nVERSION_ID="13"\nID=debian\nPRETTY_NAME="Debian GNU/Linux 13 (trixie), natemu play initramfs"\n' > etc/os-release
cat > etc/profile <<'EOP'
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root LANG=C TERM=${TERM:-vt100}
PS1='\u@\h:\w\$ '
alias ls='ls --color=auto' ll='ls -l'
EOP
cat > root/.bash_profile <<'EOP'
. /etc/profile
. ~/.bashrc
EOP
cat > root/.bashrc <<'EOP'
PS1='\u@\h:\w\$ '
EOP
echo 'set enable-bracketed-paste off' > root/.inputrc
cat > etc/motd <<'EOP'

  natemu play: Debian trixie, running entirely from RAM (changes are lost at power off).
  bash, coreutils, procps, util-linux, iproute2, less, vi, python3, gcc, make, file, htop.
  Leave with: poweroff -f      Restart with: reboot -f

EOP
# Busybox applets for names no real tool owns (poweroff, reboot, halt, ...).
for a in $(usr/bin/busybox --list); do [ -e "usr/bin/$a" ] || [ -L "usr/bin/$a" ] || ln -s busybox "usr/bin/$a"; done
cp "$here/init" init; chmod 755 init
cd "$B"
python3 "$here/mkcpio.py" "$R" out/play.cpio
zstd -q -19 -T0 -f --rm out/play.cpio -o out/play.cpio.zst
du -sb "$R" | sed 's/^/rootfs bytes: /'
ls -la out/play.cpio.zst; sha256sum out/play.cpio.zst
