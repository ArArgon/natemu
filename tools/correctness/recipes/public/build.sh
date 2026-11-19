#!/bin/bash
# build.sh: the public CPU/RAM campaign image (testkit/inputs.py:campaign_image('public'); common.sh says how it runs;
# docs/restructure/correctness-public-kernel.md is the campaign). One image and a test kernel:
#   public.cpio.zst   the play rootfs, /init = public-init.sh, /ct/run-public.sh and /ct/public-cases.tsv (cases.py), and
#                     /opt/public (Linux 6.12.107 selftests rseq, futex/functional, membarrier, ptrace and five mm tests;
#                     eight QEMU tests/tcg x86-64 programs), /opt/ltp (14 LTP tests), the test kernel's modules
#   public-kernel     Linux 6.12.107 with Debian's own config (the pinned kernel package's /boot/config, as
#                     tools/correctness/remote.sh's image stage takes it), distro modules off and the test modules on
#                     (KUnit memcpy/usercopy, atomic64, bitmap, vmalloc, lock and RCU torture) plus GUP_TEST (gup-cow-01)
#   public-cases.txt, public-boot.txt   cases.py: `PART TEST`, and each part's kernel and pubcap= (the guest cap)
# Debian's own 6.12.107 packages ship none of the test modules (its config has KUNIT, TEST_BITMAP, ATOMIC64_SELFTEST,
# TEST_VMALLOC, LOCK_TORTURE_TEST and RCU_TORTURE_TEST unset), so the kernel is built here.
#
# Sources, all pinned: Debian's linux 6.12.107-1 (linux-sources.sha256; the orig tarball, unpatched: the campaign runs
# public sources unchanged), Debian's qemu 1:10.0.13+ds-0+deb13u1 (qemu-sources.sha256; tests/tcg only), LTP 20250530
# (ltp.sha256, the tarball tools/correctness/ltp.sha256 pins) and the build packages (root-debs.sha256). Built natively
# with Debian's gcc 14 in the builder container (no cross compiler, unlike the original public-build.sh).
#
# Not built yet: the LKMM corpus (35 klitmus7 modules), which needs herdtools7 (not in Debian) built from source.
set -euo pipefail
. /recipe/correctness/recipes/common.sh
SUITE=public
ROOT_DEBS='build-essential bc bison flex libelf-dev libssl-dev cpio pkgconf'
LTP=ltp-full-20250530.tar.xz
LTP_URL=https://github.com/linux-test-project/ltp/releases/download/20250530/$LTP
if [ "${1:-}" != user ]; then
    root_phase "$(realpath "$0")" "$SUITE"
    cp "$W/out/$SUITE-boot.txt" "$W/out/$SUITE-kernel" /work/
    exit 0
fi

user_env
sysroot
sources 'linux=6.12.107-1' public/linux-sources.sha256
sources 'qemu=1:10.0.13+ds-0+deb13u1' public/qemu-sources.sha256
say "curl $LTP"
curl -sSLf --retry 5 -o "$W/src/$LTP" "$LTP_URL"
(cd "$W/src" && sha256sum "$LTP") > "$H/ltp.lst"; pins "$H/ltp.lst" public/ltp.sha256
echo "$LTP_URL $(sha256sum < "$W/src/$LTP" | cut -d' ' -f1) $LTP" >> "$W/out/downloads.txt"
play
stop_if_repin

export PATH=/usr/sbin:/sbin:/usr/bin:/bin KBUILD_BUILD_TIMESTAMP=@$SOURCE_DATE_EPOCH KBUILD_BUILD_USER=natemu KBUILD_BUILD_HOST=natemu
unset APT_CONFIG
J=$(nproc)
P=$W/payload; rm -rf "$P"; mkdir -p "$P/opt/public" "$P/ct"
KS=$W/linux-6.12.107

say "linux source"
rm -rf "$KS"; tar xf "$W"/src/linux_6.12.107.orig.tar.* -C "$W"
[ -d "$KS" ] || { echo "no linux-6.12.107/ in the orig tarball" >&2; exit 1; }

