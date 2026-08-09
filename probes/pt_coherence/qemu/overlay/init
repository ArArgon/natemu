#!/bin/sh
# /init overlay for the pt_coherence probe.
#
# Same structure as probes/qemu_baseline/guest_init.sh, plus PROCDUMP blocks
# that snapshot the guest's own page-fault / context-switch / TLB-shootdown
# counters immediately before and after the workload, so the plugin-side
# instruction counts can be cross-checked against kernel-visible events.
#
# Shipped as a second cpio appended to the baseline initramfs, so it simply
# overwrites /init.

B=/bin/busybox

$B mkdir -p /proc /sys /dev /tmp
$B mount -t proc     proc     /proc
$B mount -t sysfs    sysfs    /sys
$B mount -t devtmpfs devtmpfs /dev 2>/dev/null
$B mount -t tmpfs    tmpfs    /tmp

echo "NATEMU_INIT_UP uptime=$($B cut -d' ' -f1 /proc/uptime)"
PS1='natemu# ' $B sh -c 'echo "NATEMU_SHELL_UP uptime=$(cut -d" " -f1 /proc/uptime)"'

TASK=none
HOLD=0
for w in $($B cat /proc/cmdline); do
    case "$w" in
        natemu.task=*) TASK="${w#natemu.task=}" ;;
        natemu.hold=*) HOLD="${w#natemu.hold=}" ;;
    esac
done

procdump() { # $1 = phase tag
    P="$1"
    echo "PROCDUMP $P uptime $($B cat /proc/uptime)"
    $B awk -v p="$P" '{print "PROCDUMP " p " stat " $0}' /proc/stat \
        | $B grep -E ' (ctxt|processes|procs_running|procs_blocked|intr) '
    $B awk -v p="$P" '{print "PROCDUMP " p " vmstat " $1 " " $2}' /proc/vmstat
    $B awk -v p="$P" '{print "PROCDUMP " p " irq " $0}' /proc/interrupts
    echo "PROCDUMP $P end"
}

procdump PRE
echo "NATEMU_TASK_BEGIN $TASK"

case "$TASK" in
    none)
        ;;
    bench*)
        ARGS="${TASK#bench}"; ARGS="${ARGS#:}"
        ARGS=$(echo "$ARGS" | $B tr ',' ' ')
        [ -z "$ARGS" ] && ARGS=all
        /bench $ARGS
        ;;
    par*)
        N="${TASK#par}"
        S=$($B cut -d' ' -f1 /proc/uptime)
        i=0
        while [ "$i" -lt "$N" ]; do
            /bench alu4 ls4 > /tmp/par.$i 2>&1 &
            i=$((i+1))
        done
        wait
        E=$($B cut -d' ' -f1 /proc/uptime)
        $B cat /tmp/par.*
        echo "BENCH par$N uptime_start=$S uptime_end=$E"
        echo "BENCHDONE"
        ;;
    spawn*)
        # kernel/PT-heavy: N fork+exec of a static binary (default 400)
        N="${TASK#spawn}"
        [ -z "$N" ] && N=400
        S=$($B cut -d' ' -f1 /proc/uptime)
        i=0
        while [ $i -lt "$N" ]; do
            $B true
            i=$((i+1))
        done
        E=$($B cut -d' ' -f1 /proc/uptime)
        echo "BENCH spawn$N uptime_start=$S uptime_end=$E"
        echo "BENCHDONE"
        ;;
    sevenzip)
        S=$($B cut -d' ' -f1 /proc/uptime)
        /7zz b -mmt1 -md=22 2>&1
        E=$($B cut -d' ' -f1 /proc/uptime)
        echo "SEVENZIP_WALL start=$S end=$E"
        echo "BENCHDONE"
        ;;
    sevenzipmt*)
        N="${TASK#sevenzipmt}"
        [ -z "$N" ] && N=4
        S=$($B cut -d' ' -f1 /proc/uptime)
        /7zz b -mmt"$N" -md=22 2>&1
        E=$($B cut -d' ' -f1 /proc/uptime)
        echo "SEVENZIP_WALL start=$S end=$E"
        echo "BENCHDONE"
        ;;
    sh)
        exec $B sh
        ;;
    *)
        echo "NATEMU_UNKNOWN_TASK $TASK"
        ;;
esac

echo "NATEMU_TASK_END $TASK uptime=$($B cut -d' ' -f1 /proc/uptime)"
procdump POST

[ "$HOLD" != "0" ] && $B sleep "$HOLD"
$B sync
echo o > /proc/sysrq-trigger 2>/dev/null
$B sleep 5
echo b > /proc/sysrq-trigger 2>/dev/null
$B sleep 10
exit 0
