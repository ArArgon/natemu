#!/bin/sh
# /init for the natemu doc-26 "guest rates and footprint" measurement.
#
# Differs from guest_init.sh (which is left untouched) only in that it dumps
# guest-visible MMU counters -- /proc/vmstat, /proc/stat, /proc/interrupts,
# /proc/meminfo -- at every phase boundary, so that the QEMU-plugin numbers can
# be cross-checked against what the guest kernel itself believes happened.
#
# Every dumped line is prefixed NATEMU_VM so the host driver can pick it out of
# the console stream and timestamp it.

B=/bin/busybox

$B mkdir -p /proc /sys /dev /tmp
$B mount -t proc     proc     /proc
$B mount -t sysfs    sysfs    /sys
$B mount -t devtmpfs devtmpfs /dev 2>/dev/null
$B mount -t tmpfs    tmpfs    /tmp

dump() { # tag
    echo "NATEMU_MARK $1 uptime=$($B cut -d' ' -f1 /proc/uptime)"
    # pgfault = all minor+major faults the kernel handled; pgmajfault = major.
    # thp_fault_alloc etc. are printed too so that a hugepage-heavy phase is
    # visible rather than silently deflating the 4 KB fault count.
    $B awk -v t="$1" '/^(pgfault|pgmajfault|pgalloc_|pgfree|pgactivate|thp_fault_alloc|nr_page_table_pages|pgreuse|pgsteal_|numa_)/ {print "NATEMU_VM " t " " $1 " " $2}' /proc/vmstat
    $B awk -v t="$1" '/^(ctxt|processes|intr) / {print "NATEMU_VM " t " " $1 " " $2}' /proc/stat
    $B awk -v t="$1" '/^(MemTotal|MemFree|MemAvailable|PageTables|Mapped|AnonPages|Cached):/ {print "NATEMU_VM " t " " $1 " " $2}' /proc/meminfo
    echo "NATEMU_MARKEND $1"
}

echo "NATEMU_INIT_UP uptime=$($B cut -d' ' -f1 /proc/uptime)"
dump boot_done

TASK=none
HOLD=0
IDLE=0
for w in $($B cat /proc/cmdline); do
    case "$w" in
        natemu.task=*) TASK="${w#natemu.task=}" ;;
        natemu.hold=*) HOLD="${w#natemu.hold=}" ;;
        natemu.idle=*) IDLE="${w#natemu.idle=}" ;;
        natemu.cpuinfo=1) CPUINFO=1 ;;
    esac
done

# Diagnostic only, off by default so the measured path stays byte-identical.
# doc 26 section 6: the guest never used PCID (cr3w_noflush=0, invpcid=0 in all five
# workloads) and this reports whether the CPU advertises it at all, versus the
# kernel declining to use it.
if [ "${CPUINFO:-0}" = "1" ]; then
    echo "NATEMU_CPUFLAGS $($B awk '/^flags/{print; exit}' /proc/cpuinfo)"
    for f in pcid invpcid pge pse36 la57 smep smap; do
        if $B grep -qw "$f" /proc/cpuinfo; then echo "NATEMU_FEAT $f=yes"; else echo "NATEMU_FEAT $f=no"; fi
    done
    echo "NATEMU_PTI $($B cat /sys/devices/system/cpu/vulnerabilities/meltdown 2>/dev/null || echo unknown)"
fi

# ---- measured idle window: nothing runs but the kernel's own timer/RCU work.
if [ "$IDLE" != "0" ]; then
    echo "NATEMU_IDLE_BEGIN uptime=$($B cut -d' ' -f1 /proc/uptime)"
    dump idle_begin
    $B sleep "$IDLE"
    dump idle_end
    echo "NATEMU_IDLE_END uptime=$($B cut -d' ' -f1 /proc/uptime)"
fi

echo "NATEMU_TASK_BEGIN $TASK"
dump task_begin

case "$TASK" in
    none)
        ;;
    bench*)
        ARGS="${TASK#bench}"; ARGS="${ARGS#:}"
        ARGS=$(echo "$ARGS" | $B tr ',' ' ')
        [ -z "$ARGS" ] && ARGS=all
        /bench $ARGS
        ;;
    spawn*)
        # fork+exec storm: the MMU-heaviest thing a shell can do. Each iteration
        # is a fork (COW of the whole address space) + exec (fresh mm, CR3 load,
        # demand-fault the binary back in) + exit (tear the mm down).
        N="${TASK#spawn}"; [ -z "$N" ] && N=400
        S=$($B cut -d' ' -f1 /proc/uptime)
        i=0
        while [ "$i" -lt "$N" ]; do $B true; i=$((i+1)); done
        E=$($B cut -d' ' -f1 /proc/uptime)
        echo "BENCH spawn$N uptime_start=$S uptime_end=$E"
        ;;
    buildproxy)
        # Stand-in for "a build". There is no compiler in this initramfs, so we
        # approximate a build's MMU profile with the two things a build actually
        # does: many short-lived processes, and streaming file I/O through the
        # page cache. Labelled a PROXY everywhere; it is not a build.
        S=$($B cut -d' ' -f1 /proc/uptime)
        i=0
        while [ "$i" -lt 60 ]; do
            $B tar -cf - /usr/bin 2>/dev/null | $B gzip -1 > /tmp/o.gz
            $B gzip -d -c /tmp/o.gz | $B cksum > /dev/null
            i=$((i+1))
        done
        E=$($B cut -d' ' -f1 /proc/uptime)
        echo "BENCH buildproxy uptime_start=$S uptime_end=$E"
        ;;
    sevenzip*)
        # optional iteration count: sevenzip:1
        N="${TASK#sevenzip}"; N="${N#:}"; [ -z "$N" ] && N=1
        S=$($B cut -d' ' -f1 /proc/uptime)
        /7zz b "$N" -mmt1 -md=22 2>&1
        E=$($B cut -d' ' -f1 /proc/uptime)
        echo "SEVENZIP_WALL start=$S end=$E"
        ;;
    sh)
        exec $B sh
        ;;
    *)
        echo "NATEMU_UNKNOWN_TASK $TASK"
        ;;
esac

dump task_end
echo "NATEMU_TASK_END $TASK uptime=$($B cut -d' ' -f1 /proc/uptime)"

[ "$HOLD" != "0" ] && $B sleep "$HOLD"
$B sync
echo o > /proc/sysrq-trigger 2>/dev/null
$B sleep 3
echo b > /proc/sysrq-trigger 2>/dev/null
$B sleep 10
exit 0
