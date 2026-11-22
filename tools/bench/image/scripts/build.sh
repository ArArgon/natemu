#!/bin/bash
# build.sh BENCHDIR: on an x86-64 Linux machine without root (the reference x86 machine), fetch the pinned
# Debian trixie packages and sources, build the static benchmarks with Debian's gcc 14, make the input, and stage the
# initramfs (mkroot.sh). Needs apt-get/apt-cache, dpkg, curl, python3, readelf (binutils). The host's own compiler is
# not used: every binary in the guest is either a Debian package file or built by Debian gcc against Debian libc.a.
# Debian's packages and orig tarballs come from the snapshot.debian.org archive at SNAP, so the versions packages.txt
# pins stay downloadable: trixie main as it was when packages.txt was resolved (glibc 2.41-12+deb13u4; the moving mirror
# replaced it with Debian 13.8 on 2026-11-21). apt checks the archive's signature, as before. To re-pin, move SNAP and
# delete packages.txt: the next build resolves the set again at the new date.
set -euo pipefail
SNAP=20261120T000000Z
MIRROR=http://snapshot.debian.org/archive/debian/$SNAP
here=$(dirname "$(realpath "$0")"); mkdir -p "$1"; B=$(realpath "$1"); S=$B/sysroot
mkdir -p "$B"/{apt/lists/partial,apt/cache/archives/partial,apt/etc/preferences.d,apt/etc/apt.conf.d,debs,src,out/bin}

# 1. A user-level apt for Debian trixie main, baseline amd64 (the reference machine's own apt is Ubuntu amd64v3).
cd "$B/debs"
[ -d "$B/kr" ] || { apt-get download debian-archive-keyring >/dev/null; dpkg -x debian-archive-keyring_*.deb "$B/kr"; rm debian-archive-keyring_*.deb; }
cat > "$B/apt/apt.conf" <<EOF
Dir::State "$B/apt"; Dir::State::Lists "$B/apt/lists"; Dir::State::status "$B/apt/status"; Dir::Cache "$B/apt/cache";
Dir::Etc::SourceList "$B/apt/sources.list"; Dir::Etc::SourceParts "$B/apt/etc/none";
Dir::Etc::Preferences "$B/apt/etc/preferences"; Dir::Etc::PreferencesParts "$B/apt/etc/preferences.d";
Dir::Etc::Parts "$B/apt/etc/apt.conf.d"; APT::Architecture "amd64"; APT::Architectures { "amd64"; };
APT::Architecture-Variants "amd64"; Debug::NoLocking "true"; Acquire::Retries "5";
EOF
touch "$B/apt/status"
echo "deb [signed-by=$B/kr/usr/share/keyrings/debian-archive-keyring.pgp check-valid-until=no] $MIRROR/ trixie main" > "$B/apt/sources.list"
export APT_CONFIG=$B/apt/apt.conf
apt-get update -qq
# The exact package set and versions are pinned by packages.txt (written at the first run; later runs download it).
if [ -f "$here/packages.txt" ]; then P=$(cat "$here/packages.txt")
else
    names=$(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces \
            --no-enhances gcc-14 make busybox-static libc6-dev 2>/dev/null | grep '^[a-z0-9]' | sort -u)
    P=$(for n in $names; do echo "$n=$(apt-cache policy "$n" | sed -n 's/  Candidate: //p')"; done)
    echo "$P" > "$here/packages.txt"
