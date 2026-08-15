#!/bin/sh
# /init for the natemu guest-event-rate measurement initramfs (doc 26).
#
# Runs as PID 1 in an initramfs-only Alpine x86_64 userspace. Around each
# workload it emits a GRSNAP line carrying the guest's own view of the events
# a shadow-MMU / precise-exception design pays for:
#
#   pgfault      /proc/vmstat  -- minor+major page faults the GUEST took
#   pgmajfault   /proc/vmstat  -- of which major
#   ctxt         /proc/stat    -- context switches
#   processes    /proc/stat    -- process creations (fork)
#   uptime       /proc/uptime  -- guest-perceived elapsed time
#
# Everything is a monotonic counter, so a workload's rates come from the
# difference of the two snapshots bracketing it. The host (runrates.py) also
# timestamps every line, so we get both guest-perceived and host wall time.

B=/bin/busybox
$B mkdir -p /proc /sys /dev /tmp
$B mount -t proc     proc     /proc
$B mount -t sysfs    sysfs    /sys
$B mount -t devtmpfs devtmpfs /dev 2>/dev/null
$B mount -t tmpfs    tmpfs    /tmp

snap() {
    # $1 = tag
    V=$($B awk '/^pgfault |^pgmajfault |^pgpgin |^pgpgout |^thp_fault_alloc /{printf "%s=%s ", $1, $2}' /proc/vmstat)
    S=$($B awk '/^ctxt |^processes |^intr /{printf "%s=%s ", $1, $2}' /proc/stat)
    U=$($B cut -d' ' -f1 /proc/uptime)
    echo "GRSNAP $1 uptime=$U $V $S"
}

echo "NATEMU_INIT_UP uptime=$($B cut -d' ' -f1 /proc/uptime)"

TASK=none
for w in $($B cat /proc/cmdline); do
    case "$w" in
        natemu.task=*) TASK="${w#natemu.task=}" ;;
    esac
done

# optional: guest-side flush_tlb_* counts via the ftrace function profiler.
# Best effort -- the Alpine virt kernel may not have CONFIG_FUNCTION_PROFILER.
TR=/sys/kernel/tracing
ftrace_on() {
    [ -d "$TR" ] || $B mount -t tracefs nodev "$TR" 2>/dev/null
    [ -w "$TR/set_ftrace_filter" ] || return 1
    echo 0 > "$TR/function_profile_enabled" 2>/dev/null
    echo 'flush_tlb_*' > "$TR/set_ftrace_filter" 2>/dev/null || return 1
    echo 1 > "$TR/function_profile_enabled" 2>/dev/null || return 1
    return 0
}
ftrace_dump() {
    [ -r "$TR/trace_stat/function0" ] || return 1
    echo "GRFTRACE_BEGIN"
    $B cat "$TR"/trace_stat/function* 2>/dev/null
    echo "GRFTRACE_END"
    echo 0 > "$TR/function_profile_enabled" 2>/dev/null
}

FT=0
if [ "${NATEMU_FTRACE:-1}" = "1" ]; then ftrace_on && FT=1; fi
echo "GRFTRACE_AVAILABLE $FT"

echo "NATEMU_TASK_BEGIN $TASK"
snap before

case "$TASK" in
    none)
        ;;
    spawn*)
        # fork/exec-heavy: N fork+exec of a small static binary
        N="${TASK#spawn}"; [ -z "$N" ] && N=2000
        i=0
        while [ "$i" -lt "$N" ]; do
            $B true
            i=$((i+1))
        done
        ;;
    mem*)
        # memory-heavy: 7-Zip LZMA benchmark, single thread, 16 MiB dictionary
        D="${TASK#mem}"; [ -z "$D" ] && D=24
        /7zz b -mmt1 "-md=$D" 2>&1
        ;;
    build*)
        # compile workload: make over a real C project with the Alpine gcc 15.2
        N="${TASK#build}"; [ -z "$N" ] && N=1
        export PATH=/usr/bin:/bin:/usr/sbin:/sbin
        cd /src
        i=0
        while [ "$i" -lt "$N" ]; do
            /usr/bin/make -s clean >/dev/null 2>&1
            /usr/bin/make -s 2>&1 | $B tail -5
            i=$((i+1))
        done
        cd /
        ;;
    sh)
        exec $B sh
        ;;
    *)
        echo "NATEMU_UNKNOWN_TASK $TASK"
        ;;
esac

snap after
[ "$FT" = "1" ] && ftrace_dump
echo "NATEMU_TASK_END $TASK uptime=$($B cut -d' ' -f1 /proc/uptime)"
$B sync
echo o > /proc/sysrq-trigger 2>/dev/null
$B sleep 5
echo b > /proc/sysrq-trigger 2>/dev/null
$B sleep 10
exit 0
