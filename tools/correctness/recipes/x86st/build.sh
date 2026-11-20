#!/bin/bash
# build.sh: the kernel x86 selftest campaign image (testkit/inputs.py:campaign_image('x86st'); common.sh says how it
# runs). remote.sh's stages sysroot and play, then x86-remote.sh's selftests and image x86st. The source is Debian's
# linux 6.12.107-1 (the guest kernel's source package) through apt-get source -d, pinned in sources.sha256; the
# selftests stage takes tools/testing/selftests/x86 from its orig tarball (no Debian patch touches tools/testing) and
# builds the 64-bit programs -static (correctness-x86-litmus.md, "Suite 1": 25 programs). The 32-bit programs
# (`*_32`, M2 checkpoint A) are built here the same way, `make all_32` with the sysroot's gcc -m32 (i386.sh's multilib,
# multilib-debs.sha256): 24 programs. Another count fails the build: the suite boots one program per part
# (parts = 49).
#
# x86st-cases.txt: `PART TEST` per program, TEST = the program's file name as run-x86st.sh's CT-RESULT (or CT-SKIP)
# lines name it and PART the kernel's part= word. run-x86st.sh K/N runs programs K, K+N, ... of `ls /ct/x86st | sort`,
# so the list is made from the programs the image's /ct/x86st holds, in that order. A program named in
# tools/correctness/x86st-skip.txt (also in /ct) is listed too: run-x86st.sh reports it with CT-SKIP.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
. /recipe/correctness/i386.sh
SUITE=x86st
PARTS=49                                              # tests/correctness/family.toml: parts = 49
if [ "${1:-}" != user ]; then root_phase "$(realpath "$0")" "$SUITE"; exit 0; fi

user_env
sysroot
multilib x86st/multilib-debs.sha256
sources 'linux=6.12.107-1' x86st/sources.sha256
play
stop_if_repin

x86() { say "x86-remote.sh $*"; bash "$T/x86-remote.sh" "$W" "$@"; }
x86 selftests > "$W/log/selftests-stage.log" 2>&1 || { tail -20 "$W/log/selftests-stage.log" "$W/log/x86st-make.log" >&2; exit 1; }
# The 32-bit programs: x86-remote.sh's selftests stage again (the same files of the source, compiler and flags), all_32.
selftests32() {
    local S=$W/sysroot
    export PATH=$S/usr/bin:$PATH LD_LIBRARY_PATH=$W/nativelibs
    cd "$W"; rm -rf kst; mkdir kst
    tar xJf src/linux_6.12.107.orig.tar.xz -C kst --strip-components=1 --wildcards '*/tools/testing/selftests/x86/*' \
        '*/tools/testing/selftests/lib.mk' '*/tools/testing/selftests/kselftest.h' '*/tools/testing/selftests/kselftest_harness.h' \
        '*/tools/include/*' '*/tools/scripts/*' '*/scripts/subarch.include' '*/arch/x86/include/asm/desc_defs.h'
    make -k -C kst/tools/testing/selftests/x86 CC="$S/usr/bin/x86_64-linux-gnu-gcc-14 --sysroot=$S" EXTRA_CFLAGS=-static \
        KHDR_INCLUDES=-DKHDR_SYSROOT OUTPUT="$W/x86st" all_32 > "$W/log/x86st-make32.log" 2>&1 || true
    rm -rf kst
}
say "the 32-bit selftests"; selftests32
(cd "$W/x86st" && ls -- *_64 *_32) | sort > "$H/x86st.txt"
if [ "$(wc -l < "$H/x86st.txt")" != $PARTS ]; then
    echo "$(wc -l < "$H/x86st.txt") selftest programs built, not $PARTS:" >&2; tr '\n' ' ' < "$H/x86st.txt" >&2
    grep -E 'error|build failed' "$W/log/x86st-make.log" "$W/log/x86st-make32.log" | head -20 >&2; exit 1
fi
awk -v n=$PARTS '{print (NR - 1) % n "/" n " " $1}' "$H/x86st.txt" > "$W/out/$SUITE-cases.txt"
say "$(wc -l < "$W/out/$SUITE-cases.txt") programs listed"
mkdir -p "$W/payload/ct/x86st"; cp "$W"/x86st/*_64 "$W"/x86st/*_32 "$W/payload/ct/x86st/"; cp "$T/x86st-skip.txt" "$W/payload/ct/"
image32 "$SUITE" > "$W/log/image.log" 2>&1 || { tail -20 "$W/log/image.log" >&2; exit 1; }