fi
rm -f ./*.deb; apt-get download -qq $P
sha256sum ./*.deb > "$B/out/debs.sha256"
rm -rf "$S"; mkdir -p "$S"; for d in ./*.deb; do dpkg -x "$d" "$S"; done
for l in lib lib64 bin sbin; do ln -sfn usr/$l "$S/$l"; done
# (gcc's 32-bit multilib, for the 32-bit builds below: the packages' /lib32 goes under usr like lib and lib64)
if [ -d "$S/lib32" ] && [ ! -L "$S/lib32" ]; then mkdir -p "$S/usr/lib32"; cp -a "$S/lib32/." "$S/usr/lib32/"; rm -rf "$S/lib32"; fi
ln -sfn usr/lib32 "$S/lib32"

# 2. Sources and input, checked against pinned hashes. fetch URL FILE SHA256 [URL2]: URL2, when given, is a fixed copy
#    of the same file, tried when URL fails.
cd "$B/src"
fetch() { [ -f "$2" ] || curl -sSLf --retry 5 -o "$2" "$1" || { [ -n "${4:-}" ] && curl -sSLf --retry 5 -o "$2" "$4"; }
          echo "$3  $2" | sha256sum -c --quiet; }
fetch https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz zstd-1.5.7.tar.gz \
      eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3
fetch https://git.kernel.org/pub/scm/utils/rt-tests/rt-tests.git/snapshot/rt-tests-2.8.tar.gz rt-tests-2.8.tar.gz \
      5f5c8fac9bae903dd670bd12009f793c53cdc114c64dbec2ea8c8b02879d14d3
# (the corpus's own host was unreachable on 2026-11-22; the Internet Archive's capture of it is the same bytes)
fetch https://sun.aei.polsl.pl//~sdeor/corpus/silesia.zip silesia.zip \
      0626e25f45c0ffb5dc801f13b7c82a3b75743ba07e3a71835a41e3d9f63c77af \
      https://web.archive.org/web/20260410165452id_/https://sun.aei.polsl.pl//~sdeor/corpus/silesia.zip
fetch $MIRROR/pool/main/l/luajit/luajit_2.1.0+openresty20250117.orig.tar.xz luajit_2.1.0+openresty20250117.orig.tar.xz \
      99ad5fdfa85e08a291454f797f625fbb53f4d2129cac063922255a1fe34ccf1c
fetch $MIRROR/pool/main/s/stress-ng/stress-ng_0.19.02.orig.tar.xz stress-ng_0.19.02.orig.tar.xz \
      ffe8907154cc3b65cc66cde8663106355f88c948b58169cee1badbde9e0f52bd
fetch https://github.com/LuaJIT/LuaJIT-test-cleanup/archive/014708bceb70550a3ab8d539cff14d9085ca9cb8.tar.gz LuaJIT-test-cleanup-014708b.tar.gz \
      6003d032d74e48516526a5e81f1e40ea13072395af85c79b63aeffd415756fba
fetch https://math.nist.gov/scimark2/scimark2_1c.zip scimark2_1c.zip \
      223464cd7e90b4c22e2af08dbae6f6faa33e65b01e1c58e5a176837bc67958be
fetch https://github.com/jtsiomb/c-ray/releases/download/c-ray-1.1/c-ray-1.1.tar.gz c-ray-1.1.tar.gz \
      6f507aae47a9367334b8cb50f50eb4ad0f6fef99aeae9f2f7d55ba9818e798bf
fetch $MIRROR/pool/main/7/7zip/7zip_25.01+dfsg.orig.tar.xz 7zip_25.01+dfsg.orig.tar.xz \
      077c424cd50001e2be8847892522bc83e807e0b9448af1b69512c03d769c88ef
fetch $MIRROR/pool/main/s/sqlite3/sqlite3_3.46.1.orig.tar.xz sqlite3_3.46.1.orig.tar.xz \
      d0cdd2ece271b29e7ce18095745d892517ee26d0f270065b3a25c2e9eb11639c
fetch $MIRROR/pool/main/x/x264/x264_0.164.3108+git31e19f9.orig.tar.gz x264_0.164.3108+git31e19f9.orig.tar.gz \
      41606cb8e788a7f8c4514290646d4ba5c7bc68d9e1ccd1a73f446a90546913eb
fetch https://media.xiph.org/video/derf/y4m/foreman_cif.y4m foreman_cif.y4m \
      b0e3c35eff9fe1da91521a02eb9f0e3ef4bc5aec341ecef1567cd87f1711267b
[ -f locks.c ] || cp "$here/../src/locks.c" .
# Input: the Silesia files concatenated in name order, first 64 MiB.
python3 -I -c "
import zipfile, sys; z = zipfile.ZipFile('silesia.zip')
d = b''.join(z.read(n) for n in sorted(n for n in z.namelist() if not n.endswith('/')))
open('$B/out/input', 'wb').write(d[:64 << 20])"

# 3. Static benchmarks with Debian gcc 14 run in place: Debian's own libraries but not its glibc (the host's loader
#    runs it; libc6 is ABI-compatible upward), headers and libc.a from the sysroot.
NL=$B/nativelibs; rm -rf "$NL"; mkdir -p "$NL"
for f in "$S"/usr/lib/x86_64-linux-gnu/*.so*; do
    case ${f##*/} in libc.so*|libm.so*|libmvec*|libpthread*|libdl*|librt*|ld-*|libresolv*|libutil*|libanl*|libnss*|libthread_db*|libcrypt*|libBrokenLocale*|libmemusage*|libpcprofile*|libc_malloc*) ;;
    *) ln -sf "$f" "$NL/";; esac
