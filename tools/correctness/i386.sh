#!/bin/bash
# i386.sh: sourced after recipes/common.sh by the recipes that build or run 32-bit programs (i386, x86st).
#   multilib PIN      Debian's gcc-14-multilib (the 32-bit libgcc, libc and headers, amd64 packages) into remote.sh's
#                     sysroot, from its apt, checked against PIN: `$CC32 ...` then builds i386 programs.
#   libs32 PIN PKG..  Debian's i386 packages PKG and what they depend on (multiarch: libc6:i386 and so on), downloaded
#                     by a third private apt whose architecture is i386 ($W/a32), checked against PIN.
#   image32 SUITE [CT]  x86-remote.sh's image for SUITE with the tree $W/payload copied over the root; the guest's
#                     runner is run-CT.sh (CT: the suite's ct= word, default SUITE).
CC32="$W/sysroot/usr/bin/x86_64-linux-gnu-gcc-14 --sysroot=$W/sysroot -m32"

multilib() {
    say "sysroot packages: gcc-14-multilib"
    apt-get -y -qq -d install --no-install-recommends gcc-14-multilib >/dev/null
    debsums "$W/apt/cache/archives" | sort > "$H/sysroot-all.lst"
    sort "$H/sysroot.lst" | comm -13 - "$H/sysroot-all.lst" > "$H/multilib.lst"; pins "$H/multilib.lst" "$1"
    (cd "$W/apt/cache/archives" && uris $(awk '{print $2}' "$H/multilib.lst")) >> "$W/out/downloads.txt"
    local S=$W/sysroot l
    for d in $(awk '{print $2}' "$H/multilib.lst"); do dpkg -x "$W/apt/cache/archives/$d" "$S"; done
    for l in lib32 libx32; do                         # as remote.sh's sysroot does for lib and lib64
        if [ -d "$S/$l" ] && [ ! -L "$S/$l" ]; then mkdir -p "$S/usr/$l"; cp -a "$S/$l/." "$S/usr/$l/"; rm -rf "${S:?}/${l:?}"; fi
        ln -sfn "usr/$l" "$S/$l"
    done
    ln -sfn ../lib32/ld-linux.so.2 "$S/usr/lib/ld-linux.so.2"       # the package's link is absolute: outside the sysroot
}

libs32() {
    local pin=$1 A=$W/a32; shift
    say "i386 packages: $*"
    mkdir -p "$A"/{lists/partial,cache/archives/partial,etc/preferences.d,etc/apt.conf.d}; touch "$A/status"
    sed -e "s|$W/pb/apt|$A|g" -e 's/amd64/i386/g' "$W/pb/apt/apt.conf" > "$A/apt.conf"; cp "$W/pb/apt/sources.list" "$A/"
    APT_CONFIG=$A/apt.conf apt-get update -qq
    APT_CONFIG=$A/apt.conf apt-get -y -qq -d install --no-install-recommends "$@" >/dev/null
    debsums "$A/cache/archives" > "$H/libs32.lst"; pins "$H/libs32.lst" "$pin"
    (cd "$A/cache/archives" && APT_CONFIG=$A/apt.conf uris *.deb) >> "$W/out/downloads.txt"
}

image32() {
    local s=$1 R=$W/img-$1
    rm -rf "${R:?}"; cp -a "$W/pb/rootfs" "$R"; mkdir -p "$R/ct"
    cp "$T/init" "$R/init"; cp "$T/run-${2:-$s}.sh" "$T/run-repro.sh" "$R/ct/"; chmod 755 "$R/init" "$R"/ct/run-*.sh
    cp "$W/out/$s-cases.txt" "$R/ct/"
    cp -a --remove-destination "$W/payload/." "$R/"      # (a payload file replaces a link of the root, not its target)
    python3 "$T/play/mkcpio.py" "$R" "$W/out/$s.cpio"
    zstd -q -19 -T0 -f --rm "$W/out/$s.cpio" -o "$W/out/$s.cpio.zst"; rm -rf "${R:?}"
}