say "kernel config (Debian's, from $K)"
(cd "$H" && curl -sSLf --retry 5 -o k.deb "$MIRROR/pool/main/l/linux-signed-amd64/$K" && echo "$KDEB_SHA  k.deb" | sha256sum -c --quiet \
    && rm -rf kx && mkdir kx && dpkg -x k.deb kx && cp kx/boot/config-6.12.107+deb13-amd64 "$W/log/debian.config" && rm -rf kx k.deb)
cp "$W/log/debian.config" "$KS/.config"
# Distro modules are not needed by an initramfs-only campaign (public-build.sh's kernel stage, unchanged), and modules
# stay uncompressed (the kernel-01 image's dependency index named compressed files).
sed -i 's/^\(CONFIG_[A-Z0-9_]*\)=m$/# \1 is not set/' "$KS/.config"
(cd "$KS" && scripts/config --set-str SYSTEM_TRUSTED_KEYS '' --set-str SYSTEM_REVOCATION_KEYS '' \
    --disable DEBUG_INFO --disable DEBUG_INFO_BTF --enable DEBUG_INFO_NONE \
    --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT --disable MODULE_SIG --disable MODULE_COMPRESS \
    --disable MODULE_COMPRESS_XZ --enable MODULE_COMPRESS_NONE \
    --set-str LOCALVERSION '-natemu-public' --disable LOCALVERSION_AUTO \
    --module KUNIT --module MEMCPY_KUNIT_TEST --module USERCOPY_KUNIT_TEST \
    --module ATOMIC64_SELFTEST --module TEST_BITMAP --module TEST_VMALLOC \
    --module RCU_TORTURE_TEST --module LOCK_TORTURE_TEST --enable GUP_TEST)
say "kernel build (-j$J)"
(cd "$KS" && make -j"$J" olddefconfig > "$W/log/kernel-config.log" 2>&1 && cp .config "$W/log/kernel.config" \
    && make -j"$J" bzImage modules > "$W/log/kernel-build.log" 2>&1) || { tail -30 "$W/log/kernel-build.log" >&2; exit 1; }
for o in KUNIT MEMCPY_KUNIT_TEST USERCOPY_KUNIT_TEST ATOMIC64_SELFTEST TEST_BITMAP TEST_VMALLOC RCU_TORTURE_TEST LOCK_TORTURE_TEST; do
    grep -qx "CONFIG_$o=m" "$KS/.config" || { echo "CONFIG_$o is not a module after olddefconfig" >&2; exit 1; }
done
grep -qx CONFIG_GUP_TEST=y "$KS/.config" || { echo "CONFIG_GUP_TEST is not set after olddefconfig" >&2; exit 1; }
(cd "$KS" && make INSTALL_MOD_PATH="$P/usr" INSTALL_MOD_STRIP=1 modules_install > "$W/log/modules-install.log" 2>&1) \
    || { tail -20 "$W/log/modules-install.log" >&2; exit 1; }
KREL=$(cd "$KS" && make -s kernelrelease); echo "$KREL" > "$W/log/kernelrelease"
rm -f "$P/usr/lib/modules/$KREL/build" "$P/usr/lib/modules/$KREL/source"
for m in kunit memcpy_kunit usercopy_kunit atomic64_test test_bitmap test_vmalloc locktorture rcutorture torture; do
    find "$P/usr/lib/modules/$KREL" -name "$m.ko" | grep -q . || { echo "module $m.ko not installed" >&2; exit 1; }
done
cp "$KS/arch/x86/boot/bzImage" "$W/out/$SUITE-kernel"

