#!/bin/bash
# Run in the isolated Debian cross-build container. Sources remain unmodified.
set -euo pipefail
stage=$1
K=/build/linux-6.12.107
W=/work
export ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu-
mkdir -p "$W/build" "$W/payload/opt/public" "$W/payload/ct"
case "$stage" in
kernel)
    cp "$W/build/debian.config" "$K/.config"
    # Unrelated distro modules are unnecessary for this initramfs-only campaign.
    sed -i 's/^\(CONFIG_[A-Z0-9_]*\)=m$/# \1 is not set/' "$K/.config"
    cd "$K"
    scripts/config --set-str SYSTEM_TRUSTED_KEYS '' --set-str SYSTEM_REVOCATION_KEYS '' \
      --disable DEBUG_INFO --disable DEBUG_INFO_BTF --enable DEBUG_INFO_NONE \
      --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT --disable MODULE_SIG \
      --set-str LOCALVERSION '-natemu-public' --disable LOCALVERSION_AUTO \
      --module KUNIT --module MEMCPY_KUNIT_TEST --module USERCOPY_KUNIT_TEST \
      --module ATOMIC64_SELFTEST --module TEST_BITMAP --module TEST_VMALLOC \
      --module RCU_TORTURE_TEST --module LOCK_TORTURE_TEST
    make -j4 olddefconfig > "$W/build/kernel-config.log" 2>&1
    cp .config "$W/build/kernel.config"
    make -j4 bzImage modules > "$W/build/kernel-build.log" 2>&1
    make INSTALL_MOD_PATH="$W/payload" modules_install > "$W/build/modules-install.log" 2>&1
    cp arch/x86/boot/bzImage "$W/images/test-kernel"
    cp Module.symvers "$W/build/Module.symvers"
    make -s kernelrelease > "$W/build/kernelrelease"
    ;;
userspace)
    S=$K/tools/testing/selftests
    make -C "$K" headers_install INSTALL_HDR_PATH=/build/uapi > "$W/build/headers.log" 2>&1
    for family in rseq futex/functional membarrier ptrace; do
      name=${family//\//-}; out=$W/payload/opt/public/$name; mkdir -p "$out"
      make -C "$S/$family" -j4 CC=x86_64-linux-gnu-gcc OUTPUT="$out" \
        KHDR_INCLUDES=-I/build/uapi/include > "$W/build/$name-build.log" 2>&1
    done
    out=$W/payload/opt/public/mm; mkdir -p "$out"
    for t in mremap_dontunmap mremap_test madv_populate cow map_fixed_noreplace; do
      make -C "$S/mm" CC=x86_64-linux-gnu-gcc OUTPUT="$out" \
        KHDR_INCLUDES=-I/build/uapi/include "$out/$t" >> "$W/build/mm-build.log" 2>&1
    done
    ;;
*) echo "unknown stage: $stage" >&2; exit 2;;
esac
