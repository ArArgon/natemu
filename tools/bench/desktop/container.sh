#!/bin/bash
# container.sh: the desktop profile's guest (debug cases desktop-kde-*, tests/bench/run_desktop.py): Debian 13 with a
# KDE Plasma 6 Wayland session (kwin-wayland, plasma-workspace, plasma-desktop, no recommends), Konsole, Dolphin,
# Chromium, Firefox ESR, Mesa's llvmpipe, systemd, perf, the dbgsym symbols of the libraries a desktop's time goes to,
# and WebKit's Speedometer 3.1 at /sp, as an ext4 disk image, with the initramfs that mounts it. testkit runs it in the
# amd64 Debian builder (testkit/inputs.py:desktop_inputs); /recipe is this folder (read-only), /work the output:
#   disk.raw.zst   the root file system (ext4, the whole disk, no partition table: root=/dev/vda), zstd-compressed
#   initrd.img     initramfs-tools' initramfs for the kernel the benchmarks boot (6.12.107+deb13-amd64, MODULES=most),
#                  with an init-bottom script that copies /de-harness (the runner appends it) to the root's /run
#   elves.cpio.zst the image's ELF files (and its /usr/lib/debug symbol files), for tests/bench/sample_report.py --image
#   packages.txt, versions.txt
#
# The packages come from Debian's archive at a fixed snapshot.debian.org date (SNAP, the bench image's) and the debug
# symbols from its debian-debug archive at the same date; apt checks both archives' signatures. The kernel package is the
# one the benchmarks boot (tests/resources.toml debian-kernel: the same .deb, its sha256 pinned here), installed for its
# modules and its initramfs. packages.txt pins the installed packages after the build's own tools are purged; a build
# whose list differs stops; without packages.txt (re-pinning) the list is written to /work/packages.txt and the build
# stops: check it and copy it here.
#
# The debug files keep their symbol tables only (their DWARF is removed): sample_symbols.py and the guest's perf name
# functions from .symtab, and the DWARF would be most of the image. What the guest does at boot (the session, the
# workload) is not here: tests/bench/run_desktop.py appends it to the initramfs as /de-harness, and this image's units
# run it from /run/de-harness, so the workload changes without a rebuild.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SNAP=20261120T000000Z
SP_COMMIT=1386415be8fef2f6b6bbdbe1828872471c5d802a   # WebKit/Speedometer release/3.1, as tools/bench/speedometer
KVER=6.12.107+deb13-amd64
KDEB=linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb
KDEB_SHA=7794643cf4560de2a7e3b069e8ca8511ad2a568d652ea285f0fe051f79bc9e99            # = tests/resources.toml debian-kernel
KDEB_URL=http://snapshot.debian.org/archive/debian/20260915T000000Z/pool/main/l/linux-signed-amd64/$KDEB
DISK_GIB=16
KR=/usr/share/keyrings/debian-archive-keyring.gpg
[ -f "$KR" ] || KR=/usr/share/keyrings/debian-archive-keyring.pgp
rm -f /etc/apt/sources.list.d/* /etc/apt/sources.list
cat > /etc/apt/sources.list <<EOF
deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian/$SNAP/ trixie main
deb [signed-by=$KR check-valid-until=no] http://snapshot.debian.org/archive/debian-debug/$SNAP/ trixie-debug main
EOF
apt-get -o Acquire::Retries=5 update -qq
APT="apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends"

# The desktop, its apps, the browsers, llvmpipe, systemd and the tools the guest's harness uses.
DESKTOP="systemd systemd-sysv udev dbus dbus-user-session kmod initramfs-tools linux-base procps psmisc
  kwin-wayland xwayland plasma-workspace plasma-desktop breeze konsole dolphin chromium firefox-esr
  libgl1-mesa-dri libegl-mesa0 libgbm1 fonts-dejavu-core fonts-noto-core linux-perf python3 zstd cpio e2fsprogs busybox"
$APT $DESKTOP >/dev/null
apt-mark manual $DESKTOP >/dev/null

# The benchmarks' kernel, for its modules and initramfs: MODULES=most (virtio, ext4 and the rest), zstd, and the harness
# hook. The package's postinst builds /boot/initrd.img-$KVER.
mkdir -p /etc/initramfs-tools/conf.d /etc/initramfs-tools/scripts/init-bottom
printf 'MODULES=most\nCOMPRESS=zstd\n' > /etc/initramfs-tools/conf.d/desktop.conf
printf 'virtio_pci\nvirtio_mmio\nvirtio_blk\next4\n' >> /etc/initramfs-tools/modules
cat > /etc/initramfs-tools/scripts/init-bottom/de-harness <<'EOF'
#!/bin/sh
# The desktop profile's harness (tests/bench/run_desktop.py appends /de-harness to this initramfs): to the root's /run,
# which init moves onto the root file system after this script.
case $1 in prereqs) exit 0;; esac
[ -d /de-harness ] && cp -a /de-harness /run/de-harness      # BusyBox's cp (the image has busybox: initramfs-tools includes it)
exit 0
EOF
chmod 755 /etc/initramfs-tools/scripts/init-bottom/de-harness
python3 - "$KDEB_URL" /tmp/$KDEB <<'EOF'
import sys, time, urllib.request
for attempt in range(6):
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=120) as r, open(sys.argv[2], 'wb') as f:
            while b := r.read(1 << 20): f.write(b)
        break
    except OSError as e:
        print(f'kernel download: {e}', file=sys.stderr); time.sleep(10 * (attempt + 1))
else: sys.exit(f'Failed to fetch {sys.argv[1]}')
EOF
echo "$KDEB_SHA  /tmp/$KDEB" | sha256sum -c --quiet
dpkg -i /tmp/$KDEB >/dev/null
rm /tmp/$KDEB
test -f /boot/initrd.img-$KVER
lsinitramfs /boot/initrd.img-$KVER > /tmp/initrd.list
grep -qE '(^|/)bin/cp$' /tmp/initrd.list || grep -q 'bin/busybox$' /tmp/initrd.list || { echo 'initramfs lacks cp (busybox)' >&2; exit 1; }
for m in virtio_blk virtio_mmio virtio_pci ext4; do      # in the initramfs, or built into the kernel
  grep -q "/$m.ko" /tmp/initrd.list || grep -q "/$m.ko" /lib/modules/$KVER/modules.builtin || { echo "initramfs lacks $m" >&2; exit 1; }
done
grep -q 'scripts/init-bottom/de-harness' /tmp/initrd.list || { echo 'initramfs lacks the harness hook' >&2; exit 1; }
rm /tmp/initrd.list

# Debug symbols where they are cheap: the dbgsym packages of llvmpipe (mesa-libgallium, libLLVM), KWin, Qt, Plasma and
# Chromium, and glibc's.
DBG="libc6-dbg"
for p in mesa-libgallium libllvm19 kwin-wayland libkwin6 libqt6core6t64 libqt6gui6 libqt6qml6 libqt6quick6 libqt6widgets6 \
         libqt6waylandclient6 plasma-workspace libplasma6 konsole libkonsoleprivate1 dolphin chromium firefox-esr; do
  apt-cache show "$p-dbgsym" >/dev/null 2>&1 && DBG="$DBG $p-dbgsym"
done
$APT $DBG >/dev/null
apt-mark manual $DBG >/dev/null

# Speedometer 3.1 at /sp (its workloads are committed prebuilt), and binutils to strip the debug files' DWARF; both tools
# are purged afterwards.
$APT git ca-certificates binutils >/dev/null
git init -q /sp && cd /sp && git fetch -q --depth 1 https://github.com/WebKit/Speedometer "$SP_COMMIT" && git checkout -q FETCH_HEAD
test "$(git rev-parse HEAD)" = "$SP_COMMIT"
grep -q '<script src="resources/main.mjs" type="module"></script>' index.html   # where the harness puts the hook
rm -rf .git tests
cd /
find /usr/lib/debug -type f -name '*.debug' -print0 | while IFS= read -r -d '' f; do
  objcopy --remove-section='.debug_*' --remove-section='.zdebug_*' "$f" "$f.tmp" && mv "$f.tmp" "$f"
done
apt-get purge -y -qq git ca-certificates binutils >/dev/null
apt-get autoremove -y -qq --purge >/dev/null

# The guest: a user `de` logged in on tty1 by getty, whose login starts the Plasma session (/run/de-harness/session.sh);
# de-setup (before the login: the user's settings from the harness) and de-bench (the workload, its console on ttyS0)
# run the harness's scripts as root. Nothing else that a timer or a first boot would start.
for g in render input; do getent group $g >/dev/null || groupadd -r $g; done
useradd -m -u 1000 -G video,render,input,dialout,audio -s /bin/bash de
passwd -d de >/dev/null
echo desktop > /etc/hostname
printf '127.0.0.1 localhost\n127.0.1.1 desktop\n' > /etc/hosts
echo 'de1e7e5ede1e7e5ede1e7e5ede1e7e5e' > /etc/machine-id
echo '/dev/vda / ext4 defaults,noatime 0 1' > /etc/fstab
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'EOF'
[Unit]
After=de-setup.service
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin de --noclear %I $TERM
EOF
cat > /home/de/.bash_profile <<'EOF'
[ "$(tty)" = /dev/tty1 ] && [ -z "$WAYLAND_DISPLAY" ] && [ -f /run/de-harness/session.sh ] && exec /bin/sh /run/de-harness/session.sh
EOF
chown de:de /home/de/.bash_profile
cat > /etc/systemd/system/de-setup.service <<'EOF'
[Unit]
Description=desktop profile: the user's settings from the harness
DefaultDependencies=no
After=local-fs.target
Before=getty@tty1.service systemd-user-sessions.service
ConditionPathExists=/run/de-harness/setup.sh
[Service]
Type=oneshot
ExecStart=/bin/sh /run/de-harness/setup.sh
StandardOutput=tty
TTYPath=/dev/ttyS0
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/de-bench.service <<'EOF'
[Unit]
Description=desktop profile: the workload
After=de-setup.service
ConditionPathExists=/run/de-harness/driver.sh
[Service]
Type=simple
ExecStart=/bin/sh /run/de-harness/driver.sh
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyS0
[Install]
WantedBy=multi-user.target
EOF
systemctl enable de-setup.service de-bench.service >/dev/null 2>&1
systemctl mask serial-getty@ttyS0.service apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer \
  fstrim.timer dpkg-db-backup.timer systemd-tmpfiles-clean.timer >/dev/null 2>&1
systemctl set-default multi-user.target >/dev/null 2>&1

{ echo "speedometer $SP_COMMIT"; echo "kernel $KVER"; echo "dbgsym $DBG"
  dpkg-query -W -f='${Package} ${Version}\n' kwin-wayland plasma-workspace konsole dolphin chromium firefox-esr mesa-libgallium libqt6core6t64 systemd
} > /work/versions.txt
dpkg-query -W -f='${Package} ${Version}\n' | LC_ALL=C sort > /build-packages.txt
if [ ! -f /recipe/packages.txt ]; then
  cp /build-packages.txt /work/packages.txt; echo 'no packages.txt pins: wrote /work/packages.txt' >&2; exit 1
fi
if ! diff <(LC_ALL=C sort /recipe/packages.txt) /build-packages.txt >&2; then
  echo 'the installed packages differ from packages.txt (above: < pinned, > installed)' >&2; exit 1
fi
cp /recipe/packages.txt /work/packages.txt
cp /boot/initrd.img-$KVER /work/initrd.img
rm -rf /var/cache/apt/archives/*.deb /var/lib/apt/lists/* /build-packages.txt /boot/initrd.img-* /boot/vmlinuz-*

# The root file system: this container's, without the container runtime's mounts and files, as an ext4 image (mke2fs -d
# keeps owners, modes, hard links and extended attributes such as file capabilities).
mkdir /work/root
cp -ax / /work/root/
rm -rf /work/root/work /work/root/recipe /work/root/.dockerenv
for d in proc sys dev run tmp; do rm -rf "/work/root/$d"; mkdir -p "/work/root/$d"; done
chmod 1777 /work/root/tmp
cp /etc/hosts /etc/hostname /work/root/etc/
python3 - /work/root /work/elves.list <<'EOF'
import os, sys
root, out = sys.argv[1], sys.argv[2]
names = []
for d, subs, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        if os.path.islink(p) or not os.path.isfile(p): continue
        with open(p, 'rb') as h:
            if h.read(4) == b'\x7fELF': names.append(os.path.relpath(p, root))
open(out, 'w').write('\n'.join(sorted(names)) + '\n')
EOF
(cd /work/root && cpio -o -H newc --quiet < /work/elves.list) | zstd -q -T0 -10 -o /work/elves.cpio.zst
rm /work/elves.list
mke2fs -q -t ext4 -L desktop -E root_owner=0:0 -d /work/root /work/disk.raw ${DISK_GIB}G
rm -rf /work/root
zstd -q -T0 -10 --rm /work/disk.raw -o /work/disk.raw.zst