done
export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$NL
CC="x86_64-linux-gnu-gcc-14 --sysroot=$S"; CXX="x86_64-linux-gnu-g++-14 --sysroot=$S"
$CC -O2 -static -pthread -o "$B/out/bin/locks" locks.c
rm -rf rt-tests-2.8; tar xzf rt-tests-2.8.tar.gz
$CC -O2 -static -pthread -DVERSION=2.8 -Wno-incompatible-pointer-types -o "$B/out/bin/hackbench" rt-tests-2.8/src/hackbench/hackbench.c
# 32-bit builds of three of the programs (M2: 32-bit code under the 64-bit kernel), as Debian builds its i386 packages: gcc -m32 is
# i686 with x87 floating point, no SSE. They are not run here (an amd64 container on a Mac cannot run 32-bit code), so their
# outputs have no pinned hash: the guest prints them and checks the exit status.
CC32="$CC -m32"
$CC32 -O2 -static -pthread -DVERSION=2.8 -Wno-incompatible-pointer-types -o "$B/out/bin/hackbench32" rt-tests-2.8/src/hackbench/hackbench.c
rm -rf zstd-prog; mkdir zstd-prog; tar xzf zstd-1.5.7.tar.gz -C zstd-prog --strip-components=1
make -s -C zstd-prog/programs -j4 zstd CC="$CC" HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 LDFLAGS=-static
cp zstd-prog/programs/zstd "$B/out/bin/"
# LuaJIT 2.1 (Debian's openresty-branch orig tarball): fully static, amalgamated build; Mike Pall's benchmark programs.
rm -rf luajit-src; mkdir luajit-src; tar xJf luajit_2.1.0+openresty20250117.orig.tar.xz -C luajit-src --strip-components=1
make -s -C luajit-src amalg PREFIX=/usr CC="$CC" HOST_CC="$CC" TARGET_STRIP=true TARGET_XLDFLAGS="-static -no-pie" >/dev/null
x86_64-linux-gnu-strip -o "$B/out/bin/luajit" luajit-src/src/luajit
rm -rf "$B/out/lj"; mkdir -p "$B/out/lj"; tar xzf LuaJIT-test-cleanup-014708b.tar.gz -C "$B/out/lj" --strip-components=2 --wildcards '*/bench/*.lua'
# stress-ng 0.19.02 (Debian's orig tarball): static, default options (its own configure step probes the Debian headers).
rm -rf stress-ng-src; mkdir stress-ng-src; tar xJf stress-ng_0.19.02.orig.tar.xz -C stress-ng-src --strip-components=1
make -s -C stress-ng-src -j4 STATIC=1 CC="$CC" >/dev/null 2>&1
x86_64-linux-gnu-strip -o "$B/out/bin/stress-ng" stress-ng-src/stress-ng
# c-ray 1.1: c-ray-mt.c with the release's own flags (-O3 -ffast-math) plus -msse4.2 (the guest CPU model's ceiling is AVX, no AVX-512).
rm -rf cray-src; mkdir cray-src; tar xzf c-ray-1.1.tar.gz -C cray-src --strip-components=1
$CC -O3 -ffast-math -msse4.2 -static -o "$B/out/bin/c-ray-mt" cray-src/c-ray-mt.c -lm -lpthread
# c-ray-strict: the same source without -ffast-math (so no crtfastmath/set_fast_math: MXCSR DAZ and FTZ stay clear).
$CC -O3 -msse4.2 -static -o "$B/out/bin/c-ray-mt-strict" cray-src/c-ray-mt.c -lm -lpthread
$CC32 -O3 -ffast-math -static -o "$B/out/bin/c-ray-mt32" cray-src/c-ray-mt.c -lm -lpthread
# More 32-bit floating point. c-ray-mt32-strict: x87 without -ffast-math, as c-ray-mt-strict (intermediate results keep the
# x87's 64-bit precision on hardware: its picture may differ under an emulator that computes x87 in doubles). c-ray-mt32-sse2:
# 32-bit code with SSE2 floating point (-mfpmath=sse), as i386 programs built for newer baselines than Debian's are.
$CC32 -O3 -static -o "$B/out/bin/c-ray-mt32-strict" cray-src/c-ray-mt.c -lm -lpthread
$CC32 -O3 -ffast-math -msse2 -mfpmath=sse -static -o "$B/out/bin/c-ray-mt32-sse2" cray-src/c-ray-mt.c -lm -lpthread
# SciMark 2.0 (NIST, ANSI C: FFT, SOR, Monte Carlo, sparse matrix multiply, LU; each kernel runs for at least 2 s and
# reports Mflops, so the result is a rate, not a time): 64-bit, 32-bit x87 and 32-bit SSE2 builds of the same source.
rm -rf scimark-src; mkdir scimark-src; python3 -m zipfile -e scimark2_1c.zip scimark-src
SM="scimark-src/scimark2.c scimark-src/FFT.c scimark-src/kernel.c scimark-src/Stopwatch.c scimark-src/Random.c scimark-src/SOR.c scimark-src/SparseCompRow.c scimark-src/array.c scimark-src/MonteCarlo.c scimark-src/LU.c"
$CC -O3 -static -w -o "$B/out/bin/scimark2" $SM -lm
$CC32 -O3 -static -w -o "$B/out/bin/scimark2-32" $SM -lm
$CC32 -O3 -msse2 -mfpmath=sse -static -w -o "$B/out/bin/scimark2-32sse2" $SM -lm
cp cray-src/sphfract "$B/out/sphfract"
# 7-Zip 25.01 (Debian's +dfsg orig tarball, no unRAR): the `7zz` console program, C++ with the C fallbacks (no assembler: USE_ASM
# unset), static; AES-NI/SHA/CRC code selected at run time by CPUID. `7zz b` is not used: it sizes its passes by measured time.
rm -rf 7z-src; mkdir 7z-src; tar xJf 7zip_25.01+dfsg.orig.tar.xz -C 7z-src
make -s -C 7z-src/CPP/7zip/Bundles/Alone2 -f makefile.gcc -j4 DISABLE_RAR=1 LDFLAGS_STATIC_2=-static CC="$CC" CXX="$CXX" >/dev/null 2>&1
x86_64-linux-gnu-strip -o "$B/out/bin/7zz" 7z-src/CPP/7zip/Bundles/Alone2/_o/7zz
# SQLite 3.46.1 (Debian's full-tree orig tarball): speedtest1 from test/, linked with the amalgamation (sqlite3.c generated by the
# tree's own `make sqlite3.c`, which runs tclsh from the host: text generation only, the compiled code is Debian gcc's).
rm -rf sqlite-src; mkdir sqlite-src; tar xJf sqlite3_3.46.1.orig.tar.xz -C sqlite-src --strip-components=1
(cd sqlite-src && ./configure CC="$CC" BCC="$CC" --disable-tcl >/dev/null 2>&1 && make sqlite3.c >/dev/null 2>&1 && sha256sum sqlite3.c > "$B/out/sqlite3.c.sha256")
$CC -O2 -static -DSQLITE_OMIT_LOAD_EXTENSION -Isqlite-src -o sqlite-src/speedtest1 sqlite-src/test/speedtest1.c sqlite-src/sqlite3.c -lm -lpthread 2>/dev/null
x86_64-linux-gnu-strip -o "$B/out/bin/speedtest1" sqlite-src/speedtest1
$CC32 -O2 -static -DSQLITE_OMIT_LOAD_EXTENSION -Isqlite-src -o sqlite-src/speedtest1-32 sqlite-src/test/speedtest1.c sqlite-src/sqlite3.c -lm -lpthread 2>/dev/null
x86_64-linux-gnu-strip -o "$B/out/bin/speedtest1-32" sqlite-src/speedtest1-32
# x264 0.164.3108 (git 31e19f9; Debian's orig tarball): 8-bit, static, with its nasm assembly (nasm from the Debian package);
# the CPU feature set is chosen at run time by CPUID.
rm -rf x264-src; mkdir x264-src; tar xzf x264_0.164.3108+git31e19f9.orig.tar.gz -C x264-src --strip-components=1
(cd x264-src && CC="$CC" ./configure --enable-static --enable-strip --bit-depth=8 --disable-opencl --disable-lavf --disable-swscale \
     --disable-ffms --disable-gpac --disable-lsmash --disable-avs --extra-ldflags=-static >/dev/null 2>&1 && make -s -j4 x264 >/dev/null 2>&1)
