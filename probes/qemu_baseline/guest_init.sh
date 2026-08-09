#!/bin/sh
# /init for the natemu QEMU-baseline measurement initramfs.
# Runs as PID 1 in an initramfs-only Alpine userspace. Emits marker lines on the
# serial console that the host-side driver (runvm.py) timestamps.

/bin/busybox mkdir -p /proc /sys /dev /tmp
/bin/busybox mount -t proc     proc     /proc
/bin/busybox mount -t sysfs    sysfs    /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev 2>/dev/null
/bin/busybox mount -t tmpfs    tmpfs    /tmp

# First userspace instruction reached. Host timestamps this line.
echo "NATEMU_INIT_UP uptime=$(/bin/busybox cut -d' ' -f1 /proc/uptime)"

# Prove a real shell comes up and prints a prompt -- this is the
# "boot to a shell prompt" point.
PS1='natemu# ' /bin/busybox sh -c 'echo "NATEMU_SHELL_UP uptime=$(cut -d" " -f1 /proc/uptime)"'

TASK=none
HOLD=0
for w in $(/bin/busybox cat /proc/cmdline); do
    case "$w" in
        natemu.task=*) TASK="${w#natemu.task=}" ;;
        natemu.hold=*) HOLD="${w#natemu.hold=}" ;;
    esac
done

echo "NATEMU_TASK_BEGIN $TASK"

case "$TASK" in
    none)
        ;;
    bench*)
        ARGS="${TASK#bench}"
        ARGS="${ARGS#:}"
        ARGS=$(echo "$ARGS" | /bin/busybox tr ',' ' ')
        [ -z "$ARGS" ] && ARGS=all
        /bench $ARGS
        ;;
    par*)
        # MTTCG probe: N concurrent single-threaded alu4+ls4 workers.
        N="${TASK#par}"
        S=$(/bin/busybox cut -d' ' -f1 /proc/uptime)
        i=0
        while [ "$i" -lt "$N" ]; do
            /bench alu4 ls4 > /tmp/par.$i 2>&1 &
            i=$((i+1))
        done
        wait
        E=$(/bin/busybox cut -d' ' -f1 /proc/uptime)
        /bin/busybox cat /tmp/par.*
        echo "BENCH par$N uptime_start=$S uptime_end=$E"
        echo "BENCHDONE"
        ;;
    qemuuser)
        # QEMU 11.0.1 linux-user x86-64 -> aarch64, running INSIDE the aarch64
        # HVF guest. qemu-user has no software MMU (it uses the host MMU with a
        # guest-base offset), so this isolates TCG code generation from the
        # softmmu cost that the full-system numbers include.
        /qemu-x86_64 --version | /bin/busybox head -1
        /qemu-x86_64 /bench-x64 all
        ;;
    nativearm)
        /bench all
        ;;
    spawn)
        # kernel/syscall-heavy real workload: 400 fork+exec of a static binary
        S=$(/bin/busybox cut -d' ' -f1 /proc/uptime)
        i=0
        while [ $i -lt 400 ]; do
            /bin/busybox true
            i=$((i+1))
        done
        E=$(/bin/busybox cut -d' ' -f1 /proc/uptime)
        echo "BENCH spawn400 uptime_start=$S uptime_end=$E"
        ;;
    sevenzip)
        S=$(/bin/busybox cut -d' ' -f1 /proc/uptime)
        /7zz b -mmt1 -md=22 2>&1
        E=$(/bin/busybox cut -d' ' -f1 /proc/uptime)
        echo "SEVENZIP_WALL start=$S end=$E"
        echo "BENCHDONE"
        ;;
    sh)
        exec /bin/busybox sh
        ;;
    *)
        echo "NATEMU_UNKNOWN_TASK $TASK"
        ;;
esac

echo "NATEMU_TASK_END $TASK uptime=$(/bin/busybox cut -d' ' -f1 /proc/uptime)"
[ "$HOLD" != "0" ] && /bin/busybox sleep "$HOLD"
/bin/busybox sync
echo o > /proc/sysrq-trigger 2>/dev/null
/bin/busybox sleep 5
echo b > /proc/sysrq-trigger 2>/dev/null
/bin/busybox sleep 10
exit 0
