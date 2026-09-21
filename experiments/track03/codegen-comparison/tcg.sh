#!/bin/sh
set -eu
cd /work
mkdir upstream build outputs empty-home
tar --extract --gzip --file source/upstream.tar.gz --directory upstream --strip-components=1 --no-same-owner
tar --extract --file source/dependency-overlay.tar --directory upstream --no-same-owner
python3 source/tcg-export.py upstream
python3 source/ordering-profile.py tcg upstream "${NATEMU_HARDWARE_TSO:-0}"
python3 source/make-fixtures.py source/fixtures.json fixtures
find upstream -type f -print0 | sort -z | xargs -0 sha256sum > source-before-build.sha256
cd build
../upstream/configure --target-list=x86_64-linux-user --cc=clang-19 \
    --disable-system --disable-tools --disable-docs --disable-plugins \
    --disable-werror --disable-tests --disable-rust --disable-download
ninja -j2 qemu-x86_64
cd /work
for fixture in fixtures/*.count; do
    name=${fixture##*/}
    name=${name%.count}
    count=$(cat fixtures/$name.count)
    bytes=$(wc -c < fixtures/$name.bin)
    env -i PATH=/usr/bin:/bin HOME=/work/empty-home \
        NATEMU_COUNT="$count" NATEMU_BYTES="$bytes" NATEMU_EXPORT="/work/outputs/$name.native" \
        timeout 15 build/qemu-x86_64 -cpu max -d in_asm,out_asm,op,op_opt -D "/work/outputs/$name.disassembly.log" \
        "/work/fixtures/$name.elf" > "outputs/$name.log" 2>&1
    test -s "outputs/$name.native"
    test -s "outputs/$name.native.search"
done
