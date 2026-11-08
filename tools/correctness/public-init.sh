#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C TZ=UTC
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /dev/pts /dev/shm /tmp /run
mount -t devpts devpts /dev/pts
mount -t tmpfs shm /dev/shm
mount -t tmpfs tmp /tmp
mount -t tmpfs run /run
chmod 1777 /tmp /dev/shm
ln -s /proc/self/fd /dev/fd
ln -s /proc/self/fd/0 /dev/stdin
ln -s /proc/self/fd/1 /dev/stdout
ln -s /proc/self/fd/2 /dev/stderr
hostname natemu-public
ip link set lo up
part=0/1
for a in $(cat /proc/cmdline); do case "$a" in part=*) part=${a#part=};; esac; done
exec </dev/null >/dev/ttyS0 2>&1
echo "CT-BEGIN suite=public part=$part cpus=$(nproc) kernel=$(uname -r)"
t0=$(cut -d' ' -f1 /proc/uptime)
/ct/run-public.sh /ct "$part"
rc=$?
echo "CT-END suite=public part=$part rc=$rc guest_s=$(awk -v a="$t0" '{print $1-a}' /proc/uptime)"
sync
poweroff -f
