#!/bin/bash
# native.sh BENCHDIR [RUNS [BENCHES]]  (env NS="1 2 3 4" lists the N values): the native ceiling on the reference machine: each benchmark at N = 1-4 (taskset to CPUs
# 0..N-1 and jobs=N, as a guest with N vCPUs), RUNS runs each (default 3), the same binaries, sources, input and
# command lines as /init (keep the four commands in step with init), make's shell = the same busybox sh.
# Prints one line per run: bench N run seconds; zstd also one line per phase (zstd-compress, zstd-decompress,
# zstd-verify: busybox sha256sum of the decompressed output and of the input, equal to the input's pinned hash).
set -euo pipefail
HERE=$(dirname "$(realpath "$0")"); B=$(realpath "$1"); RUNS=${2:-3}; S=$B/sysroot; R=$B/rootfs
T=$B/native-tc; rm -rf "$T"; mkdir -p "$T"
for a in $("$S/usr/bin/busybox" --list); do ln -s "$S/usr/bin/busybox" "$T/$a"; done
for t in make x86_64-linux-gnu-gcc-14 x86_64-linux-gnu-as x86_64-linux-gnu-ar; do ln -sf "$S/usr/bin/$t" "$T/"; done
ln -sf "$S/usr/bin/x86_64-linux-gnu-gcc-14" "$T/gcc"; ln -sf "$S/usr/bin/x86_64-linux-gnu-as" "$T/as"
W=$B/native-work; rm -rf "$W"; mkdir -p "$W"; tar xzf "$B/src/zstd-1.5.7.tar.gz" -C "$W"
cd "$W/zstd-1.5.7"
run() {  # bench jobs
    case $1 in
    build)     make -s -C lib -j$2 libzstd.a CC="gcc --sysroot=$S" AR=x86_64-linux-gnu-ar CFLAGS="-O0 -ffile-prefix-map=$PWD/=" ZSTD_LEGACY_SUPPORT=0 ZSTD_LIB_DICTBUILDER=0 SHELL=$T/sh >/dev/null 2>&1 ;;
    zstd-compress)   "$R/bench/bin/zstd" -q -f -12 -B4194304 -T$2 "$R/bench/input" -o /tmp/natemu-bench-out.zst ;;
    zstd-decompress) "$R/bench/bin/zstd" -q -f -d /tmp/natemu-bench-out.zst -o /tmp/natemu-bench-out.raw ;;
    zstd-verify)     a=$(sha256sum < "$R/bench/input" | cut -d' ' -f1); b=$(sha256sum < /tmp/natemu-bench-out.raw | cut -d' ' -f1)
                     [ "$a" = "$b" ] && [ "$a" = "$ISHA" ] || { echo "FAIL VERIFY sha256 $a $b" >&2; exit 1; } ;;
    hackbench) "$R/bench/bin/hackbench" -p -g 4 -l 5000 >/dev/null ;;
    locks)     "$R/bench/bin/locks" $2 $((200000000 / $2)) >/dev/null ;;
    luajit)    mkdir -p /tmp/lj; rm -f /tmp/lj/*; xargs -P$2 -I{} sh -c '{}' < "$B/out/lj-tasks.native"
               (cd /tmp/lj && md5sum -c -s "$B/out/expect-lj.md5") || { echo "FAIL VERIFY luajit" >&2; exit 1; } ;;
    luajit-interp) mkdir -p /tmp/lj; rm -f /tmp/lj/*; xargs -P$2 -I{} sh -c '{}' < "$B/out/lj-tasks-interp.native"
               (cd /tmp/lj && md5sum -c -s "$B/out/expect-lj-interp.md5") || { echo "FAIL VERIFY luajit-interp" >&2; exit 1; } ;;
    7zip-compress) cd /tmp/natemu-7z; rm -f in7.7z; "$R/bench/bin/7zz" a -bso0 -bsp0 -mx=5 -m0=lzma2:c=4m -mmt=$2 -mtm=off in7.7z in7 ;;
    7zip-test) "$R/bench/bin/7zz" t -bso0 -bsp0 -mmt=$2 /tmp/natemu-7z/in7.7z ;;
    7zip-verify) [ "$("$R/bench/bin/7zz" x -so -bso0 -bsp0 /tmp/natemu-7z/in7.7z | sha256sum | cut -d' ' -f1)" = "$(sed -n 's/^.*in7 \([0-9a-f]*\) 7z.*$/\1/p' "$B/out/expected.txt")" ] || { echo "FAIL VERIFY 7zip" >&2; exit 1; } ;;
    sqlite)    "$R/bench/bin/speedtest1" --memdb --verify --size 160 :memory: > /tmp/natemu-st.out
               [ "$(sed -n 's/^Verification Hash: //p' /tmp/natemu-st.out | tr -d ' ')" = "$(sed -n 's/^.*sqlite \([0-9a-f]*\) x264.*$/\1/p' "$B/out/expected.txt")" ] || { echo "FAIL VERIFY sqlite" >&2; exit 1; } ;;
    avx)       "$R/bench/bin/x264" --preset slower --crf 23 --threads $2 --asm MMX2,SSE2Fast,SSSE3,SSE4.2,AVX,FMA3,BMI2,AVX2 -o /tmp/natemu-x.264 "$R/bench/foreman_cif.y4m" 2>/dev/null
               [ "$(md5sum < /tmp/natemu-x.264 | cut -d' ' -f1)" = "$(sed -n "s#^.*-e s/@X264_MD5_$2@/\([0-9a-f]*\)/.*\$#\1#p" "$B/out/expected.txt")" ] || { echo "FAIL VERIFY avx" >&2; exit 1; } ;;
    mem-*)     ph=${1#mem-}; n=$2; sng="$R/bench/bin/stress-ng --no-rand-seed --verify --metrics"
               case $ph in
               vm)   o="--vm $n --vm-bytes $((32 * n))M --vm-method lfsr32 --vm-ops 6000"; w=6000 ;;
               mmap) o="--mmap $n --mmap-bytes $((32 * n))M --mmap-ops 48"; w=48 ;;
               fork) o="--fork $n --fork-vm --fork-ops 720"; w=720 ;;
               esac
               $sng $o > /tmp/natemu-mem.log 2>&1 && "$HERE/memchk" /tmp/natemu-mem.log $w || { echo "FAIL VERIFY mem-$ph" >&2; exit 1; } ;;
    cray)      "$R/bench/c-ray-mt" -t $2 -s 1280x960 -r 1 < "$R/bench/sphfract" 2>/dev/null > /tmp/natemu-cray.ppm
               [ "$(md5sum < /tmp/natemu-cray.ppm | cut -d' ' -f1)" = "$CMD5" ] || { echo "FAIL VERIFY cray" >&2; exit 1; } ;;
    cray-strict) "$R/bench/c-ray-mt-strict" -t $2 -s 1280x960 -r 1 < "$R/bench/sphfract" 2>/dev/null > /tmp/natemu-cray.ppm
               [ "$(md5sum < /tmp/natemu-cray.ppm | cut -d' ' -f1)" = "$SMD5" ] || { echo "FAIL VERIFY cray-strict" >&2; exit 1; } ;;
    esac
}
plist() { case $1 in zstd) echo zstd-compress zstd-decompress zstd-verify;; 7zip) echo 7zip-compress 7zip-test 7zip-verify;; mem) echo mem-vm mem-mmap mem-fork;; *) echo $1;; esac; }
ISHA=$(sha256sum "$R/bench/input" | cut -d' ' -f1)
CMD5=$(sed -n 's/^.*c-ray \([0-9a-f]*\) in7.*$/\1/p' "$B/out/expected.txt")
SMD5=$(sed -n 's/^.*c-ray-strict \([0-9a-f]*\).*$/\1/p' "$B/out/expected.txt")
for t in luajit stress-ng; do ln -sf "$R/bench/bin/$t" "$T/$t"; done
sed "s#/bench/lj#$B/out/lj#" "$R/bench/lj/tasks" > "$B/out/lj-tasks.native"
sed "s#/bench/lj#$B/out/lj#" "$R/bench/lj/tasks-interp" > "$B/out/lj-tasks-interp.native"
mkdir -p /tmp/natemu-7z; (umask 022; head -c 12582912 "$R/bench/input" > /tmp/natemu-7z/in7)
for b in ${3:-build zstd hackbench locks luajit luajit-interp mem cray 7zip sqlite avx}; do
    for n in ${NS:-1 2 3 4}; do
        for r in $(seq "$RUNS"); do
            [ $b = build ] && LD_LIBRARY_PATH=$B/nativelibs "$T/make" -s -C lib clean >/dev/null
            total=0
            for p in $(plist $b); do
                t0=$(date +%s.%N)
                env -i PATH=$T LD_LIBRARY_PATH=$B/nativelibs S=$S T=$T R=$R B=$B HERE=$HERE ISHA=$ISHA CMD5=$CMD5 SMD5=$SMD5 /usr/bin/taskset -c 0-$((n - 1)) /bin/bash -c "$(declare -f run); run $p $n"
                t1=$(date +%s.%N); d=$(awk "BEGIN{printf \"%.3f\", $t1 - $t0}"); [ $p = 7zip-verify ] || total=$(awk "BEGIN{printf \"%.3f\", $total + $d}")
                [ $p = $b ] || echo "$p $n $r $d"
            done
            echo "$b $n $r $total"
        done
    done
done
[ ! -f lib/libzstd.a ] || echo "build md5 $(md5sum lib/libzstd.a | cut -d' ' -f1)"