cp x264-src/x264 "$B/out/bin/x264"
cp foreman_cif.y4m "$B/out/"
sha256sum "$B"/out/bin/* "$B/out/input" "$B/out/sphfract" > "$B/out/bin.sha256"

# 4. The guest's expected zstd output (deterministic at any -T) into init, then the initramfs.
zmd5=$("$B/out/bin/zstd" -q -f -12 -B4194304 -T1 "$B/out/input" -c | md5sum | cut -d' ' -f1)
# The library build's archive (path-independent by -ffile-prefix-map; same command as init and native.sh).
rm -rf zstd-lib; mkdir zstd-lib; tar xzf zstd-1.5.7.tar.gz -C zstd-lib --strip-components=1; cd zstd-lib
make -s -C lib -j4 libzstd.a CC="$CC" AR=x86_64-linux-gnu-ar CFLAGS="-O0 -ffile-prefix-map=$PWD/=" \
     ZSTD_LEGACY_SUPPORT=0 ZSTD_LIB_DICTBUILDER=0 >/dev/null
bmd5=$(md5sum lib/libzstd.a | cut -d' ' -f1); cd ..
isha=$(sha256sum "$B/out/input" | cut -d' ' -f1)
# Expected outputs of the new benchmarks, from native runs that must agree with each other (2 runs of the LuaJIT task list at
# 4 jobs; c-ray at 1 and 4 threads): LuaJIT's per-program outputs (out/expect-lj.md5) and c-ray's PPM md5.
mkdir -p /tmp/lj; rm -f /tmp/lj/*; export PATH="$B/out/bin:$PATH"
sed "s#/bench/lj#$B/out/lj#" "$here/lj-tasks" > "$B/out/lj-tasks.native"
xargs -P4 -I{} sh -c '{}' < "$B/out/lj-tasks.native"; (cd /tmp/lj && md5sum *.out) > "$B/out/expect-lj.md5"
rm -f /tmp/lj/*; xargs -P4 -I{} sh -c '{}' < "$B/out/lj-tasks.native"; (cd /tmp/lj && md5sum -c --quiet "$B/out/expect-lj.md5")
cmd5=$("$B/out/bin/c-ray-mt" -t 1 -s 1280x960 -r 1 < "$B/out/sphfract" 2>/dev/null | md5sum | cut -d' ' -f1)
[ "$cmd5" = "$("$B/out/bin/c-ray-mt" -t 4 -s 1280x960 -r 1 < "$B/out/sphfract" 2>/dev/null | md5sum | cut -d' ' -f1)" ]
smd5=$("$B/out/bin/c-ray-mt-strict" -t 1 -s 1280x960 -r 1 < "$B/out/sphfract" 2>/dev/null | md5sum | cut -d' ' -f1)
[ "$smd5" = "$("$B/out/bin/c-ray-mt-strict" -t 4 -s 1280x960 -r 1 < "$B/out/sphfract" 2>/dev/null | md5sum | cut -d' ' -f1)" ]
# November 5 benchmarks (the same commands as init and native.sh; each hash must agree between native runs):
# luajit-interp: per-program output md5s (out/expect-lj-interp.md5, two runs at 4 jobs).
sed "s#/bench/lj#$B/out/lj#" "$here/lj-tasks-interp" > "$B/out/lj-tasks-interp.native"
rm -f /tmp/lj/*; xargs -P4 -I{} sh -c '{}' < "$B/out/lj-tasks-interp.native"; (cd /tmp/lj && md5sum *.out) > "$B/out/expect-lj-interp.md5"
rm -f /tmp/lj/*; xargs -P4 -I{} sh -c '{}' < "$B/out/lj-tasks-interp.native"; (cd /tmp/lj && md5sum -c --quiet "$B/out/expect-lj-interp.md5")
# 7zip: the first 12 MiB of the input, LZMA2 level 5 with 4 MiB blocks (so the archive is the same at any -mmt); sha256 of the
# slice and md5 of the archive (no timestamps stored; umask 022 as in the guest).
umask 022; W=/tmp/z7w; rm -rf $W; mkdir $W; cd $W; head -c 12582912 "$B/out/input" > in7
i7sha=$(sha256sum in7 | cut -d' ' -f1)
z7md5=""; for n in 1 4 1 4; do rm -f in7.7z; 7zz a -bso0 -bsp0 -mx=5 -m0=lzma2:c=4m -mmt=$n -mtm=off in7.7z in7
    m=$(md5sum in7.7z | cut -d' ' -f1); [ -z "$z7md5" ] || [ "$m" = "$z7md5" ]; z7md5=$m; done
7zz t -bso0 -bsp0 in7.7z; [ "$(7zz x -so -bso0 -bsp0 in7.7z | sha256sum | cut -d' ' -f1)" = "$i7sha" ]
cd "$B/src"
# sqlite: speedtest1's verification hash of all query results (twice).
sqh=$(speedtest1 --memdb --verify --size 160 :memory: | sed -n 's/^Verification Hash: //p' | tr -d ' '); [ -n "$sqh" ]
[ "$sqh" = "$(speedtest1 --memdb --verify --size 160 :memory: | sed -n 's/^Verification Hash: //p' | tr -d ' ')" ]
# x264: md5 of the bitstream per --threads value 1-8 (x264's output depends on the thread count and on the CPU feature set
# only through AVX-512 and the plain-C fallback; --asm here keeps this machine's AVX-512 out, as the guest CPU has none).
xm=""; for n in 1 2 3 4 5 6 7 8; do
    m=$(taskset -c 0-3 x264 --preset slower --crf 23 --threads $n --asm MMX2,SSE2Fast,SSSE3,SSE4.2,AVX,FMA3,BMI2,AVX2 -o /tmp/x264.out "$B/out/foreman_cif.y4m" 2>/dev/null; md5sum < /tmp/x264.out | cut -d' ' -f1)
    xm="$xm -e s/@X264_MD5_$n@/$m/"; done
sed -e "s/@ZSTD_MD5@/$zmd5/" -e "s/@BUILD_MD5@/$bmd5/" -e "s/@INPUT_SHA@/$isha/g" -e "s/@CRAY_MD5@/$cmd5/" -e "s/@CRAYS_MD5@/$smd5/" \
    -e "s/@IN7_SHA@/$i7sha/" -e "s/@Z7_MD5@/$z7md5/" -e "s/@SQL_HASH@/$sqh/" $xm "$here/init" > "$B/init"
echo "expected: zstd $zmd5 build $bmd5 input sha256 $isha c-ray $cmd5 in7 $i7sha 7z $z7md5 sqlite $sqh x264 $xm c-ray-strict $smd5" | tee "$B/out/expected.txt"
INIT=$B/init "$here/mkroot.sh" "$B"
