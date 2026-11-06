#!/bin/sh
set -eu
cd /work
mkdir upstream build outputs empty-home
tar --extract --gzip --file source/upstream.tar.gz --directory upstream --strip-components=1 --no-same-owner
python3 source/box64-export.py upstream
python3 source/ordering-profile.py box64 upstream "${NATEMU_HARDWARE_TSO:-0}"
box64_strongmem=3
if [ "${NATEMU_HARDWARE_TSO:-0}" = 1 ]; then box64_strongmem=0; fi
python3 source/make-fixtures.py source/fixtures.json fixtures
find upstream -type f -print0 | sort -z | xargs -0 sha256sum > source-before-build.sha256
cmake -S upstream -B build -DARM_DYNAREC=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBOX32=OFF
cmake --build build --parallel 2
for fixture in fixtures/*.count; do
    name=${fixture##*/}
    name=${name%.count}
    count=$(cat fixtures/$name.count)
    bytes=$(wc -c < fixtures/$name.bin)
    env -i PATH=/usr/bin:/bin HOME=/work/empty-home BOX64_RCFILE=/dev/null BOX64_LOG=2 \
        BOX64_DYNAREC=1 BOX64_DYNAREC_DUMP=1 BOX64_DYNAREC_BIGBLOCK=0 \
        BOX64_DYNAREC_CALLRET=0 BOX64_DYNAREC_STRONGMEM="$box64_strongmem" BOX64_DYNAREC_SAFEFLAGS=2 \
        NATEMU_COUNT="$count" NATEMU_BYTES="$bytes" NATEMU_EXPORT="/work/outputs/$name.native" \
        timeout 15 build/box64 "/work/fixtures/$name.elf" > "outputs/$name.log" 2>&1
    test -s "outputs/$name.native"
done
