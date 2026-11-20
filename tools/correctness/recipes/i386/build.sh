#!/bin/bash
# build.sh: the 32-bit program campaign image (testkit/inputs.py:campaign_image('i386'); common.sh says how it runs):
# M2 checkpoint A's programs (docs/restructure/m2-checkpoint-a-findings.md). remote.sh's stages sysroot and play, then
#   - the programs of progs/*.c, built for i686 with x87 floating point (Debian's i386 default: gcc 14 --with-arch-32=
#     i686) by the sysroot's gcc 14 -m32 (gcc-14-multilib, multilib-debs.sha256), each twice: NAME.static (-static, no
#     PIE: libc's INT 0x80 start-up, local-exec TLS) and NAME.dyn (PIE: ld.so, TLS through ___tls_get_addr);
#   - Debian's i386 packages (image-debs.sha256): libc6 and libgcc-s1 unpacked into the root (multiarch paths, and
#     /lib/ld-linux.so.2), busybox-static's binary as /ct/i386/bin/busybox, python3.13-minimal's as
#     /usr/bin/python3.13-i386 beside the amd64 one (its library unpacked over the amd64 package's: the same files
#     plus the i386 extension modules);
#   - the scripts progs/busybox, progs/python and progs/mix, which run those;
#   - CPython's own regression tests for the modules pytests.txt names (libpython3.13-stdlib:i386 and the
#     architecture-independent libpython3.13-testsuite), one test py-MODULE each, run by pytest.sh.
# Every file of /ct/i386/tests is one test, listed in i386-cases.txt (`0/1 NAME`: one part). run-i386.sh prints each
# test's output; tests/correctness/verdicts.py compares it with tests/correctness/i386/, recorded on the x86 reference
# machine. A builder cannot run the programs itself: the Mac's (Rosetta) runs no 32-bit code.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
. /recipe/correctness/i386.sh
SUITE=i386
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
multilib i386/multilib-debs.sha256
play
libs32 i386/image-debs.sha256 libc6 libgcc-s1 busybox-static python3.13-minimal libpython3.13-stdlib libpython3.13-testsuite
stop_if_repin

say "building the programs"
P=$W/payload; D=$P/ct/i386; A=$W/a32/cache/archives; src=$RC/i386
mkdir -p "$D/tests" "$D/bin" "$P/usr/bin" "$W/x32"
FLAGS='-O2 -march=i686 -mfpmath=387 -fstack-protector-strong -Wall -Wno-unused'
export PATH=$W/sysroot/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs
for c in "$src"/progs/*.c; do
    n=$(basename "$c" .c)
    $CC32 $FLAGS -fno-pie -no-pie -static -pthread -o "$D/tests/$n.static" "$c" -lm 2>> "$W/log/i386-cc.log" || { tail -30 "$W/log/i386-cc.log" >&2; exit 1; }
    $CC32 $FLAGS -fPIE -pie -pthread -o "$D/tests/$n.dyn" "$c" -lm 2>> "$W/log/i386-cc.log" || { tail -30 "$W/log/i386-cc.log" >&2; exit 1; }
done
for d in "$A"/*.deb; do
    case $(dpkg-deb -f "$d" Package) in
        busybox-static) dpkg -x "$d" "$W/x32/bb"; cp "$W/x32/bb/usr/bin/busybox" "$D/bin/busybox";;
        python3.13-minimal) dpkg -x "$d" "$W/x32/py"; cp "$W/x32/py/usr/bin/python3.13" "$P/usr/bin/python3.13-i386";;
        *) dpkg -x "$d" "$P";;
    esac
done
rm -rf "${P:?}/usr/share"
for l in lib bin sbin; do                             # as rootfs.sh: the root's /lib is a link to usr/lib
    [ ! -d "$P/$l" ] || [ -L "$P/$l" ] || { mkdir -p "$P/usr/$l"; cp -a "$P/$l/." "$P/usr/$l/"; rm -rf "${P:?}/${l:?}"; }
done
[ -e "$P/usr/lib/ld-linux.so.2" ] || { echo "no /lib/ld-linux.so.2 in libc6:i386" >&2; exit 1; }
for s in busybox python mix; do cp "$src/progs/$s" "$D/tests/$s"; done
for m in $(grep -v '^#' "$src/pytests.txt"); do printf '#!/bin/sh\nexec "${0%%/*}/../pytest.sh" %s\n' "$m" > "$D/tests/py-$m"; done
cp "$src/python.py" "$src/pytest.sh" "$D/"; chmod 755 "$D/pytest.sh"; chmod 755 "$D"/tests/* "$D/bin/busybox" "$P/usr/bin/python3.13-i386"
(cd "$D/tests" && sha256sum -- *) > "$W/log/built.sha256"
ls "$D/tests" | sort | sed 's|^|0/1 |' > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") tests listed"
image32 "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
