#!/bin/bash
# mkroot.sh BENCHDIR: stage the guest userland in BENCHDIR/rootfs and write BENCHDIR/out/bench.cpio.
# Inputs (made by build.sh): BENCHDIR/sysroot (Debian trixie packages, dpkg -x), BENCHDIR/out/bin/{zstd,hackbench,locks,
# luajit,stress-ng,c-ray-mt,c-ray-mt-strict,7zz,speedtest1,x264,sampler_check,coldtrip}, BENCHDIR/src/zstd-1.5.7.tar.gz, BENCHDIR/out/input, BENCHDIR/out/{lj,sphfract,expect-lj.md5,expect-lj-interp.md5,foreman_cif.y4m},
# and init, lj-tasks, memchk + mkcpio.py beside this script.
set -euo pipefail
B=$(realpath "$1"); S=$B/sysroot; R=$B/rootfs; here=$(dirname "$(realpath "$0")")
rm -rf "$R"; mkdir -p "$R"/{usr/bin,usr/lib/x86_64-linux-gnu,usr/lib64,proc,sys,dev,tmp,bench/bin}
for l in bin sbin; do ln -s usr/bin "$R/$l"; done
ln -s usr/lib "$R/lib"; ln -s usr/lib64 "$R/lib64"; ln -s bin "$R/usr/sbin"

copy() { mkdir -p "$R/$(dirname "$1")"; cp -a "$S/$1" "$R/$1"; }
# Toolchain: the gcc driver, cc1, as, ar, make (+ the names make and gcc call them by).
for f in usr/bin/x86_64-linux-gnu-gcc-14 usr/bin/x86_64-linux-gnu-as usr/bin/x86_64-linux-gnu-ar usr/bin/make \
         usr/libexec/gcc/x86_64-linux-gnu/14/cc1 usr/lib64/ld-linux-x86-64.so.2; do copy "$f"; done
for n in gcc cc gcc-14; do ln -s x86_64-linux-gnu-gcc-14 "$R/usr/bin/$n"; done
ln -s x86_64-linux-gnu-as "$R/usr/bin/as"; ln -s x86_64-linux-gnu-ar "$R/usr/bin/ar"
cp -a "$S/usr/include" "$R/usr/"
copy usr/lib/gcc/x86_64-linux-gnu/14/include
# Shared libraries: the NEEDED closure of every dynamic ELF staged above, from the Debian sysroot.
todo=$(cd "$R" && find usr/bin usr/libexec -type f); seen=" "
while [ -n "$todo" ]; do
    next=""
    for f in $todo; do
        for lib in $(readelf -d "$R/$f" 2>/dev/null | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p'); do
            case $seen in *" $lib "*) continue;; esac; seen="$seen$lib "
            p=usr/lib/x86_64-linux-gnu/$lib
            [ -e "$S/$p" ] || { echo "missing $lib (needed by $f)" >&2; exit 1; }
            cp -L "$S/$p" "$R/$p"; next="$next $p"
        done
    done
    todo=$next
done
# Busybox applets, skipping any name a real tool above already owns.
cp "$S/usr/bin/busybox" "$R/usr/bin/"
for a in $("$S/usr/bin/busybox" --list); do [ -e "$R/usr/bin/$a" ] || [ -L "$R/usr/bin/$a" ] || ln -s busybox "$R/usr/bin/$a"; done
# Benchmarks.
cp "$B"/out/bin/{zstd,hackbench,locks,luajit,stress-ng,7zz,speedtest1,x264} "$R/bench/bin/"
for x in zstd hackbench locks luajit stress-ng 7zz speedtest1 x264; do ln -s /bench/bin/$x "$R/usr/bin/$x"; done
cp "$B"/out/bin/{hackbench32,speedtest1-32} "$R/bench/bin/"; for x in hackbench32 speedtest1-32; do ln -s /bench/bin/$x "$R/usr/bin/$x"; done
cp "$B/out/bin/c-ray-mt" "$B/out/bin/c-ray-mt-strict" "$B/out/bin/c-ray-mt32" "$B/out/bin/c-ray-mt32-strict" "$B/out/bin/c-ray-mt32-sse2" "$B/out/sphfract" "$R/bench/"
cp "$B"/out/bin/{scimark2,scimark2-32,scimark2-32sse2} "$R/bench/bin/"; for x in scimark2 scimark2-32 scimark2-32sse2; do ln -s /bench/bin/$x "$R/usr/bin/$x"; done
cp "$B"/out/bin/{sampler_check,coldtrip} "$R/bench/bin/"; for x in sampler_check coldtrip; do ln -s /bench/bin/$x "$R/usr/bin/$x"; done
mkdir -p "$R/bench/lj"; cp "$B"/out/lj/*.lua "$R/bench/lj/"; cp "$here/lj-tasks" "$R/bench/lj/tasks"; cp "$here/lj-tasks-interp" "$R/bench/lj/tasks-interp"; cp "$B/out/expect-lj-interp.md5" "$R/bench/lj/expect-interp.md5"; cp "$B/out/expect-lj.md5" "$R/bench/lj/expect.md5"
cp "$here/memchk" "$R/bench/memchk"; chmod 755 "$R/bench/memchk"
tar xzf "$B/src/zstd-1.5.7.tar.gz" -C "$R/bench"
cp "$B/out/input" "$R/bench/input"; cp "$B/out/foreman_cif.y4m" "$R/bench/"
cp "${INIT:-$here/init}" "$R/init"; chmod 755 "$R/init"
python3 "$here/mkcpio.py" "$R" "$B/out/bench.cpio"
ls -la "$B/out/bench.cpio"; sha256sum "$B/out/bench.cpio"