say "selftests"
(cd "$KS" && make headers > "$W/log/headers.log" 2>&1) || { tail -20 "$W/log/headers.log" >&2; exit 1; }   # usr/include (headers_install needs rsync)
S=$KS/tools/testing/selftests
for family in rseq futex/functional membarrier ptrace; do
    name=${family//\//-}; o=$P/opt/public/$name; mkdir -p "$o"
    make -C "$S/$family" -j"$J" OUTPUT="$o" KHDR_INCLUDES=-I"$KS/usr/include" > "$W/log/$name-build.log" 2>&1 \
        || { tail -20 "$W/log/$name-build.log" >&2; exit 1; }
done
o=$P/opt/public/mm; mkdir -p "$o"
for t in mremap_dontunmap mremap_test madv_populate cow map_fixed_noreplace; do
    make -C "$S/mm" OUTPUT="$o" KHDR_INCLUDES=-I"$KS/usr/include" "$o/$t" >> "$W/log/mm-build.log" 2>&1 \
        || { tail -20 "$W/log/mm-build.log" >&2; exit 1; }
done
find "$P/opt/public" -name '*.o' -delete

say "qemu tests/tcg"
rm -rf "$W/qemu"; mkdir "$W/qemu"
tar xf "$W"/src/qemu_*.orig.tar.* -C "$W/qemu" --strip-components=1 --wildcards '*/tests/tcg/*'
o=$P/opt/public/qemu; mkdir -p "$o"
for t in cmpxchg adox noexec test-1648 test-2175 test-2413 fma cross-modifying-code; do
    f=$(ls "$W/qemu/tests/tcg/x86_64/$t.c" "$W/qemu/tests/tcg/i386/$t.c" 2>/dev/null | head -1 || true)
    [ -n "$f" ] || { echo "qemu tests/tcg has no $t.c" >&2; exit 1; }
    echo "$f" >> "$W/log/qemu-tests.log"
    # tests/tcg/Makefile.target's flags (-Wall -O0 -g -fno-strict-aliasing, static), without -Werror (another gcc)
    (cd "$(dirname "$f")" && gcc -Wall -O0 -g -fno-strict-aliasing -static -pthread -o "$o/$t" "$t.c" -lm) >> "$W/log/qemu-tests.log" 2>&1 \
        || { tail -20 "$W/log/qemu-tests.log" >&2; exit 1; }
done

say "LTP"
rm -rf "$W/ltp-src" "$W/ltp-inst"; mkdir "$W/ltp-src"; tar xJf "$W/src/$LTP" -C "$W/ltp-src" --strip-components=1
(cd "$W/ltp-src" && ./configure --prefix=/opt/ltp --without-modules --without-numa --without-tirpc CFLAGS="-O2 -g0" \
    > "$W/log/ltp-configure.log" 2>&1 && make -j"$J" -C lib > "$W/log/ltp-make.log" 2>&1 \
    && for d in mprotect mmap madvise futex; do make -j"$J" -C testcases/kernel/syscalls/$d >> "$W/log/ltp-make.log" 2>&1 || exit 1; done) \
    || { tail -30 "$W/log/ltp-make.log" "$W/log/ltp-configure.log" >&2; exit 1; }
mkdir -p "$P/opt/ltp/testcases/bin"
for t in mprotect01 mprotect02 mprotect03 mprotect04 mprotect05 mmap01 mmap02 mmap03 mmap04 madvise01 \
         futex_cmp_requeue01 futex_cmp_requeue02 futex_wait01 futex_wake01; do
    f=$(find "$W/ltp-src/testcases/kernel/syscalls" -type f -name "$t" -perm -u+x | head -1)
    [ -n "$f" ] || { echo "LTP built no $t" >&2; exit 1; }
    install -m 755 -s "$f" "$P/opt/ltp/testcases/bin/$t"
done

say "image"
python3 -I "$RC/public/cases.py" "$W/out" > "$W/log/cases.log"
cp "$W/out/$SUITE-cases.tsv" "$W/out/$SUITE-cases.txt" "$P/ct/"
cp "$T/public-init.sh" "$P/init"; cp "$T/run-public.sh" "$P/ct/run-public.sh"; chmod 755 "$P/init" "$P/ct/run-public.sh"
R=$W/img; rm -rf "$R"; cp -a "$W/pb/rootfs" "$R"
(cd "$P" && tar cf - .) | tar xf - -C "$R"
depmod -b "$R" "$KREL"
du -sh "$R" "$P/opt/public" "$P/opt/ltp" "$P/usr/lib/modules" > "$W/log/sizes.txt"
python3 "$T/play/mkcpio.py" "$R" "$W/out/$SUITE.cpio"
zstd -q -19 -T0 -f --rm "$W/out/$SUITE.cpio" -o "$W/out/$SUITE.cpio.zst"; rm -rf "$R"
ls -la "$W/out/$SUITE.cpio.zst" "$W/out/$SUITE-kernel" >&2
say "$(wc -l < "$W/out/$SUITE-cases.txt") rows listed"
